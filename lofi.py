#!/usr/bin/env python3
"""Lofi Hip Hop Radio — macOS menu bar audio player."""

import collections
import json
import os
import socket
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


STREAM_URL = "https://www.youtube.com/watch?v=jfKfPfyJRdk"
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
        self.is_playing = False
        self.process = None
        self.volume = 70
        self._resolve_lock = threading.Lock()
        self._stderr_buffer = collections.deque(maxlen=20)
        self._auth_window = None
        self._auth_window_delegate = None
        self._auth_web_view = None

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
        """Listen for media key events (play/pause on Apple keyboard)."""
        NX_KEYTYPE_PLAY = 16

        def _handle_media_key(event):
            try:
                if event.subtype() != 8:  # 8 = subtype for media keys
                    return
                data = event.data1()
                key_code = (data & 0xFFFF0000) >> 16
                key_state = (data & 0xFF00) >> 8  # 0xA = key down, 0xB = key up
                if key_code == NX_KEYTYPE_PLAY and key_state == 0x0A:
                    self.toggle(None)
            except Exception:
                pass

        NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            0x00004000,  # NSEventMaskSystemDefined (NSSystemDefinedMask)
            _handle_media_key,
        )

    def _on_volume_changed(self, value):
        self.volume = value
        self._volume_label.setStringValue_(f"{self.volume}%")
        self._send_mpv_volume()

    def _send_mpv_volume(self):
        self._send_mpv_command({"command": ["set_property", "volume", self.volume]})

    def _build_ytdl_raw_options(self):
        if os.path.exists(COOKIES_FILE):
            cookies_part = f"cookies={COOKIES_FILE}"
        else:
            cookies_part = "cookies-from-browser=chrome"
        return f"{cookies_part},remote-components=ejs:github"

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

            self.process = subprocess.Popen(
                [
                    MPV_BIN,
                    "--no-video",
                    "--no-terminal",
                    f"--volume={self.volume}",
                    f"--input-ipc-server={MPV_SOCKET}",
                    "--ytdl-format=91",
                    f"--ytdl-raw-options={self._build_ytdl_raw_options()}",
                    STREAM_URL,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            threading.Thread(
                target=self._drain_stderr, args=(self.process.stderr,), daemon=True
            ).start()
            self._update_ui(lambda: setattr(self, 'title', '♫'))
            threading.Thread(
                target=self._watch_for_early_exit, args=(self.process,), daemon=True
            ).start()
            threading.Thread(target=self._poll_mpv_state, daemon=True).start()

    def _watch_for_early_exit(self, proc):
        """If mpv exits within EARLY_EXIT_SECONDS, treat as a failure."""
        deadline = time.time() + EARLY_EXIT_SECONDS
        while time.time() < deadline:
            if proc.poll() is not None:
                # Give the stderr drain a moment to catch up.
                time.sleep(0.3)
                self._show_error_state()
                return
            time.sleep(0.2)

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

    def _poll_mpv_state(self):
        """Poll mpv's pause property to keep UI in sync with actual playback."""
        while self.process is not None:
            time.sleep(1)
            proc = self.process
            if proc is None:
                break
            if proc.poll() is not None:
                # mpv died after startup. Surface the failure.
                self._show_error_state()
                break
            resp = self._send_mpv_command({"command": ["get_property", "pause"]})
            if resp is None:
                continue
            paused = resp.get("data")
            if paused is None:
                continue
            if paused and self.is_playing:
                self.is_playing = False
                self._update_ui(lambda: (
                    setattr(self.play_pause_button, 'title', '▶  Play'),
                    setattr(self, 'title', '♪'),
                ))
            elif not paused and not self.is_playing:
                self.is_playing = True
                self._update_ui(lambda: (
                    setattr(self.play_pause_button, 'title', '⏸  Pause'),
                    setattr(self, 'title', '♫'),
                ))

    def _send_mpv_command(self, cmd):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect(MPV_SOCKET)
            sock.sendall((json.dumps(cmd) + "\n").encode())
            data = sock.recv(4096).decode()
            sock.close()
            return json.loads(data)
        except Exception:
            return None

    def toggle(self, _):
        if self.is_playing:
            self._stop()
        elif self.process and self.process.poll() is None:
            # mpv is still alive but paused externally — unpause it
            self._send_mpv_command({"command": ["set_property", "pause", False]})
            self.is_playing = True
            self.play_pause_button.title = "⏸  Pause"
            self.title = "♫"
        else:
            self._play()

    def _play(self):
        self._clear_error_state()
        self.is_playing = True
        self.play_pause_button.title = "⏸  Pause"
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
            self.process.terminate()
            self.process = None

    def quit_app(self, _):
        self._stop()
        rumps.quit_application()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    LofiPlayer().run()
