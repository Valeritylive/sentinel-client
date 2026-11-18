import os
import threading
import unittest
import urllib.request
from unittest.mock import patch

from sentinel_protocols import (
    authenticated_opener,
    iter_alert_payloads,
    parse_streaming_channel_ids,
)


class FakeResponse:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.read_calls = 0

    def read(self, _size):
        self.read_calls += 1
        return self.chunks.pop(0) if self.chunks else b""


class ProtocolTests(unittest.TestCase):
    def test_streaming_channel_ids_collapse_main_and_substreams(self):
        payload = b"""<StreamingChannelList>
        <StreamingChannel><id>101</id></StreamingChannel>
        <StreamingChannel><id>102</id></StreamingChannel>
        <StreamingChannel><id>201</id></StreamingChannel>
        </StreamingChannelList>"""

        self.assertEqual(parse_streaming_channel_ids(payload), [1, 2])

    def test_device_http_opener_does_not_use_environment_proxies(self):
        env = {
            "http_proxy": "http://127.0.0.1:1",
            "https_proxy": "http://127.0.0.1:1",
            "no_proxy": "",
            "NO_PROXY": "",
        }
        with patch.dict(os.environ, env):
            default_opener = urllib.request.build_opener()
            device_opener = authenticated_opener("http://nvr.example.test", "operator", "secret")

        self.assertTrue(any(isinstance(handler, urllib.request.ProxyHandler)
                            for handler in default_opener.handlers))
        self.assertFalse(any(isinstance(handler, urllib.request.ProxyHandler)
                             for handler in device_opener.handlers))

    def test_alert_stream_yields_complete_xml_events_across_chunks(self):
        response = FakeResponse([
            b"<EventNotificationAlert><eventType>V",
            b"MD</eventType></EventNotificationAlert>",
        ])
        payload = next(iter_alert_payloads(response, threading.Event()))

        self.assertIn(b"<eventType>VMD</eventType>", payload)

    def test_unexpected_alert_stream_eof_raises_without_busy_loop(self):
        response = FakeResponse([])

        with self.assertRaises(ConnectionError):
            list(iter_alert_payloads(response, threading.Event()))

        self.assertEqual(response.read_calls, 1)


if __name__ == "__main__":
    unittest.main()
