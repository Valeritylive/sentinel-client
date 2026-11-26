"""Архив NVR: плеер playbin3 и основной поток (tracks/X01)."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import gtk_stub

gtk_stub.install()
import sentinel_protocols as protocols
import sentinel_storage as storage
import app as app_module


class FakeElement:
    def __init__(self, factory):
        self.factory, self.props, self.state = factory, {}, None

    def set_property(self, name, value): self.props[name] = value
    def connect(self, *a): pass
    def get_bus(self): return self
    def add_signal_watch(self): pass
    def set_state(self, state):
        self.state = state
        return "ok"


class FakeGst:
    class State:
        PLAYING, PAUSED, NULL = "PLAYING", "PAUSED", "NULL"

    class StateChangeReturn:
        FAILURE = "failure"

    class Caps:
        @staticmethod
        def from_string(text): return text

    def __init__(self, available):
        self.available, self.made = set(available), []
        gst = self

        class ElementFactory:
            @staticmethod
            def make(factory, _name):
                if factory not in gst.available:
                    return None
                element = FakeElement(factory)
                gst.made.append(element)
                return element

        self.ElementFactory = ElementFactory


DEVICE = {"id": 205, "name": "Street3", "host": "37.252.73.126", "channel": 3, "stream": "sub",
          "protocol": "hikvision", "rtsp_port": 41083, "http_port": 48083}
CREDS = {"username": "admin", "password": "secret"}


class PlayerChoiceTests(unittest.TestCase):
    def test_archive_prefers_playbin3(self):
        self.assertEqual(protocols.player_factories(True)[0], "playbin3")
        self.assertIn("playbin", protocols.player_factories(True))

    def test_live_keeps_playbin(self):
        self.assertEqual(protocols.player_factories(False), ("playbin",))

    def test_archive_stream_is_main(self):
        self.assertEqual(protocols.archive_stream(), "main")


class StartPreviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name) / "s"
        for name, value in (("DATA", data), ("DB_PATH", data / "d.sqlite3")):
            p = patch.object(storage, name, value); p.start(); self.addCleanup(p.stop)
        self.win = app_module.SentinelWindow(None)
        self.win._load_timeline_segments = lambda: None  # без фонового поиска записей

    def _start(self, available, **kwargs):
        gst = FakeGst(available)
        with patch.object(app_module, "Gst", gst):
            self.win._start_preview(0, dict(DEVICE), dict(CREDS), **kwargs)
        players = [e for e in gst.made if e.factory.startswith("playbin")]
        self.assertEqual(len(players), 1)
        return players[0]

    def test_archive_uses_playbin3_and_main_track(self):
        player = self._start({"playbin3", "playbin", "appsink"},
                             playback=("2026-11-25 06:04:12", "2026-11-26 00:00:00"))
        self.assertEqual(player.factory, "playbin3")
        self.assertIn("/Streaming/tracks/301?", player.props["uri"])
        self.assertEqual(player.state, "PLAYING")
        self.assertTrue(self.win.streams[0]["playback"])

    def test_archive_falls_back_to_playbin_without_playbin3(self):
        player = self._start({"playbin", "appsink"},
                             playback=("2026-11-25 06:04:12", "2026-11-26 00:00:00"))
        self.assertEqual(player.factory, "playbin")

    def test_local_file_archive_uses_playbin3(self):
        player = self._start({"playbin3", "playbin", "appsink"}, direct_uri="file:///tmp/x.mkv",
                             stream_override="main")
        self.assertEqual(player.factory, "playbin3")
        self.assertEqual(player.props["uri"], "file:///tmp/x.mkv")

    def test_live_uses_playbin_and_channels(self):
        player = self._start({"playbin3", "playbin", "appsink"})
        self.assertEqual(player.factory, "playbin")
        self.assertIn("/Streaming/Channels/3", player.props["uri"])


if __name__ == "__main__":
    unittest.main()
