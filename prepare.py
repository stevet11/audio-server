#!/usr/bin/env python3
"""
prepare.py - pre-convert a music library into the cache that radio.py plays.

Every source file becomes a 44.1 kHz stereo FLAC that is:
  * loudness-normalised (two-pass EBU R128, so every track plays at the same level)
  * trimmed of leading/trailing silence (so crossfades overlap actual music)
  * long dead air inside a track (e.g. before a hidden track) shortened, and reported
  * stripped of cover art and other non-audio streams
plus a small JSON sidecar holding title / artist / album / duration.

Re-running is incremental: unchanged files are skipped, changed ones are
redone, and (with --prune) cache entries whose source was deleted are removed.

Usage:
    python3 prepare.py ~/Music ./cache
    python3 prepare.py ~/Music ./cache --lufs -16 --jobs 4 --prune
"""
import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

AUDIO_EXTS = {
    ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".mp4", ".aac", ".wav",
    ".aif", ".aiff", ".wma", ".ape", ".wv", ".mka", ".webm",
}
SAMPLE_RATE = 44100
SILENCE_DB = -60 # anything quieter than this at the very start/end is trimmed


def cache_paths(src_root: Path, cache_root: Path, src: Path):
    """song.mp3 -> cache/song.mp3.flac + cache/song.mp3.json (keeps names unique)."""
    rel = src.relative_to(src_root)
    flac = cache_root / rel.parent / (rel.name + ".flac")
    return flac, flac.with_suffix(".json")


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", "-select_streams", "a:0", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    info = json.loads(out.stdout)
    if not info.get("streams"):
        raise RuntimeError("no audio stream")
    tags = {}
    sources = [info.get("format", {}).get("tags", {})]
    sources += [s.get("tags", {}) for s in info["streams"]]
    for src in sources:
        for k, v in src.items():
            tags.setdefault(k.lower(), v.strip())
    duration = float(info.get("format", {}).get("duration") or 0)
    return {"tags": tags, "duration": duration}


def trim_filter() -> str:
    one_side = f"silenceremove=start_periods=1:start_threshold={SILENCE_DB}dB:start_silence=0.05"
    # remove leading silence, flip, remove (former) trailing silence, flip back
    return f"{one_side},areverse,{one_side},areverse"


def fmt_time(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def parse_gaps(stderr: str, min_gap: float) -> list:
    """silencedetect output -> [(start, end), ...] for silent stretches inside the track."""
    gaps, start = [], None
    for line in stderr.splitlines():
        if m := re.search(r"silence_start: (-?[\d.]+)", line):
            start = max(0.0, float(m.group(1)))
        elif (m := re.search(r"silence_end: ([\d.]+)", line)) and start is not None:
            end = float(m.group(1))
            if end - start >= min_gap and start > 0:  # leading silence is the trimmer's job
                gaps.append((start, end))
            start = None
    return gaps  # a silence_start with no end is trailing silence, also the trimmer's job


def gap_filter(gaps: list, keep: float) -> str:
    """Drop the middle of each gap, keeping `keep` seconds of silence (half on each side)."""
    cuts = "+".join(f"between(t,{s + keep / 2:.3f},{e - keep / 2:.3f})" for s, e in gaps)
    # small fixed-size frames so the cut lands close to the requested times
    return f"asetnsamples=n=1024,aselect='not({cuts})',asetpts=N/SR/TB"


def measure_loudness(src: Path, pre: str, lufs: float, tp: float, gap_detect: str = ""):
    detect = f"{gap_detect}," if gap_detect else ""
    af = f"{pre},asetpts=N/SR/TB,{detect}loudnorm=I={lufs}:TP={tp}:LRA=20:print_format=json"
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(src),
         "-map", "0:a:0", "-af", af, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr else "ffmpeg failed")
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", out.stderr)
    if not m:
        raise RuntimeError("could not read loudness measurement")
    return json.loads(m[-1]), out.stderr


def convert(src: Path, dst: Path, sidecar: Path, settings: dict) -> str:
    meta = probe(src)
    pre = f"aresample={SAMPLE_RATE}"
    if settings["trim"]:
        pre += "," + trim_filter()

    lufs, tp = settings["lufs"], settings["true_peak"]
    max_gap, keep = settings["max_gap"], settings["gap_keep"]
    detect = f"silencedetect=noise={SILENCE_DB}dB:d={max_gap}" if max_gap > 0 else ""
    silent = lambda m: m["input_i"] in ("-inf", "inf") or float(m["input_i"]) < -70
    # one analysis pass: loudness (silence is below loudnorm's gate, so cutting it
    # later doesn't change the measurement) plus any long internal silences
    m, log = measure_loudness(src, pre, lufs, tp, detect)
    if silent(m) and settings["trim"]:
        # a very quiet recording can fall entirely under the trim threshold; keep it whole
        pre = f"aresample={SAMPLE_RATE}"
        m, log = measure_loudness(src, pre, lufs, tp, detect)
    if silent(m):
        raise RuntimeError("track is silent")

    gaps = parse_gaps(log, max_gap) if detect else []
    pre += ",asetpts=N/SR/TB"  # timestamps the gap cuts are measured against
    if gaps:
        pre += "," + gap_filter(gaps, keep)
    norm = (
        f"loudnorm=I={lufs}:TP={tp}:LRA=20"
        f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
        f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
        f":offset={m['target_offset']}:linear=true"
    )
    af = f"{pre},{norm},aresample={SAMPLE_RATE}"  # loudnorm upsamples internally

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(src),
         "-map", "0:a:0", "-af", af, "-ac", "2", "-ar", str(SAMPLE_RATE),
         "-sample_fmt", "s16", "-map_metadata", "-1", "-c:a", "flac",
         "-f", "flac", str(tmp)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr else "ffmpeg failed")

    final = probe(tmp)
    tags = meta["tags"]
    info = {
        "title": tags.get("title") or src.stem,
        "artist": tags.get("artist") or tags.get("album_artist") or tags.get("albumartist") or "",
        "album": tags.get("album") or "",
        "duration": round(final["duration"], 2),
        "source": str(src),
        "loudness_in": float(m["input_i"]),
        "gaps_removed": [{"at": round(s, 2), "length": round(e - s, 2)} for s, e in gaps],
        "settings": settings,
    }
    os.replace(tmp, dst)  # atomic, so the server never sees a half-written file
    sidecar.write_text(json.dumps(info, indent=2, ensure_ascii=False))
    name = f"{info['artist']} - {info['title']}" if info["artist"] else info["title"]
    for s, e in gaps:
        name += f"\n          ! dead air: {e - s:.1f}s at {fmt_time(s)}, cut to {keep:g}s"
    return name


def up_to_date(src: Path, dst: Path, sidecar: Path, settings: dict) -> bool:
    if not (dst.exists() and sidecar.exists()):
        return False
    if dst.stat().st_mtime < src.stat().st_mtime:
        return False
    try:
        return json.loads(sidecar.read_text()).get("settings") == settings
    except (OSError, ValueError):
        return False


def prune(src_root: Path, cache_root: Path) -> int:
    removed = 0
    for f in list(cache_root.rglob("*")):
        if f.name.endswith(".part"):
            f.unlink(missing_ok=True)
            continue
        if f.suffix != ".flac":
            continue
        rel = f.relative_to(cache_root)
        source = src_root / rel.parent / rel.stem  # strip the added ".flac"
        if not source.exists():
            f.unlink(missing_ok=True)
            f.with_suffix(".json").unlink(missing_ok=True)
            removed += 1
            print(f"  pruned  {rel}")
    return removed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, help="directory of music files (searched recursively)")
    ap.add_argument("cache", type=Path, help="output directory that radio.py will read")
    ap.add_argument("--lufs", type=float, default=-14.0, help="target integrated loudness (default -14)")
    ap.add_argument("--true-peak", type=float, default=-1.5, help="true-peak ceiling in dBTP (default -1.5)")
    ap.add_argument("--no-trim", action="store_true", help="keep leading/trailing silence")
    ap.add_argument("--max-gap", type=float, default=5.0,
                    help="shorten silences inside a track longer than this many seconds (0 = off)")
    ap.add_argument("--gap-keep", type=float, default=2.0, help="seconds of silence left where a gap was cut")
    ap.add_argument("--jobs", "-j", type=int, default=os.cpu_count() or 2, help="parallel conversions")
    ap.add_argument("--prune", action="store_true", help="delete cache entries whose source is gone")
    ap.add_argument("--force", action="store_true", help="re-convert everything")
    args = ap.parse_args()

    src_root, cache_root = args.source.resolve(), args.cache.resolve()
    if not src_root.is_dir():
        sys.exit(f"source directory not found: {src_root}")
    if cache_root == src_root or src_root in cache_root.parents:
        sys.exit("cache directory must be outside the source directory")
    cache_root.mkdir(parents=True, exist_ok=True)

    if args.max_gap > 0 and not 0 <= args.gap_keep < args.max_gap:
        sys.exit("--gap-keep must be at least 0 and less than --max-gap")
    settings = {"lufs": args.lufs, "true_peak": args.true_peak, "trim": not args.no_trim,
                "max_gap": args.max_gap, "gap_keep": args.gap_keep}
    files = sorted(p for p in src_root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    todo = []
    for src in files:
        dst, side = cache_paths(src_root, cache_root, src)
        if args.force or not up_to_date(src, dst, side, settings):
            todo.append((src, dst, side))

    print(f"{len(files)} audio files found, {len(files) - len(todo)} already prepared, {len(todo)} to convert")
    ok = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(convert, s, d, j, settings): s for s, d, j in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            src = futures[fut]
            try:
                name = fut.result()
                ok += 1
                print(f"[{i}/{len(todo)}] ok      {name}")
            except Exception as e:  # keep going on bad files
                failed += 1
                print(f"[{i}/{len(todo)}] FAILED  {src.relative_to(src_root)}: {e}")

    removed = prune(src_root, cache_root) if args.prune else 0
    print(f"done: {ok} converted, {failed} failed" + (f", {removed} pruned" if args.prune else ""))
    sys.exit(1 if failed and not ok else 0)


if __name__ == "__main__":
    main()
