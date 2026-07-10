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
