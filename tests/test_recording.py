import os
import stat
import sys
import tempfile
import time
import unittest
from datetime import timezone
from pathlib import Path

import sentinel_protocols as protocols
from sentinel_protocols import FfmpegJob, parse_device_time

URI = "rtsp://operator:p%40ss%27word@192.0.2.10:554/Streaming/Channels/101"


def fake_ffmpeg(directory: Path, body: str) -> str:
    path = directory / "ffmpeg"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


class ConcatListTests(unittest.TestCase):
    def test_list_is_private_and_holds_the_url(self):
        path = protocols.write_rtsp_concat_list(URI)
        self.addCleanup(os.unlink, path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        text = Path(path).read_text()
        self.assertIn(f"file '{URI}'", text)
        self.assertTrue(text.startswith("ffconcat version 1.0"))

    def test_single_quote_in_url_is_escaped(self):
        path = protocols.write_rtsp_concat_list("rtsp://u:a'b@h/x")
        self.addCleanup(os.unlink, path)
        self.assertIn("file 'rtsp://u:a'\\''b@h/x'", Path(path).read_text())

    def test_command_line_has_no_credentials(self):
        command = protocols.ffmpeg_copy_command("/usr/bin/ffmpeg", "/tmp/list", "/tmp/out.mkv")
        self.assertNotIn("p%40ss", " ".join(command))
        self.assertNotIn("rtsp://", " ".join(command).replace("file,rtsp,tcp", ""))


class FfmpegJobTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = Path(self.dir.name)

    def test_argv_never_contains_password_and_list_is_removed(self):
        argv_file = self.work / "argv"
        ffmpeg = fake_ffmpeg(self.work, f'printf "%s\\n" "$@" > {argv_file}\nexit 0\n')
        job = FfmpegJob(ffmpeg, URI, str(self.work / "out.mkv"))
        job.wait(10)
        argv = argv_file.read_text()
        self.assertNotIn("p%40ss", argv)
        self.assertNotIn("operator", argv)
        list_arg = argv.splitlines()[argv.splitlines().index("-i") + 1]
        self.assertFalse(os.path.exists(list_arg))
        self.assertEqual(job.poll(), 0)

    def test_process_is_reaped_after_stop(self):
        ffmpeg = fake_ffmpeg(self.work, "trap 'exit 255' INT\nwhile true; do sleep 0.1; done\n")
        errors = []
        job = FfmpegJob(ffmpeg, URI, str(self.work / "out.mkv"), on_error=lambda *a: errors.append(a))
        time.sleep(0.3)
        self.assertIsNone(job.poll())
        job.stop()
        job.wait(10)
        self.assertIsNotNone(job.poll())  # waited on -> not a zombie
        self.assertEqual(errors, [])  # requested stop is not an error

    def test_unexpected_failure_is_reported_with_stderr(self):
        ffmpeg = fake_ffmpeg(self.work, "echo 'Connection refused' >&2\nexit 1\n")
        errors = []
        job = FfmpegJob(ffmpeg, URI, str(self.work / "out.mkv"), on_error=lambda *a: errors.append(a))
        job.wait(10)
        self.assertEqual(errors, [(1, "Connection refused")])

    def test_ffmpeg_ignoring_sigint_is_killed(self):
        ffmpeg = fake_ffmpeg(self.work, "trap '' INT\nwhile true; do sleep 0.1; done\n")
        job = FfmpegJob(ffmpeg, URI, str(self.work / "out.mkv"))
        job.STOP_GRACE_SECONDS = 1
        time.sleep(0.3)
        job.stop()
        job.wait(10)
        self.assertIsNotNone(job.poll())

    def test_missing_binary_cleans_up_list_file(self):
        before = set(Path(protocols._private_temp_dir() or tempfile.gettempdir()).glob("sentinel-rtsp-*"))
        with self.assertRaises(OSError):
            FfmpegJob("/nonexistent/ffmpeg", URI, str(self.work / "out.mkv"))
        after = set(Path(protocols._private_temp_dir() or tempfile.gettempdir()).glob("sentinel-rtsp-*"))
        self.assertEqual(before, after)


class ParseDeviceTimeTests(unittest.TestCase):
    def test_zulu_suffix_is_accepted(self):
        parsed = parse_device_time("2026-11-24T10:00:00Z")
        self.assertEqual(parsed.astimezone(timezone.utc).hour, 10)

    def test_offset_is_converted_to_the_same_instant(self):
        a = parse_device_time("2026-11-24T14:00:00+04:00")
        b = parse_device_time("2026-11-24T10:00:00Z")
        self.assertEqual(a, b)

    def test_naive_time_is_treated_as_local(self):
        self.assertIsNotNone(parse_device_time("2026-11-24T10:00:00").tzinfo)


if __name__ == "__main__":
    unittest.main()
