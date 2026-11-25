"""Protocol helpers shared by the UI and focused tests."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

# The alert stream interleaves XML events with possibly large binary parts
# (snapshots). Never let the scratch buffer grow without bound.
MAX_ALERT_BUFFER = 2 * 1024 * 1024
ALERT_BUFFER_KEEP = 64 * 1024

_allow_insecure_basic = False


def set_allow_insecure_basic(value: bool) -> None:
    """Allow HTTP Basic auth over plain http:// (password sent in clear text)."""
    global _allow_insecure_basic
    _allow_insecure_basic = bool(value)


def allows_insecure_basic() -> bool:
    return _allow_insecure_basic


def parse_channel_ids(payload: bytes, channel_tag: str) -> list[int]:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(payload)
    channel_ids = set()
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != channel_tag:
            continue
        for child in node.iter():
            if child.tag.rsplit("}", 1)[-1] in ("id", "channelID", "inputProxyChannelID"):
                value = (child.text or "").strip()
                if value.isdigit() and 1 <= int(value) <= 512:
                    channel_ids.add(int(value))
                break
    return sorted(channel_ids)


def parse_channel_names(payload: bytes, channel_tag: str) -> dict[int, str]:
    """{номер канала: имя} из ответа InputProxy/VideoInput (пустые имена пропускаются)."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(payload)
    names: dict[int, str] = {}
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != channel_tag:
            continue
        number, name = None, ""
        for child in node.iter():
            tag = child.tag.rsplit("}", 1)[-1]
            text = (child.text or "").strip()
            if number is None and tag in ("id", "channelID", "inputProxyChannelID") and text.isdigit():
                number = int(text)
            elif tag == "name" and not name:
                name = text
        if number is not None and 1 <= number <= 512 and name:
            names[number] = name[:120]
    return names


def parse_device_info(payload: bytes) -> dict[str, str]:
    """deviceName / model / serialNumber из /ISAPI/System/deviceInfo."""
    import xml.etree.ElementTree as ET

    info = {}
    for node in ET.fromstring(payload).iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag in ("deviceName", "model", "serialNumber") and (node.text or "").strip():
            info[tag] = node.text.strip()[:120]
    return info


def build_time_xml(moment) -> str:
    """Тело PUT /ISAPI/System/time: ручная установка времени (aware datetime)."""
    local = moment.astimezone().isoformat(timespec="seconds")
    return ('<Time version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            f"<timeMode>manual</timeMode><localTime>{local}</localTime></Time>")


def parse_streaming_channel_ids(payload: bytes) -> list[int]:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(payload)
    channel_ids = set()
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "StreamingChannel":
            continue
        for child in node.iter():
            if child.tag.rsplit("}", 1)[-1] != "id":
                continue
            value = (child.text or "").strip()
            if not value.isdigit():
                break
            stream_id = int(value)
            channel = stream_id // 100 if stream_id >= 100 else stream_id
            if 1 <= channel <= 512:
                channel_ids.add(channel)
            break
    return sorted(channel_ids)


def authenticated_opener(url: str, username: str, password: str):
    """Opener for device HTTP requests (no env proxies).

    Digest is always offered. Basic only over https://, or over http:// when
    the user explicitly opted in; otherwise a MITM could downgrade the
    challenge to Basic and capture the password.
    """
    password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, url, username, password)
    handlers = [
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPDigestAuthHandler(password_manager),
    ]
    if urllib.parse.urlsplit(url).scheme == "https" or _allow_insecure_basic:
        handlers.append(urllib.request.HTTPBasicAuthHandler(password_manager))
    return urllib.request.build_opener(*handlers)


def iter_alert_payloads(response, stop_event: threading.Event):
    buffer = b""
    closing = b"</EventNotificationAlert>"
    while not stop_event.is_set():
        chunk = response.read(4096)
        if not chunk:
            if stop_event.is_set():
                return
            raise ConnectionError("Устройство закрыло поток тревог")
        buffer += chunk
        while closing in buffer:
            part, buffer = buffer.split(closing, 1)
            start = part.rfind(b"<EventNotificationAlert")
            if start >= 0:
                yield part[start:] + closing
        if len(buffer) > MAX_ALERT_BUFFER:
            buffer = buffer[-ALERT_BUFFER_KEEP:]


def parse_device_time(value: str) -> datetime:
    """Parse an ISO-8601 device timestamp (``Z`` suffix allowed) into local aware time."""
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).astimezone()


def _private_temp_dir() -> str | None:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return runtime if runtime and os.path.isdir(runtime) and os.access(runtime, os.W_OK) else None


def write_rtsp_concat_list(uri: str) -> str:
    """Write an ffconcat list pointing at ``uri`` into a private (0600) temp file.

    ffmpeg only accepts RTSP credentials inside the URL, and anything on the
    command line is readable by every local user through ``ps``. A concat list
    keeps the URL (and so the password) out of argv.
    """
    escaped = uri.replace("'", "'\\''")
    fd, path = tempfile.mkstemp(prefix="sentinel-rtsp-", suffix=".ffconcat", dir=_private_temp_dir())
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"ffconcat version 1.0\nfile '{escaped}'\noption rtsp_transport tcp\n")
    except BaseException:
        os.unlink(path)
        raise
    return path


def ffmpeg_copy_command(ffmpeg: str, list_path: str, output: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-loglevel", "error",
            "-protocol_whitelist", "file,rtsp,tcp,udp,rtp",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy", "-y", output]


class FfmpegJob:
    """``ffmpeg -c copy`` from an RTSP URL, without credentials in argv.

    A watcher thread always reaps the process (no zombies), removes the
    credentials list file, escalates to SIGKILL if ffmpeg ignores SIGINT, and
    reports unexpected failures through ``on_error(returncode, stderr_text)``.
    """

    STOP_GRACE_SECONDS = 10

    def __init__(self, ffmpeg: str, uri: str, output: str, on_error=None, command=None):
        """``command(list_path) -> argv`` заменяет стандартную команду копирования
        (например, сегментированная резервная запись); URL всё равно остаётся вне argv."""
        self._on_error = on_error
        self._stopping = False
        self._list_path = write_rtsp_concat_list(uri)
        try:
            argv = command(self._list_path) if command else ffmpeg_copy_command(ffmpeg, self._list_path, str(output))
            self.process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, start_new_session=True)
        except BaseException:
            os.unlink(self._list_path)
            raise
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def poll(self):
        return self.process.poll()

    def stop(self) -> None:
        """Ask ffmpeg to finish the file cleanly (SIGINT)."""
        self._stopping = True
        if self.process.poll() is None:
            try:
                self.process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    def wait(self, timeout=None) -> None:
        self._watcher.join(timeout)

    def _watch(self) -> None:
        deadline = None
        stderr = b""
        try:
            while True:
                try:
                    _out, stderr = self.process.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if self._stopping:
                        deadline = deadline or time.monotonic() + self.STOP_GRACE_SECONDS
                        if time.monotonic() > deadline:
                            self.process.kill()
        finally:
            try:
                os.unlink(self._list_path)
            except FileNotFoundError:
                pass
        code = self.process.returncode
        if code != 0 and not self._stopping and self._on_error:
            self._on_error(code, stderr.decode("utf-8", "replace").strip()[-400:])
