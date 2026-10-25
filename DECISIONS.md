# Decision Log

A running, chronological history of the meaningful decisions made on this
project — **what** we decided, **why**, and what we traded away. The point is
future context: if a later change contradicts an earlier one, this log explains
the original reason so the reversal is a conscious choice, not an accident
("we did this six months ago because X — is X still true?").

**Conventions**
- Newest entries go at the **bottom** (read top-to-bottom as a timeline).
- One entry per meaningful decision. Skip trivial mechanical changes.
- Keep **Why** honest and specific — that is the part future-you will need.
- When a new decision reverses an older one, link back to it by date + title.

---

## 2026-03-19 — Menu-bar player, not a full app
**Decision:** Build the player as a macOS menu-bar app using `rumps`, controlling
audio through a headless `mpv` subprocess that streams a YouTube live URL.
**Why:** The goal is unobtrusive background music. A menu-bar app stays out of the
way; `mpv` + `yt-dlp` is the most reliable way to pull audio from a YouTube
livestream without a browser tab.

## 2026-03-25 — Enable yt-dlp remote components
**Decision:** Pass `remote-components=ejs:github` in `--ytdl-raw-options`.
**Why:** YouTube playback broke without it; the remote components are needed for
yt-dlp to resolve the stream.

## 2026-04-28 — Handle YouTube bot-challenges with an in-app WKWebView
**Decision:** When mpv fails with a "sign in to confirm you're not a bot" style
error, surface a "Solve YouTube challenge…" menu item that opens a `WKWebView`;
on close, export the cookies and retry. Show the item **only** on those errors.
**Why:** YouTube intermittently gates the stream behind a bot check. Letting the
user solve it once and reusing the cookies keeps playback working without making
them leave the app. Hiding the item otherwise avoids clutter.

## 2026-06-05 — Self-healing playback
**Decision:** Resolve the live stream dynamically from the channel `/live` URL on
every launch, auto-retry on early failure (with backoff), and fall back to a
last-known-good watch URL on the final attempt.
**Why:** YouTube rotates the livestream's video id and streams end. Re-resolving
each time and auto-retrying means the player heals itself instead of needing a
URL edit every time the stream changes.

## 2026-06-28 — Stop = full teardown (no keep-alive pause)
**Decision:** Stop fully terminates mpv (process + stream). There is no
"resume" — Play always starts a fresh stream.
**Why:** A live radio stream can't be resumed mid-point, and a kept-alive/paused
mpv would leave a YouTube connection running in the background. Full teardown is
the honest behavior and avoids zombie connections.

## 2026-06-28 — Capture Play/Pause via CGEventTap, not a passive monitor
**Decision:** Intercept the keyboard's Play/Pause media key with a session-level
`CGEventTap` that returns `None` to swallow the event; fall back to a passive
`NSEvent` monitor only when Accessibility permission is missing.
**Why:** The old passive `addGlobalMonitor` could only *observe* the key, so macOS
still routed Play to Apple Music and launched it. An event tap consumes the key
first (the BeardedSpice/noTunes approach) so it toggles this player instead.
**Trade-off:** The event tap requires Accessibility permission for the Python
binary; without it we degrade to the (Music-opening) passive behavior.

## 2026-06-28 — Run as an accessory (menu-bar-only) app
**Decision:** Set `NSApplicationActivationPolicyAccessory` in `__init__` so there
is no Dock icon or app-switcher entry. The auth-window flow temporarily flips to
Regular and reverts on close.
**Why:** It's a background music controller — a Dock icon is noise. Accessory
policy matches the intent of an always-running menu-bar utility.

## 2026-07-04 — Anonymous yt-dlp by default; no more Chrome cookies
**Decision:** Stop passing `cookies-from-browser=chrome` to yt-dlp as the default.
Extraction now runs anonymously (JS challenges solved via `remote-components=
ejs:github` + deno); only the cookies exported by the in-app challenge solver
(2026-04-28 entry) are still used, when that file exists.
**Why:** Playback broke completely: with signed-in YouTube account cookies,
yt-dlp (2026.06.09, latest) switches to the "tv downgraded" player client, which
YouTube now serves zero formats — every attempt failed with "No video formats
found". The identical extraction without cookies succeeds. Chrome's cookies were
only ever a convenience fallback for bot checks; anonymous + the EJS challenge
solver is the path yt-dlp actually supports today.
**Trade-off:** If YouTube raises a bot challenge for anonymous traffic, the user
must solve it once via the "Solve YouTube challenge…" window (that flow is
unchanged).

## 2026-07-04 — Fix restart.sh kill pattern (`lofi.py`, not `python3.*lofi.py`)
**Decision:** `restart.sh` now kills old instances with `pkill -f "lofi.py"`.
**Why:** The venv's `python3` resolves to the framework binary
`…/Python.app/Contents/MacOS/Python`, so the old pattern `python3.*lofi.py`
never matched a running player. Restarts silently left old instances alive — a
ghost player from the project's previous directory (`yt-background-music`,
since deleted) had been running for days with its own menu-bar icon and
media-key handler.

## 2026-07-10 — Only capture the Play/Pause key while we are playing
**Decision:** The CGEventTap stays installed at all times, but it now swallows
the Play/Pause key only when `self.is_playing` is true (`_owns_media_keys()`).
When the player is stopped, the event is passed through untouched. The passive
fallback monitor follows the same rule.
**Why:** This reverses part of the original media-key design (2026-04-27 —
"Capture the media Play/Pause key with a CGEventTap"), which swallowed the key
unconditionally. That made the lofi player hog the binding system-wide: pressing
Play/Pause while watching YouTube or listening to Spotify did not pause them —
it started the lofi stream instead. Ownership of the transport key belongs to
whichever player is currently making sound, so we only claim it when we are that
player.
**Trade-off:** The Play/Pause key can no longer *start* lofi from a stopped
state — it can only stop it. Starting is done from the menu bar. This is the
right side of the trade: a background app that is silent should never intercept
a key meant for the app the user is actually listening to. There is no way to
have both, because the tap must decide to swallow or pass the event before
knowing the user's intent.

## 2026-07-10 — Claim the Play/Pause key when no other app holds an audio session
**Decision:** `_owns_media_keys()` now answers three cases instead of two: if we
are playing, the key is ours (it stops us); if another process holds a CoreAudio
*output session*, the key is theirs and we pass the event through; if nobody
else holds one, the key is ours and it starts us. Ownership is read from the
public CoreAudio process-object API (`kAudioHardwarePropertyProcessObjectList` +
`kAudioProcessPropertyIsRunningOutput`, macOS 14.4+), via ctypes.
**Why:** This restores the ability to *start* lofi from the keyboard, which the
earlier entry today ("Only capture the Play/Pause key while we are playing")
gave up as an unavoidable trade-off. It turned out to be avoidable: we can ask
the OS who owns the transport key rather than guessing.
**Why not MediaRemote:** The obvious tool is the private MediaRemote framework
(`MRMediaRemoteGetNowPlayingApplicationIsPlaying`). Measured on macOS 26.5, its
symbols still load and its callbacks still fire, but it reports
`isPlaying=False` / `nowPlayingInfo=None` even while QuickTime is audibly
playing — Apple gated now-playing data behind an entitlement in macOS 15.4. It
is a dead end, not just a risky one. `kAudioDevicePropertyDeviceIsRunning-
Somewhere` was also rejected: it reads 1 whenever the output device is live, so
it cannot tell one app from another.
**A paused app still counts as an owner.** Verified: QuickTime keeps its output
session while paused. This is deliberate and correct — if a YouTube video is
paused, Play should resume *that video*, not start lofi over the top of it.
**Trade-off:** Any app holding an output session blocks the key from starting
lofi, including a Chrome tab that merely has a paused video (Chrome keeps the
stream open). On a Mac with such a tab open, Play/Pause will still not start
lofi; the menu bar always works. This is the conservative direction on purpose:
the failure mode is "lofi doesn't start", never "lofi hijacks someone else's
key". For the same reason the CoreAudio probe fails open — any error, or a
pre-14.4 macOS, degrades to the stop-only behavior rather than guessing.

## 2026-08-15 — Detect silent stalls, and let ffmpeg reconnect through them
**Decision:** Two layers against "the UI says playing but there is no sound":
1. mpv now runs with `--stream-lavf-o=reconnect=1,reconnect_streamed=1,
   reconnect_delay_max=5`, so ffmpeg re-opens the HTTP connection when the live
   stream drops mid-play.
2. `_poll_mpv_state` no longer watches only for process death. It polls
   `time-pos` over IPC every second and, if the playback clock has not moved for
   `STALL_TIMEOUT_SECONDS` (90), kills mpv and routes through
   `_handle_unexpected_exit(stalled=True)` to reconnect.
**Why:** The player kept going silent while the menu still showed it playing.
Reproduced live on a running instance: mpv was alive with `pause=false` but
`paused-for-cache=true`, `core-idle=true`, and `time-pos` frozen at the same
value for minutes. Every health signal the app had was tied to the mpv process
exiting (2026-06-28 — "Make playback self-healing"), and a wedged mpv never
exits, so nothing fired and the UI kept lying. This was not rare: a freshly
launched mpv reproduced the same terminal wedge within a minute.
**Why the reconnect flags are the primary fix:** measured on the LofiGirl
stream, a stall that was permanent without them instead froze for 46s and then
recovered on its own. Preventing the wedge beats restarting after it.
Restarting is only the backstop for wedges ffmpeg cannot recover from.
**Why 90s and not 20s:** the first version used 20s, on the assumption that a
genuine rebuffer recovers in a second or two. Measurement killed that
assumption — a healthy stream sat frozen for 46.4s and then resumed. A 20s
threshold would have destroyed a stream that was about to heal itself, and a
restart costs several seconds of silence plus a fresh yt-dlp resolve. 90s is
~2x the observed worst-case recovery.
**Trade-off:** in the wedge case the user hears up to 90s of silence before the
reconnect starts. That is the price of not fighting ffmpeg's own recovery; the
menu bar Stop/Play always works immediately.
**Detail:** a stall skips the bot-challenge check, because stderr can still hold
a stale challenge warning from an earlier attempt and that would wrongly block
the reconnect. `_stop` now escalates SIGTERM to SIGKILL on a background thread —
a stalled mpv can ignore SIGTERM, and waiting on the main thread would freeze
the menu bar.

## 2026-08-15 — Buffer behind the live edge (the actual stall fix)
**Decision:** mpv now starts ~60s behind the HLS live edge and keeps a deep
buffer: `--demuxer-lavf-o=live_start_index=-12`, `--cache=yes`,
`--cache-secs=180`, `--demuxer-readahead-secs=180`.
**Why:** This is the root cause of "the menu says playing but there is no
sound". At the live edge mpv has no cushion, so the first delivery hiccup
empties the buffer and it wedges in `paused-for-cache` permanently — alive,
`pause=false`, silent. Measured side by side on the same stream in the same
95s window: the live-edge player produced **0.0s of audio**, while the buffered
player produced **95.1s** and held its 60s cushion the whole time.
**Trade-off:** playback is ~60s behind live. For a radio stream nobody can tell,
so the cushion is nearly free. It also costs a few MB of memory.
**Reverses:** the `--stream-lavf-o=reconnect=...` flag added earlier today
(2026-08-15 — "Detect silent stalls…"). It is removed. It was added on a single
observation that a stall "recovered after 46s" with it. That was wrong: a
controlled concurrent A/B showed plain mpv at 0.050x and the reconnect build at
0.071x — i.e. no real difference, both starving. HLS fetches each segment as its
own short request, so connection-level retry has little to work with.
**Method note, worth keeping:** this stream's throughput swings wildly minute to
minute — the same config measured 1.000x and 0.000x minutes apart, and ffmpeg
showed the same swings on the same URL. Sequential A/B tests here are worthless;
they produced two confident and opposite wrong conclusions. Only run competing
configs **concurrently** against this stream.

## 2026-08-15 — Tune the stall detector to the kind of stall
**Decision:** `_poll_mpv_state` now watches two clocks, not one: `time-pos`
(is sound coming out) and `demuxer-cache-time` (are bytes arriving). Frozen
playback while the cache still fills is an ordinary rebuffer and gets
`STALL_TIMEOUT_SECONDS` (60). Frozen playback with a frozen cache means nothing
is arriving at all and gets `NO_DATA_TIMEOUT_SECONDS` (30).
**Why:** a single timer cannot tell "nearly refilled" from "connection is dead",
so it has to assume the hopeful case and make every dead stream wait out the
long timeout. Verified live in the running app: the first version restarted a
wedged mpv after ~100s; splitting the cases cuts the dead-connection path to
~33s while still letting a genuine rebuffer finish.
**Status:** with the buffering fix above, this should now rarely fire. It stays
as the backstop, since the wedge state is unrecoverable when it does happen.

## 2026-09-17 — Pin the stream video id; /live becomes the fallback
**Decision:** `STREAM_URL` is now the specific "beats to relax/study to" watch
URL and `@LofiGirl/live` is the last-resort fallback. This reverses the
2026-08 choice to resolve through `/live` first.
**Why:** Lofi Girl runs several livestreams at the same time (relax, sleep,
synthwave, ambience). `/live` returns whichever one YouTube picks, and today
it picked a city-ambience stream, so the player played background traffic
instead of music. A pinned id always plays the right stream; the cost is a
manual id update when YouTube rotates it, which `/live` still covers as the
final retry so the player never goes fully silent.

## 2026-09-17 — Deeper live cushion + rebuild it after a rebuffer
**Decision:** `live_start_index=-12` becomes `-60` (about 5 min behind live),
`--cache-secs`/`--demuxer-readahead-secs` go to 600, and
`--cache-pause-wait=30` is added.
**Why:** the user heard playback pause and resume by itself, many times an
hour. Sampling the running mpv over IPC showed the real cushion was only ~55s,
never the 180s the flags asked for: on a live stream mpv cannot read ahead of
what YouTube has published, so the cushion equals the start offset and no
more. YouTube's segment delivery stops for 30-60s at a time. When one gap
outlasted the 55s cushion, mpv's `cache-pause` drained, paused, resumed after
the default 1s of new data, and from then on sat at the live edge with no
cushion at all, so every later hiccup paused it again. That is why it went
from "sometimes" to "a lot" within one session. The playlist holds a 4h DVR
window, so a 5 min start offset is free. `cache-pause-wait=30` means a
rebuffer, if one ever happens, rebuilds a 30s cushion instead of 1s.
**Trade-off:** playback is 5 min behind live (still meaningless for radio),
and a genuine rebuffer now costs up to 30s of silence instead of many 1-2s
blips. Extends 2026-08-15 "Buffer behind the live edge".

## 2026-09-17 — The blips were 403s, not buffering: audio-only format + fresh yt-dlp
**Decision:** `--ytdl-format=91` becomes `234/233/91` (audio-only first, old
muxed format as fallback), yt-dlp is upgraded from 2026.06.09 to current, and
`_handle_unexpected_exit` now logs every automatic mpv restart to stderr.
**Why:** watching mpv pids showed a metronome: every ~90s mpv died and the app
restarted it silently, and the ~4s gap (1.5s backoff + yt-dlp resolve) is the
pause the user heard. Verbose ffmpeg output showed the cause: with the stale
yt-dlp (android_vr client, muxed formats only) YouTube returned HTTP 403 on
every media segment about 90s into each session while the playlist itself
still loaded. The cushion from the entry above does not prevent this, it only
delays the exit until the cushion drains. Concurrent A/B on the same stream:
old setup restarted 3 times in the window, yt-dlp 2026.08 + format 234 played
6.5 min continuously with no underrun. Bonus: 234 is audio-only, so the app
stops downloading and decoding a 144p video track it discards.
**Logging:** the log was one line long. The app restarted mpv all morning with
no trace, which turned a 30-second diagnosis into a 45-minute one.
**Do not retry:** ffmpeg `reconnect`/`seg_max_retry` options were tested again
and make it worse (mpv wedges in paused-for-cache instead of exiting in 2s).
Second time this idea was tried and dropped; see 2026-08-15.

## 2026-09-17 — Add a README, and adopt the self-documenting agent instructions
**Decision:** the repo gets a `README.md` (what the app is, how to run it, where
the log is, a symptom → cause → action troubleshooting table, and the read-only
mpv IPC one-liner), and `CLAUDE.md` gains the agent rules used in the other
repos here: decide and log instead of asking, investigate before fixing, verify
live before reporting done, keep the README troubleshooting table current, never
remove logging, and a "Known dead ends" list.
**Why:** today's 403 diagnosis took 45 minutes instead of 30 seconds, and most
of that was rediscovering things `DECISIONS.md` already knew — that the log was
empty, that sequential A/B tests on this stream are worthless, and that the
ffmpeg reconnect flags had already been rejected once. The log was good at
holding the *why* of past changes but nothing told a future session to read it
first, or gave an operator a place to look up a symptom. The README is that
place; the CLAUDE.md rules are what make a session use it.
**Wording source:** ported from `/Users/bot/apps/loadem/CLAUDE.md` (updated
2026-09-15, the newest and most complete of the set) — its "Documentation and
decision records (MANDATORY)", "Evidence Before Assertion" and "Task Completion
Convention" sections — with the compact declare-or-do shape from
`marketing-atso`, which suits a small repo better. Adapted: no `docs/` tree, no
ADR numbering, no MR gate and no test suite here, so the triggers point at the
two files that exist (`DECISIONS.md`, `README.md`) and "done" is a live soak
rather than a green suite.
**Trade-off:** more instruction text in a one-file project. Accepted because the
failures here are timing-dependent and only show up after minutes of playing,
which is exactly the kind of thing a fresh session gets wrong quickly.

## 2026-10-25 — Native favorites and temporary YouTube playback
**Decision:** Keep the Classic Mac menu using `rumps`/AppKit. Add native paste-link
and save dialogs, a pinned Lofi Girl default, direct one-click favorites, and
native rename/remove/move controls. Store a versioned favorites JSON file under
`~/Library/Application Support/Lofi Player/` using atomic replacement. Invalid
files are preserved and writes blocked with a visible explanation. Temporary
links stay session-only; launch remains silent with the default selected.
**Why:** The user wants long YouTube mixes without a background Chrome tab and
explicitly wants native Mac controls, not Electron. Favorites require no account
or database. Normalize video/share/live links and strip playlist/tracking/time
parameters so alternate links to the same video don't create duplicate entries.
Explicit channel `/live` URLs remain distinct because their destination changes.
**Trade-off:** Manage Favorites uses a native picker plus Rename, Move Up, Move
Down, and Remove actions, rather than a separate library window. The paste dialog
has an unchecked Save option; Save Current is available later. Native text fields
explicitly route standard Command-key editing shortcuts without an Edit menu.

## 2026-10-25 — Separate live radio from recorded mix playback
**Decision:** Split UI, dialogs, storage and playback into small Python modules.
One playback worker serializes metadata extraction, process creation, IPC,
recovery and teardown. Every selection/stop invalidates the prior session; queued
UI callbacks also check that session. Metadata extraction runs off the main
thread, is cancellable, and has a deadline. Recorded mixes pause/resume, can
restart from the beginning, recover near their last position, and stop on EOF.
**Why:** Extends 2026-06-28 "Stop = full teardown (no keep-alive pause)": its
reasoning still applies to live radio, but destroying a recorded one-hour mix
every time the user pauses loses their place. mpv `keep-open` and `eof-reached`
let us distinguish a completed recording from an unexpected process exit. If EOF
is substantially before a known duration, treat it as a recoverable truncation.
Paused clocks are excluded from stall detection.
**Preserved:** Live start offset, deep buffering, format preference, cookie
challenge flow, media-key ownership rules, and recovery logging. General audio
format fallbacks support other YouTube sources. Only the built-in default may
use the channel fallback from 2026-09-17; it is now indicated in the menu. Custom
failures never substitute another source. Retry budgets reset after sustained
playback rather than merely surviving the first six seconds.
**Trade-offs:** Metadata adds a resolution step for custom links. Playlist-only
URLs are rejected. Positions are not saved across restarts. mpv runs without
user config so global looping, scripts or playlists cannot change these rules.
The restart script now detaches the session and standard streams explicitly:
bare background `nohup` was being reaped when this automation session ended.
**Validation:** 14 serial unittest checks passed, including real mpv against a
silent local WAV, URL normalization, persistence failures, stale selection
cancellation, retry position, paused-stall exclusion, truncated EOF, and native
menu/dialog construction. A muted YouTube soak held the default mpv PID for 180
seconds with an advancing clock and no reconnect. The recorded mix
`CFGLoQIhmow` resolved its title/duration, paused without clock drift, resumed,
and reached normal completion after seeking near its end. Native visual/click
verification through computer control was blocked by app lookup timeouts; these
checks do not claim that visual verification or a full multi-hour soak occurred.
