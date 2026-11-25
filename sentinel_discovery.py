"""Поиск устройств Hikvision в локальной сети по протоколу SADP (без GTK).

Клиент отправляет XML-запрос Probe на мультикаст 239.255.255.250:37020,
устройства отвечают ProbeMatch с адресом, портами, моделью, серийным номером,
версией прошивки и признаком активации — это вкладка «Автопоиск» в окне
добавления устройства.
"""
from __future__ import annotations

import socket
import time
import uuid
import xml.etree.ElementTree as ET

SADP_GROUP = "239.255.255.250"
SADP_PORT = 37020


def probe_message(probe_uuid=None):
    probe_uuid = (probe_uuid or str(uuid.uuid4())).upper()
    return (f'<?xml version="1.0" encoding="utf-8"?><Probe><Uuid>{probe_uuid}</Uuid>'
            f"<Types>inquiry</Types></Probe>").encode("utf-8")


def _text(root, name):
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == name:
            return (node.text or "").strip()
    return ""


def parse_probe_match(payload):
    """Словарь устройства из ответа ProbeMatch или None."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    if root.tag.rsplit("}", 1)[-1] != "ProbeMatch":
        return None
    address = _text(root, "IPv4Address")
    if not address:
        return None

    def number(name, default):
        value = _text(root, name)
        return int(value) if value.isdigit() else default

    activated = _text(root, "Activated").lower()
    return {
        "host": address,
        "model": _text(root, "DeviceDescription") or _text(root, "DeviceType"),
        "serial_no": _text(root, "DeviceSN"),
        "mac": _text(root, "MAC"),
        "sdk_port": number("CommandPort", 8000),
        "http_port": number("HttpPort", 80),
        "firmware": _text(root, "SoftwareVersion"),
        "activated": activated != "false",
        "dhcp": _text(root, "DHCP").lower() == "true",
        "channels": number("AnalogChannelNum", 0) + number("DigitalChannelNum", 0),
        "subnet": _text(root, "IPv4SubnetMask"),
        "gateway": _text(root, "IPv4Gateway"),
    }


def discover(timeout=3.0, interface_address="0.0.0.0", sock_factory=None):
    """Разослать Probe и собрать ответы за timeout секунд. Список устройств без дублей."""
    factory = sock_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP))
    sock = factory()
    found = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        try:
            sock.bind((interface_address, SADP_PORT))
            membership = socket.inet_aton(SADP_GROUP) + socket.inet_aton(interface_address)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        except OSError:
            sock.bind((interface_address, 0))  # порт занят (например, SADP Tool) — слушаем ответы на свой порт
        sock.settimeout(0.3)
        message = probe_message()
        deadline = time.monotonic() + timeout
        sent_again = False
        sock.sendto(message, (SADP_GROUP, SADP_PORT))
        while time.monotonic() < deadline:
            if not sent_again and time.monotonic() > deadline - timeout / 2:
                sock.sendto(message, (SADP_GROUP, SADP_PORT))  # второй запрос: UDP теряется
                sent_again = True
            try:
                payload, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            device = parse_probe_match(payload)
            if device:
                found[device["serial_no"] or device["host"]] = device
    finally:
        sock.close()
    return sorted(found.values(), key=lambda d: tuple(int(p) if p.isdigit() else 0 for p in d["host"].split(".")))
