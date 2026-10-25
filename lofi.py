#!/usr/bin/env python3
"""Native macOS menu bar player for YouTube radio and saved mixes."""

import ctypes
import os
import struct
import signal
import sys
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

from library import Library, DEFAULT_SOURCE, normalize_url
from playback import Playback, COOKIES_FILE, is_bot_challenge
from dialogs import text_dialog, manage_dialog, message


NSWindowStyleMaskTitled = 1 << 0
NSWindowStyleMaskClosable = 1 << 1
NSWindowStyleMaskResizable = 1 << 3
SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)
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
        self.volume = 70
        self.library = Library()
        self.source = dict(DEFAULT_SOURCE, is_live=True)
        self.status = "stopped"
        self._save_pending = False
        self._fallback = False
        self.player = Playback(self._playback_event)
        self._auth_window = None
        self._auth_window_delegate = None
        self._auth_web_view = None
        self._event_tap = None
        self._event_tap_source = None
        self._audio_sessions = self._make_audio_sessions()

        self.play_pause_button = rumps.MenuItem("▶  Play", callback=self.toggle)

        self._now_playing = rumps.MenuItem(self.source["title"])
        self._detail = rumps.MenuItem("Live radio · Ready")
        self._restart_item = rumps.MenuItem("Restart from Beginning", callback=self._restart)
        self._save_item = rumps.MenuItem("Save Current to Favorites…", callback=self._save_current)
        self._favorite_items = []

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

        self._rebuild_menu()

        # rumps doesn't expose .hidden directly on MenuItem in all versions —
        # fall back to setting the underlying NSMenuItem visibility.
        self._set_item_hidden(self._error_item, True)
        self._set_item_hidden(self._retry_item, True)
        self._set_item_hidden(self._solve_item, True)

        self._build_volume_slider()
        self._setup_media_keys()
        self._refresh_ui()
        if self.library.error:
            AppHelper.callAfter(message, "Favorites could not be loaded", self.library.error)

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

        The worker keeps its process reference until teardown finishes, so a
        dying mpv does not look like another app during a rapid Stop/Play."""
        pids = {os.getpid()}
        for proc in (self.player.process,):
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
        self.player.volume = self.volume

    def _rebuild_menu(self):
        self.menu.clear()
        for key, item in (
            ("now", self._now_playing), ("detail", self._detail),
            ("transport", self.play_pause_button), ("restart", self._restart_item),
            ("error", self._error_item), ("retry", self._retry_item),
            ("challenge", self._solve_item), ("volume", self._volume_item),
        ):
            self.menu[key] = item
        self.menu.add(None)
        self.menu["paste"] = rumps.MenuItem("Play YouTube URL…", callback=self._paste_url)
        self.menu["save"] = self._save_item
        self.menu.add(None)
        self.menu["favorites-label"] = rumps.MenuItem("Favorites")
        self._favorite_items = []
        for source in [DEFAULT_SOURCE] + self.library.items:
            title = source["title"]
            label = title[:65] + ("…" if len(title) > 65 else "")
            item = rumps.MenuItem(label, callback=lambda _, source=source: self._select_source(source))
            item._menuitem.setToolTip_(title + "\n" + source["url"])
            self.menu["favorite:" + source["url"]] = item
            self._favorite_items.append((source["url"], item))
        self.menu.add(None)
        self.menu["manage"] = rumps.MenuItem("Manage Favorites…", callback=self._manage_favorites)
        self.menu["quit"] = rumps.MenuItem("Quit", callback=self.quit_app)

    def _refresh_ui(self):
        live = self.source.get("is_live")
        busy = self.status in ("loading", "reconnecting")
        self.is_playing = self.status in ("loading", "reconnecting", "playing")
        self.title = "⚠" if self.status == "error" else "♪…" if busy else "♫" if self.is_playing else "♪"
        title = self.source.get("title") or "YouTube audio"
        self._now_playing.title = title[:85] + ("…" if len(title) > 85 else "")
        self._now_playing._menuitem.setToolTip_(title)
        kind = "Live radio" if live else "Recorded mix" if live is False else "YouTube audio"
        suffix = " · Channel fallback" if self._fallback else ""
        self._detail.title = f"{kind} · {self.status.capitalize()}{suffix}"
        if busy or (live and self.is_playing):
            action = "⏹  Stop"
        elif self.is_playing:
            action = "⏸  Pause"
        elif self.status == "paused":
            action = "▶  Resume"
        else:
            action = "▶  Play"
        self.play_pause_button.title = action
        self._set_item_hidden(self._restart_item, live is not False or busy)
        self._save_item.set_callback(self._save_current if not self.library.contains(self.source["url"]) else None)
        self._save_item.title = "Saved to Favorites" if self.library.contains(self.source["url"]) else "Save Current to Favorites…"
        for url, item in self._favorite_items:
            item.state = int(url == self.source["url"])

    def _playback_event(self, generation, status, details):
        def apply():
            if generation != self.player.generation:
                return
            if status == "metadata":
                self.source = details["source"]
                for item in self.library.items:
                    if item["url"] == self.source["url"]:
                        self.source["title"] = item["title"]
                if self._save_pending:
                    self._save_pending = False
                    self._persist_favorite(self.source)
            elif status == "fallback":
                self._fallback = True
            else:
                self.status = status
                if status == "error":
                    self._save_pending = False
                    error = details["message"]
                    self._error_item.title = error.replace("\n", " ")[:90]
                    self._error_item._menuitem.setToolTip_(error)
                    self._set_item_hidden(self._error_item, False)
                    self._set_item_hidden(self._retry_item, False)
                    self._set_item_hidden(self._solve_item, not is_bot_challenge(error))
            self._refresh_ui()
        self._update_ui(apply)

    def _clear_error_state(self):
        for item in (self._error_item, self._retry_item, self._solve_item):
            self._set_item_hidden(item, True)

    def _select_source(self, source, save=False):
        self.source = dict(source)
        self._save_pending = save
        self._play()

    def _play(self):
        self._clear_error_state()
        self._fallback = False
        self.status = "loading"
        self.player.play(self.source)
        self._refresh_ui()

    def toggle(self, _):
        if self.status in ("loading", "reconnecting") or (self.source.get("is_live") and self.is_playing):
            self._stop()
        elif self.status in ("playing", "paused"):
            self.player.paused = self.status == "playing"
            self.status = "paused" if self.player.paused else "playing"
            self._refresh_ui()
        else:
            self._play()

    def _restart(self, _):
        self._play()

    def _on_retry_clicked(self, _):
        self._play()

    def _on_solve_clicked(self, _):
        self._open_yt_auth_window()

    def _paste_url(self, _):
        initial = ""
        while True:
            result = text_dialog("Play a YouTube link", "Paste a video or live stream URL. Leave Save unchecked to play just for this session.", initial, "Play", True)
            if result is None:
                return
            initial, save = result
            try:
                url = normalize_url(initial)
            except ValueError as exc:
                message("Check the YouTube link", str(exc))
                continue
            source = next((item for item in [DEFAULT_SOURCE] + self.library.items if item["url"] == url), {"url": url, "title": ""})
            self._select_source(source, save)
            return

    def _persist_favorite(self, source):
        try:
            self.library.add(source)
        except OSError as exc:
            message("Favorite could not be saved", str(exc))
            return
        self._rebuild_menu()
        self._refresh_ui()

    def _save_current(self, _):
        source = dict(self.source)
        result = text_dialog("Save to Favorites", "Give this video or live stream a name.", source.get("title") or "YouTube audio")
        if result is None:
            return
        title, _ = result
        if not title:
            message("A name is required", "Choose a name for this favorite.")
            return
        source["title"] = title[:200]
        if self.source["url"] == source["url"]:
            self.source["title"] = source["title"]
        self._persist_favorite(source)

    def _manage_favorites(self, _):
        selected = 0
        while self.library.items:
            action, selected, title = manage_dialog(self.library.items, selected)
            if action == "done":
                return
            items = [dict(item) for item in self.library.items]
            if action == "rename":
                if not title:
                    message("A name is required", "Choose a name for this favorite.")
                    continue
                items[selected]["title"] = title[:200]
            elif action == "remove":
                items.pop(selected)
                selected = max(0, selected - 1)
            elif action in ("up", "down"):
                target = selected + (-1 if action == "up" else 1)
                if not 0 <= target < len(items):
                    continue
                items[selected], items[target] = items[target], items[selected]
                selected = target
            try:
                self.library.save(items)
            except OSError as exc:
                message("Favorites could not be updated", str(exc))
                return
            for item in items:
                if item["url"] == self.source["url"]:
                    self.source["title"] = item["title"]
            self._rebuild_menu()
            self._refresh_ui()
        message("No saved favorites yet", "Lofi Girl is always available. Use Play YouTube URL… to add your own mixes.")

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

        self._auth_generation = self.player.generation
        url = NSURL.URLWithString_(self.source["url"])
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
                message("Could not save YouTube cookies", str(exc))
                return
            # Stop any zombie mpv first, then start fresh.
            if self._auth_generation == self.player.generation:
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
        self.player.stop()
        self._save_pending = False
        self.status = "stopped"
        self._refresh_ui()

    def quit_app(self, _):
        self.player.close()
        rumps.quit_application()


if __name__ == "__main__":
    app = LofiPlayer()
    signal.signal(signal.SIGINT, lambda *_: app.quit_app(None))
    signal.signal(signal.SIGTERM, lambda *_: app.quit_app(None))
    app.run()
