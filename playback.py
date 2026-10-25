"""Serial, cancellable mpv playback; independent of AppKit.

One worker owns process creation, metadata lookup, IPC, recovery and teardown.
Changing the session invalidates all old work, including already queued UI events.
"""

import collections
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

from library import STREAM_URL, FALLBACK_STREAM_URL

MPV_BIN = "/opt/homebrew/bin/mpv"
YTDLP_BIN = "/opt/homebrew/bin/yt-dlp"
MPV_SOCKET = os.path.join(tempfile.gettempdir(), "lofi-mpv.sock")
COOKIES_FILE = os.path.join(tempfile.gettempdir(), "lofi-yt-cookies.txt")
MAX_AUTO_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5
STALL_TIMEOUT_SECONDS = 60
NO_DATA_TIMEOUT_SECONDS = 30


def is_bot_challenge(message):
    return any(text in message.lower() for text in ("sign in to confirm", "not a bot", "use --cookies"))


def mpv_command(source, volume, position=0, socket_path=MPV_SOCKET, cookies=COOKIES_FILE):
    live = source["is_live"]
    raw = "remote-components=ejs:github"
    if os.path.exists(cookies):
        raw += f",cookies={cookies}"
    args = [MPV_BIN, "--no-config", "--no-video", "--no-terminal", "--input-media-keys=no",
            "--cache=yes", f"--volume={volume}", f"--input-ipc-server={socket_path}",
            f"--ytdl-raw-options={raw}", "--ytdl=yes"]
    if live:
        # Preserve the proven live cushion and format preference (DECISIONS.md,
        # 2026-09-17). General fallback accommodates other channels' formats.
        args += ["--demuxer-lavf-o=live_start_index=-60", "--cache-secs=600",
                 "--demuxer-readahead-secs=600", "--cache-pause-wait=30",
                 "--ytdl-format=234/233/91/bestaudio/best"]
    else:
        args += ["--ytdl-format=bestaudio/best", "--keep-open=yes", "--cache-secs=120",
                 "--cache-pause-wait=3", f"--start={max(0, position):.3f}"]
    return args + [source["url"]]


def ipc(command, socket_path=MPV_SOCKET):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(1)
            connection.connect(socket_path)
            connection.sendall((json.dumps({"command": command, "request_id": 1}) + "\n").encode())
            with connection.makefile("r") as response:
                for line in response:
                    message = json.loads(line)
                    if message.get("request_id") == 1:
                        return message
    except (OSError, ValueError):
        pass
    return None


class Playback:
    def __init__(self, callback, socket_path=MPV_SOCKET):
        self.callback = callback
        self.socket_path = socket_path
        self.generation = 0
        self.process = None
        self.metadata_process = None
        self.volume = 70
        self.paused = False
        self._request = None
        self._seek_request = None
        self._condition = threading.Condition()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def play(self, source):
        with self._condition:
            self.generation += 1
            self._request = (self.generation, dict(source))
            self.paused = False
            self._seek_request = None
            self._condition.notify_all()
            return self.generation

    def stop(self):
        with self._condition:
            self.generation += 1
            self._request = None
            self.paused = False
            self._seek_request = None
            self._condition.notify_all()

    def seek(self, seconds):
        """Queue an absolute seek; only the current session's worker applies it."""
        with self._condition:
            self._seek_request = (self.generation, max(0, float(seconds)))
            self._condition.notify_all()

    def close(self):
        self.stop()
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._thread.join(timeout=8)

    def _active(self, generation):
        return not self._closed and generation == self.generation

    def _emit(self, generation, status, **details):
        if self._active(generation):
            self.callback(generation, status, details)

    def _wait(self, generation, seconds):
        deadline = time.monotonic() + seconds
        with self._condition:
            while self._active(generation) and time.monotonic() < deadline:
                self._condition.wait(timeout=max(0, deadline - time.monotonic()))
        return self._active(generation)

    @staticmethod
    def _terminate(proc):
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def _resolve(self, generation, source):
        if source["url"] == STREAM_URL:
            return dict(source, is_live=True)
        args = [YTDLP_BIN, "--ignore-config", "--no-playlist", "--skip-download",
                "--dump-single-json", "--no-warnings", "--socket-timeout", "15",
                "--retries", "1", "--remote-components", "ejs:github"]
        if os.path.exists(COOKIES_FILE):
            args += ["--cookies", COOKIES_FILE]
        # communicate drains both pipes, including large format lists.
        proc = self.metadata_process = subprocess.Popen(args + [source["url"]], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 45
        try:
            while self._active(generation):
                try:
                    output, error = proc.communicate(timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("YouTube took too long to respond. Try again.")
            else:
                return None
            if proc.returncode:
                raise RuntimeError(error.decode(errors="replace").strip()[-1500:] or "Could not load this YouTube video.")
            info = json.loads(output)
            if info.get("live_status") == "is_upcoming":
                raise RuntimeError("This live stream has not started yet.")
            return dict(source, title=source.get("title") or info.get("title") or "YouTube audio",
                        is_live=bool(info.get("is_live")), duration=info.get("duration"))
        finally:
            self._terminate(proc)
            proc.stdout.close()
            proc.stderr.close()
            self.metadata_process = None

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._request is not None or self._closed)
                if self._closed:
                    return
                generation, source = self._request
                self._request = None
            try:
                self._emit(generation, "loading")
                resolved = self._resolve(generation, source)
                if resolved is not None and self._active(generation):
                    self._emit(generation, "metadata", source=resolved)
                    self._session(generation, resolved)
            except Exception as exc:
                print(f"Playback failed: {exc}", file=sys.stderr, flush=True)
                self._emit(generation, "error", message=str(exc))
            finally:
                self._terminate(self.process)
                self.process = None
                try:
                    os.unlink(self.socket_path)
                except FileNotFoundError:
                    pass

    def _session(self, generation, source):
        position = 0
        attempt = 0
        while attempt <= MAX_AUTO_RETRIES:
            if not self._active(generation):
                return
            effective = dict(source)
            if attempt == MAX_AUTO_RETRIES and source["url"] == STREAM_URL:
                effective["url"] = FALLBACK_STREAM_URL
                print(f"Falling back to {FALLBACK_STREAM_URL}", file=sys.stderr, flush=True)
                self._emit(generation, "fallback")
            errors = collections.deque(maxlen=30)
            proc = self.process = subprocess.Popen(
                mpv_command(effective, self.volume, position, self.socket_path),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            def drain(stream, buffer):
                for line in iter(stream.readline, b""):
                    buffer.append(line.decode(errors="replace").strip())
            reader = threading.Thread(target=drain, args=(proc.stderr, errors), daemon=True)
            reader.start()
            self._healthy_playback = False
            try:
                result, position = self._monitor(generation, proc, source, position)
            finally:
                self._terminate(proc)
                reader.join(timeout=2)
                proc.stderr.close()
                self.process = None
            if not self._active(generation):
                return
            if result == "finished":
                self._emit(generation, "finished")
                return
            message = next((line for line in reversed(errors) if "error" in line.lower() or is_bot_challenge(line)), None)
            message = message or (errors[-1] if errors else result)
            print(f"mpv exited ({result}), attempt {attempt + 1}/{MAX_AUTO_RETRIES + 1}: {message}", file=sys.stderr, flush=True)
            # Preserve long-running radio recovery: only consecutive failures
            # exhaust the budget. Merely surviving startup isn't proof of health.
            if self._healthy_playback:
                attempt = 0
            if attempt == MAX_AUTO_RETRIES or is_bot_challenge(message):
                raise RuntimeError(message)
            self._emit(generation, "reconnecting", message=message)
            if not self._wait(generation, RETRY_BACKOFF_SECONDS):
                return
            attempt += 1

    def _property(self, name):
        response = ipc(["get_property", name], self.socket_path)
        return response.get("data") if response and response.get("error") == "success" else None

    def _monitor(self, generation, proc, source, position):
        started = time.monotonic()
        last_progress = last_data = started
        last_position = last_cache = None
        applied_pause = False
        applied_volume = self.volume
        announced = False
        progress_second = None
        while self._active(generation):
            if proc.poll() is not None:
                return "died", position
            with self._condition:
                seek = self._seek_request
                self._seek_request = None
            if seek and seek[0] == generation and not source["is_live"]:
                destination = seek[1]
                duration = source.get("duration")
                if duration:
                    destination = min(destination, max(0, duration - .1))
                response = ipc(["seek", destination, "absolute+exact"], self.socket_path)
                if response and response.get("error") == "success":
                    position = destination
                    # Seeking may require a fresh network fetch. Give it the
                    # same recovery grace as normal playback, including pause.
                    last_position = None
                    last_progress = last_data = time.monotonic()
                    self._emit(generation, "progress", position=destination)
                else:
                    print("Seek could not be applied; playback continues", file=sys.stderr, flush=True)
            if applied_volume != self.volume:
                value = self.volume
                if ipc(["set_property", "volume", value], self.socket_path):
                    applied_volume = value
            if applied_pause != self.paused:
                value = self.paused
                response = ipc(["set_property", "pause", value], self.socket_path)
                if response and response.get("error") == "success":
                    applied_pause = value
                    self._emit(generation, "paused" if value else "playing")
            pos = self._property("time-pos")
            now = time.monotonic()
            if not source["is_live"] and self._property("eof-reached"):
                duration = self._property("duration") or source.get("duration")
                # Truncated network media can also report EOF. Recover if we
                # know there should still be more than a few seconds left.
                if duration and (pos if pos is not None else position) < duration - 3:
                    return "premature end of file", position
                return "finished", 0
            if pos is not None:
                if not source["is_live"]:
                    position = pos
                    if int(pos) != progress_second:
                        progress_second = int(pos)
                        self._emit(generation, "progress", position=pos)
                if not announced:
                    announced = True
                    self._emit(generation, "paused" if applied_pause else "playing")
                if pos != last_position:
                    last_position = pos
                    last_progress = now
                    if now - started >= 60:
                        self._healthy_playback = True
            if applied_pause:
                last_progress = last_data = now
            else:
                cache = self._property("demuxer-cache-time")
                if cache is None or cache != last_cache:
                    last_cache = cache
                    last_data = now
                if not announced and now - started >= 60:
                    return "startup timed out", position
                if announced and now - last_progress >= NO_DATA_TIMEOUT_SECONDS and now - last_data >= NO_DATA_TIMEOUT_SECONDS:
                    return "stalled: no data received", position
                if announced and now - last_progress >= STALL_TIMEOUT_SECONDS:
                    return "stalled: playback is not advancing", position
            if not self._wait(generation, .5):
                break
        return "cancelled", position
