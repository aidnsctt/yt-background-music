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
