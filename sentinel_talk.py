"""Двусторонняя связь с устройством Hikvision через ISAPI TwoWayAudio.

Микрофон ПК -> GStreamer (8 кГц, моно, G.711 µ-law или A-law) -> длинный
PUT /ISAPI/System/TwoWayAudio/channels/<n>/audioData. Звук с устройства
оператор слышит из обычного видеопотока (кнопка «Звук»).
"""
from __future__ import annotations

import hashlib
import http.client
import os
import re
import socket
import ssl
import threading
import urllib.request
import xml.etree.ElementTree as ET

from sentinel_protocols import authenticated_opener

CODECS = {"G.711ulaw": "mulawenc", "G.711alaw": "alawenc"}


def parse_challenge(header):
    """WWW-Authenticate: Digest realm="…", nonce="…", qop="auth" -> dict."""
    if not header or not header.lower().startswith("digest"):
        return None
    return {key.lower(): quoted or bare
            for key, quoted, bare in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', header[6:])}


def digest_authorization(method, uri, username, password, challenge, cnonce=None, nc=1):
    """Заголовок Authorization по RFC 2617 (MD5, qop=auth или без qop)."""
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    qops = [q.strip() for q in challenge.get("qop", "").split(",") if q.strip()]
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    if "auth" in qops:
        cnonce = cnonce or os.urandom(8).hex()
        nc_text = f"{nc:08x}"
        response = hashlib.md5(f"{ha1}:{nonce}:{nc_text}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        parts += ["qop=auth", f"nc={nc_text}", f'cnonce="{cnonce}"']
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    parts.append(f'response="{response}"')
    parts.append("algorithm=MD5")
    if challenge.get("opaque"):
        parts.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(parts)


def parse_codec(xml_bytes):
    """Тип сжатия из ответа /ISAPI/System/TwoWayAudio/channels/<n>."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return "G.711ulaw"
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "audioCompressionType" and element.text:
            return element.text.strip()
    return "G.711ulaw"


def mic_pipeline_description(codec):
    encoder = CODECS.get(codec, "mulawenc")
    return ("autoaudiosrc ! audioconvert ! audioresample ! "
            "audio/x-raw,format=S16LE,rate=8000,channels=1 ! "
            f"{encoder} ! appsink name=sink emit-signals=true sync=false max-buffers=50 drop=true")


class TalkSession:
    """Один сеанс разговора. start()/stop() вызываются из UI-потока."""

    def __init__(self, device, credentials, gst, on_state=None, channel=1):
        self.device = device
        self.credentials = credentials
        self.gst = gst
        self.on_state = on_state or (lambda _text, _error=False: None)
        self.channel = channel
        self.scheme = "https" if str(device.get("http_port", 80)) == "443" else "http"
        self.base = f"{self.scheme}://{device['host']}:{device.get('http_port', 80)}"
        self.pipeline = None
        self.connection = None
        self.active = False
        self._lock = threading.Lock()

    def _path(self, suffix=""):
        return f"/ISAPI/System/TwoWayAudio/channels/{self.channel}{suffix}"

    def _request(self, path, method="GET"):
        url = self.base + path
        opener = authenticated_opener(url, self.credentials.get("username", ""),
                                      self.credentials.get("password", ""))
        request = urllib.request.Request(url, data=b"" if method == "PUT" else None, method=method)
        with opener.open(request, timeout=5) as response:
            return response.read(65536)

    def start(self):
        self.active = True
        threading.Thread(target=self._run, daemon=True).start()

    def _new_connection(self):
        port = int(self.device.get("http_port", 80) or 80)
        if self.scheme == "https":
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection(self.device["host"], port, timeout=5, context=context)
        return http.client.HTTPConnection(self.device["host"], port, timeout=5)

    def _run(self):
        try:
            try:
                codec = parse_codec(self._request(self._path()))
            except Exception:
                codec = "G.711ulaw"
            try:
                self._request(self._path("/close"), "PUT")  # канал мог остаться открытым
            except Exception:
                pass
            self._request(self._path("/open"), "PUT")
            path = self._path("/audioData")
            probe = self._new_connection()
            probe.request("PUT", path, headers={"Content-Length": "0"})
            reply = probe.getresponse()
            reply.read()
            challenge = parse_challenge(reply.getheader("WWW-Authenticate", ""))
            probe.close()
            connection = self._new_connection()
            connection.putrequest("PUT", path, skip_accept_encoding=True)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", "0")
            connection.putheader("Connection", "keep-alive")
            if challenge:
                connection.putheader("Authorization", digest_authorization(
                    "PUT", path, self.credentials.get("username", ""),
                    self.credentials.get("password", ""), challenge))
            connection.endheaders()
            connection.sock.settimeout(10)
            with self._lock:
                if not self.active:
                    connection.close()
                    return
                self.connection = connection
            self._start_microphone(codec)
            self.on_state(f"Разговор: микрофон подключён ({codec})")
        except Exception as exc:
            self.active = False
            self.on_state(f"Двусторонняя связь недоступна: {exc}", True)
            self._close_channel()

    def _start_microphone(self, codec):
        Gst = self.gst
        pipeline = Gst.parse_launch(mic_pipeline_description(codec))
        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        self.pipeline = pipeline
        pipeline.set_state(Gst.State.PLAYING)

    def _on_sample(self, sink):
        Gst = self.gst
        sample = sink.emit("pull-sample")
        if not sample or not self.active:
            return Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(mapped.data)
        finally:
            buffer.unmap(mapped)
        connection = self.connection
        try:
            if connection is not None and connection.sock is not None:
                connection.sock.sendall(data)
        except (OSError, socket.timeout) as exc:
            self.on_state(f"Разговор прерван: {exc}", True)
            threading.Thread(target=self.stop, daemon=True).start()
        return Gst.FlowReturn.OK

    def stop(self):
        with self._lock:
            if not self.active and self.connection is None and self.pipeline is None:
                return
            self.active = False
            connection, self.connection = self.connection, None
            pipeline, self.pipeline = self.pipeline, None
        if pipeline is not None:
            pipeline.set_state(self.gst.State.NULL)
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        threading.Thread(target=self._close_channel, daemon=True).start()

    def _close_channel(self):
        try:
            self._request(self._path("/close"), "PUT")
        except Exception:
            pass
