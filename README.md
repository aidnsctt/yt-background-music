# lofi-player

A native macOS menu-bar audio player for YouTube livestreams and recorded mixes.
Lofi Girl remains the pinned default; your saved favorites appear directly in
the menu for one-click playback. Uses AppKit and `rumps`, with no Electron or
browser-based player UI.

`lofi.py` owns the native menu and media keys; `dialogs.py` contains AppKit
dialogs, `library.py` handles favorites, and `playback.py` runs `mpv` (with
`yt-dlp` behind it). A single background worker resolves links, watches playback
over mpv's JSON IPC socket, and recovers from failures.

## Using the player

- **Play YouTube URL…** accepts watch URLs, `youtu.be` share links, `/live/VIDEO`
  links, and explicit channel `/live` URLs. The title loads automatically.
  Playlist-only and arbitrary non-YouTube URLs are rejected. A video link with
  playlist or timestamp parameters plays just that video, from the beginning.
- Check **Save to favorites** in the dialog to save after the link resolves,
  or leave it unchecked for temporary playback. **Save Current to Favorites…**
  lets you save and name a temporary mix later.
- Select a favorite directly in the menu to start it. **Manage Favorites…**
  lets you choose a saved entry, rename it, move it up/down, or remove it.
  Removing a currently playing favorite does not interrupt playback.
- Recorded videos support **Pause / Resume** and **Restart from Beginning**.
  A completed mix stops; it does not loop or switch to radio. Automatic recovery
  resumes near the last observed position.
- Live streams retain **Stop / Play**, deep buffering, and reconnect behavior.
  Only the pinned default can fall back to Lofi Girl's channel `/live` URL; the
  menu indicates when this happens. Custom links always retry the selected URL.
- Startup remains silent with Lofi Girl selected. Temporary URLs and playback
  positions are session-only; favorites survive app restarts.

Favorites are stored at
`~/Library/Application Support/Lofi Player/favorites.json` using atomic writes.
If this file is unreadable or has an unsupported format, the app preserves it
and disables library writes rather than overwriting it. Copy it somewhere safe
before repairing it or moving it aside to start a new library.

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

`restart.sh` is the normal way to start and to restart. It stops running
`lofi.py` and its matching `mpv`, then starts a detached process. It picks Python in
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

The app gives mpv a JSON IPC socket at `MPV_SOCKET` in `playback.py`
(`lofi-mpv.sock` in Python's temp dir, which on macOS is `$TMPDIR`, not
`/tmp`). You can read properties from outside the app. `get_property` does not
change playback.

```bash
SOCK=$(python3 -c 'import tempfile,os;print(os.path.join(tempfile.gettempdir(),"lofi-mpv.sock"))')
printf '{"command":["get_property","time-pos"]}\n{"command":["get_property","demuxer-cache-duration"]}\n{"command":["get_property","paused-for-cache"]}\n' \
  | nc -U -w1 "$SOCK"
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
| Menu appears frozen, a dialog is inactive, or more than one music icon appears | An old player may have survived restart, or a modal dialog opened before the status menu finished dismissing. | Run the current `./restart.sh`; it waits for old instances and force-stops survivors. Dialogs now open after menu dismissal, explicitly take focus, and log their opening/closing. |
| Audio pauses and resumes by itself, about every 90 seconds | Stale `yt-dlp`. YouTube returns HTTP 403 on media segments, mpv exits, and the app restarts it. | Update `yt-dlp`, then check `/tmp/lofi-player.log` for `mpv exited` lines. `playback.py` keeps the proven live format preference `234/233/91` before general audio fallbacks. |
| Long silence, but the menu still says it is playing | mpv is wedged in `paused-for-cache`: the live cushion drained and no new segments arrived. The stall detector should kill and restart it. | Read `paused-for-cache` and `time-pos` over IPC (above). The cushion comes from `--demuxer-lavf-o=live_start_index=-60` (about 5 minutes behind live) plus `--cache-pause-wait=30`. On a live stream the cushion can never be larger than that start offset, so raising `--cache-secs` alone does nothing. |
| Default radio plays city noise, sleep or synthwave instead of study music | `STREAM_URL` in `library.py` pins one video id. If that id fails, the final retry uses `@LofiGirl/live`, which may select a different stream. The menu says "Channel fallback". | Open the desired livestream and paste its URL, or update `STREAM_URL` in `library.py`. |
| Nothing plays, and the menu shows "Solve YouTube challenge…" | YouTube is asking for a bot check on this IP. | Click that menu item. It opens a web view; sign in or pass the check, then close the window. The cookies it exports are the only cookies the app uses. |
| A pasted link will not play | The video may be private, removed, restricted, upcoming, or extraction may have timed out. | Check the menu error and `/tmp/lofi-player.log`; use Retry or choose another URL. The player will not substitute a different source. |
| Favorites cannot be loaded or saved | The local favorites file is malformed, unsupported, or not writable. | Follow the dialog's path, preserve a backup, and repair the file or permissions. Playback and the pinned default remain available. |

For the full reasoning behind each of these settings, read `DECISIONS.md`.

## Local checks

```bash
.venv/bin/python3 -m unittest -v test_player
```

The standard-library test runner is single-process. The suite covers URL
normalization, atomic persistence, stale-source cancellation, retry position,
pause and EOF handling, and native menu/dialog construction. Its mpv integration
test uses a short local WAV and a null audio output; it does not access YouTube
or modify your real favorites. YouTube playback still requires a live soak.

## Testing stream changes

This stream's throughput swings a lot from minute to minute. The same config has
measured 1.000x and 0.000x a few minutes apart. **Never compare two mpv configs
one after the other** — run them at the same time, side by side, against the same
stream. Sequential tests here have twice produced confident and opposite wrong
conclusions.
