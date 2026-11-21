#!/usr/bin/env python3
"""
radio.py - a shuffle-and-crossfade internet radio server.

Plays the tracks prepared by prepare.py in random order (a shuffle bag, so
nothing repeats until everything has played), blends each track into the next
with an equal-power crossfade, encodes once, and streams the same audio to
every listener over plain HTTP.

    http://HOST:PORT/          little web player with now-playing + skip
    http://HOST:PORT/stream    the audio stream (open in VLC, a browser, etc.)
    http://HOST:PORT/status    JSON: now playing, up next, history, listeners
    POST /skip                 fade out the current track early (not during ads)

With --ads DIR (another prepare.py cache), an ad break plays at the first song
boundary after every --ad-interval minutes of airtime.

Usage:
    python3 radio.py ./cache
    python3 radio.py ./cache --port 8000 --crossfade 8 --bitrate 256k --skip-token secret
    python3 radio.py ./cache --ads ./ads-cache --ad-interval 15
"""
import argparse
import collections
import html
import json
import logging
import math
import os
import queue
import random
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

SR = 44100
CH = 2
FRAME_BYTES = CH * 4          # float32 stereo
BLOCK = SR // 10              # mixer works in 100 ms blocks
ICY_METAINT = 16000
EMPTY = np.zeros((0, CH), dtype=np.float32)

log = logging.getLogger("radio")


def fmt_duration(seconds: float) -> str:
    """3725 -> '1 h 02 min', 125 -> '2 min 05 s', 2 days+ -> '2 d 03 h 10 min'."""
    s = int(round(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d} d {h:02d} h {m:02d} min"
    if h:
        return f"{h} h {m:02d} min"
    return f"{m} min {s:02d} s"


# --------------------------------------------------------------------------- library

@dataclass
class Track:
    path: Path
    meta: dict = field(default_factory=dict)
    ad: bool = False

    @property
    def title(self):
        return self.meta.get("title") or self.path.name.removesuffix(".flac").rsplit(".", 1)[0]

    @property
    def artist(self):
        return self.meta.get("artist", "")

    @property
    def display(self):
        return f"{self.artist} - {self.title}" if self.artist else self.title

    def as_dict(self):
        return {"title": self.title, "artist": self.artist, "album": self.meta.get("album", ""),
                "duration": self.meta.get("duration"), "ad": self.ad}


class Library:
    """Watches the cache directory and hands out tracks in shuffled order."""

    def __init__(self, root: Path, history: int, ads: bool = False, label: str = "library"):
        self.root = root
        self.history = history
        self.ads = ads
        self.label = label
        self.lock = threading.Lock()
        self.tracks: dict[str, Track] = {}
        self.bag: list[str] = []
        self.recent = collections.deque(maxlen=max(history, 1))

    def scan(self):
        found = {}
        for flac in self.root.rglob("*.flac"):
            try:
                meta = json.loads(flac.with_suffix(".json").read_text())
            except (OSError, ValueError):
                meta = {}  # a bare FLAC dropped into the cache still plays
            found[str(flac)] = Track(flac, meta, ad=self.ads)
        with self.lock:
            new = [p for p in found if p not in self.tracks]
            added, removed = len(new), len(self.tracks.keys() - found.keys())
            self.tracks = found
            self.bag = [p for p in self.bag if p in found]
            for p in new:  # new arrivals join the current shuffle at random spots
                self.bag.insert(random.randint(0, len(self.bag)), p)
        if added or removed:
            secs, unknown = self.total_duration()
            extra = f", {unknown} without a duration" if unknown else ""
            log.info("%s: %d tracks, %s total (+%d / -%d%s)",
                     self.label, len(found), fmt_duration(secs), added, removed, extra)
        return len(found)

    def total_duration(self):
        """(seconds of audio across all tracks, number of tracks whose sidecar has no duration)."""
        with self.lock:
            durs = [t.meta.get("duration") for t in self.tracks.values()]
        known = [float(d) for d in durs if d]
        return sum(known), len(durs) - len(known)

    def _refill(self):
        n = len(self.tracks)
        avoid = set(list(self.recent)[-min(self.history, n // 2):]) if n > 1 else set()
        pool = [p for p in self.tracks if p not in avoid]
        random.shuffle(pool)
        self.bag = pool

    def next(self):
        with self.lock:
            if not self.tracks:
                return None
            if not self.bag:
                self._refill()
            p = self.bag.pop()
            self.recent.append(p)
            return self.tracks[p]

    def peek(self):
        with self.lock:
            if not self.tracks:
                return None
            if not self.bag:
                self._refill()
            return self.tracks[self.bag[-1]]

    def __len__(self):
        return len(self.tracks)


# --------------------------------------------------------------------------- decoding / mixing

class TrackReader:
    """Decodes one cached track to float32 PCM through an ffmpeg pipe."""

    def __init__(self, track: Track):
        self.track = track
        self.eof = False
        self.frames = 0
        self.proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(track.path), "-map", "0:a:0",
             "-f", "f32le", "-ac", str(CH), "-ar", str(SR), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def read(self, frames: int) -> np.ndarray:
        if self.eof or frames <= 0:
            return EMPTY
        data = self.proc.stdout.read(frames * FRAME_BYTES)
        if len(data) < frames * FRAME_BYTES:
            self.eof = True
        n = len(data) // FRAME_BYTES
        self.frames += n
        return np.frombuffer(data[: n * FRAME_BYTES], dtype=np.float32).reshape(n, CH)

    def close(self):
        self.eof = True
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()


class Mixer:
    """
    Endless generator of PCM blocks. Keeps `crossfade` seconds of the current
    track buffered ahead; when the decoder hits the end, that buffered tail is
    exactly the last N seconds, so it is blended with the head of the next track.
    """

    def __init__(self, library: Library, crossfade: float, skip_fade: float, state,
                 ads: Library | None = None, ad_interval: float = 900, ads_per_break: int = 1,
                 ad_crossfade: float = 1.0):
        self.library = library
        self.fade = max(0, int(crossfade * SR))
        self.skip_fade = max(1, int(skip_fade * SR))
        self.state = state
        self.skip_event = threading.Event()
        self.ads = ads
        self.ad_interval = int(ad_interval * SR)
        self.ads_per_break = max(1, ads_per_break)
        self.ad_fade = min(self.fade, max(0, int(ad_crossfade * SR)))
        self.since_break = 0      # frames of audio output since the last ad break started
        self.break_left = 0       # ads still to play in the current break

    def _ad_due(self):
        if not self.ads or not len(self.ads):
            return False
        return self.break_left > 0 or self.since_break >= self.ad_interval

    def _pick(self):
        """Next track to play: an ad when a break is due (always at a song boundary), else music."""
        if self._ad_due():
            if self.break_left == 0:
                self.break_left, self.since_break = self.ads_per_break, 0
                log.info("ad break")
            self.break_left -= 1
            return self.ads.next()
        return self.library.next()

    def _peek(self, current: Track | None = None):
        """Best guess at what follows `current`, including a break that will fall due while it plays."""
        due = self._ad_due()
        if not due and current is not None and self.ads and len(self.ads) and not current.ad:
            remaining = int(float(current.meta.get("duration") or 0) * SR)
            due = self.since_break + remaining >= self.ad_interval
        return self.ads.peek() if due else self.library.peek()

    def _open(self):
        track = self._pick()
        return TrackReader(track) if track else None

    @staticmethod
    def _chunks(pcm):
        for i in range(0, len(pcm), BLOCK):
            yield pcm[i:i + BLOCK]

    def blocks(self):
        for block in self._blocks():
            self.since_break += len(block)
            yield block

    def _blocks(self):
        cur, pending, dead_streak = None, EMPTY, 0
        while True:
            if cur is None:
                cur = self._open()
                if cur is None:  # empty library: keep the stream alive with silence
                    self.state.set_now(None, self._peek())
                    yield np.zeros((SR, CH), dtype=np.float32)
                    continue
                self.state.set_now(cur.track, self._peek(cur.track))
                pending = EMPTY

            if self.skip_event.is_set() and cur.track.ad:
                self.skip_event.clear()          # ads can't be skipped
            if self.skip_event.is_set():
                self.skip_event.clear()
                pending = np.concatenate([pending, cur.read(self.skip_fade - len(pending))])[: self.skip_fade]
                cur.close()
                fade_len = len(pending)
                log.info("skip: %s", cur.track.display)
            else:
                need = self.fade + BLOCK - len(pending)
                if need > 0:
                    pending = np.concatenate([pending, cur.read(need)])
                if not cur.eof:
                    out, pending = pending[:BLOCK], pending[BLOCK:]
                    yield out
                    continue
                into_or_out_of_ad = cur.track.ad or self._ad_due()
                fade_len = min(self.ad_fade if into_or_out_of_ad else self.fade, len(pending))

            # ---- current track is ending: play the unfaded part, then blend
            cur.close()
            if cur.frames == 0:
                log.warning("could not decode %s", cur.track.path)
                dead_streak += 1
                if dead_streak >= 5:
                    dead_streak = 0
                    yield np.zeros((SR, CH), dtype=np.float32)
            else:
                dead_streak = 0

            split = len(pending) - fade_len
            yield from self._chunks(pending[:split])
            tail = pending[split:]

            nxt = self._open()
            incoming = nxt.read(fade_len) if nxt else EMPTY
            if len(incoming) < fade_len:
                incoming = np.concatenate([incoming, np.zeros((fade_len - len(incoming), CH), np.float32)])
            if nxt:
                self.state.set_now(nxt.track, self._peek(nxt.track))
            if fade_len:
                t = (np.arange(fade_len, dtype=np.float32) + 0.5) / fade_len
                mixed = tail * np.cos(t * math.pi / 2)[:, None] + incoming * np.sin(t * math.pi / 2)[:, None]
                np.clip(mixed, -1.0, 1.0, out=mixed)
                yield from self._chunks(mixed.astype(np.float32))
            cur, pending = nxt, EMPTY


# --------------------------------------------------------------------------- broadcast

class Listener:
    def __init__(self):
        self.q = queue.Queue(maxsize=400)   # ~ 30 s of audio at 192k before we give up on a client
        self.alive = True


class Broadcaster:
    def __init__(self, burst_bytes: int):
        self.lock = threading.Lock()
        self.listeners: set[Listener] = set()
        self.burst = collections.deque()
        self.burst_size = 0
        self.burst_max = burst_bytes

    def publish(self, data: bytes):
        with self.lock:
            self.burst.append(data)
            self.burst_size += len(data)
            while self.burst_size - len(self.burst[0]) >= self.burst_max:
                self.burst_size -= len(self.burst.popleft())
            for l in list(self.listeners):
                try:
                    l.q.put_nowait(data)
                except queue.Full:       # client too slow; drop it
                    l.alive = False
                    self.listeners.discard(l)

    def subscribe(self) -> Listener:
        l = Listener()
        with self.lock:
            for chunk in self.burst:     # a few seconds up front so playback starts instantly
                l.q.put_nowait(chunk)
            self.listeners.add(l)
        return l

    def unsubscribe(self, l: Listener):
        with self.lock:
            self.listeners.discard(l)

    def count(self):
        with self.lock:
            return len(self.listeners)


class Encoder:
    """Feeds mixer PCM into ffmpeg in real time and broadcasts the encoded bytes."""

    def __init__(self, mixer: Mixer, broadcaster: Broadcaster, fmt: str, bitrate: str, lead: float):
        self.mixer, self.broadcaster, self.lead = mixer, broadcaster, lead
        codec = {
            "mp3": ["-c:a", "libmp3lame", "-b:a", bitrate, "-write_xing", "0", "-id3v2_version", "0", "-f", "mp3"],
            "aac": ["-c:a", "aac", "-b:a", bitrate, "-f", "adts"],
        }[fmt]
        self.cmd = ["ffmpeg", "-nostdin", "-v", "error", "-f", "f32le", "-ar", str(SR), "-ac", str(CH),
                    "-i", "pipe:0", *codec, "-flush_packets", "1", "pipe:1"]

    def _pump(self, proc):
        fd = proc.stdout.fileno()
        while True:
            data = os.read(fd, 8192)
            if not data:
                break
            self.broadcaster.publish(data)

    def run(self):
        blocks = self.mixer.blocks()
        while True:
            proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
            start, sent = time.monotonic(), 0
            try:
                for block in blocks:
                    proc.stdin.write(block.tobytes())
                    proc.stdin.flush()
                    sent += len(block)
                    ahead = sent / SR - (time.monotonic() - start)
                    if ahead > self.lead:
                        time.sleep(ahead - self.lead)
                    elif ahead < -2:  # we fell behind (machine suspended?) - don't race to catch up
                        start, sent = time.monotonic(), 0
            except (BrokenPipeError, OSError) as e:
                log.error("encoder died (%s), restarting", e)
            proc.kill()
            proc.wait()
            time.sleep(1)


# --------------------------------------------------------------------------- state + http

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.now = None
        self.next = None
        self.started = None
        self.history = collections.deque(maxlen=20)

    def set_now(self, track, upcoming):
        with self.lock:
            if track is not self.now and track is not None:
                log.info("now playing%s: %s", " [ad]" if track.ad else "", track.display)
                if not track.ad:                 # keep commercials out of "recently played"
                    self.history.appendleft({**track.as_dict(), "started": time.time()})
            self.now, self.next = track, upcoming
            self.started = time.time() if track else None

    def is_ad(self):
        with self.lock:
            return bool(self.now and self.now.ad)

    def title(self):
        with self.lock:
            return self.now.display if self.now else ""

    def snapshot(self):
        with self.lock:
            return {
                "now_playing": {**self.now.as_dict(), "started": self.started} if self.now else None,
                "up_next": self.next.as_dict() if self.next else None,
                "recent": list(self.history)[1:11],
            }


def icy_block(title: str, last: list) -> bytes:
    if title == last[0]:
        return b"\x00"
    last[0] = title
    payload = "StreamTitle='{}';".format(title.replace("'", "’")).encode("utf-8")[: 255 * 16]
    n = math.ceil(len(payload) / 16)
    return bytes([n]) + payload.ljust(n * 16, b"\x00")


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name}</title>
<style>
 body{{font:16px/1.5 system-ui,sans-serif;background:#111;color:#eee;display:grid;place-items:center;min-height:100vh;margin:0}}
 main{{width:min(92vw,460px)}} h1{{font-size:14px;letter-spacing:.12em;text-transform:uppercase;color:#888;margin:0 0 24px}}
 #title{{font-size:28px;font-weight:600;line-height:1.2}} #artist{{color:#aaa;margin:4px 0 24px}}
 audio{{width:100%}} button{{margin-top:16px;background:#222;color:#eee;border:1px solid #444;border-radius:6px;padding:8px 16px;cursor:pointer}}
 button:hover{{background:#333}} .small{{color:#777;font-size:13px;margin-top:20px}} ol{{padding-left:20px;color:#999;font-size:14px}}
</style></head><body><main>
<h1>{name}</h1>
<div id="title">&nbsp;</div><div id="artist">&nbsp;</div>
<audio controls preload="none" src="stream"></audio>
<button id="skip">Skip</button>
<div class="small">Up next: <span id="next">-</span> &middot; <span id="listeners">0</span> listening</div>
<div class="small">Recently played</div><ol id="recent"></ol>
</main><script>
const $=id=>document.getElementById(id), fmt=t=>t?(t.artist?t.artist+' – ':'')+t.title:'-';
async function poll(){{try{{const s=await (await fetch('status')).json();
 $('title').textContent=s.now_playing?s.now_playing.title:'(nothing playing)';
 $('artist').textContent=s.now_playing?(s.now_playing.artist||' '):' ';
 $('next').textContent=fmt(s.up_next); $('listeners').textContent=s.listeners;
 $('recent').innerHTML=''; s.recent.forEach(t=>{{const li=document.createElement('li');li.textContent=fmt(t);$('recent').append(li)}});
}}catch(e){{}}}}
$('skip').onclick=async()=>{{let tok=localStorage.getItem('skipToken')||'';
 let r=await fetch('skip?token='+encodeURIComponent(tok),{{method:'POST'}});
 if(r.status==403){{tok=prompt('Skip token');if(tok===null)return;localStorage.setItem('skipToken',tok);
  r=await fetch('skip?token='+encodeURIComponent(tok),{{method:'POST'}})}}
 setTimeout(poll,2500)}};
poll(); setInterval(poll,5000);
</script></body></html>"""


def make_handler(ctx):
    class Handler(BaseHTTPRequestHandler):
        server_version = "radio.py"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.client_address[0], *args)

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, indent=2).encode(), "application/json")

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/":
                self._send(200, PAGE.format(name=html.escape(ctx.name)).encode(), "text/html; charset=utf-8")
            elif path in ("/stream", "/stream.mp3", "/stream.aac"):
                self.stream()
            elif path == "/status":
                self._json({**ctx.state.snapshot(), "listeners": ctx.broadcaster.count(),
                            "library_size": len(ctx.library),
                            "library_seconds": round(ctx.library.total_duration()[0]),
                            "crossfade_seconds": ctx.crossfade})
            else:
                self._send(404, b"not found\n", "text/plain")

        def do_POST(self):
            url = urlparse(self.path)
            if url.path.rstrip("/") != "/skip":
                return self._send(404, b"not found\n", "text/plain")
            token = parse_qs(url.query).get("token", [""])[0] or self.headers.get("X-Skip-Token", "")
            if ctx.skip_token and token != ctx.skip_token:
                return self._json({"error": "bad token"}, 403)
            if ctx.state.is_ad():
                return self._json({"error": "ads can't be skipped"}, 409)
            ctx.mixer.skip_event.set()
            self._json({"ok": True, "skipping": ctx.state.title()})

        def stream(self):
            icy = self.headers.get("Icy-MetaData") == "1"
            self.send_response(200)
            self.send_header("Content-Type", ctx.mime)
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "close")
            self.send_header("icy-name", ctx.name)
            self.send_header("icy-br", ctx.bitrate.rstrip("kK"))
            if icy:
                self.send_header("icy-metaint", str(ICY_METAINT))
            self.end_headers()

            listener = ctx.broadcaster.subscribe()
            log.info("listener connected from %s (%d total)", self.client_address[0], ctx.broadcaster.count())
            until_meta, last_title = ICY_METAINT, [None]
            try:
                while listener.alive:
                    try:
                        chunk = listener.q.get(timeout=5)
                    except queue.Empty:
                        continue
                    if not icy:
                        self.wfile.write(chunk)
                        continue
                    while chunk:
                        part, chunk = chunk[:until_meta], chunk[until_meta:]
                        self.wfile.write(part)
                        until_meta -= len(part)
                        if until_meta == 0:
                            self.wfile.write(icy_block(ctx.state.title(), last_title))
                            until_meta = ICY_METAINT
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                ctx.broadcaster.unsubscribe(listener)
                log.info("listener %s left (%d total)", self.client_address[0], ctx.broadcaster.count())

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cache", type=Path, help="directory produced by prepare.py")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--name", default="radio.py", help="station name shown to players")
    ap.add_argument("--crossfade", type=float, default=6.0, help="crossfade length in seconds (0 = gapless)")
    ap.add_argument("--skip-fade", type=float, default=2.0, help="fade length when skipping")
    ap.add_argument("--format", choices=["mp3", "aac"], default="mp3")
    ap.add_argument("--bitrate", default="192k")
    ap.add_argument("--history", type=int, default=50, help="don't replay any of the last N tracks across shuffles")
    ap.add_argument("--rescan", type=float, default=60, help="seconds between library rescans")
    ap.add_argument("--skip-token", default=os.environ.get("RADIO_SKIP_TOKEN", ""),
                    help="require this token for /skip (default: anyone can skip)")
    ap.add_argument("--ads", type=Path, help="directory of ads prepared by prepare.py (optional)")
    ap.add_argument("--ad-interval", type=float, default=15, help="minutes between ad breaks")
    ap.add_argument("--ads-per-break", type=int, default=1, help="ads played back to back in each break")
    ap.add_argument("--ad-crossfade", type=float, default=1.0, help="crossfade into, out of and between ads (seconds)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not args.cache.is_dir():
        raise SystemExit(f"cache directory not found: {args.cache} (run prepare.py first)")

    library = Library(args.cache.resolve(), args.history)
    if not library.scan():
        log.warning("no tracks in %s yet - streaming silence until some appear", args.cache)

    ads = None
    if args.ads:
        if not args.ads.is_dir():
            raise SystemExit(f"ads directory not found: {args.ads}")
        ads = Library(args.ads.resolve(), args.history, ads=True, label="ads")
        if not ads.scan():
            log.warning("no ads in %s yet - breaks start once some appear", args.ads)
    libraries = [lib for lib in (library, ads) if lib]

    def scan_all(*_):
        for lib in libraries:
            try:
                lib.scan()
            except Exception:
                log.exception("rescan of %s failed", lib.root)

    def rescan_loop():
        while True:
            time.sleep(args.rescan)
            scan_all()

    threading.Thread(target=rescan_loop, daemon=True).start()
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, scan_all)

    kbps = int("".join(c for c in args.bitrate if c.isdigit()) or 192)
    state = State()
    mixer = Mixer(library, args.crossfade, args.skip_fade, state,
                  ads=ads, ad_interval=args.ad_interval * 60, ads_per_break=args.ads_per_break,
                  ad_crossfade=args.ad_crossfade)
    broadcaster = Broadcaster(burst_bytes=kbps * 1000 // 8 * 3)  # ~3 s burst for new listeners
    encoder = Encoder(mixer, broadcaster, args.format, args.bitrate, lead=1.0)
    threading.Thread(target=encoder.run, daemon=True, name="encoder").start()

    class Ctx:
        pass
    ctx = Ctx()
    ctx.name, ctx.crossfade, ctx.bitrate, ctx.skip_token = args.name, args.crossfade, args.bitrate, args.skip_token
    ctx.mime = "audio/mpeg" if args.format == "mp3" else "audio/aac"
    ctx.library, ctx.state, ctx.mixer, ctx.broadcaster = library, state, mixer, broadcaster

    server = ThreadingHTTPServer((args.host, args.port), make_handler(ctx))
    server.daemon_threads = True
    log.info("streaming on http://%s:%d/  (%d tracks, %gs crossfade, %s %s)",
             args.host, args.port, len(library), args.crossfade, args.format, args.bitrate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
