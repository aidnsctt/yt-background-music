# lofi-player

A macOS menu-bar player for the Lofi Girl YouTube livestream.

It is one Python file (`lofi.py`). It uses [rumps](https://github.com/jaredks/rumps)
for the menu-bar item, and it runs `mpv` (with `yt-dlp` behind it) as a child
process to play the stream. The app watches that child process over mpv's JSON
IPC socket and restarts it when playback dies or goes silent.

## Requirements

Homebrew packages:

```bash
brew install mpv yt-dlp
```

Keep `yt-dlp` fresh. An old `yt-dlp` still resolves the stream but YouTube then
returns HTTP 403 for the media segments (see Troubleshooting).

Python packages (`requirements.txt`): `rumps`, `pyobjc`.

## Run it

```bash
cd /path/to/lofi-player
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./restart.sh
```

`restart.sh` is the normal way to start and to restart. It kills any running
`lofi.py` and `mpv`, then starts a new one with `nohup`. It picks the Python in
this order: `.venv/bin/python3`, then an active `VIRTUAL_ENV`, then the system
`python3`.

## Log

```
/tmp/lofi-player.log
```

`restart.sh` sends stdout and stderr there. The app writes a line for every
automatic mpv restart, so this file is the first thing to read when playback
misbehaves. The file is replaced on every restart, not appended to.

## Check the live player over mpv IPC (read-only)

The app gives mpv a JSON IPC socket at `MPV_SOCKET` in `lofi.py`
(`/tmp/lofi-mpv.sock` by default). You can read properties from outside the app.
`get_property` does not change playback.

```bash
printf '{"command":["get_property","time-pos"]}\n{"command":["get_property","demuxer-cache-duration"]}\n{"command":["get_property","paused-for-cache"]}\n' \
  | nc -U -w1 /tmp/lofi-mpv.sock
```

How to read the answer:

- `time-pos` — the playback clock. Run the command twice, a few seconds apart.
  If the number does not move, no sound is coming out.
- `demuxer-cache-duration` — seconds of audio buffered ahead. Healthy is a few
  hundred seconds. A value near 0 means the cushion is gone.
- `paused-for-cache` — `true` means mpv ran out of data and is waiting.

If the socket file does not exist, mpv is not running.

## Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| Audio pauses and resumes by itself, about every 90 seconds | Stale `yt-dlp`. YouTube returns HTTP 403 on every media segment about 90s into a session, mpv exits, and the app restarts it. The ~4s gap is what you hear. | `brew upgrade yt-dlp` (2026.08.19 or newer). Then check `/tmp/lofi-player.log` for `mpv exited` lines: after the fix there should be none. `lofi.py` also passes `--ytdl-format=234/233/91`, audio-only first. |
| Long silence, but the menu still says it is playing | mpv is wedged in `paused-for-cache`: the live cushion drained and no new segments arrived. The stall detector should kill and restart it. | Read `paused-for-cache` and `time-pos` over IPC (above). The cushion comes from `--demuxer-lavf-o=live_start_index=-60` (about 5 minutes behind live) plus `--cache-pause-wait=30`. On a live stream the cushion can never be larger than that start offset, so raising `--cache-secs` alone does nothing. |
| Wrong stream plays — city noise, sleep or synthwave instead of music | `STREAM_URL` in `lofi.py` pins one video id. If that id has been rotated by YouTube, resolving fails and the app falls back to `FALLBACK_STREAM_URL` (`@LofiGirl/live`), which returns whichever of the several Lofi Girl livestreams YouTube picks. | Open the "beats to relax/study to" livestream on YouTube, copy its video id, and update `STREAM_URL`. |
| Nothing plays, and the menu shows "Solve YouTube challenge…" | YouTube is asking for a bot check on this IP. | Click that menu item. It opens a web view; sign in or pass the check, then close the window. The cookies it exports are the only cookies the app uses. |

For the full reasoning behind each of these settings, read `DECISIONS.md`.

## Testing stream changes

This stream's throughput swings a lot from minute to minute. The same config has
measured 1.000x and 0.000x a few minutes apart. **Never compare two mpv configs
one after the other** — run them at the same time, side by side, against the same
stream. Sequential tests here have twice produced confident and opposite wrong
conclusions.
