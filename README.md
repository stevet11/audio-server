# radio.py — shuffle + crossfade streaming server

Two scripts:

| | |
|---|---|
| `prepare.py` | Converts your music folder into a cache of uniform FLAC files: 44.1 kHz stereo, loudness-normalised (two-pass EBU R128, default −14 LUFS), leading/trailing silence trimmed, long dead air inside a track (like the gap before a hidden track) shortened and reported, plus a JSON sidecar with title/artist/album. Incremental and parallel. |
| `radio.py` | Plays the cache in random order with equal-power crossfades, encodes once and streams the same audio to every listener over HTTP. |

## Requirements

- Python 3.9+ with `numpy`
- `ffmpeg` and `ffprobe` on the PATH

```bash
sudo apt install ffmpeg python3-numpy     # Debian/Ubuntu
brew install ffmpeg && pip3 install numpy  # macOS
```

## Usage

```bash
# 1. prepare (re-run any time you add music; only new or changed files are converted)
python3 prepare.py ~/Music ./cache --prune

# 2. serve
python3 radio.py ./cache --name "My Radio" --crossfade 6

# optional: ad breaks every 15 minutes from a second prepared cache
python3 prepare.py ~/Ads ./ads-cache --prune
python3 radio.py ./cache --ads ./ads-cache --ad-interval 15
```

Then open:

- `http://SERVER:8000/` is a small web player showing what's playing now, what's up next, recent tracks and a Skip button
- `http://SERVER:8000/stream` is the raw stream for VLC, foobar2000, mpv, a phone app, or a smart speaker. Players that ask for ICY metadata (like VLC) show the current artist and title.
- `http://SERVER:8000/status` returns now playing, up next, history and listener count as JSON
- `curl -X POST http://SERVER:8000/skip` skips the current track with a short fade

You can run `prepare.py` while the server is running. The server rescans the cache every 60 s (or on `kill -HUP`), and new tracks are mixed into the current shuffle.

## Options

**prepare.py**

| flag | default | |
|---|---|---|
| `--lufs` | −14 | target loudness (−16 is a bit quieter, with more headroom) |
| `--true-peak` | −1.5 | true-peak ceiling, dBTP |
| `--no-trim` | off | keep leading/trailing silence |
| `--max-gap` | 5 | silences inside a track longer than this (seconds, below −60 dB) are shortened; 0 = off |
| `--gap-keep` | 2 | seconds of silence left where a gap was cut |
| `-j / --jobs` | CPU count | parallel conversions |
| `--prune` | off | remove cache entries whose source file was deleted |
| `--force` | off | re-convert everything |

Each cut is printed as it happens (`! dead air: 120.5s at 4:39.0, cut to 2s`) and listed under `gaps_removed` in the track's JSON sidecar.

If you change `--lufs`, `--true-peak`, `--no-trim`, `--max-gap` or `--gap-keep`, the next run reconverts the affected files automatically.

**radio.py**

| flag | default | |
|---|---|---|
| `--port` / `--host` | 8000 / 0.0.0.0 | |
| `--crossfade` | 6 | seconds of overlap between tracks (0 = gapless) |
| `--skip-fade` | 2 | fade length when skipping |
| `--format` | mp3 | `mp3` or `aac` |
| `--bitrate` | 192k | |
| `--history` | 50 | when the shuffle restarts, don't replay any of the last N tracks (capped at half the library) |
| `--rescan` | 60 | seconds between library rescans |
| `--skip-token` | none | require `?token=…` to skip (or set `RADIO_SKIP_TOKEN`) |
| `--trusted-proxies` | none | IPs/CIDRs of reverse proxies whose `X-Forwarded-For` is believed (or set `RADIO_TRUSTED_PROXIES`); see [Behind nginx](#behind-nginx) |
| `--ads` | none | directory of ads prepared with `prepare.py`; enables ad breaks |
| `--ad-interval` | 15 | minutes of airtime between ad breaks |
| `--ads-per-break` | 1 | ads played back to back in each break |
| `--ad-crossfade` | 1 | crossfade into, out of and between ads, seconds (capped at `--crossfade`) |

## Choosing a bitrate

Bandwidth is bitrate × listeners. Use `--format` and `--bitrate` to change it:

| setting | per listener, per hour | quality |
|---|---|---|
| `--bitrate 192k` (default) | ~86 MB | very good |
| `--bitrate 128k` | ~58 MB | good; standard for internet radio |
| `--format aac --bitrate 128k` | ~58 MB | slightly better than MP3 at 128k |
| `--format aac --bitrate 96k` | ~43 MB | decent |
| `--bitrate 96k` (MP3) | ~43 MB | noticeably worse on cymbals and reverb |
| 64k | ~29 MB | only OK for talk |

`--format aac --bitrate 128k` (or plain `--bitrate 128k` for MP3) saves a third of the bandwidth with little audible difference. Both formats play in every browser, VLC, phones and SHOUTcast relays. Below about 96k, ffmpeg's built-in AAC encoder gets weak.

The stream bitrate doesn't affect the cache. The cache is lossless FLAC, and the stream is encoded from it as it plays.

## How it works

```
cache/*.flac ─► ffmpeg decode ─► Mixer (numpy) ─► ffmpeg encode ─► broadcaster ─► N HTTP listeners
                 (per track)      crossfade,        (one encoder,     3 s burst for
                                  shuffle, skip      real-time paced)  instant start
```

- **Shuffle bag:** every track plays once before any track repeats. When the bag refills, the most recently played tracks are held back, so you never hear the same song twice in a row across the reshuffle.
- **Crossfade:** the mixer always keeps a crossfade's worth of the current track buffered ahead. When the decoder reaches the end of the file, that buffer holds exactly the last N seconds, and those are blended with the start of the next track using an equal-power (cos/sin) curve. Because `prepare.py` trims the silence first, the overlap always lands on actual music.
- **Ad breaks:** once `--ad-interval` minutes of audio have aired, the break starts at the next song boundary, so songs are never cut off. Ads have their own shuffle bag, so they rotate evenly. They use the short `--ad-crossfade`, so the opening line of an ad isn't buried under the end of a song. Ads can't be skipped (`/skip` returns 409), are left out of "recently played", and are marked `"ad": true` in `/status`. If the ads directory is empty, no breaks are played.
- **One encode for everyone:** the encoder runs at real time no matter how many listeners there are, so CPU use stays the same with 1 or 100 listeners. A listener whose connection can't keep up for about 30 s gets dropped rather than stalling everyone else.

## Running it in Docker

The image is `python:3.12-slim` with ffmpeg and numpy added, plus both scripts. `docker-compose.yml` defines the station and two prepare jobs:

```bash
docker compose run --rm prepare        # music/   -> cache/      (re-run after adding music)
docker compose run --rm prepare-ads    # ads/     -> ads-cache/
docker compose up -d --build           # start the station on port 8000
docker compose kill -s HUP radio       # pick up new tracks now instead of within 60 s
docker compose logs -f radio
```

Station settings (name, format, bitrate, crossfade, ads) are in the `command:` list of the `radio` service. You can override the host folders, port, user and skip token in a `.env` file next to the compose file:

```ini
MUSIC_DIR=/srv/music
CACHE_DIR=/srv/radio-cache
ADS_DIR=/srv/ads
ADS_CACHE_DIR=/srv/radio-ads-cache
RADIO_PORT=8000
RADIO_UID=1000          # must be able to write the cache folders
RADIO_GID=1000
RADIO_SKIP_TOKEN=secret
```

The station mounts the caches read-only and runs as an unprivileged user. The prepare jobs have the `tools` profile, so `up` doesn't start them. A host cron job can keep the cache in sync: `*/30 * * * * cd /opt/radio && docker compose run --rm prepare`.

## Running it as a service (systemd)

```ini
# /etc/systemd/system/radio.service
[Unit]
Description=radio.py
After=network.target

[Service]
User=radio
WorkingDirectory=/opt/radio
ExecStart=/usr/bin/python3 /opt/radio/radio.py /srv/radio-cache --name "My Radio"
ExecReload=/bin/kill -HUP $MAINPID
Restart=always

[Install]
WantedBy=multi-user.target
```

A cron job like `*/30 * * * * python3 /opt/radio/prepare.py /srv/music /srv/radio-cache --prune` keeps the cache in sync with your music folder.

## Behind nginx

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location /stream {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_http_version 1.1;
    proxy_buffering    off;      # send audio as it arrives
    proxy_cache        off;      # never cache a live stream
    proxy_read_timeout 1h;       # long-lived connection
    gzip               off;
}
```

nginx forwards the `Icy-MetaData` request header by default, so players that ask for titles still get them.

`/stream` sends `X-Accel-Buffering: no`, so nginx won't buffer it even without `proxy_buffering off`. The explicit settings are still worth having. If nginx has a `proxy_cache` configured for the whole site, `proxy_cache off` keeps the stream out of it. Pages and `/status` are sent with `Cache-Control: no-cache`.

Real client IPs: by default the server logs the address that connected to it, which is nginx. To log listeners' real addresses, tell it which proxies to trust:

| where nginx runs | setting |
|---|---|
| same host, radio.py run directly | `--trusted-proxies 127.0.0.1` |
| same host, radio in Docker (published port) | `RADIO_TRUSTED_PROXIES=172.16.0.0/12` in `.env` (connections arrive from the Docker bridge gateway) |
| another container on the same Docker network | that network's subnet, e.g. `172.16.0.0/12` |
| another machine | that machine's IP |

The server only believes `X-Forwarded-For` / `X-Real-IP` when the connection comes from a trusted proxy. A listener who connects directly and sends a fake header still gets logged by their real address. If you're not sure which address nginx connects from, run with `-v` and look at the log, or check the "listener connected from" line.

## Notes

- Disk: the FLAC cache is lossless, so it's roughly 20–30 MB per 4-minute song.
- The server has no authentication apart from the optional skip token. To expose it to the internet, put it behind a reverse proxy for TLS (see [Behind nginx](#behind-nginx)).
