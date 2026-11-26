"""Управление службой Sentinel PRO Server с рабочего стола (без GTK): состояние, настройки, пароль.

Общая часть окна настроек (sentinel_server_settings.py) и значка в трее (sentinel_server_tray.py).

* состояние — ``systemctl show`` плюс проверка ``/api/v1/health`` (отвечает ли сервер,
  создан ли администратор);
* запуск/остановка — ``systemctl`` (правило polkit из пакета пускает администраторов
  компьютера без пароля, иначе GNOME спросит пароль);
* настройки, пароль admin, сертификат — через ``pkexec sentinel-server-helper`` (root),
  потому что /etc/default/sentinel-server и база в /var/lib/sentinel-server принадлежат root
  и пользователю sentinel.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

SERVICE = "sentinel-server.service"
CONFIG_PATH = Path(os.environ.get("SENTINEL_SERVER_CONFIG", "/etc/default/sentinel-server"))
INSTALLED_HELPER = Path("/usr/lib/sentinel-server/sentinel-server-helper")
SOURCE_HELPER = Path(__file__).resolve().parent / "sentinel_server_helper.py"
DEFAULT_PORT = 8765
CONFIG_KEYS = ("SENTINEL_HOST", "SENTINEL_PORT", "SENTINEL_TLS_CERT", "SENTINEL_TLS_KEY",
               "SENTINEL_ALLOW_PLAIN_HTTP", "SENTINEL_OPTS")
_LINE = re.compile(r"^\s*(#\s*)?([A-Z_][A-Z0-9_]*)=(.*)$")


class ControlError(RuntimeError):
    pass


# ==================================================================== файл настроек
def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_config(text):
    """KEY=VALUE из файла EnvironmentFile; закомментированные строки пропускаются."""
    values = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match and not match.group(1):
            values[match.group(2)] = _unquote(match.group(3))
    return values


def _quote(value):
    value = str(value)
    if any(c in value for c in "\n\r\"'\\$`"):
        raise ValueError("Недопустимые символы в значении настройки")
    return f'"{value}"' if any(c.isspace() for c in value) else value


def render_config(text, updates):
    """Новый текст файла: меняет строки KEY=, раскомментирует #KEY= или дописывает в конец.

    Значение None комментирует строку (настройка выключена). Комментарии и прочие ключи
    остаются на месте.
    """
    lines = text.splitlines()
    pending = dict(updates)
    active = {m.group(2) for m in map(_LINE.match, lines) if m and not m.group(1)}
    for index, line in enumerate(lines):
        match = _LINE.match(line)
        if not match or match.group(2) not in updates:
            continue
        key, commented = match.group(2), bool(match.group(1))
        value = updates[key]
        if value is None:
            if not commented:
                lines[index] = "#" + line.lstrip()
            pending.pop(key, None)
        elif key in pending and (not commented or key not in active):
            lines[index] = f"{key}={_quote(value)}"
            pending.pop(key)
        elif not commented:
            lines[index] = "#" + line.lstrip()   # повтор ключа: systemd взял бы последний
    for key, value in pending.items():
        if value is not None:
            lines.append(f"{key}={_quote(value)}")
    return "\n".join(lines) + "\n"


def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def validate_settings(settings):
    """Проверка настроек из окна; возвращает словарь для render_config."""
    host = str(settings.get("host", "")).strip() or "127.0.0.1"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
            raise ValueError("Адрес: IP (например 0.0.0.0) или имя компьютера") from None
    try:
        port = int(settings.get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        raise ValueError("Порт должен быть числом") from None
    if not 1 <= port <= 65535:
        raise ValueError("Порт: от 1 до 65535")
    cert = str(settings.get("tls_cert") or "").strip()
    key = str(settings.get("tls_key") or "").strip()
    if bool(cert) != bool(key):
        raise ValueError("Укажите и сертификат, и ключ TLS (или ни того, ни другого)")
    for path in (cert, key):
        if path and not os.path.isabs(path):
            raise ValueError(f"Путь должен быть полным: {path}")
    plain = bool(settings.get("allow_plain_http"))
    if not cert and not plain and not is_loopback(host):
        raise ValueError("Для доступа из сети нужен TLS-сертификат. Создайте его кнопкой ниже "
                         "или разрешите HTTP без шифрования.")
    return {"SENTINEL_HOST": host, "SENTINEL_PORT": str(port), "SENTINEL_TLS_CERT": cert or None,
            "SENTINEL_TLS_KEY": key or None, "SENTINEL_ALLOW_PLAIN_HTTP": "1" if plain else None}


def read_settings(path=None):
    path = Path(path or CONFIG_PATH)
    try:
        values = parse_config(path.read_text(encoding="utf-8"))
    except OSError:
        values = {}
    try:
        port = int(values.get("SENTINEL_PORT") or DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    return {"host": values.get("SENTINEL_HOST") or "127.0.0.1", "port": port,
            "tls_cert": values.get("SENTINEL_TLS_CERT", ""), "tls_key": values.get("SENTINEL_TLS_KEY", ""),
            "allow_plain_http": values.get("SENTINEL_ALLOW_PLAIN_HTTP") == "1",
            "opts": values.get("SENTINEL_OPTS", "")}


def local_addresses():
    """IPv4-адреса компьютера в сети (для адреса подключения и сертификата)."""
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    result = []
    for item in out.split():
        try:
            if ipaddress.ip_address(item).version == 4:
                result.append(item)
        except ValueError:
            pass
    return result


def client_urls(settings):
    """Адреса, которые вводить в клиенте («Подключение к серверу»)."""
    scheme = "https" if settings.get("tls_cert") else "http"
    host, port = settings.get("host") or "127.0.0.1", settings.get("port") or DEFAULT_PORT
    if host in ("0.0.0.0", "::"):
        hosts = ["127.0.0.1"] + local_addresses()
    else:
        hosts = [host]
    return [f"{scheme}://{'[' + h + ']' if ':' in h else h}:{port}" for h in hosts]


# ==================================================================== состояние службы
@dataclass
class Status:
    state: str            # running | starting | warning | error | stopped | missing
    text: str
    detail: str = ""
    enabled: bool = False
    setup_required: bool | None = None
    version: str = ""
    urls: list = field(default_factory=list)
    cluster: dict | None = None     # роль узла горячего резерва из /health (None — одиночный сервер)

    @property
    def active(self):
        return self.state in ("running", "starting", "warning")


def _run(args, timeout=10, input_text=None):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input_text)
    except FileNotFoundError as exc:
        raise ControlError(f"Не найдена программа {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ControlError(f"{args[0]}: нет ответа") from exc


def unit_properties():
    props = "LoadState,ActiveState,SubState,UnitFileState,Result,ExecMainStatus,ActiveEnterTimestampMonotonic"
    try:
        out = _run(["systemctl", "show", SERVICE, "-p", props], timeout=5).stdout
    except ControlError:
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def probe_health(settings, timeout=1.5):
    """GET /api/v1/health на этом компьютере. None — сервер не отвечает."""
    host = settings.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::", "localhost"):
        host = "127.0.0.1"
    scheme = "https" if settings.get("tls_cert") else "http"
    url = f"{scheme}://{'[' + host + ']' if ':' in host else host}:{settings.get('port') or DEFAULT_PORT}/api/v1/health"
    handlers = [urllib.request.ProxyHandler({})]
    if scheme == "https":
        # локальная проверка «жив ли»: самоподписанный сертификат тут не мешает
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    try:
        with urllib.request.build_opener(*handlers).open(url, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except (OSError, urllib.error.URLError, ValueError, ssl.SSLError):
        return None


def _uptime_seconds(props):
    try:
        started = int(props.get("ActiveEnterTimestampMonotonic") or 0) / 1e6
    except ValueError:
        return 0
    return time.monotonic() - started if started else 0


def get_status(settings=None, props=None, health=...):
    settings = settings or read_settings()
    props = unit_properties() if props is None else props
    urls = client_urls(settings)
    enabled = props.get("UnitFileState") == "enabled"
    load = props.get("LoadState", "")
    if not props or load == "not-found":
        return Status("missing", "Служба не установлена",
                      "Установите пакет sentinel-server: sudo apt install ./sentinel_server_*.deb", urls=urls)
    active = props.get("ActiveState", "")
    if active in ("activating", "deactivating", "reloading"):
        text = "Запускается…" if active != "deactivating" else "Останавливается…"
        if props.get("SubState") == "auto-restart":
            return Status("error", "Сервер падает и перезапускается", last_log_lines(), enabled, urls=urls)
        return Status("starting", text, "", enabled, urls=urls)
    if active == "failed":
        result = props.get("Result", "")
        return Status("error", "Сервер остановился с ошибкой",
                      last_log_lines() or f"Причина: {result}", enabled, urls=urls)
    if active != "active":
        return Status("stopped", "Сервер остановлен", "", enabled, urls=urls)
    if health is ...:
        health = probe_health(settings)
    if health is None:
        if _uptime_seconds(props) < 15:
            return Status("starting", "Запускается…", "", enabled, urls=urls)
        return Status("error", "Сервер запущен, но не отвечает",
                      f"Нет ответа на {urls[0]} — проверьте адрес, порт и сертификат", enabled, urls=urls)
    setup = bool(health.get("setup_required"))
    version = str(health.get("version", ""))
    node = health.get("cluster") if isinstance(health.get("cluster"), dict) else None
    if setup:
        return Status("warning", "Работает, администратор не создан",
                      "Задайте логин и пароль администратора в настройках сервера", enabled, True, version, urls,
                      node)
    if node and not node.get("active"):
        return Status("running", "Резервный узел в ожидании", f"Версия {version}. Службы запустятся, если основной "
                      "узел перестанет отвечать." if version else "", enabled, False, version, urls, node)
    return Status("running", "Сервер работает", f"Версия {version}" if version else "", enabled, False, version,
                  urls, node)


def last_log_lines(count=4):
    try:
        out = _run(["journalctl", "-u", SERVICE, "-n", str(count), "--no-pager", "-o", "cat"], timeout=5)
    except ControlError:
        return ""
    lines = [line for line in out.stdout.splitlines() if line.strip() and not line.startswith("--")]
    return "\n".join(lines[-count:])


def journal(count=300):
    out = _run(["journalctl", "-u", SERVICE, "-n", str(count), "--no-pager", "-o", "short-iso"], timeout=10)
    text = out.stdout.strip()
    if not text or "No entries" in text or "Hint:" in out.stderr:
        hint = out.stderr.strip()
        return text + ("\n\n" if text else "") + (hint or "Журнал пуст. Чтобы читать журнал службы, ваш "
                                                       "пользователь должен быть в группе adm или systemd-journal.")
    return text


# ==================================================================== действия
def service(action):
    """start | stop | restart через systemctl (polkit спросит пароль, если правило не разрешает)."""
    if action not in ("start", "stop", "restart"):
        raise ValueError(action)
    result = _run(["systemctl", action, SERVICE], timeout=90)
    if result.returncode:
        raise ControlError(_error_text(result, f"Не удалось выполнить «{action}»"))


def _error_text(result, fallback):
    text = (result.stderr or result.stdout or "").strip()
    text = "\n".join(line.removeprefix("Ошибка: ") for line in text.splitlines() if line.strip())
    if result.returncode in (126, 127) and not text:
        return "Действие отменено: пароль администратора компьютера не введён"
    if "Not authorized" in text or "dismissed" in text.lower():
        return "Действие отменено: пароль администратора компьютера не введён"
    return text or fallback


def helper_command():
    """Команда помощника с правами root: pkexec для установленного пакета."""
    if INSTALLED_HELPER.exists():
        if not shutil.which("pkexec"):
            raise ControlError("Не найден pkexec (пакет pkexec или policykit-1)")
        return ["pkexec", str(INSTALLED_HELPER)]
    if os.geteuid() == 0:
        return [shutil.which("python3") or "python3", str(SOURCE_HELPER)]
    raise ControlError("Помощник sentinel-server-helper не установлен: установите пакет sentinel-server")


def run_helper(*args, payload=None, timeout=120):
    stdin = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
    result = _run(helper_command() + list(args), timeout=timeout, input_text=stdin)
    if result.returncode:
        raise ControlError(_error_text(result, "Помощник завершился с ошибкой"))
    return result.stdout.strip()


def set_password(username, password):
    return run_helper("set-password", payload={"username": username, "password": password})


def save_settings(settings, restart=True):
    clean = validate_settings(settings)
    return run_helper("set-config", payload={"values": clean, "restart": bool(restart)})


def generate_certificate(hosts=None):
    """Самоподписанный сертификат в /etc/sentinel-server; возвращает пути и отпечаток SHA-256."""
    out = run_helper("gen-cert", payload={"hosts": hosts or local_addresses()})
    return json.loads(out or "{}")


def set_autostart(enabled):
    return run_helper("enable" if enabled else "disable")


def list_users():
    return json.loads(run_helper("list-users") or "{}").get("users", [])


def certificate_fingerprint(path):
    try:
        out = _run(["openssl", "x509", "-in", str(path), "-noout", "-fingerprint", "-sha256"], timeout=5).stdout
    except ControlError:
        return ""
    return out.strip().split("=", 1)[-1] if "=" in out else ""


def cluster_config():
    """Настройки кластера сервера (через помощник: файл доступен только службе)."""
    return json.loads(run_helper("cluster-get") or "{}")


def save_cluster(values, restart=True):
    return json.loads(run_helper("cluster-set", payload={"values": values, "restart": bool(restart)}) or "{}")

