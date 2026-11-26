"""Находки проверки 0.6.4 на реальных регистраторах (регистратор за NAT)."""
import io, sys, threading, unittest, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sentinel_protocols as protocols

DEVICE = {"host": "203.0.113.10", "rtsp_port": 41083, "http_port": 48083}
CREDS = {"username": "admin", "password": "p@ss:w"}


class PlaybackUriTests(unittest.TestCase):
    def test_nvr_lan_address_replaced_by_device_endpoint(self):
        uri = ("rtsp://192.168.88.4:554/Streaming/tracks/901/?starttime=20261126T083319Z"
               "&endtime=20261126T093820Z&name=0000000330300000000&size=1073500152")
        result = protocols.playback_uri_for_device(uri, DEVICE, CREDS)
        self.assertEqual(result, "rtsp://admin:p%40ss%3Aw@203.0.113.10:41083/Streaming/tracks/901/"
                                 "?starttime=20261126T083319Z&endtime=20261126T093820Z"
                                 "&name=0000000330300000000&size=1073500152")

    def test_rejects_non_rtsp(self):
        with self.assertRaises(ValueError):
            protocols.playback_uri_for_device("http://x/y", DEVICE, CREDS)


class TransportTests(unittest.TestCase):
    def test_options(self):
        self.assertEqual(protocols.live_rtsp_options("rtsp://h/x"), (("protocols", "tcp"),))
        self.assertEqual(protocols.live_rtsp_options("rtsp://h/x", "auto"), ())
        self.assertEqual(protocols.live_rtsp_options("fd://5", "tcp"), ())
        self.assertEqual(protocols.normalize_rtsp_transport("bogus"), "tcp")


class PtzErrorTests(unittest.TestCase):
    def test_not_supported(self):
        body = (b'<ResponseStatus><statusCode>4</statusCode><statusString>Invalid Operation</statusString>'
                b'<subStatusCode>notSupport</subStatusCode></ResponseStatus>')
        exc = urllib.error.HTTPError("http://x", 403, "Forbidden", {}, io.BytesIO(body))
        self.assertIn("не поддерживает", protocols.ptz_error_text(exc))


class AlertStreamTests(unittest.TestCase):
    def test_event_delivered_without_waiting_for_4kb(self):
        event = b'<EventNotificationAlert version="2.0"><eventType>VMD</eventType></EventNotificationAlert>'

        class Slow:
            """Как HTTP-ответ NVR: read1 отдаёт пришедшее, read(4096) ждал бы ещё данных."""
            def __init__(self):
                self.parts = [b"--boundary\r\n\r\n" + event]

            def read1(self, _n):
                return self.parts.pop(0) if self.parts else b""

            def read(self, _n):
                raise AssertionError("read(4096) ждёт полный блок")

        stop = threading.Event()
        payloads = protocols.iter_alert_payloads(Slow(), stop)
        self.assertEqual(next(payloads), event)


if __name__ == "__main__":
    unittest.main()
