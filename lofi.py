#!/usr/bin/env python3
"""Lofi Hip Hop Radio — macOS menu bar audio player."""

import json
import os
import select
import socket
import subprocess
import threading
import signal
import sys
import tempfile
import time
import rumps
import objc
from AppKit import NSSlider, NSView, NSTextField, NSFont, NSMakeRect, NSEvent, NSSystemDefined
from Foundation import NSObject


STREAM_URL = "https://www.youtube.com/watch?v=jfKfPfyJRdk"
MPV_BIN = "/opt/homebrew/bin/mpv"
MPV_SOCKET = os.path.join(tempfile.gettempdir(), "lofi-mpv.sock")


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


class LofiPlayer(rumps.App):
    def __init__(self):
        super().__init__("♪", quit_button=None)
        self.is_playing = False
        self.process = None
        self.volume = 30
        self._resolve_lock = threading.Lock()

        self.play_pause_button = rumps.MenuItem("▶  Play", callback=self.toggle)

        label = rumps.MenuItem("lofi hip hop radio")
        label.set_callback(None)

        # Volume slider placeholder — we'll attach the NSView to its _menuitem
        self._volume_item = rumps.MenuItem("Volume")
        self._volume_item.set_callback(None)

        self.menu = [
            label,
            None,
            self.play_pause_button,
            None,
            self._volume_item,
            None,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        self._build_volume_slider()
        self._setup_media_keys()

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
            if event.subtype() != 8:  # 8 = subtype for media keys
                return
            data = event.data1()
            key_code = (data & 0xFFFF0000) >> 16
            key_state = (data & 0xFF00) >> 8  # 0xA = key down, 0xB = key up
            if key_code == NX_KEYTYPE_PLAY and key_state == 0x0A:
                self.toggle(None)

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

    def _start_playback(self):
        with self._resolve_lock:
            try:
                os.unlink(MPV_SOCKET)
            except FileNotFoundError:
                pass

            self.process = subprocess.Popen(
                [
                    MPV_BIN,
                    "--no-video",
                    "--no-terminal",
                    f"--volume={self.volume}",
                    f"--input-ipc-server={MPV_SOCKET}",
                    "--ytdl-format=91",
                    "--ytdl-raw-options=cookies-from-browser=chrome",
                    STREAM_URL,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.title = "♫"
            threading.Thread(target=self._monitor_mpv_state, daemon=True).start()

    def _monitor_mpv_state(self):
        """Watch mpv's pause property via IPC so external controls stay in sync."""
        # Wait for socket to appear
        for _ in range(50):
            if os.path.exists(MPV_SOCKET):
                break
            time.sleep(0.1)
        else:
            return

        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(MPV_SOCKET)
            # Ask mpv to notify us when "pause" changes
            cmd = json.dumps({"command": ["observe_property", 1, "pause"]})
            sock.sendall((cmd + "\n").encode())
            sock.setblocking(False)

            buf = b""
            while self.is_playing and self.process and self.process.poll() is None:
                ready, _, _ = select.select([sock], [], [], 1.0)
                if not ready:
                    continue
                data = sock.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("event") == "property-change" and msg.get("name") == "pause":
                        paused = msg.get("data", False)
                        self._sync_ui_from_mpv(paused)
            sock.close()
        except Exception:
            pass

    def _sync_ui_from_mpv(self, mpv_paused):
        """Update app UI to match mpv's actual pause state."""
        if mpv_paused and self.is_playing:
            self.is_playing = False
            self.play_pause_button.title = "▶  Play"
            self.title = "♪"
        elif not mpv_paused and not self.is_playing:
            self.is_playing = True
            self.play_pause_button.title = "⏸  Pause"
            self.title = "♫"

    def _send_mpv_command(self, cmd):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect(MPV_SOCKET)
            sock.sendall((json.dumps(cmd) + "\n").encode())
            sock.close()
            return True
        except Exception:
            return False

    def toggle(self, _):
        if self.is_playing:
            # Pause mpv instead of killing it
            self._send_mpv_command({"command": ["set_property", "pause", True]})
        elif self.process and self.process.poll() is None:
            # mpv is still running but paused — resume it
            self._send_mpv_command({"command": ["set_property", "pause", False]})
        else:
            self._play()

    def _play(self):
        self.is_playing = True
        self.play_pause_button.title = "⏸  Pause"
        self.title = "♪…"
        threading.Thread(target=self._start_playback, daemon=True).start()

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
