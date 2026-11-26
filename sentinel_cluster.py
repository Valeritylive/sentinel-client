"""Горячий резерв Sentinel Server (без GTK): два узла — основной и резервный.

Основной узел работает как обычно. Резервный не принимает клиентов, не
подписывается на события и не пишет видео; он раз в несколько секунд
проверяет основной (сердцебиение) и при изменениях забирает снимок базы
(и файла секретов, если пароли устройств хранятся в файле).

Если основной не отвечает дольше заданного времени (и, если задан, доступен
контрольный узел — так резерв не захватит роль, оказавшись сам отрезанным от
сети), резерв становится активным: запускает службы и принимает клиентов.
Клиенты Sentinel переключаются на него сами.

Когда основной возвращается и видит, что резерв активен, он остаётся
пассивным, пока администратор не выполнит «возврат» (failback): основной
забирает базу у резерва (данные за время аварии не теряются), становится
активным, а резерв возвращается в режим ожидания.

Узлы общаются по HTTPS сервера Sentinel; запросы подписываются общим ключом
(HMAC-SHA256 с меткой времени), ключ хранится в cluster.json (права 0600).
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import io
import json
import os
import secrets
import socket
import sqlite3
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import sentinel_storage as storage

STANDALONE, PRIMARY, STANDBY = "standalone", "primary", "standby"
ROLE_TITLES = {STANDALONE: "Одиночный сервер", PRIMARY: "Основной", STANDBY: "Резервный"}
DEFAULTS = {"role": STANDALONE, "node": socket.gethostname(), "peer_url": "", "key": "", "heartbeat": 5,
            "failover": 30, "sync": 60, "witness": "", "verify_tls": True, "ca_file": ""}
SIGN_WINDOW = 60
TIME_HEADER, SIGN_HEADER, NODE_HEADER = "X-Sentinel-Cluster-Time", "X-Sentinel-Cluster-Sign", "X-Sentinel-Cluster-Node"


class ClusterError(Exception):
    pass


def config_path():
    return storage.DATA / "cluster.json"


def load_config():
    path = config_path()
    config = dict(DEFAULTS)
    if path.exists():
        try:
            config.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise ClusterError(f"Файл настроек кластера повреждён: {path}") from exc
    return config


def validate(config):
    clean = dict(DEFAULTS)
    clean.update({k: v for k, v in config.items() if k in DEFAULTS})
    if clean["role"] not in ROLE_TITLES:
        raise ClusterError("Роль: standalone, primary или standby")
    clean["node"] = str(clean["node"] or socket.gethostname()).strip()[:64]
    for key, low, high in (("heartbeat", 1, 60), ("failover", 5, 3600), ("sync", 5, 3600)):
        clean[key] = int(clean[key])
        if not low <= clean[key] <= high:
            raise ClusterError(f"{key}: от {low} до {high} с")
    if clean["failover"] < clean["heartbeat"] * 3:
        raise ClusterError("Время переключения должно быть не меньше трёх интервалов сердцебиения")
    if clean["role"] != STANDALONE:
        if not str(clean["peer_url"]).startswith(("https://", "http://")):
            raise ClusterError("Укажите адрес второго узла: https://host:8765")
        if len(str(clean["key"])) < 32:
            raise ClusterError("Общий ключ кластера не короче 32 символов (sentinel-server cluster-init создаёт его)")
    clean["peer_url"] = str(clean["peer_url"]).rstrip("/")
    return clean


def save_config(config):
    clean = validate(config)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(clean, handle, ensure_ascii=False, indent=2)
    os.replace(path.with_suffix(".tmp"), path)
    os.chmod(path, 0o600)
    return clean


def new_key():
    return secrets.token_hex(32)


# ------------------------------------------------------------ подпись
def sign(key, method, path, stamp):
    message = f"{method.upper()}\n{path}\n{int(stamp)}".encode()
    return hmac.new(str(key).encode(), message, hashlib.sha256).hexdigest()


def signed_headers(key, method, path, node="", now=None):
    stamp = int(now if now is not None else time.time())
    return {TIME_HEADER: str(stamp), SIGN_HEADER: sign(key, method, path, stamp), NODE_HEADER: node}


def verify(key, method, path, headers, now=None):
    if not key:
        return False
    try:
        stamp = int(headers.get(TIME_HEADER) or 0)
    except ValueError:
        return False
    now = now if now is not None else time.time()
    if abs(now - stamp) > SIGN_WINDOW:
        return False
    return hmac.compare_digest(sign(key, method, path, stamp), str(headers.get(SIGN_HEADER) or ""))


# ------------------------------------------------------------ снимок базы
def db_version():
    """Метка изменения базы (меняется при каждой записи)."""
    try:
        stat = storage.DB_PATH.stat()
    except FileNotFoundError:
        return ""
    secrets_file = storage.DATA / "secrets.json"
    extra = secrets_file.stat().st_mtime_ns if secrets_file.exists() else 0
    return f"{stat.st_mtime_ns}-{stat.st_size}-{extra}"


def make_snapshot():
    """ZIP: sentinel.sqlite3 (согласованная копия через backup API) и secrets.json, если есть. (bytes, sha256)."""
    with storage.database() as source, tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "db.sqlite3"
        target = sqlite3.connect(copy)
        try:
            source.backup(target)
        finally:
            target.close()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(copy, "sentinel.sqlite3")
            secrets_file = storage.DATA / "secrets.json"
            if secrets_file.exists():
                archive.write(secrets_file, "secrets.json")
            archive.writestr("snapshot.json", json.dumps({"created": datetime.now().astimezone().isoformat(),
                                                          "version": db_version()}))
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def restore_snapshot(data, sha256=""):
    """Проверить и подменить базу снимком. Бросает ClusterError при повреждении."""
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise ClusterError("Снимок повреждён при передаче (не совпала контрольная сумма)")
    storage.DATA.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=storage.DATA) as tmp:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
                if "sentinel.sqlite3" not in names:
                    raise ClusterError("В снимке нет базы")
                archive.extract("sentinel.sqlite3", tmp)
                secrets_data = archive.read("secrets.json") if "secrets.json" in names else None
        except zipfile.BadZipFile as exc:
            raise ClusterError("Снимок не является архивом") from exc
        candidate = Path(tmp) / "sentinel.sqlite3"
        check = sqlite3.connect(candidate)
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if result != "ok":
            raise ClusterError(f"База в снимке повреждена: {result}")
        os.chmod(candidate, 0o600)
        os.replace(candidate, storage.DB_PATH)
        if secrets_data is not None:
            path = storage.DATA / "secrets.json"
            fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(secrets_data)
            os.replace(path.with_suffix(".tmp"), path)


def witness_ok(witness, timeout=3):
    """Контрольный узел (host:port) доступен по TCP. Пусто — проверка выключена."""
    if not witness:
        return True
    host, _sep, port = str(witness).rpartition(":")
    try:
        with socket.create_connection((host or witness, int(port or 80)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------ узел
class ClusterNode:
    """Состояние и логика узла. transport(method, path) -> (status, dict|bytes, headers) — запрос ко второму узлу."""

    def __init__(self, config, transport=None, on_activate=None, on_deactivate=None, clock=time.time):
        self.config = validate(config)
        self.transport = transport
        self.on_activate = on_activate or (lambda: None)
        self.on_deactivate = on_deactivate or (lambda: None)
        self.clock = clock
        self.lock = threading.RLock()
        self.active = False
        self.promoted = False          # резерв взял роль основного
        self.yielded = False           # основной уступил активному резерву и ждёт возврата
        self.since = clock()
        self.peer = {}
        self.peer_ok_at = 0.0
        self.peer_error = ""
        self.synced_version = ""
        self.synced_at = 0.0
        self.last_sync_try = 0.0
        self.journal = collections.deque(maxlen=100)
        self._stop = threading.Event()
        self._thread = None

    @property
    def role(self):
        return self.config["role"]

    def log(self, text):
        self.journal.appendleft((datetime.now().astimezone().isoformat(timespec="seconds"), text))

    def _set_active(self, active, reason):
        with self.lock:
            if self.active == active:
                return
            self.active = active
            self.since = self.clock()
            self.log(reason)
        (self.on_activate if active else self.on_deactivate)()

    # ------------------------------------------------------------ запуск
    def start(self, background=True):
        if self.role == STANDALONE:
            self._set_active(True, "Одиночный сервер")
            return
        if self.role == PRIMARY:
            peer = self._ask_peer()
            if peer and peer.get("active") and peer.get("promoted"):
                self.yielded = True
                self.log("Резервный узел работает вместо основного — основной ждёт возврата роли (failback)")
            else:
                self._set_active(True, "Основной узел активен")
        else:
            self.peer_ok_at = self.clock()      # отсчёт до переключения начинается с запуска
            self.log("Резервный узел в режиме ожидания")
        if background:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="sentinel-cluster")
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.config["heartbeat"]):
            try:
                self.tick()
            except Exception as exc:   # узел не должен падать из-за сбоя сети
                self.peer_error = str(exc)

    # ------------------------------------------------------------ обмен
    def _ask_peer(self):
        if self.transport is None:
            return None
        try:
            status, payload, _headers = self.transport("GET", "/cluster/heartbeat")
        except Exception as exc:
            self.peer_error = str(exc) or exc.__class__.__name__
            return None
        if status != 200 or not isinstance(payload, dict):
            self.peer_error = {403: "ключи узлов не совпадают (HTTP 403)",
                               404: "второй сервер ещё не настроен для пары (HTTP 404)"}.get(status, f"HTTP {status}")
            return None
        self.peer, self.peer_ok_at, self.peer_error = payload, self.clock(), ""
        return payload

    def heartbeat(self):
        return {"node": self.config["node"], "role": self.role, "active": self.active, "promoted": self.promoted,
                "yielded": self.yielded, "version": db_version(), "time": self.clock()}

    def tick(self):
        """Один шаг: сердцебиение, синхронизация, переключение."""
        peer = self._ask_peer()
        now = self.clock()
        if self.role == PRIMARY:
            if peer and peer.get("active") and peer.get("promoted") and self.active:
                # сеть восстановилась после разделения: данные теперь у резерва
                self.yielded = True
                self._set_active(False, "Резервный узел активен — основной уступает роль до возврата (failback)")
            return
        if self.role != STANDBY:
            return
        if peer is not None:
            if peer.get("active") and not self.active:
                if peer.get("version") and peer["version"] != self.synced_version and \
                        now - self.last_sync_try >= self.config["sync"]:
                    self.sync()
            return
        if self.active:
            return
        if now - self.peer_ok_at >= self.config["failover"]:
            if not witness_ok(self.config["witness"]):
                if "контрольный узел" not in self.peer_error:
                    self.peer_error += " · контрольный узел недоступен — переключение отложено"
                return
            self.promoted = True
            self._set_active(True, f"Основной узел не отвечает {int(now - self.peer_ok_at)} с — резервный "
                                   "узел принял работу")

    def sync(self):
        self.last_sync_try = self.clock()
        status, data, headers = self.transport("GET", "/cluster/snapshot")
        if status != 200 or not isinstance(data, (bytes, bytearray)):
            raise ClusterError(f"Снимок базы не получен: HTTP {status}")
        restore_snapshot(bytes(data), headers.get("X-Snapshot-SHA256", ""))
        self.synced_version = headers.get("X-Snapshot-Version", "") or self.peer.get("version", "")
        self.synced_at = self.clock()
        self.log(f"База синхронизирована с основным узлом ({len(data) // 1024} КБ)")

    # ------------------------------------------------------------ ручные операции
    def promote(self, reason="Ручное переключение администратором"):
        if self.role != STANDBY:
            raise ClusterError("Взять роль может только резервный узел")
        self.promoted = True
        self._set_active(True, reason)

    def demote(self, reason="Возврат роли основному узлу"):
        if self.role != STANDBY:
            raise ClusterError("Вернуться в ожидание может только резервный узел")
        self.promoted = False
        self._set_active(False, reason)

    def failback(self):
        """На основном узле: забрать базу у активного резерва, перевести его в ожидание и стать активным."""
        if self.role != PRIMARY:
            raise ClusterError("Возврат роли выполняется на основном узле")
        peer = self._ask_peer()
        if peer and peer.get("active"):
            self.sync()
            status, _payload, _headers = self.transport("POST", "/cluster/demote")
            if status != 200:
                raise ClusterError(f"Резервный узел не перешёл в ожидание: HTTP {status}")
        self.yielded = False
        self._set_active(True, "Основной узел снова активен (failback)")

    def status(self):
        now = self.clock()
        return {"role": self.role, "role_title": ROLE_TITLES[self.role], "node": self.config["node"],
                "active": self.active, "promoted": self.promoted, "yielded": self.yielded,
                "since": datetime.fromtimestamp(self.since).astimezone().isoformat(timespec="seconds"),
                "peer_url": self.config["peer_url"], "peer": self.peer,
                "peer_seen": int(now - self.peer_ok_at) if self.peer_ok_at else None,
                "peer_error": self.peer_error, "witness": self.config["witness"],
                "synced_at": datetime.fromtimestamp(self.synced_at).astimezone().isoformat(timespec="seconds")
                if self.synced_at else "", "failover": self.config["failover"],
                "journal": [{"time": t, "text": x} for t, x in list(self.journal)[:30]]}


def http_transport(config):
    """Запросы ко второму узлу по HTTP(S) с подписью."""
    import ssl
    import urllib.error
    import urllib.request
    base = config["peer_url"]
    context = None
    if base.startswith("https://"):
        context = ssl.create_default_context(cafile=config.get("ca_file") or None) if config.get("verify_tls", True) \
            else ssl._create_unverified_context()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         *([urllib.request.HTTPSHandler(context=context)] if context else []))

    def transport(method, path):
        full = "/api/v1" + path
        request = urllib.request.Request(base + full, method=method, data=b"{}" if method == "POST" else None,
                                         headers=signed_headers(config["key"], method, full, config["node"]))
        try:
            with opener.open(request, timeout=max(3, config["heartbeat"] * 2)) as response:
                raw, headers = response.read(), dict(response.headers)
                status = response.status
        except urllib.error.HTTPError as exc:
            return exc.code, {}, {}
        if headers.get("Content-Type", "").startswith("application/json"):
            return status, json.loads(raw or b"{}"), headers
        return status, raw, headers

    return transport
