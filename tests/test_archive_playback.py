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
    def connect(self, signal, *a):
        self.props.setdefault("_signals", {})[signal] = a
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

    @staticmethod
    def util_set_object_arg(element, name, value):
        element.props[name] = value

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


class FakeRtspSrc(FakeElement):
    def __init__(self):
        super().__init__("rtspsrc")

    def find_property(self, name):
        return object() if name in ("onvif-mode", "protocols") else None


class ArchiveTimeTests(StartPreviewTests):
    def test_archive_rtsp_source_uses_onvif_clock_range(self):
        gst = FakeGst({"playbin3", "playbin", "appsink"})
        with patch.object(app_module, "Gst", gst):
            self.win._start_preview(0, dict(DEVICE), dict(CREDS),
                                    playback=("2026-11-26 09:00:13", "2026-11-26 09:10:13"))
            player = [e for e in gst.made if e.factory == "playbin3"][0]
            handler, uri = player.props["_signals"]["source-setup"]
            source = FakeRtspSrc()
            handler(player, source, uri)
        self.assertEqual(source.props, {"onvif-mode": "true", "protocols": "tcp"})

    def test_live_rtsp_uses_tcp_by_default(self):
        live = self._start({"playbin3", "playbin", "appsink"})
        handler, options = live.props["_signals"]["source-setup"]
        source = FakeRtspSrc()
        with patch.object(app_module, "Gst", FakeGst(set())):
            handler(live, source, options)
        self.assertEqual(source.props, {"protocols": "tcp"})

    def test_live_auto_transport_has_no_source_hook(self):
        storage.set_setting(protocols.RTSP_TRANSPORT_SETTING, "auto")
        live = self._start({"playbin3", "playbin", "appsink"})
        self.assertNotIn("source-setup", live.props.get("_signals", {}))

    def test_live_udp_transport(self):
        storage.set_setting(protocols.RTSP_TRANSPORT_SETTING, "udp")
        live = self._start({"playbin3", "playbin", "appsink"})
        handler, options = live.props["_signals"]["source-setup"]
        source = FakeRtspSrc()
        with patch.object(app_module, "Gst", FakeGst(set())):
            handler(live, source, options)
        self.assertEqual(source.props, {"protocols": "udp+udp-mcast"})

    def test_local_file_has_no_source_hook(self):
        local = self._start({"playbin3", "playbin", "appsink"}, direct_uri="file:///tmp/x.mkv",
                            stream_override="main")
        self.assertNotIn("source-setup", local.props.get("_signals", {}))

    def test_archive_url_uses_nvr_wall_clock(self):
        import sentinel_archive as archive
        from datetime import timedelta, timezone
        plus4 = timezone(timedelta(hours=4))
        self.win.device_clocks = {(DEVICE["host"], DEVICE["http_port"]): archive.DeviceClock(plus4)}
        from datetime import datetime
        start = datetime(2026, 11, 26, 9, 0, 13, tzinfo=plus4).astimezone()
        end = start + timedelta(minutes=10)
        player = self._start({"playbin3", "playbin", "appsink"},
                             playback=(start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")))
        self.assertIn("starttime=20261126T090013Z", player.props["uri"])
        self.assertIn("endtime=20261126T091013Z", player.props["uri"])
