"""Serial unittest suite; real mpv integration uses silent local audio."""

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from library import DEFAULT_SOURCE, Library, STREAM_URL, normalize_url
import playback


class LibraryTests(unittest.TestCase):
    def test_share_links_are_the_same_favorite(self):
        for url in ("https://youtu.be/rFZHOHl-L8A?si=abc", STREAM_URL + "&list=abc&t=34",
                    "https://www.youtube.com/live/rFZHOHl-L8A", "https://m.youtube.com/watch?v=rFZHOHl-L8A"):
            self.assertEqual(normalize_url(url), STREAM_URL)

    def test_rejects_nonvideo_and_untrusted_urls(self):
        for url in ("file:///tmp/video", "https://youtube.com.evil.test/watch?v=rFZHOHl-L8A",
                    "https://evil.test@youtube.com/watch?v=rFZHOHl-L8A", "https://youtube.com/playlist?list=abc",
                    "https://youtube.com/@LofiGirl", "--script=evil", "https://youtu.be/nope"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_url(url)

    def test_channel_live(self):
        self.assertEqual(normalize_url("https://youtube.com/@LofiGirl/live?si=abc"), playback.FALLBACK_STREAM_URL)

    def test_roundtrip_duplicate_default_and_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested/favorites.json"
            library = Library(path)
            library.add(DEFAULT_SOURCE)
            self.assertFalse(path.exists())
            source = {"url": "https://www.youtube.com/watch?v=jfKfPfyJRdk", "title": "A mix"}
            library.add(source)
            library.add(source)
            self.assertEqual(Library(path).items, [source])
            library.save([dict(source, title="Renamed")])
            self.assertEqual(Library(path).items[0]["title"], "Renamed")
            library.save([])
            self.assertEqual(Library(path).items, [])

    def test_corrupt_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "favorites.json"
            path.write_text("broken json")
            library = Library(path)
            self.assertIsNotNone(library.error)
            with self.assertRaises(OSError):
                library.save([])
            self.assertEqual(path.read_text(), "broken json")

    def test_failed_atomic_write_preserves_old_library(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "favorites.json"
            library = Library(path)
            library.save([])
            with patch("library.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    library.save([{"url": STREAM_URL, "title": "Changed"}])
            self.assertEqual(json.loads(path.read_text())["favorites"], [])
            self.assertEqual(library.items, [])
            self.assertEqual(len(list(path.parent.iterdir())), 1)


class PlaybackTests(unittest.TestCase):
    def test_live_and_recorded_profiles(self):
        live = playback.mpv_command(dict(DEFAULT_SOURCE, is_live=True), 70)
        vod = playback.mpv_command(dict(DEFAULT_SOURCE, is_live=False), 40, 512)
        self.assertIn("--demuxer-lavf-o=live_start_index=-60", live)
        self.assertIn("--ytdl-format=234/233/91/bestaudio/best", live)
        self.assertIn("--keep-open=yes", vod)
        self.assertIn("--start=512.000", vod)
        self.assertNotIn("--demuxer-lavf-o=live_start_index=-60", vod)

    def test_cancel_during_metadata_does_not_start_old_source(self):
        started, release, selected = threading.Event(), threading.Event(), []
        engine = playback.Playback(lambda *args: None)
        def resolve(generation, source):
            if source["title"] == "Old":
                started.set()
                release.wait(2)
            return dict(source, is_live=False)
        with patch.object(engine, "_resolve", side_effect=resolve), patch.object(engine, "_session", side_effect=lambda g, s: selected.append(s["title"])):
            engine.play(dict(DEFAULT_SOURCE, title="Old"))
            self.assertTrue(started.wait(2))
            engine.play(dict(DEFAULT_SOURCE, title="New"))
            release.set()
            deadline = time.monotonic() + 2
            while not selected and time.monotonic() < deadline:
                time.sleep(.01)
            engine.close()
        self.assertEqual(selected, ["New"])

    def test_custom_source_never_falls_back_and_retries_resume_position(self):
        engine = playback.Playback(lambda *args: None)
        source = {"url": "https://www.youtube.com/watch?v=jfKfPfyJRdk", "title": "Mix", "is_live": False}
        class Process:
            def __init__(self):
                import io
                self.stderr = io.BytesIO(b"ERROR: network\n")
            def poll(self):
                return 1
        with patch("playback.subprocess.Popen", side_effect=lambda *a, **k: Process()) as spawn, patch.object(engine, "_monitor", return_value=("died", 123)), patch.object(engine, "_wait", return_value=True):
            with self.assertRaises(RuntimeError):
                engine._session(engine.generation, source)
            self.assertEqual(len(spawn.call_args_list), 4)
            for call in spawn.call_args_list:
                self.assertEqual(call.args[0][-1], source["url"])
            self.assertIn("--start=123.000", spawn.call_args_list[1].args[0])
        engine.close()

    def test_paused_clock_is_not_a_stall(self):
        engine = playback.Playback(lambda *args: None)
        engine.paused = True
        class Process:
            def poll(self):
                return None
        def prop(name):
            return {"time-pos": 10, "eof-reached": False, "demuxer-cache-time": 10}.get(name)
        with patch.object(engine, "_property", side_effect=prop), patch("playback.ipc", return_value={"error":"success"}), patch.object(engine, "_wait", side_effect=[True, True, False]), patch("playback.NO_DATA_TIMEOUT_SECONDS", 0), patch("playback.STALL_TIMEOUT_SECONDS", 0):
            self.assertEqual(engine._monitor(0, Process(), {"is_live": False}, 10), ("cancelled", 10))
        engine.close()

    def test_premature_eof_is_recoverable(self):
        engine = playback.Playback(lambda *args: None)
        class Process:
            def poll(self):
                return None
        with patch.object(engine, "_property", side_effect=lambda name: {"time-pos": 50, "eof-reached": True, "duration": 3600}.get(name)):
            result, position = engine._monitor(0, Process(), {"is_live": False}, 49)
            self.assertEqual(result, "premature end of file")
            self.assertEqual(position, 49)
        engine.close()


class NativeUITests(unittest.TestCase):
    def test_native_dialogs_and_checkbox(self):
        import dialogs
        def inspect_alert(alert):
            self.assertEqual(alert.messageText(), "Play a YouTube link")
            self.assertIsInstance(alert.accessoryView(), dialogs.EditingField)
            self.assertEqual(alert.suppressionButton().state(), 0)
            alert.accessoryView().setStringValue_(STREAM_URL)
            alert.suppressionButton().setState_(1)
            return 0
        with patch("dialogs.run_alert", side_effect=inspect_alert):
            self.assertEqual(dialogs.text_dialog("Play a YouTube link", "Paste", save_option=True), (STREAM_URL, True))
        with patch("dialogs.run_alert", return_value=1):
            self.assertEqual(dialogs.manage_dialog([DEFAULT_SOURCE])[0], "rename")

    def test_menu_duplicates_and_stale_callbacks(self):
        from lofi import LofiPlayer
        with tempfile.TemporaryDirectory() as directory, patch.object(LofiPlayer, "_setup_media_keys"), patch.object(LofiPlayer, "_make_audio_sessions", return_value=None), patch("lofi.Library", return_value=Library(Path(directory) / "favorites.json")):
            app = LofiPlayer()
            try:
                app.library.save([
                    {"url": "https://www.youtube.com/watch?v=jfKfPfyJRdk", "title": "Same name"},
                    {"url": "https://www.youtube.com/watch?v=CFGLoQIhmow", "title": "Same name"},
                ])
                app._rebuild_menu()
                self.assertEqual(len(app._favorite_items), 3)
                queued, opened = [], []
                with patch("lofi.AppHelper.callAfter", side_effect=queued.append):
                    app._schedule_dialog(lambda: opened.append(True))
                    app._schedule_dialog(lambda: opened.append(False))
                    self.assertEqual(opened, [])
                    self.assertEqual(len(queued), 1)
                    queued.pop()()
                    self.assertEqual(opened, [True])
                    self.assertFalse(app._dialog_open)
                with patch.object(app, "_update_ui", side_effect=lambda fn: fn()):
                    app._playback_event(app.player.generation - 1, "error", {"message": "Old failure"})
                    self.assertEqual(app.status, "stopped")
                    app._playback_event(app.player.generation, "metadata", {"source": {"url": "https://www.youtube.com/watch?v=CFGLoQIhmow", "title": "YouTube title", "is_live": False}})
                    self.assertEqual(app.source["title"], "Same name")
                    app.status = "playing"
                    app._refresh_ui()
                    app.toggle(None)
                    self.assertTrue(app.player.paused)
                    self.assertEqual(app.play_pause_button.title, "▶  Resume")
                    app.toggle(None)
                    self.assertFalse(app.player.paused)
            finally:
                app.player.close()


@unittest.skipUnless(Path(playback.MPV_BIN).exists(), "mpv is required")
class MpvIntegrationTests(unittest.TestCase):
    def test_real_mpv_pause_resume_and_natural_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "silence.wav")
            with wave.open(path, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b"\0\0" * 8000 * 3)
            events = []
            engine = playback.Playback(lambda g, s, d: events.append(s), str(Path(directory) / "mpv.sock"))
            original = playback.mpv_command
            def silent_command(*args):
                command = original(*args)
                return command[:-1] + ["--ao=null", command[-1]]
            def wait_for(status, timeout=8):
                deadline = time.monotonic() + timeout
                while status not in events and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertIn(status, events)
            try:
                with patch.object(engine, "_resolve", side_effect=lambda g, s: dict(s, is_live=False, duration=3)), patch("playback.mpv_command", side_effect=silent_command):
                    engine.play({"url": path, "title": "Silent test"})
                    wait_for("playing")
                    engine.paused = True
                    wait_for("paused")
                    pos = engine._property("time-pos")
                    time.sleep(.8)
                    self.assertAlmostEqual(pos, engine._property("time-pos"), delta=.05)
                    engine.paused = False
                    wait_for("finished")
                    self.assertNotIn("error", events)
                    self.assertNotIn("reconnecting", events)
            finally:
                engine.close()


if __name__ == "__main__":
    # unittest is deliberately single-process: no worker pool or watch mode.
    unittest.main()
