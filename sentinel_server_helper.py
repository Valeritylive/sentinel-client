#!/usr/bin/python3
"""Помощник окна настроек Sentinel Server: действия, которым нужны права root.

Вызывается через ``pkexec /usr/lib/sentinel-server/sentinel-server-helper КОМАНДА``
(polkit спрашивает пароль администратора компьютера). Данные — JSON на stdin, чтобы пароль
не попадал в список процессов.

Команды: set-password, set-config, gen-cert, enable, disable, list-users.
"""
from __future__ import annotations

import grp
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sentinel_server_ctl as ctl  # noqa: E402

SERVER_BIN = os.environ.get("SENTINEL_SERVER_BIN", "/usr/bin/sentinel-server")
CERT_DIR = Path(os.environ.get("SENTINEL_CERT_DIR", "/etc/sentinel-server"))
SERVICE_GROUP = "sentinel"


def fail(message, code=1):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def read_payload():
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        fail("Помощник ожидает JSON на stdin")
    if not isinstance(value, dict):
        fail("Помощник ожидает JSON-объект")
    return value


def _group_id():
    try:
        return grp.getgrnam(SERVICE_GROUP).gr_gid
    except KeyError:
        return -1


def _systemctl(*args):
    result = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=90)
    if result.returncode:
        fail(result.stderr.strip() or f"systemctl {' '.join(args)}: ошибка")


def _write_atomic(path, text, mode=0o644, group=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        if group is not None and group >= 0:
            os.chown(tmp, 0, group)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------ команды
def cmd_set_password(payload):
    username = str(payload.get("username") or "admin").strip()
    password = str(payload.get("password") or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", username):
        fail("Логин: латиница, цифры и ._- (до 32 символов)")
    if not password:
        fail("Введите пароль")
    env = dict(os.environ, SENTINEL_ADMIN_PASSWORD=password)
    result = subprocess.run([SERVER_BIN, "reset-password", "--username", username], capture_output=True,
                            text=True, timeout=60, env=env, cwd="/")
    if result.returncode:
        fail(result.stderr.strip() or "Не удалось задать пароль")
    print(result.stdout.strip())


def _install_tls_file(source, name, mode):
    """Сертификат из домашней папки служба не видит (ProtectHome) — копируем в /etc/sentinel-server."""
    source = Path(source)
    if not source.is_file():
        fail(f"Файл не найден: {source}")
    target = CERT_DIR / name
    if source.resolve() == target.resolve():
        os.chmod(target, mode)
        os.chown(target, 0, max(_group_id(), 0))
        return str(target)
    _write_atomic(target, source.read_text(encoding="utf-8"), mode, _group_id())
    return str(target)


def cmd_set_config(payload):
    values = payload.get("values") or {}
    settings = {"host": values.get("SENTINEL_HOST"), "port": values.get("SENTINEL_PORT"),
                "tls_cert": values.get("SENTINEL_TLS_CERT") or "", "tls_key": values.get("SENTINEL_TLS_KEY") or "",
                "allow_plain_http": values.get("SENTINEL_ALLOW_PLAIN_HTTP") == "1"}
    try:
        clean = ctl.validate_settings(settings)
    except ValueError as exc:
        fail(str(exc))
    if clean["SENTINEL_TLS_CERT"]:
        clean["SENTINEL_TLS_CERT"] = _install_tls_file(clean["SENTINEL_TLS_CERT"], "cert.pem", 0o644)
        clean["SENTINEL_TLS_KEY"] = _install_tls_file(clean["SENTINEL_TLS_KEY"], "key.pem", 0o640)
    path = ctl.CONFIG_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = "# Настройки службы sentinel-server (systemd читает этот файл при запуске).\n"
    _write_atomic(path, ctl.render_config(text, clean))
    if payload.get("restart"):
        _systemctl("restart", ctl.SERVICE)
    print("Настройки сохранены")


def cmd_gen_cert(payload):
    if not shutil.which("openssl"):
        fail("Не найден openssl: sudo apt install openssl")
    hostname = socket.gethostname()
    names = ["localhost", hostname]
    ips = ["127.0.0.1"]
    for host in payload.get("hosts") or []:
        host = str(host).strip()
        try:
            ipaddress.ip_address(host)
            ips.append(host)
        except ValueError:
            if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
                names.append(host)
    san = ",".join([f"DNS:{n}" for n in dict.fromkeys(names)] + [f"IP:{i}" for i in dict.fromkeys(ips)])
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CERT_DIR, 0o755)
    with tempfile.TemporaryDirectory() as tmp:
        cert, key = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
        result = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
                                 "-days", "3650", "-subj", f"/CN={hostname}/O=Sentinel Server",
                                 "-addext", f"subjectAltName={san}", "-keyout", str(key), "-out", str(cert)],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            fail(result.stderr.strip() or "openssl: ошибка")
        _write_atomic(CERT_DIR / "cert.pem", cert.read_text(), 0o644, _group_id())
        _write_atomic(CERT_DIR / "key.pem", key.read_text(), 0o640, _group_id())
    print(json.dumps({"cert": str(CERT_DIR / "cert.pem"), "key": str(CERT_DIR / "key.pem"),
                      "fingerprint": ctl.certificate_fingerprint(CERT_DIR / "cert.pem"), "names": san}))


def cmd_list_users(_payload):
    result = subprocess.run([SERVER_BIN, "list-users", "--json"], capture_output=True, text=True, timeout=30,
                            cwd="/")
    if result.returncode:
        fail(result.stderr.strip() or "Не удалось прочитать пользователей")
    print(result.stdout.strip())


COMMANDS = {
    "set-password": cmd_set_password,
    "set-config": cmd_set_config,
    "gen-cert": cmd_gen_cert,
    "enable": lambda _p: _systemctl("enable", ctl.SERVICE),
    "disable": lambda _p: _systemctl("disable", ctl.SERVICE),
    "list-users": cmd_list_users,
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in COMMANDS:
        fail("Использование: sentinel-server-helper " + "|".join(COMMANDS), 2)
    if os.geteuid() != 0:
        fail("Помощник запускается через pkexec (нужны права root)")
    COMMANDS[argv[0]](read_payload())


if __name__ == "__main__":
    main()
