import os
import threading
import unittest
import urllib.request
from unittest.mock import patch

import sentinel_protocols as protocols
from sentinel_protocols import (
    authenticated_opener,
    iter_alert_payloads,
    parse_channel_ids,
    parse_streaming_channel_ids,
)


class FakeResponse:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.read_calls = 0

    def read(self, _size):
        self.read_calls += 1
        return self.chunks.pop(0) if self.chunks else b""


def handler_types(opener):
    return {type(handler) for handler in opener.handlers}


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(protocols.set_allow_insecure_basic, protocols.allows_insecure_basic())
        protocols.set_allow_insecure_basic(False)

    def test_streaming_channel_ids_collapse_main_and_substreams(self):
        payload = b"""<StreamingChannelList>
        <StreamingChannel><id>101</id></StreamingChannel>
        <StreamingChannel><id>102</id></StreamingChannel>
        <StreamingChannel><id>201</id></StreamingChannel>
        </StreamingChannelList>"""
        self.assertEqual(parse_streaming_channel_ids(payload), [1, 2])

    def test_parse_channel_ids_reads_input_proxy_channels(self):
        payload = b"""<InputProxyChannelList xmlns="http://www.hikvision.com/ver20/XMLSchema">
        <InputProxyChannel><id>3</id></InputProxyChannel>
        <InputProxyChannel><id>1</id></InputProxyChannel>
        <InputProxyChannel><id>1</id></InputProxyChannel>
        </InputProxyChannelList>"""
        self.assertEqual(parse_channel_ids(payload, "InputProxyChannel"), [1, 3])

    def test_parse_channel_ids_ignores_invalid_and_out_of_range_ids(self):
        payload = b"""<VideoInputChannelList>
        <VideoInputChannel><id>0</id></VideoInputChannel>
        <VideoInputChannel><id>513</id></VideoInputChannel>
        <VideoInputChannel><id>abc</id></VideoInputChannel>
        <VideoInputChannel><id>512</id></VideoInputChannel>
        </VideoInputChannelList>"""
        self.assertEqual(parse_channel_ids(payload, "VideoInputChannel"), [512])

    def test_device_http_opener_does_not_use_environment_proxies(self):
        env = {"http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1",
               "no_proxy": "", "NO_PROXY": ""}
        with patch.dict(os.environ, env):
            default_opener = urllib.request.build_opener()
            device_opener = authenticated_opener("http://nvr.example.test", "operator", "secret")
        self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) for h in default_opener.handlers))
        self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) for h in device_opener.handlers))

    def test_basic_auth_is_not_offered_over_plain_http_by_default(self):
        opener = authenticated_opener("http://nvr.example.test", "operator", "secret")
        self.assertIn(urllib.request.HTTPDigestAuthHandler, handler_types(opener))
        self.assertNotIn(urllib.request.HTTPBasicAuthHandler, handler_types(opener))

    def test_basic_auth_is_offered_over_https(self):
        opener = authenticated_opener("https://nvr.example.test", "operator", "secret")
        self.assertIn(urllib.request.HTTPBasicAuthHandler, handler_types(opener))

    def test_basic_auth_over_http_requires_explicit_opt_in(self):
        protocols.set_allow_insecure_basic(True)
        opener = authenticated_opener("http://nvr.example.test", "operator", "secret")
        self.assertIn(urllib.request.HTTPBasicAuthHandler, handler_types(opener))

    def test_alert_stream_yields_complete_xml_events_across_chunks(self):
        response = FakeResponse([
            b"<EventNotificationAlert><eventType>V",
            b"MD</eventType></EventNotificationAlert>",
        ])
        payload = next(iter_alert_payloads(response, threading.Event()))
        self.assertIn(b"<eventType>VMD</eventType>", payload)

    def test_alert_stream_skips_binary_parts_between_events(self):
        response = FakeResponse([
            b"--boundary\r\n<EventNotificationAlert><eventType>A</eventType></EventNotificationAlert>",
            b"\r\n--boundary\r\n\x00\xff\x00binary-picture",
            b"\r\n<EventNotificationAlert><eventType>B</eventType></EventNotificationAlert>",
        ])
        payloads = []
        with self.assertRaises(ConnectionError):
            for payload in iter_alert_payloads(response, threading.Event()):
                payloads.append(payload)
        self.assertIn(b"<eventType>A</eventType>", payloads[0])
        self.assertIn(b"<eventType>B</eventType>", payloads[1])

    def test_alert_stream_buffer_is_bounded_and_recovers(self):
        junk = b"x" * 4096
        chunks = [junk] * ((protocols.MAX_ALERT_BUFFER // 4096) * 3)
        chunks.append(b"<EventNotificationAlert><eventType>OK</eventType></EventNotificationAlert>")
        payload = next(iter_alert_payloads(FakeResponse(chunks), threading.Event()))
        self.assertIn(b"<eventType>OK</eventType>", payload)
        self.assertLessEqual(len(payload), protocols.ALERT_BUFFER_KEEP + 100)

    def test_unexpected_alert_stream_eof_raises_without_busy_loop(self):
        response = FakeResponse([])
        with self.assertRaises(ConnectionError):
            list(iter_alert_payloads(response, threading.Event()))
        self.assertEqual(response.read_calls, 1)


if __name__ == "__main__":
    unittest.main()
