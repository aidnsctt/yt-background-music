#!/usr/bin/env python3
"""Lofi Hip Hop Radio — macOS menu bar audio player."""

import collections
import ctypes
import json
import os
import socket
import struct
import subprocess
import threading
import signal
import sys
import tempfile
import time
import rumps
import objc
from AppKit import (
    NSSlider, NSView, NSTextField, NSFont, NSMakeRect, NSEvent, NSSystemDefined,
    NSWindow, NSBackingStoreBuffered, NSApplication,
    NSApplicationActivationPolicyRegular, NSApplicationActivationPolicyAccessory,
)
from Foundation import NSObject, NSURL, NSURLRequest
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration, WKWebsiteDataStore
import Quartz


# Pinned watch URL for the "beats to relax/study to" stream. Lofi Girl runs
# several livestreams at once (sleep, synthwave, ambience), and the channel
# /live endpoint picks whichever it likes, which is how we ended up playing
# city traffic. Update this id when YouTube rotates the stream.
STREAM_URL = "https://www.youtube.com/watch?v=rFZHOHl-L8A"
# Tried as the final attempt if the pinned id is dead, so the player still
# heals itself (with the wrong-stream risk above) rather than going silent.
FALLBACK_STREAM_URL = "https://www.youtube.com/@LofiGirl/live"
MAX_AUTO_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5
MPV_BIN = "/opt/homebrew/bin/mpv"
MPV_SOCKET = os.path.join(tempfile.gettempdir(), "lofi-mpv.sock")
COOKIES_FILE = os.path.join(tempfile.gettempdir(), "lofi-yt-cookies.txt")

NSWindowStyleMaskTitled = 1 << 0
NSWindowStyleMaskClosable = 1 << 1
NSWindowStyleMaskResizable = 1 << 3
SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)
EARLY_EXIT_SECONDS = 6
# How long mpv's playback clock may stand still before we give up on the stream
# and reconnect from scratch. mpv keeps running (and reports pause=false) when a
# live stream stalls, so process death alone can't detect it.
#
# Covers the case where bytes are still trickling in but playback never
# resumes. A restart costs a few seconds of silence plus a fresh yt-dlp
# resolve, so give a genuine rebuffer room to finish first.
STALL_TIMEOUT_SECONDS = 60
# The faster path out of a stall. The clock above can't tell "buffer is
# refilling, nearly there" from "the connection is dead", so it has to assume
# the hopeful case and wait. `demuxer-cache-time` does tell them apart: it only
# moves when bytes actually arrive. If it has not moved at all, nothing is being
# received and waiting out the longer timeout just adds silence.
#
# Measured wedge: cache advanced once to 88% buffered, then stopped dead with
# playback still frozen — mpv sat waiting forever for a buffer that would never
# fill. That state is unrecoverable and is what this catches.
NO_DATA_TIMEOUT_SECONDS = 30

# Media-key plumbing. NX_SYSDEFINED (14) is the CGEvent type that carries
# Apple's special keys; key code 16 (NX_KEYTYPE_PLAY) is Play/Pause.
NX_SYSDEFINED = 14
NX_KEYTYPE_PLAY = 16


class AudioSessions:
    """Ask CoreAudio which processes currently hold an output audio session.

    Used to decide whether some *other* app owns the Play/Pause key. A process
    shows up here while it is playing AND while it is merely paused (verified on
    macOS 26.5 with QuickTime), which is exactly the ownership rule we want: if a
    YouTube video is paused, Play should resume that video, not start lofi.

    Public CoreAudio API (process objects, macOS 14.4+). We do NOT use the
    private MediaRemote framework: since macOS 15.4 its now-playing queries
    return nothing to unentitled processes — verified on 26.5, where
    MRMediaRemoteGetNowPlayingApplicationIsPlaying reports False even while
    QuickTime is audibly playing.
    """

    _GLOBAL = struct.unpack(">I", b"glob")[0]
    _PROCESS_LIST = struct.unpack(">I", b"prs#")[0]   # kAudioHardwarePropertyProcessObjectList
    _PID = struct.unpack(">I", b"ppid")[0]            # kAudioProcessPropertyPID
    _IS_RUNNING_OUTPUT = struct.unpack(">I", b"piro")[0]  # kAudioProcessPropertyIsRunningOutput
    _SYSTEM_OBJECT = 1

    class _Address(ctypes.Structure):
        _fields_ = [
            ("mSelector", ctypes.c_uint32),
            ("mScope", ctypes.c_uint32),
            ("mElement", ctypes.c_uint32),
        ]

    def __init__(self):
        self._ca = ctypes.CDLL(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        )

    def _get(self, obj, selector, ctype):
        addr = self._Address(selector, self._GLOBAL, 0)
        out = ctype()
        size = ctypes.c_uint32(ctypes.sizeof(ctype))
        err = self._ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(obj), ctypes.byref(addr),
            ctypes.c_uint32(0), None, ctypes.byref(size), ctypes.byref(out),
        )
        if err:
            raise OSError(f"AudioObjectGetPropertyData(sel={selector}) err={err}")
        return out.value

    def _process_objects(self):
        addr = self._Address(self._PROCESS_LIST, self._GLOBAL, 0)
        size = ctypes.c_uint32(0)
        err = self._ca.AudioObjectGetPropertyDataSize(
            ctypes.c_uint32(self._SYSTEM_OBJECT), ctypes.byref(addr),
            ctypes.c_uint32(0), None, ctypes.byref(size),
        )
        if err:
            raise OSError(f"AudioObjectGetPropertyDataSize err={err}")
        count = size.value // ctypes.sizeof(ctypes.c_uint32)
        buf = (ctypes.c_uint32 * count)()
        err = self._ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(self._SYSTEM_OBJECT), ctypes.byref(addr),
            ctypes.c_uint32(0), None, ctypes.byref(size), buf,
        )
        if err:
            raise OSError(f"AudioObjectGetPropertyData(process list) err={err}")
        return buf[: size.value // ctypes.sizeof(ctypes.c_uint32)]

    def other_app_holds_output(self, exclude_pids):
        """True if a process other than ours holds an output session.

        Fails open (returns True) if CoreAudio can't answer: better to let the
        key through to whatever else might be playing than to hijack it.
        """
        try:
            objects = self._process_objects()
        except OSError:
            return True
        for obj in objects:
            try:
                if not self._get(obj, self._IS_RUNNING_OUTPUT, ctypes.c_uint32):
                    continue
                pid = self._get(obj, self._PID, ctypes.c_int32)
            except OSError:
                continue
            if pid not in exclude_pids:
                return True
        return False


class SliderTarget(NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(SliderTarget, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def sliderChanged_(self, sender):
        if self._callback:
            self._callback(int(sender.intValue()))


class _AuthWindowDelegate(NSObject):
    def initWithApp_(self, app):
        self = objc.super(_AuthWindowDelegate, self).init()
        if self is None:
            return None
        self._app = app
        return self

    def windowWillClose_(self, _notification):
        if self._app is not None:
            self._app._on_auth_window_will_close()


class LofiPlayer(rumps.App):
    def __init__(self):
        super().__init__("♪", quit_button=None)
        # Run as a menu-bar-only background app: no Dock icon, no app switcher
        # entry. The auth-window flow flips to Regular and back to Accessory.
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
        self.is_playing = False
        self.process = None
        # Kept after self.process is cleared so _own_pids() can still recognise a
        # dying mpv as ours.
        self._last_process = None
        self.volume = 70
        self._retry_count = 0
        self._resolve_lock = threading.Lock()
        self._stderr_buffer = collections.deque(maxlen=20)
        self._auth_window = None
        self._auth_window_delegate = None
        self._auth_web_view = None
        self._event_tap = None
        self._event_tap_source = None
        self._audio_sessions = self._make_audio_sessions()

        self.play_pause_button = rumps.MenuItem("▶  Play", callback=self.toggle)

        label = rumps.MenuItem("lofi hip hop radio")
        label.set_callback(None)

        self._error_item = rumps.MenuItem("")
        self._error_item.set_callback(None)
        self._error_item.hidden = True

        self._solve_item = rumps.MenuItem(
            "Solve YouTube challenge…", callback=self._on_solve_clicked
        )
        self._retry_item = rumps.MenuItem("Retry", callback=self._on_retry_clicked)
        self._retry_item.hidden = True

        # Volume slider placeholder — we'll attach the NSView to its _menuitem
        self._volume_item = rumps.MenuItem("Volume")
        self._volume_item.set_callback(None)

        self.menu = [
            label,
            None,
            self.play_pause_button,
            self._error_item,
            self._retry_item,
            self._solve_item,
            None,
            self._volume_item,
            None,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        # rumps doesn't expose .hidden directly on MenuItem in all versions —
        # fall back to setting the underlying NSMenuItem visibility.
        self._set_item_hidden(self._error_item, True)
        self._set_item_hidden(self._retry_item, True)
        self._set_item_hidden(self._solve_item, True)

        self._build_volume_slider()
        self._setup_media_keys()

    def _make_audio_sessions(self):
        """Build the CoreAudio probe, proving it works before we trust it.

        The process-object API is macOS 14.4+. On anything older the selectors
        are unknown and every query errors; probing once here means the media-key
        path can treat `None` as "can't tell" instead of guessing per keypress.
        """
        try:
            sessions = AudioSessions()
            # Probe the raw call, not other_app_holds_output() — that one fails
            # open and would swallow the very error we're testing for.
            sessions._process_objects()
            return sessions
        except Exception as exc:
            print(
                f"media-keys: CoreAudio process objects unavailable ({exc}) — "
                "Play/Pause will only stop the player, never start it.",
                file=sys.stderr, flush=True,
            )
            return None

    def _set_item_hidden(self, item, hidden):
        try:
            item._menuitem.setHidden_(bool(hidden))
        except Exception:
            pass

    def _update_ui(self, fn):
        """Schedule a UI update on the main thread (required by AppKit)."""
        AppHelper.callAfter(fn)

    def _build_volume_slider(self):
        padding = 14
        slider_w = 160
        label_w = 34
        view_w = padding + slider_w + 6 + label_w + padding
        view_h = 24

        container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, view_w, view_h))

        slider = NSSlider.alloc().initWithFrame_(
            NSMakeRect(padding, 0, slider_w, view_h)
        )
        slider.setMinValue_(0)
        slider.setMaxValue_(100)
        slider.setIntValue_(self.volume)
        slider.setContinuous_(True)

        self._slider_target = SliderTarget.alloc().initWithCallback_(
            self._on_volume_changed
        )
        slider.setTarget_(self._slider_target)
        slider.setAction_(b"sliderChanged:")
        self._slider = slider

        self._volume_label = NSTextField.labelWithString_(f"{self.volume}%")
        self._volume_label.setFrame_(
            NSMakeRect(padding + slider_w + 6, 3, label_w, view_h - 4)
        )
        self._volume_label.setFont_(
            NSFont.monospacedDigitSystemFontOfSize_weight_(11, 0.0)
        )

        container.addSubview_(slider)
        container.addSubview_(self._volume_label)

        # Attach the view directly to the rumps MenuItem's underlying NSMenuItem
        self._volume_item._menuitem.setView_(container)

    def _setup_media_keys(self):
        """Make the keyboard's Play/Pause key stop THIS player — but only while
        this player is the one making sound.

        A passive NSEvent global monitor can only *observe* the key — macOS still
        delivers Play to the system, which launches Music. So we install a
        CGEventTap at the session tap head: it receives the system-defined
        media-key event first and returns None to swallow it (the same trick
        BeardedSpice/noTunes use). This needs Accessibility permission; if the
        tap can't be created we fall back to the passive monitor so the key at
        least stops us (Music will still pop up until permission is granted).

        The tap stays installed at all times, but it only *claims* the key when
        we own it (see _owns_media_keys): when we're playing, or when no other
        app holds an audio session. Otherwise the event is passed straight
        through, so Spotify, YouTube, and the rest of macOS keep their normal
        Play/Pause behavior."""
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,  # active tap — may consume events
            Quartz.CGEventMaskBit(NX_SYSDEFINED),
            self._media_key_tap_callback,
            None,
        )
        self._event_tap = tap
        if tap is None:
            # No Accessibility permission yet — degrade to a passive monitor.
            print(
                "media-keys: CGEventTap unavailable (grant Accessibility "
                "permission to this Python binary) — falling back to passive "
                "monitor; Apple Music will still open on Play.",
                file=sys.stderr, flush=True,
            )
            self._setup_media_keys_passive()
            return
        print("media-keys: CGEventTap active — Play/Pause is captured.",
              file=sys.stderr, flush=True)

        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._event_tap_source = source
        Quartz.CFRunLoopAddSource(
            Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes
        )
        Quartz.CGEventTapEnable(tap, True)

    def _media_key_tap_callback(self, proxy, event_type, cg_event, refcon):
        # The OS disables a tap that's too slow or interrupted; re-enable it.
        if event_type in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            if self._event_tap is not None:
                Quartz.CGEventTapEnable(self._event_tap, True)
            return cg_event
        try:
            ns_event = NSEvent.eventWithCGEvent_(cg_event)
            if ns_event is not None and ns_event.subtype() == 8:  # 8 = media keys
                data = ns_event.data1()
                key_code = (data & 0xFFFF0000) >> 16
                key_state = (data & 0xFF00) >> 8  # 0x0A = down, 0x0B = up
                if key_code == NX_KEYTYPE_PLAY and self._owns_media_keys():
                    if key_state == 0x0A:  # act only on key-down
                        self.toggle(None)
                    return None  # swallow down+up so Apple Music never sees it
        except Exception:
            pass
        return cg_event

    def _owns_media_keys(self):
        """Decide whether the Play/Pause key belongs to us on this press.

        Three cases:
          1. We're playing            -> ours; the key stops us.
          2. Another app holds an     -> theirs; pass the key through so Spotify
             output audio session        or a YouTube tab plays/pauses normally.
             (playing or paused)
          3. Nothing else holds one   -> ours; the key starts us.

        Case 2 covers paused apps on purpose: a paused YouTube video should
        resume on Play, not be shadowed by lofi starting up.
        """
        if self.is_playing:
            return True
        if self._audio_sessions is None:
            return False  # can't tell who else is around; never hijack the key
        return not self._audio_sessions.other_app_holds_output(self._own_pids())

    def _own_pids(self):
        """Pids whose audio sessions are ours, not "another app's".

        Includes an mpv that is still winding down: _stop() clears self.process
        and then terminates it, so for a moment a dying mpv still holds an output
        session. Without this, a Play press right after Stop would see that
        session, conclude another app owns the key, and refuse to start."""
        pids = {os.getpid()}
        for proc in (self.process, self._last_process):
            if proc is not None and proc.poll() is None:
                pids.add(proc.pid)
        return pids

    def _setup_media_keys_passive(self):
        """Fallback when no Accessibility permission: observe the Play key and
        stop playback, but we can't stop Music from also opening."""
        def _handle_media_key(event):
            try:
                if event.subtype() != 8:
                    return
                data = event.data1()
                key_code = (data & 0xFFFF0000) >> 16
                key_state = (data & 0xFF00) >> 8
                if (
                    key_code == NX_KEYTYPE_PLAY
                    and key_state == 0x0A
                    and self._owns_media_keys()
                ):
                    self.toggle(None)
            except Exception:
                pass

        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            0x00004000,  # NSEventMaskSystemDefined
            _handle_media_key,
        )

    def _on_volume_changed(self, value):
        self.volume = value
        self._volume_label.setStringValue_(f"{self.volume}%")
        self._send_mpv_volume()

    def _send_mpv_volume(self):
        self._send_mpv_command({"command": ["set_property", "volume", self.volume]})

    def _build_ytdl_raw_options(self):
        # Anonymous by default. Signed-in account cookies (e.g. Chrome's) make
        # yt-dlp switch to the "tv downgraded" player client, which YouTube now
        # serves zero formats — extraction fails with "No video formats found".
        # Only the cookies exported by the in-app challenge solver are used.
        if os.path.exists(COOKIES_FILE):
            return f"cookies={COOKIES_FILE},remote-components=ejs:github"
        return "remote-components=ejs:github"

    def _drain_stderr(self, stream):
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode(errors="replace").rstrip()
                if line:
                    self._stderr_buffer.append(line)
        except Exception:
            pass

    def _last_error_line(self):
        for line in reversed(self._stderr_buffer):
            stripped = line.strip()
            if not stripped:
                continue
            low = stripped.lower()
            if "error" in low or "warn" in low or "sign in" in low:
                return stripped
        return self._stderr_buffer[-1].strip() if self._stderr_buffer else ""

    def _start_playback(self):
        with self._resolve_lock:
            try:
                os.unlink(MPV_SOCKET)
            except FileNotFoundError:
                pass

            self._stderr_buffer.clear()

            # The final auto-retry falls back to the channel /live endpoint in
            # case the pinned watch URL is no longer live.
            url = (
                FALLBACK_STREAM_URL
                if self._retry_count >= MAX_AUTO_RETRIES
                else STREAM_URL
            )

            self.process = subprocess.Popen(
                [
                    MPV_BIN,
                    "--no-video",
                    "--no-terminal",
                    # Don't let mpv grab macOS media keys and toggle its own
                    # internal pause — the app owns play/stop, and a self-paused
                    # mpv would keep the process + YouTube connection alive.
                    "--input-media-keys=no",
                    # Start ~60s behind the HLS live edge and keep a deep
                    # buffer. This is what actually stops the "playing but
                    # silent" bug: at the live edge mpv has no cushion, so the
                    # first delivery hiccup empties the buffer and it wedges in
                    # paused-for-cache forever. Measured side by side on the
                    # same stream: live-edge mpv played 0.0s of audio in 95s
                    # while this config played 95.1s and held its 60s cushion
                    # throughout.
                    #
                    # Being a minute behind live is meaningless for a radio
                    # stream, so the cushion is nearly free.
                    #
                    # NOTE: do NOT add ffmpeg's `reconnect` options via
                    # --stream-lavf-o here. Tried and reverted — see
                    # DECISIONS.md 2026-08-15.
                    "--demuxer-lavf-o=live_start_index=-12",
                    "--cache=yes",
                    "--cache-secs=180",
                    "--demuxer-readahead-secs=180",
                    f"--volume={self.volume}",
                    f"--input-ipc-server={MPV_SOCKET}",
                    "--ytdl-format=91",
                    f"--ytdl-raw-options={self._build_ytdl_raw_options()}",
                    url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._last_process = self.process
            threading.Thread(
                target=self._drain_stderr, args=(self.process.stderr,), daemon=True
            ).start()
            self._update_ui(lambda: setattr(self, 'title', '♫'))
            threading.Thread(
                target=self._watch_for_early_exit, args=(self.process,), daemon=True
            ).start()
            threading.Thread(
                target=self._poll_mpv_state, args=(self.process,), daemon=True
            ).start()

    def _watch_for_early_exit(self, proc):
        """If mpv exits within EARLY_EXIT_SECONDS, treat as a failure."""
        deadline = time.time() + EARLY_EXIT_SECONDS
        while time.time() < deadline:
            if proc.poll() is not None:
                # Give the stderr drain a moment to catch up.
                time.sleep(0.3)
                self._handle_unexpected_exit(proc)
                return
            time.sleep(0.2)
        # Survived startup — playback is healthy, so reset the retry budget.
        self._retry_count = 0

    def _handle_unexpected_exit(self, proc, stalled=False):
        """Decide what to do when mpv stops playing unexpectedly — because it
        died, or because it stalled (stalled=True) and we killed it: auto-retry
        or surface an error. Both watcher threads route through here; the lock +
        identity check ensure a single death is handled exactly once."""
        with self._resolve_lock:
            if self.process is not proc:
                return  # another watcher already handled this death
            self.process = None  # claim it; also stops _poll_mpv_state's loop
            msg = self._last_error_line()
            # A stall says nothing about bot challenges, and stderr may still
            # hold a stale warning from an earlier attempt — don't let that
            # misread block the reconnect.
            if self._is_bot_challenge(msg) and not stalled:
                decision = "error"  # never auto-retry — needs a manual solve
            elif self._retry_count < MAX_AUTO_RETRIES:
                self._retry_count += 1
                decision = "retry"
            else:
                decision = "error"

        if decision == "retry":
            self._update_ui(lambda: setattr(self, "title", "♪…"))  # reconnecting
            time.sleep(RETRY_BACKOFF_SECONDS)
            if self.is_playing:  # bail if the user hit Stop during the backoff
                self._start_playback()
        else:
            self._show_error_state()

    def _is_bot_challenge(self, msg):
        low = msg.lower()
        return (
            "sign in to confirm" in low
            or "not a bot" in low
            or "use --cookies" in low
        )

    def _show_error_state(self):
        msg = self._last_error_line() or "Playback failed — check /tmp/lofi-player.log"
        truncated = (msg[:80] + "…") if len(msg) > 80 else msg
        needs_challenge = self._is_bot_challenge(msg)

        def apply():
            self.is_playing = False
            self.process = None
            self.title = "⚠"
            self.play_pause_button.title = "▶  Play"
            self._error_item.title = truncated
            self._set_item_hidden(self._error_item, False)
            self._set_item_hidden(self._retry_item, False)
            self._set_item_hidden(self._solve_item, not needs_challenge)

        self._update_ui(apply)

    def _clear_error_state(self):
        def apply():
            self._set_item_hidden(self._error_item, True)
            self._set_item_hidden(self._retry_item, True)
            self._set_item_hidden(self._solve_item, True)
            self._error_item.title = ""

        self._update_ui(apply)
        self._stderr_buffer.clear()

    def _on_retry_clicked(self, _):
        self._clear_error_state()
        self._play()

    def _on_solve_clicked(self, _):
        self._open_yt_auth_window()

    def _poll_mpv_state(self, proc):
        """Watch a healthy-started mpv for the two ways playback can end:

        1. mpv exits (e.g. the live stream ended).
        2. mpv stays alive but stops making sound — a stalled network stream
           leaves it in paused-for-cache/core-idle forever with pause=false, so
           the app looks like it is playing while the room is silent.

        Case 2 is invisible to process death, so we watch two clocks instead:
        `time-pos` (is sound coming out?) and `demuxer-cache-time` (are bytes
        still arriving?). Frozen playback while the cache still fills is an
        ordinary rebuffer and gets the long grace period; frozen playback with a
        frozen cache means nothing is arriving at all, and gets cut short.
        Bound to one mpv process so it retires when a reconnect supersedes it."""
        last_pos = None
        last_cache = None
        now = time.time()
        last_progress = now  # playback clock last moved
        last_data = now      # new bytes last arrived

        while self.process is proc:
            time.sleep(1)
            if self.process is not proc:
                break
            if proc.poll() is not None:
                # mpv died after startup (e.g. the live stream ended). Try to
                # auto-heal by reconnecting; falls back to an error if needed.
                self._handle_unexpected_exit(proc)
                break

            pos = self._get_mpv_property("time-pos")
            cache = self._get_mpv_property("demuxer-cache-time")
            now = time.time()

            # None means "no reading" (still loading, or IPC hiccup) — that is
            # not evidence of a stall, so only a repeated identical clock counts.
            if cache is None or cache != last_cache:
                last_cache = cache
                last_data = now

            if pos is None or pos != last_pos:
                last_pos = pos
                last_progress = now
                continue

            # Playback is frozen. How long we tolerate it depends on whether the
            # stream is still feeding us anything.
            starved_for = now - last_data
            stalled_for = now - last_progress
            if starved_for >= NO_DATA_TIMEOUT_SECONDS:
                reason = f"no data received for {int(starved_for)}s"
            elif stalled_for >= STALL_TIMEOUT_SECONDS:
                reason = f"buffering for {int(stalled_for)}s without recovering"
            else:
                continue

            # Alive but silent. Kill it ourselves, otherwise the stuck mpv
            # lingers holding the socket and its dead YouTube connection.
            self._stderr_buffer.append(f"Stream stalled — {reason}")
            self._terminate(proc)
            self._handle_unexpected_exit(proc, stalled=True)
            break

    def _send_mpv_command(self, cmd):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect(MPV_SOCKET)
            sock.sendall((json.dumps(cmd) + "\n").encode())
            data = sock.recv(4096).decode()
            sock.close()
            # mpv can emit event lines alongside the reply; return the reply.
            for line in data.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and "error" in msg:
                    return msg
            return None
        except Exception:
            return None

    def _get_mpv_property(self, name):
        """Read one mpv property, or None if unavailable/unset."""
        reply = self._send_mpv_command({"command": ["get_property", name]})
        if not reply or reply.get("error") != "success":
            return None
        return reply.get("data")

    def _terminate(self, proc):
        """Stop an mpv process, escalating to SIGKILL if it ignores SIGTERM.
        A stalled mpv is wedged in network I/O and may not exit politely."""
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        except Exception:
            pass

    def toggle(self, _):
        # Stop fully tears down mpv (process + stream); Play always starts a
        # fresh stream. There is deliberately no "resume" — a live radio stream
        # can't be resumed mid-point, and a kept-alive mpv would leave a YouTube
        # connection running in the background.
        if self.is_playing:
            self._stop()
        else:
            self._play()

    def _play(self):
        self._clear_error_state()
        self._retry_count = 0
        self.is_playing = True
        self.play_pause_button.title = "⏹  Stop"
        self.title = "♪…"
        threading.Thread(target=self._start_playback, daemon=True).start()

    def _open_yt_auth_window(self):
        """Show a WKWebView so the user can sign in / pass YouTube's bot challenge.
        On close, export the resulting cookies to COOKIES_FILE and retry playback.
        """
        if self._auth_window is not None:
            self._auth_window.makeKeyAndOrderFront_(None)
            return

        config = WKWebViewConfiguration.alloc().init()
        data_store = WKWebsiteDataStore.defaultDataStore()
        config.setWebsiteDataStore_(data_store)

        rect = NSMakeRect(120, 120, 900, 700)
        web_view = WKWebView.alloc().initWithFrame_configuration_(rect, config)
        web_view.setCustomUserAgent_(SAFARI_UA)

        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskResizable
        )
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        window.setTitle_(
            "YouTube — sign in / solve challenge, then close this window"
        )
        window.setReleasedWhenClosed_(False)
        window.setContentView_(web_view)

        delegate = _AuthWindowDelegate.alloc().initWithApp_(self)
        window.setDelegate_(delegate)

        self._auth_window = window
        self._auth_window_delegate = delegate
        self._auth_web_view = web_view

        # Switch the app to a regular activation policy so the window can come
        # to the front and accept focus. We restore accessory mode on close.
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyRegular
        )
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

        url = NSURL.URLWithString_(STREAM_URL)
        web_view.loadRequest_(NSURLRequest.requestWithURL_(url))
        window.makeKeyAndOrderFront_(None)

    def _on_auth_window_will_close(self):
        web_view = self._auth_web_view
        self._auth_window = None
        self._auth_window_delegate = None
        self._auth_web_view = None

        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )

        if web_view is None:
            return

        store = web_view.configuration().websiteDataStore().httpCookieStore()

        def on_cookies(cookies):
            try:
                self._write_netscape_cookies(cookies)
                rumps.notification(
                    "Lofi", "", "Cookies saved — retrying playback"
                )
            except Exception as exc:
                self._stderr_buffer.append(f"cookie export failed: {exc}")
                self._show_error_state()
                return
            # Stop any zombie mpv first, then start fresh.
            self._stop()
            self._play()

        store.getAllCookies_(on_cookies)

    def _write_netscape_cookies(self, cookies):
        lines = ["# Netscape HTTP Cookie File", "# Generated by lofi.py", ""]
        for c in cookies:
            domain = str(c.domain())
            include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
            path = str(c.path()) or "/"
            secure = "TRUE" if c.isSecure() else "FALSE"
            expires_date = c.expiresDate()
            if expires_date is None:
                expiry = 0
            else:
                expiry = int(expires_date.timeIntervalSince1970())
            name = str(c.name())
            value = str(c.value())
            if "\t" in name or "\t" in value or "\n" in value:
                continue
            lines.append(
                "\t".join([domain, include_subdomains, path, secure,
                           str(expiry), name, value])
            )
        with open(COOKIES_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")

    def _stop(self):
        self.is_playing = False
        self.play_pause_button.title = "▶  Play"
        self.title = "♪"
        if self.process:
            # Escalate to SIGKILL off the main thread — a stalled mpv can ignore
            # SIGTERM, and waiting on it here would freeze the menu bar.
            proc, self.process = self.process, None
            threading.Thread(target=self._terminate, args=(proc,), daemon=True).start()

    def quit_app(self, _):
        self._stop()
        rumps.quit_application()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    LofiPlayer().run()
