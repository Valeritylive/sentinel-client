#!/usr/bin/env python3
"""Sentinel Server — серверная часть Sentinel (аналог службы HikCentral Lite / iVMS-4200 Service).

Работает без графики (systemd-служба или консоль), хранит общий каталог устройств,
пользователей, правила тревог и аккаунты Hik-Connect, постоянно держит подписки на
события устройств и раздаёт всё клиентам через REST API и поток событий (SSE).

Запуск::

    sentinel-server create-admin              # первый запуск: учётная запись admin
    sentinel-server run --host 0.0.0.0 --port 8765 --tls-cert cert.pem --tls-key key.pem
    sentinel-server hikconnect-login --account user@example.com --region ru --remember

Только стандартная библиотека Python 3.10+.
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import sqlite3
import ssl
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import sentinel_alarms as alarms  # noqa: E402
import sentinel_engine as engine_mod  # noqa: E402
import sentinel_hikconnect as hikconnect  # noqa: E402
import sentinel_arming as arming  # noqa: E402
import sentinel_cluster as cluster  # noqa: E402
import sentinel_security as security  # noqa: E402
import sentinel_shared_views as shared_views  # noqa: E402
import sentinel_storage as storage  # noqa: E402
from sentinel_protocols import parse_channel_ids, parse_device_info, parse_streaming_channel_ids  # noqa: E402

try:
    VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
except OSError:
    VERSION = "dev"

API = "/api/v1"
DEFAULT_PORT = 8765
TOKEN_TTL = 12 * 3600
MAX_BODY = 1024 * 1024
SSE_KEEPALIVE = 15
log = logging.getLogger("sentinel.server")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = int(status)
        self.message = message


# ==================================================================== сеансы
class TokenStore:
    """Токены доступа клиентов (в памяти: перезапуск сервера = повторный вход)."""

    def __init__(self, ttl=TOKEN_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[security.Session, float]] = {}

    def issue(self, session):
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            self._tokens[token] = (session, time.time() + self.ttl)
        return token, self.ttl

    def get(self, token):
        if not token:
            return None
        with self._lock:
            item = self._tokens.get(token)
            if not item:
                return None
            session, expires = item
            if expires < time.time():
                self._tokens.pop(token, None)
                return None
            self._tokens[token] = (session, time.time() + self.ttl)   # скользящий срок
            return session

    def revoke(self, token):
        with self._lock:
            self._tokens.pop(token, None)

    def revoke_user(self, username):
        with self._lock:
            for token in [t for t, (s, _e) in self._tokens.items() if s.username == username]:
                self._tokens.pop(token, None)

    def count(self):
        with self._lock:
            self._purge()
            return len(self._tokens)

    def _purge(self):
        now = time.time()
        for token in [t for t, (_s, e) in self._tokens.items() if e < now]:
            self._tokens.pop(token, None)


# ==================================================================== приложение
class SentinelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, engine, tokens=None, tls_context=None):
        super().__init__(address, RequestHandler)
        self.engine = engine
        self.cluster = None        # ClusterNode при работе в паре основной/резервный
        self.new_engine = None     # фабрика Engine для смены роли на ходу
        self.cluster_transport = None   # подмена транспорта между узлами (тесты)
        self.tokens = tokens or TokenStore()
        self.stopping = threading.Event()
        if tls_context is not None:
            # рукопожатие — в потоке обработчика, чтобы медленный клиент не блокировал приём соединений
            self.socket = tls_context.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)

    def shutdown(self):
        self.stopping.set()
        super().shutdown()


ROUTES = []
PASSIVE_ALLOWED = (API + "/health", API + "/auth/", API + "/cluster/")


def route(method, pattern, permission=None, auth=True):
    regex = re.compile("^" + API + pattern + "$")

    def decorator(func):
        ROUTES.append((method, regex, permission, auth, func))
        return func
    return decorator


def _json_default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return str(value)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = f"SentinelServer/{VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = 60   # простаивающие соединения не держат потоки вечно (SSE пишет keepalive чаще)

    def setup(self):
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(15)
            try:
                self.request.do_handshake()
            except (ssl.SSLError, OSError):
                self.request.close()
                raise
        super().setup()

    # ------------------------------------------------------------ служебное
    def log_message(self, fmt, *args):  # без query-строк (в них может быть токен SSE)
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Слишком большой запрос")
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Тело запроса должно быть JSON") from exc
        if not isinstance(value, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Ожидается JSON-объект")
        return value

    def _token(self, query):
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return (query.get("token") or [""])[0]   # EventSource не умеет заголовки

    def _dispatch(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        self.query = query
        self.session = None
        try:
            node = self.server.cluster
            if node is not None and not node.active and not parsed.path.startswith(PASSIVE_ALLOWED):
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                    "error": "Это резервный узел кластера Sentinel, сейчас работает другой узел",
                    "standby": True, "active_url": node.config["peer_url"]})
                return
            for route_method, regex, permission, auth, func in ROUTES:
                match = regex.match(parsed.path)
                if not match or route_method != method:
                    continue
                if auth:
                    self.session = self.server.tokens.get(self._token(query))
                    if self.session is None:
                        raise ApiError(HTTPStatus.UNAUTHORIZED, "Требуется вход")
                    if permission and not self.session.can(permission):
                        raise ApiError(HTTPStatus.FORBIDDEN, "Недостаточно прав")
                body = self._body() if method in ("POST", "PUT", "PATCH") else {}
                result = func(self, body, *match.groups())
                if result is not None:
                    status, payload = result if isinstance(result, tuple) else (HTTPStatus.OK, result)
                    self._send_json(status, payload)
                return
            if any(regex.match(parsed.path) for _m, regex, *_rest in ROUTES):
                raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "Метод не поддерживается")
            raise ApiError(HTTPStatus.NOT_FOUND, "Не найдено")
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except hikconnect.WrongCredentials as exc:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": str(exc), "code": exc.code})
        except hikconnect.HikConnectError as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc), "code": exc.code})
        except (ValueError, KeyError, TypeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc) or "Некорректный запрос"})
        except sqlite3.IntegrityError:
            self._send_json(HTTPStatus.CONFLICT, {"error": "Запись уже существует"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("Ошибка обработки %s %s", method, parsed.path)
            try:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Внутренняя ошибка сервера"})
            except OSError:
                pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # ------------------------------------------------------------ помощники
    @property
    def engine(self) -> engine_mod.Engine:
        return self.server.engine

    def q(self, name, default=None):
        values = self.query.get(name)
        return values[0] if values else default

    def audit(self, action, target="", details=""):
        alarms.log_operation(self.session.username if self.session else "", action, str(target), details)


def _session_payload(session):
    return {"username": session.username, "role": session.role, "permissions": sorted(session.permissions),
            "password_expires_in": session.password_expires_in}


# ==================================================================== маршруты: система и вход
@route("GET", "/health", auth=False)
def health(h, _body):
    node = h.server.cluster
    return {"status": "ok", "version": VERSION, "server": "sentinel", "setup_required": not security.has_users(),
            "active": node is None or node.active,
            "cluster": None if node is None else {"role": node.role, "node": node.config["node"],
                                                  "active": node.active, "peer_url": node.config["peer_url"]}}


# ------------------------------------------------------------ кластер (горячий резерв)
def _cluster_peer(h):
    node = h.server.cluster
    if node is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "Кластер не настроен")
    path = urllib.parse.urlsplit(h.path).path
    if not cluster.verify(node.config["key"], h.command, path, h.headers):
        raise ApiError(HTTPStatus.FORBIDDEN, "Неверная подпись узла кластера")
    return node


@route("GET", "/cluster/heartbeat", auth=False)
def cluster_heartbeat(h, _body):
    return _cluster_peer(h).heartbeat()


@route("GET", "/cluster/snapshot", auth=False)
def cluster_snapshot(h, _body):
    node = _cluster_peer(h)
    if not node.active:
        raise ApiError(HTTPStatus.CONFLICT, "Снимок отдаёт только активный узел")
    version = cluster.db_version()
    data, digest = cluster.make_snapshot()
    h.send_response(HTTPStatus.OK)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("X-Snapshot-SHA256", digest)
    h.send_header("X-Snapshot-Version", version)
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(data)
    return None


@route("POST", "/cluster/demote", auth=False)
def cluster_demote(h, _body):
    node = _cluster_peer(h)
    node.demote("Основной узел забрал роль обратно (failback)")
    return {"ok": True}


def _cluster_node(h):
    if h.server.cluster is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "Сервер работает без резерва (одиночный)")
    return h.server.cluster


@route("GET", "/cluster/status", "system")
def cluster_status(h, _body):
    node = h.server.cluster
    return node.status() if node else {"role": cluster.STANDALONE, "role_title": cluster.ROLE_TITLES[cluster.STANDALONE],
                                       "active": True}


@route("POST", "/cluster/promote", "system")
def cluster_promote(h, _body):
    try:
        _cluster_node(h).promote(f"Ручное переключение: {h.session.username}")
    except cluster.ClusterError as exc:
        raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
    h.audit("Кластер: резервный узел принял работу")
    return {"ok": True}


@route("POST", "/cluster/failback", "system")
def cluster_failback(h, _body):
    try:
        _cluster_node(h).failback()
    except cluster.ClusterError as exc:
        raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
    h.audit("Кластер: роль возвращена основному узлу")
    return {"ok": True}


@route("GET", "/cluster/config", "system")
def cluster_config_get(h, _body):
    config = h.server.cluster.config if h.server.cluster else cluster.load_config()
    return {**config, "hostname": socket.gethostname()}


@route("PUT", "/cluster/config", "system")
def cluster_config_put(h, body):
    """Сменить роль и параметры узла из интерфейса — без перезапуска службы."""
    current = h.server.cluster.config if h.server.cluster else cluster.load_config()
    config = dict(current)
    config.update({k: v for k, v in (body or {}).items() if k in cluster.DEFAULTS})
    if config["role"] != cluster.STANDALONE and not str(config.get("key") or "").strip():
        config["key"] = cluster.new_key()
    try:
        cluster.validate(config)
    except cluster.ClusterError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    h.audit("Кластер: изменены настройки узла", config["role"], config.get("peer_url") or "")
    node = reconfigure_cluster(h.server, config)
    return {"config": {**(node.config if node else cluster.load_config())},
            "status": node.status() if node else {"role": cluster.STANDALONE, "active": True}}


@route("POST", "/auth/login", auth=False)
def login(h, body):
    if not security.has_users():
        raise ApiError(HTTPStatus.CONFLICT, "Сервер не активирован: выполните «sentinel-server create-admin»")
    try:
        session = security.authenticate(body.get("username", ""), body.get("password", ""), security.load_policy())
    except security.LoginError as exc:
        time.sleep(0.5)   # замедляем перебор
        raise ApiError(HTTPStatus.UNAUTHORIZED, str(exc)) from exc
    token, ttl = h.server.tokens.issue(session)
    alarms.log_operation(session.username, "Вход в сервер", h.client_address[0])
    return {"token": token, "expires_in": ttl, "user": _session_payload(session), "version": VERSION}


@route("POST", "/auth/logout")
def logout(h, _body):
    h.server.tokens.revoke(h._token(h.query))
    return {"ok": True}


@route("GET", "/auth/me")
def me(h, _body):
    return _session_payload(h.session)


@route("GET", "/system")
def system_info(h, _body):
    node = h.server.cluster
    cluster_info = None if node is None else {"role": node.role, "node": node.config["node"], "active": node.active,
                                              "promoted": node.promoted, "peer_ok": not node.peer_error}
    return {"version": VERSION, "time": engine_mod.now_iso(), "sessions": h.server.tokens.count(),
            "cluster": cluster_info, **h.engine.summary()}


@route("GET", "/log", "system")
def operation_log(h, _body):
    return {"items": alarms.load_operation_log(int(h.q("limit", 200)))}


# ==================================================================== устройства
def _area_names():
    return {g["id"]: g["name"] for g in storage.load_groups()}


def _device_out(device, areas):
    item = engine_mod.public_device(device)
    item["area"] = areas.get(device.get("group_id"), "")
    return item


@route("GET", "/devices", "live")
def list_devices(h, _body):
    areas = _area_names()
    return {"items": [_device_out(d, areas) for d in h.engine.devices()], "areas": sorted(areas.values())}


def _discover_channels(device, credentials):
    for path, parser in (("/ISAPI/ContentMgmt/InputProxy/channels", lambda p: parse_channel_ids(p, "InputProxyChannel")),
                         ("/ISAPI/System/Video/inputs/channels", lambda p: parse_channel_ids(p, "VideoInputChannel")),
                         ("/ISAPI/Streaming/channels", parse_streaming_channel_ids)):
        try:
            channels = parser(engine_mod.isapi_request(device, credentials, path))
        except Exception as exc:
            if "401" in str(exc):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Устройство отклонило логин или пароль") from exc
            continue
        if channels:
            return channels
    raise ApiError(HTTPStatus.BAD_GATEWAY, "Не удалось получить список каналов по ISAPI")


@route("POST", "/devices", "devices")
def add_device(h, body):
    host = str(body.get("host", "")).strip()
    if not host:
        raise ValueError("Укажите адрес устройства")
    device = {"name": str(body.get("name") or host), "host": host, "protocol": body.get("protocol", "hikvision"),
              "http_port": int(body.get("http_port", 80)), "rtsp_port": int(body.get("rtsp_port", 554)),
              "sdk_port": int(body.get("sdk_port", 8000)), "stream": body.get("stream", "main"),
              "category": body.get("category", "video"), "group_name": str(body.get("area", "")).strip()}
    if device["protocol"] not in ("hikvision", "rtsp"):
        raise ValueError("protocol: hikvision или rtsp")
    credentials = {"username": str(body.get("username", "")), "password": str(body.get("password", "")),
                   "rtsp_url": str(body.get("rtsp_url", ""))}
    channels = [int(c) for c in body.get("channels") or []]
    if device["protocol"] == "hikvision" and credentials["username"]:
        if not channels:
            channels = _discover_channels(device, credentials)
        try:
            info = parse_device_info(engine_mod.isapi_request(device, credentials, "/ISAPI/System/deviceInfo"))
            device.update(device_name=info.get("deviceName", ""), device_model=info.get("model", ""),
                          serial_no=info.get("serialNumber", ""), firmware=info.get("firmwareVersion", ""))
        except Exception:
            pass
        device["status"] = "online"
    channels = channels or [1]
    ids = hikconnect.store_device(device, credentials if credentials["username"] else None, channels)
    h.engine.forget_credentials(device)
    h.engine.sync_event_subscriptions()
    h.audit("Добавлено устройство", host, f"каналов: {len(ids)}")
    return HTTPStatus.CREATED, {"ids": ids, "channels": channels}


@route("PATCH", r"/devices/(\d+)", "devices")
def update_device(h, body, device_id):
    device = next((d for d in h.engine.devices() if d["id"] == int(device_id)), None)
    if not device:
        raise ApiError(HTTPStatus.NOT_FOUND, "Устройство не найдено")
    with storage.database() as db:
        if "name" in body:
            db.execute("UPDATE devices SET name=? WHERE id=?", (str(body["name"]).strip() or device["name"],
                                                                 device["id"]))
        if "area" in body:
            area = str(body["area"]).strip()
            group_id = storage.add_group(area) if area else None
            db.execute("UPDATE devices SET group_id=? WHERE host=? AND http_port=?",
                       (group_id, device["host"], device["http_port"]))
        if "stream" in body and body["stream"] in ("main", "sub", "third"):
            db.execute("UPDATE devices SET stream=? WHERE id=?", (body["stream"], device["id"]))
    if body.get("username"):
        storage.save_credentials(device, {"username": body["username"], "password": body.get("password", ""),
                                          "rtsp_url": body.get("rtsp_url", "")})
        h.engine.forget_credentials(device)
        h.engine.sync_event_subscriptions()
    h.audit("Изменено устройство", device["host"])
    return {"ok": True}


@route("DELETE", r"/devices/(\d+)", "devices")
def delete_device(h, _body, device_id):
    device = next((d for d in h.engine.devices() if d["id"] == int(device_id)), None)
    if not device:
        raise ApiError(HTTPStatus.NOT_FOUND, "Устройство не найдено")
    with storage.database() as db:
        db.execute("DELETE FROM devices WHERE id=?", (device["id"],))
        left = db.execute("SELECT COUNT(*) FROM devices WHERE host=? AND http_port=?",
                          (device["host"], device["http_port"])).fetchone()[0]
    if not left:
        try:
            storage.delete_saved_credentials(device)
        except storage.CredentialStoreError:
            pass
        h.engine.forget_credentials(device)
    h.engine.sync_event_subscriptions()
    h.audit("Удалено устройство", device["host"], device["name"])
    return {"ok": True}


@route("POST", "/devices/check", "devices")
def check_devices(h, _body):
    return {"items": h.engine.check_devices()}


@route("GET", r"/devices/(\d+)/stream", "live")
def stream_url(h, _body, device_id):
    device = next((d for d in h.engine.devices() if d["id"] == int(device_id)), None)
    if not device:
        raise ApiError(HTTPStatus.NOT_FOUND, "Устройство не найдено")
    credentials = h.engine.credentials_for(device)
    if not credentials:
        raise ApiError(HTTPStatus.CONFLICT, "На сервере не сохранён пароль устройства")
    url = engine_mod.rtsp_url(device, credentials, h.q("stream"))
    h.audit("Выдан адрес потока", device["host"], f"канал {device['channel']}")
    return {"url": url, "device": engine_mod.public_device(device)}


@route("POST", r"/devices/(\d+)/alarm-output", "alarm_ack")
def alarm_output(h, body, device_id):
    h.engine.trigger_alarm_output(int(device_id), int(body.get("output", 1)), int(body.get("auto_close", 0)))
    return {"ok": True}


# ==================================================================== события и тревоги
@route("GET", "/events", "live")
def list_events(h, _body):
    return {"items": alarms.load_events(int(h.q("limit", 200)), h.q("category"), h.q("since"), h.q("until"),
                                        h.q("text", ""))}


@route("POST", "/events", "alarms")
def user_event(h, body):
    event = {"time": engine_mod.now_iso(), "device_id": body.get("device_id"),
             "device_name": str(body.get("source", "Пользовательское событие")), "channel": "",
             "raw_type": "user", "event_type": "user", "category": "event", "state": "active", "plate": "",
             "details": {"text": str(body.get("text", ""))[:500], "by": h.session.username}}
    return HTTPStatus.CREATED, {"alarms": h.engine.handle_event(event)}


@route("GET", "/alarms", "alarm_ack")
def list_alarms(h, _body):
    return {"items": alarms.load_alarm_records(int(h.q("limit", 200)), h.q("status"), h.q("priority"),
                                               h.q("since")),
            "unacknowledged": alarms.unacknowledged_count()}


@route("POST", "/alarms/ack", "alarm_ack")
def ack_alarms(h, body):
    ids = [int(i) for i in body.get("ids") or []]
    if not ids:
        raise ValueError("Укажите ids тревог")
    alarms.acknowledge(ids, h.session.username, body.get("remarks"))
    h.engine.bus.publish("alarm_ack", {"ids": ids, "by": h.session.username})
    return {"ok": True}


@route("GET", "/stream")
def event_stream(h, _body):
    """Server-Sent Events: event, alarm, alarm_ack, device_status, cloud_sync, notice."""
    try:
        last_id = int(h.headers.get("Last-Event-ID") or h.q("last_id", 0) or 0)
    except ValueError:
        last_id = 0
    subscriber = h.engine.bus.subscribe(last_id)
    h.send_response(HTTPStatus.OK)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Connection", "close")
    h.end_headers()
    h.close_connection = True
    try:
        h.wfile.write(b"retry: 3000\n\n")
        h.wfile.flush()
        while not h.server.stopping.is_set():
            try:
                message = subscriber.get(timeout=SSE_KEEPALIVE)
            except Exception:
                h.wfile.write(b": keepalive\n\n")
                h.wfile.flush()
                continue
            data = json.dumps(message, ensure_ascii=False, default=_json_default)
            h.wfile.write(f"id: {message['id']}\nevent: {message['type']}\ndata: {data}\n\n".encode("utf-8"))
            h.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        h.engine.bus.unsubscribe(subscriber)
    return None


# ==================================================================== охрана
@route("GET", "/arming", "live")
def arming_status(h, _body):
    states = arming.load_states()
    devices = h.engine.devices()
    return {"states": [{"scope": s, "target": t, **v} for (s, t), v in states.items()],
            "devices": {str(d["id"]): arming.effective(d, states) for d in devices},
            "summary": dict(zip(("armed", "total"), arming.summary(devices, states)))}


@route("POST", "/arming", "alarm_ack")
def set_arming(h, body):
    scope = body.get("scope", arming.DEVICE)
    armed = bool(body.get("armed", True))
    targets = body.get("targets")
    if scope == arming.DEVICE and isinstance(targets, list):
        arming.set_devices_armed([int(t) for t in targets], armed, h.session.username)
    else:
        arming.set_armed(scope, int(body.get("target") or 0), armed, h.session.username)
    h.audit("Поставлено на охрану" if armed else "Снято с охраны", f"{scope}:{targets or body.get('target', 0)}")
    h.engine.bus.publish("arming", {"scope": scope, "armed": armed})
    return {"ok": True}


# ==================================================================== виды (приватные и публичные)
def _view_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except shared_views.ViewAccessError as exc:
        raise ApiError(HTTPStatus.FORBIDDEN, str(exc)) from exc
    except KeyError as exc:
        raise ApiError(HTTPStatus.NOT_FOUND, "Вид не найден") from exc


@route("GET", "/views", "live")
def list_views(h, _body):
    return {"items": shared_views.list_views(h.session),
            "can_publish": h.session.can(shared_views.PUBLIC_PERMISSION)}


@route("POST", "/views", "live")
def create_view(h, body):
    view_id = _view_call(shared_views.create_view, h.session, body.get("name", ""), bool(body.get("public")),
                         body.get("layout"))
    h.audit("Сохранён вид", body.get("name", ""), "публичный" if body.get("public") else "приватный")
    h.engine.bus.publish("views", {"id": view_id})
    return HTTPStatus.CREATED, {"id": view_id}


@route("PUT", r"/views/(\d+)", "live")
def update_view(h, body, view_id):
    item = _view_call(shared_views.update_view, h.session, int(view_id), body.get("name"),
                      body.get("public"), body.get("layout"))
    h.audit("Изменён вид", item["name"])
    h.engine.bus.publish("views", {"id": item["id"]})
    return {"ok": True, "item": item}


@route("DELETE", r"/views/(\d+)", "live")
def delete_view(h, _body, view_id):
    item = _view_call(shared_views.delete_view, h.session, int(view_id))
    h.audit("Удалён вид", item["name"])
    h.engine.bus.publish("views", {"id": item["id"]})
    return {"ok": True}


# ==================================================================== пользователи
@route("GET", "/users", "users")
def list_users(h, _body):
    users = security.load_users()
    for user in users:
        user.pop("password_hash", None)
    return {"items": users, "roles": security.load_roles()}


@route("POST", "/users", "users")
def create_user(h, body):
    roles = {r["name"]: r["id"] for r in security.load_roles()}
    role_id = body.get("role_id") or roles.get(body.get("role", "Оператор"))
    if role_id is None:
        raise ValueError("Неизвестная роль")
    user_id = security.create_user(body.get("username", ""), body.get("password", ""), int(role_id),
                                   body.get("email", ""), body.get("expires", ""), security.load_policy())
    h.audit("Создан пользователь", body.get("username", ""))
    return HTTPStatus.CREATED, {"id": user_id}


@route("DELETE", r"/users/(\d+)", "users")
def delete_user(h, _body, user_id):
    user = next((u for u in security.load_users() if u["id"] == int(user_id)), None)
    if not user:
        raise ApiError(HTTPStatus.NOT_FOUND, "Пользователь не найден")
    if user["username"] == h.session.username:
        raise ValueError("Нельзя удалить свою учётную запись")
    security.delete_user(int(user_id))
    h.server.tokens.revoke_user(user["username"])
    h.audit("Удалён пользователь", user["username"])
    return {"ok": True}


@route("POST", "/auth/password")
def change_own_password(h, body):
    security.change_password(h.session.username, body.get("old_password", ""), body.get("new_password", ""),
                             security.load_policy())
    h.audit("Смена пароля", h.session.username)
    return {"ok": True}


# ==================================================================== Hik-Connect
def _account_out(account, engine):
    item = {k: account[k] for k in ("id", "account", "api_url", "remember_password", "auto_sync", "last_sync",
                                    "last_error")}
    item["remember_password"] = bool(item["remember_password"])
    item["auto_sync"] = bool(item["auto_sync"])
    item["devices"] = len(hikconnect.load_cloud_devices(account["id"]))
    return item


def _region_url(body):
    region = body.get("region", "auto")
    if body.get("api_url"):
        return hikconnect.normalize_api_url(body["api_url"])
    for key, _title, url in hikconnect.REGIONS:
        if key == region and url:
            return url
    return hikconnect.DEFAULT_API


@route("GET", "/hikconnect/regions")
def hikconnect_regions(h, _body):
    return {"items": [{"key": k, "title": t, "api_url": u} for k, t, u in hikconnect.REGIONS]}


@route("GET", "/hikconnect/accounts", "devices")
def hikconnect_accounts(h, _body):
    return {"items": [_account_out(a, h.engine) for a in hikconnect.load_accounts()]}


@route("POST", "/hikconnect/accounts", "devices")
def hikconnect_login(h, body):
    account_id, devices = hikconnect.login_account(
        str(body.get("account", "")), str(body.get("password", "")), _region_url(body),
        bool(body.get("remember_password")), h.engine.hikconnect_factory)
    hikconnect.apply_cloud_status()
    h.audit("Вход в Hik-Connect", body.get("account", ""), f"устройств: {len(devices)}")
    h.engine.bus.publish("cloud_sync", {"accounts": {account_id: len(devices)}})
    return HTTPStatus.CREATED, {"account": _account_out(hikconnect.get_account(account_id), h.engine),
                                "devices": hikconnect.load_cloud_devices(account_id)}


@route("PATCH", r"/hikconnect/accounts/(\d+)", "devices")
def hikconnect_update(h, body, account_id):
    account = hikconnect.get_account(int(account_id))
    if not account:
        raise ApiError(HTTPStatus.NOT_FOUND, "Аккаунт не найден")
    if "auto_sync" in body:
        with storage.database() as db:
            db.execute("UPDATE cloud_accounts SET auto_sync=? WHERE id=?", (int(bool(body["auto_sync"])),
                                                                          account["id"]))
    return {"account": _account_out(hikconnect.get_account(account["id"]), h.engine)}


@route("DELETE", r"/hikconnect/accounts/(\d+)", "devices")
def hikconnect_logout(h, _body, account_id):
    account = hikconnect.get_account(int(account_id))
    if not account:
        raise ApiError(HTTPStatus.NOT_FOUND, "Аккаунт не найден")
    hikconnect.delete_account(account["id"])
    h.audit("Выход из Hik-Connect", account["account"])
    return {"ok": True}


@route("POST", r"/hikconnect/accounts/(\d+)/sync", "devices")
def hikconnect_sync(h, body, account_id):
    result = h.engine.sync_cloud_accounts(int(account_id), body.get("password") or None)
    return {"accounts": result, "devices": hikconnect.load_cloud_devices(int(account_id))}


@route("GET", "/hikconnect/devices", "live")
def hikconnect_devices(h, _body):
    account_id = h.q("account_id")
    return {"items": hikconnect.load_cloud_devices(int(account_id) if account_id else None)}


@route("POST", "/hikconnect/import", "devices")
def hikconnect_import(h, body):
    serials = [str(s) for s in body.get("serials") or []]
    if not serials:
        raise ValueError("Выберите устройства (serials)")
    credentials = None
    if body.get("username"):
        credentials = {"username": str(body["username"]), "password": str(body.get("password", "")),
                       "rtsp_url": ""}
    address = body.get("address", "auto")
    if address not in ("auto", "local", "wan"):
        raise ValueError("address: auto, local или wan")
    added, errors = hikconnect.import_devices(serials, credentials, address, str(body.get("area", "Hik-Connect")),
                                              account_id=body.get("account_id"))
    h.engine.forget_credentials()
    h.engine.sync_event_subscriptions()
    h.audit("Импорт из Hik-Connect", ", ".join(serials), f"добавлено: {added}")
    return {"added": added, "errors": errors}


# ==================================================================== запуск
def tls_context(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


def _is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


def attach_cluster(server, config, new_engine, transport=None, background=True):
    """Узел кластера: службы (engine) работают только на активном узле."""
    def activate():
        if server.engine._threads:
            return
        server.engine = new_engine()
        server.engine.start()
        server.engine.bus.publish("notice", {"message": f"Узел «{node.config['node']}» стал активным"})
        log.warning("Кластер: узел активен, службы запущены")

    def deactivate():
        old = server.engine
        server.engine = new_engine()       # не запущенный: только для сводки
        old.bus.publish("notice", {"message": "Узел переходит в режим ожидания", "error": True})
        old.stop()
        server.tokens = TokenStore()       # клиенты перейдут на активный узел
        log.warning("Кластер: узел в режиме ожидания, службы остановлены")

    server.new_engine = new_engine
    node = cluster.ClusterNode(config, transport or cluster.http_transport(cluster.validate(config)), activate,
                               deactivate)
    server.cluster = node
    node.start(background)
    log.info("Кластер: роль %s, узел %s, второй узел %s", node.role, node.config["node"], node.config["peer_url"])
    return node


def reconfigure_cluster(server, config):
    """Сохранить настройки кластера и сразу перейти в новую роль."""
    config = cluster.save_config(config)
    factory = server.new_engine or (lambda: engine_mod.Engine())
    if server.cluster is not None:
        server.cluster.stop()
        server.cluster = None
    if config["role"] == cluster.STANDALONE:
        if not server.engine._threads:
            server.engine = factory()
            server.engine.start()
        log.warning("Кластер: узел работает без резерва")
        return None
    node = attach_cluster(server, config, factory, transport=server.cluster_transport)
    if not node.active and server.engine._threads:
        old = server.engine                 # узел в ожидании: службы остановить, клиенты уйдут на активный
        server.engine = factory()
        old.stop()
        server.tokens = TokenStore()
    return node


def make_server(host="127.0.0.1", port=DEFAULT_PORT, engine=None, tls=None):
    engine = engine or engine_mod.Engine()
    return SentinelServer((host, port), engine, tls_context=tls)


def cmd_run(args):
    if bool(args.tls_cert) != bool(args.tls_key):
        raise SystemExit("Укажите и --tls-cert, и --tls-key")
    tls = tls_context(args.tls_cert, args.tls_key) if args.tls_cert else None
    if tls is None and not _is_loopback(args.host):
        if not args.allow_plain_http:
            raise SystemExit("Сервер слушает сеть без TLS: пароли и адреса потоков пойдут открытым текстом. "
                             "Укажите --tls-cert/--tls-key или явно --allow-plain-http.")
        log.warning("TLS выключен на сетевом интерфейсе %s — используйте только в доверенной сети", args.host)
    if not security.has_users():
        log.warning("Пользователи не созданы: вход в сервер невозможен до «sentinel-server create-admin»")
    def new_engine():
        return engine_mod.Engine(receive_events=not args.no_events, recording_enabled=not args.no_recording,
                                 status_interval=args.status_interval, cloud_interval=args.cloud_interval)

    engine = new_engine()
    server = make_server(args.host, args.port, engine, tls)
    server.new_engine = new_engine
    config = cluster.load_config()
    if config["role"] == cluster.STANDALONE:
        engine.start()
    else:
        server.cluster = attach_cluster(server, config, new_engine)

    def stop(*_args):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    scheme = "https" if tls else "http"
    log.info("Sentinel Server %s: %s://%s:%s%s (данные: %s, секреты: %s)", VERSION, scheme, args.host, args.port,
             API, storage.DATA, storage.credential_backend())
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        if server.cluster is not None:
            server.cluster.stop()
        server.engine.stop()
        server.server_close()
        log.info("Остановлен")


def _read_new_password(prompt="Пароль: "):
    password = os.environ.get("SENTINEL_ADMIN_PASSWORD")
    if password:
        return password
    first = getpass.getpass(prompt)
    if first != getpass.getpass("Повторите пароль: "):
        raise SystemExit("Пароли не совпадают")
    return first


def cmd_create_admin(args):
    security.ensure_builtin_roles()
    if args.username == "admin" and not security.has_users():
        security.ensure_admin(_read_new_password())
    else:
        roles = {r["name"]: r["id"] for r in security.load_roles()}
        security.create_user(args.username, _read_new_password(), roles["Администратор"],
                             policy=security.load_policy())
    print(f"Пользователь {args.username} создан (роль «Администратор»)")


def cmd_reset_password(args):
    """Задать пароль пользователю (по умолчанию admin); нет такого — создать с ролью «Администратор».

    Снимает блокировку после неверных попыток и включает учётную запись. Нужен, когда пароль
    забыт или сервер ещё не активирован; окно настроек сервера вызывает эту же команду.
    """
    security.ensure_builtin_roles()
    username = (args.username or "admin").strip()
    password = _read_new_password("Новый пароль: ")
    user = next((u for u in security.load_users() if u["username"] == username), None)
    if user is None:
        if username == "admin" and not security.has_users():
            security.ensure_admin(password)
        else:
            roles = {r["name"]: r["id"] for r in security.load_roles()}
            security.create_user(username, password, roles["Администратор"], policy=security.load_policy())
        print(f"Пользователь {username} создан (роль «Администратор»)")
        return
    security.reset_password(user["id"], password, username, security.load_policy())
    security.update_user(user["id"], enabled=True)
    print(f"Пароль пользователя {username} изменён")


def cmd_list_users(args):
    users = [{"username": u["username"], "role": u["role_name"] or "", "enabled": bool(u["enabled"]),
              "locked_until": u["locked_until"] or ""} for u in security.load_users()]
    if args.json:
        print(json.dumps({"users": users}, ensure_ascii=False))
        return
    for u in users:
        flags = "" if u["enabled"] else " (отключён)"
        print(f"{u['username']:<24} {u['role']}{flags}")


def cmd_hikconnect_login(args):
    password = os.environ.get("HIKCONNECT_PASSWORD") or getpass.getpass("Пароль Hik-Connect: ")
    url = _region_url({"region": args.region, "api_url": args.api_url})
    account_id, devices = hikconnect.login_account(args.account, password, url, args.remember)
    print(f"Аккаунт #{account_id}: устройств {len(devices)}")
    for d in devices:
        state = "в сети" if d["online"] else "не в сети"
        print(f"  {d['serial']:<12} {d['name']:<28} {d['model']:<20} {state:<10} {d['local_ip'] or d['wan_ip']}"
              f"  каналов: {len(d['cameras']) or 1}")


def cmd_hikconnect_devices(_args):
    for d in hikconnect.load_cloud_devices():
        flag = "✓" if d["imported"] else " "
        print(f"{flag} {d['account']:<24} {d['serial']:<12} {d['name']:<28} {'в сети' if d['online'] else '—':<7}"
              f" {d['local_ip'] or d['wan_ip']}")


COMMANDS = ("run", "create-admin", "reset-password", "list-users", "hikconnect-login", "hikconnect-devices", "cluster-init", "cluster-set", "cluster-status")


def cmd_cluster_init(args):
    config = cluster.load_config()
    config.update(role=args.role, peer_url=args.peer or "", node=args.node or config["node"],
                  witness=args.witness or "", failover=args.failover, heartbeat=args.heartbeat,
                  verify_tls=not args.no_verify_tls, ca_file=args.ca_file or "")
    if args.role != cluster.STANDALONE:
        config["key"] = args.key or config.get("key") or cluster.new_key()
    try:
        saved = cluster.save_config(config)
    except cluster.ClusterError as exc:
        raise SystemExit(f"Ошибка: {exc}") from exc
    print(f"Кластер: {cluster.ROLE_TITLES[saved['role']]} узел «{saved['node']}» → {saved['peer_url'] or '—'}")
    if saved["role"] != cluster.STANDALONE and not args.key:
        print("Общий ключ (укажите его на втором узле через --key):")
        print(saved["key"])
    print("Перезапустите сервер, чтобы применить.")


def cmd_cluster_set(_args):
    """Настройки кластера JSON-объектом со stdin (так их передаёт окно настроек сервера через помощник)."""
    try:
        values = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit("Ожидается JSON на stdin") from exc
    config = cluster.load_config()
    config.update({k: v for k, v in values.items() if k in cluster.DEFAULTS})
    if config["role"] != cluster.STANDALONE and not str(config.get("key") or "").strip():
        config["key"] = cluster.new_key()
    try:
        saved = cluster.save_config(config)
    except cluster.ClusterError as exc:
        raise SystemExit(f"Ошибка: {exc}") from exc
    print(json.dumps(saved, ensure_ascii=False))


def cmd_cluster_status(args):
    config = cluster.load_config()
    if getattr(args, "json", False):
        print(json.dumps(config, ensure_ascii=False))
        return
    print(f"Роль: {cluster.ROLE_TITLES[config['role']]} · узел {config['node']} · второй узел "
          f"{config['peer_url'] or '—'} · переключение через {config['failover']} с · контрольный узел "
          f"{config['witness'] or 'нет'}")


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS,
                        help="каталог базы сервера (иначе SENTINEL_DATA_DIR или каталог клиента)")
    common.add_argument("--credential-store", choices=("secret-service", "file"), default=argparse.SUPPRESS,
                        help="где хранить пароли устройств; file — для службы без D-Bus (файл 0600)")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(prog="sentinel-server", description="Серверная часть Sentinel",
                                     parents=[common])
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", parents=[common], help="запустить сервер (по умолчанию)")
    run.add_argument("--host", default=os.environ.get("SENTINEL_HOST", "127.0.0.1"))
    run.add_argument("--port", type=int, default=int(os.environ.get("SENTINEL_PORT", DEFAULT_PORT)))
    run.add_argument("--tls-cert", default=os.environ.get("SENTINEL_TLS_CERT"))
    run.add_argument("--tls-key", default=os.environ.get("SENTINEL_TLS_KEY"))
    run.add_argument("--allow-plain-http", action="store_true",
                     default=os.environ.get("SENTINEL_ALLOW_PLAIN_HTTP") == "1")
    run.add_argument("--no-events", action="store_true", help="не подписываться на события устройств")
    run.add_argument("--no-recording", action="store_true", help="не вести резервную запись на сервере")
    run.add_argument("--status-interval", type=int, default=300)
    run.add_argument("--cloud-interval", type=int, default=600)
    admin = sub.add_parser("create-admin", parents=[common],
                           help="создать администратора (пароль из SENTINEL_ADMIN_PASSWORD или ввод)")
    admin.add_argument("--username", default="admin")
    reset = sub.add_parser("reset-password", parents=[common],
                           help="задать или сбросить пароль (по умолчанию admin), снять блокировку")
    reset.add_argument("--username", default="admin")
    users = sub.add_parser("list-users", parents=[common], help="показать пользователей сервера")
    users.add_argument("--json", action="store_true")
    hc_login = sub.add_parser("hikconnect-login", parents=[common],
                              help="войти в аккаунт Hik-Connect и загрузить устройства")
    hc_login.add_argument("--account", required=True)
    hc_login.add_argument("--region", default="auto", choices=[k for k, _t, _u in hikconnect.REGIONS])
    hc_login.add_argument("--api-url")
    hc_login.add_argument("--remember", action="store_true", help="хранить пароль для автопродления")
    sub.add_parser("hikconnect-devices", parents=[common], help="показать устройства аккаунтов Hik-Connect")
    init = sub.add_parser("cluster-init", parents=[common], help="настроить горячий резерв (основной/резервный)")
    init.add_argument("--role", required=True, choices=(cluster.STANDALONE, cluster.PRIMARY, cluster.STANDBY))
    init.add_argument("--peer", help="адрес второго узла, например https://10.0.0.2:8765")
    init.add_argument("--node", help="имя этого узла")
    init.add_argument("--key", help="общий ключ (на втором узле — ключ, выданный первым)")
    init.add_argument("--witness", help="контрольный узел host:port (например, шлюз 10.0.0.1:80)")
    init.add_argument("--failover", type=int, default=30, help="через сколько секунд без ответа переключаться")
    init.add_argument("--heartbeat", type=int, default=5)
    init.add_argument("--no-verify-tls", action="store_true", help="не проверять сертификат второго узла")
    init.add_argument("--ca-file", help="сертификат CA второго узла")
    status = sub.add_parser("cluster-status", parents=[common], help="показать настройки кластера")
    status.add_argument("--json", action="store_true", help="вывести настройки (с ключом) в JSON")
    sub.add_parser("cluster-set", parents=[common], help="задать настройки кластера JSON-объектом со stdin")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a in COMMANDS for a in argv) and not any(a in ("-h", "--help") for a in argv):
        argv.insert(0, "run")
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if getattr(args, "credential_store", None):
        os.environ[storage.CREDENTIAL_STORE_ENV] = args.credential_store
    if getattr(args, "data_dir", None):
        storage.DATA = Path(args.data_dir).expanduser()
        storage.DB_PATH = storage.DATA / "devices.sqlite3"
    handlers = {"run": cmd_run, "create-admin": cmd_create_admin,
                "reset-password": cmd_reset_password, "list-users": cmd_list_users,
                "hikconnect-login": cmd_hikconnect_login, "hikconnect-devices": cmd_hikconnect_devices,
                "cluster-init": cmd_cluster_init, "cluster-set": cmd_cluster_set,
                "cluster-status": cmd_cluster_status}
    try:
        handlers[args.command](args)
    except (ValueError, hikconnect.HikConnectError, storage.CredentialStoreError) as exc:
        raise SystemExit(f"Ошибка: {exc}") from exc


if __name__ == "__main__":
    main()
