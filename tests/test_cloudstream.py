import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import sentinel_cloudstream as cs


def rtp(payload, pt=96, marker=False, csrc=0, extension=b"", padding=0):
    first = 0x80 | (0x20 if padding else 0) | (0x10 if extension else 0) | csrc
    header = bytes([first, (0x80 if marker else 0) | pt]) + b"\x00\x01" + b"\x00" * 8 + b"\x00\x00\x00\x07" * csrc
    if extension:
        header += b"\xab\xcd" + (len(extension) // 4).to_bytes(2, "big") + extension
    tail = (b"\x00" * (padding - 1) + bytes([padding])) if padding else b""
    return header + payload + tail


class RtpTests(unittest.TestCase):
    def test_payload_with_csrc_extension_and_padding(self):
        pt, payload = cs.rtp_payload(rtp(b"\x40\x01abc", csrc=1, extension=b"\x00" * 8, padding=3))
        self.assertEqual((pt, payload), (96, b"\x40\x01abc"))
        self.assertEqual(cs.rtp_payload(b"\x00" * 20), (-1, b""))

    def test_codec_guess(self):
        self.assertEqual(cs.guess_codec(b"\x40\x01"), cs.H265)     # VPS
        self.assertEqual(cs.guess_codec(b"\x62\x01\x93"), cs.H265)  # FU
        self.assertEqual(cs.guess_codec(b"\x67\x42"), cs.H264)      # SPS
        self.assertEqual(cs.guess_codec(b"\x7c\x85"), cs.H264)      # FU-A
        self.assertIsNone(cs.guess_codec(b"\xff"))

    def test_hevc_fragments_and_aggregation(self):
        dep = cs.RtpDepacketizer()
        out = dep.feed(rtp(b"\x40\x01VPS"))
        out += dep.feed(rtp(b"\x70\x01META", pt=112))                     # метаданные Hikvision
        out += dep.feed(rtp(b"\x62\x01" + bytes([0x80 | 19]) + b"part1"))  # FU start, IDR_W_RADL
        out += dep.feed(rtp(b"\x62\x01" + bytes([19]) + b"part2"))
        out += dep.feed(rtp(b"\x62\x01" + bytes([0x40 | 19]) + b"part3", marker=True))
        out += dep.feed(rtp(b"\x60\x01" + b"\x00\x03\x42\x01S" + b"\x00\x03\x44\x01P"))
        sc = b"\x00\x00\x00\x01"
        self.assertEqual(dep.codec, cs.H265)
        self.assertEqual(out, sc + b"\x40\x01VPS" + sc + b"\x26\x01part1part2part3" + sc + b"\x42\x01S" + sc + b"\x44\x01P")

    def test_h264_fua_and_stap(self):
        dep = cs.RtpDepacketizer()
        sc = b"\x00\x00\x00\x01"
        out = dep.feed(rtp(b"\x67SPS"))
        out += dep.feed(rtp(b"\x7c\x85IDR1"))
        out += dep.feed(rtp(b"\x7c\x45IDR2"))
        out += dep.feed(rtp(b"\x18" + b"\x00\x02\x68P" + b"\x00\x02\x06S"))
        self.assertEqual(dep.codec, cs.H264)
        self.assertEqual(out, sc + b"\x67SPS" + sc + b"\x65IDR1IDR2" + sc + b"\x68P" + sc + b"\x06S")


class PsTests(unittest.TestCase):
    def test_mpeg_ps_in_rtp_is_passed_through(self):
        dep = cs.RtpDepacketizer()
        ps = b"\x00\x00\x01\xba" + b"\x44" * 10
        self.assertEqual(dep.feed(rtp(ps)), ps)
        self.assertEqual(dep.codec, cs.MPEG_PS)
        self.assertEqual(dep.feed(rtp(b"\x00\x00\x01\xe0rest")), b"\x00\x00\x01\xe0rest")
        self.assertEqual(dep.feed(rtp(b"meta", pt=112)), b"")


class RequestTests(unittest.TestCase):
    def test_build_request_and_stream_kind(self):
        device = {"cloud_serial": "FH2381693", "channel": 3, "stream": "sub"}
        request = cs.build_request(device, {"api_url": "https://apiisgp.hik-connect.com", "session_id": "s",
                                            "feature_code": "f"})
        self.assertEqual((request["serial"], request["channel"], request["stream"], request["feature_code"]),
                         ("FH2381693", 3, cs.STREAM_SUB, "f"))
        self.assertEqual(cs.stream_kind({"stream": "main"}), cs.STREAM_MAIN)
        with self.assertRaises(cs.CloudStreamError) as ctx:
            cs.build_request(device, {})
        self.assertEqual(ctx.exception.code, "session")
        with self.assertRaises(ValueError):
            cs.build_request({"channel": 1}, {"session_id": "s"})

    def test_parse_error(self):
        err = cs.parse_error("noise\n" + cs.ERROR_PREFIX + '{"code": "encrypted"}\n')
        self.assertEqual(err.code, "encrypted")
        self.assertIn("зашифровано", str(err))
        self.assertIsNone(cs.parse_error("plain failure"))

    def test_runtime_python_env(self):
        with patch.dict(os.environ, {cs.RUNTIME_ENV: sys.executable}):
            self.assertEqual(cs.runtime_python(), sys.executable)
        with patch.dict(os.environ, {cs.RUNTIME_ENV: "/nonexistent/python"}):
            self.assertIsNone(cs.runtime_python())
        with patch.dict(os.environ, {cs.RUNTIME_ENV: ""}), tempfile.TemporaryDirectory() as temp, \
                patch.object(cs, "runtime_dir", lambda: Path(temp)):
            self.assertIsNone(cs.runtime_python())
            with self.assertRaises(cs.CloudStreamError):
                cs.CloudStreamProcess({})


class ProcessTests(unittest.TestCase):
    def test_process_stream_and_error(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "helper.py"
            script.write_text(textwrap.dedent(f"""
                import json, sys
                request = json.loads(sys.stdin.read())
                sys.stdout.buffer.write(request["serial"].encode()); sys.stdout.flush()
                sys.stderr.write({cs.ERROR_PREFIX!r} + json.dumps({{"code": "not_found"}}) + "\\n")
            """))
            proc = cs.CloudStreamProcess({"serial": "ABC"}, python=sys.executable, script=str(script))
            self.assertTrue(proc.uri.startswith("fd://"))
            self.assertEqual(proc.process.stdout.read(), b"ABC")
            proc.process.wait(timeout=10)
            self.assertEqual(proc.error().code, "not_found")
            proc.stop()

    def test_stop_kills_running_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "helper.py"
            script.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(60)\n")
            proc = cs.CloudStreamProcess({}, python=sys.executable, script=str(script))
            proc.stop()
            self.assertIsNotNone(proc.process.poll())


if __name__ == "__main__":
    unittest.main()
