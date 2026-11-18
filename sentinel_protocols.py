"""Protocol helpers shared by the UI and focused tests."""
from __future__ import annotations

import threading
import urllib.request


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
    password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, url, username, password)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPDigestAuthHandler(password_manager),
        urllib.request.HTTPBasicAuthHandler(password_manager),
    )


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
