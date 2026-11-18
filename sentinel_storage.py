"""SQLite persistence and Secret Service-backed device credentials."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

APP_ID = "io.sentinel.VideoClient"
SECRET_LABEL = "Sentinel camera credentials"
DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "sentinel"
DB_PATH = DATA / "devices.sqlite3"


class CredentialStoreError(RuntimeError):
    """Raised when device credentials cannot be safely read or written."""


def database():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    db = sqlite3.connect(DB_PATH)
    os.chmod(DB_PATH, 0o600)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA secure_delete=ON")
    db.execute("""CREATE TABLE IF NOT EXISTS devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, host TEXT NOT NULL, sdk_port INTEGER NOT NULL DEFAULT 8000,
        http_port INTEGER NOT NULL DEFAULT 80, rtsp_port INTEGER NOT NULL DEFAULT 554, channel INTEGER NOT NULL DEFAULT 1,
        stream TEXT NOT NULL DEFAULT 'main', protocol TEXT NOT NULL DEFAULT 'hikvision',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS saved_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        layout INTEGER NOT NULL, mapping TEXT NOT NULL,
        grid_columns INTEGER NOT NULL DEFAULT 0, grid_rows INTEGER NOT NULL DEFAULT 0)""")
    saved_view_columns = {row[1] for row in db.execute("PRAGMA table_info(saved_views)")}
    if "grid_columns" not in saved_view_columns:
        db.execute("ALTER TABLE saved_views ADD COLUMN grid_columns INTEGER NOT NULL DEFAULT 0")
    if "grid_rows" not in saved_view_columns:
        db.execute("ALTER TABLE saved_views ADD COLUMN grid_rows INTEGER NOT NULL DEFAULT 0")
    db.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS device_credentials (
        host TEXT NOT NULL, http_port INTEGER NOT NULL,
        username TEXT NOT NULL, password TEXT NOT NULL, rtsp_url TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(host, http_port))""")
    db.execute("CREATE TABLE IF NOT EXISTS maps (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, image_path TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS map_hotspots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, map_id INTEGER NOT NULL, device_id INTEGER NOT NULL,
        label TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
        FOREIGN KEY(map_id) REFERENCES maps(id) ON DELETE CASCADE)""")
    db.execute("""CREATE TABLE IF NOT EXISTS saved_windows (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        capacity INTEGER NOT NULL, screen_index INTEGER NOT NULL DEFAULT 0,
        mapping TEXT NOT NULL, use_substream INTEGER NOT NULL DEFAULT 1,
        fullscreen INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    window_columns = {row[1] for row in db.execute("PRAGMA table_info(saved_windows)")}
    if "fullscreen" not in window_columns:
        db.execute("ALTER TABLE saved_windows ADD COLUMN fullscreen INTEGER NOT NULL DEFAULT 1")
    columns = {row[1] for row in db.execute("PRAGMA table_info(devices)")}
    if "http_port" not in columns:
        db.execute("ALTER TABLE devices ADD COLUMN http_port INTEGER NOT NULL DEFAULT 80")
    for index in db.execute("PRAGMA index_list(devices)").fetchall():
        if index[2]:
            index_columns = [row[2] for row in db.execute(f"PRAGMA index_info({index[1]})")]
            if index_columns == ["host", "sdk_port"]:
                db.execute("ALTER TABLE devices RENAME TO devices_legacy")
                db.execute("""CREATE TABLE devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, host TEXT NOT NULL,
                    sdk_port INTEGER NOT NULL DEFAULT 8000, http_port INTEGER NOT NULL DEFAULT 80,
                    rtsp_port INTEGER NOT NULL DEFAULT 554, channel INTEGER NOT NULL DEFAULT 1,
                    stream TEXT NOT NULL DEFAULT 'main', protocol TEXT NOT NULL DEFAULT 'hikvision',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
                db.execute("""INSERT INTO devices(id,name,host,sdk_port,http_port,rtsp_port,channel,stream,protocol,created_at)
                    SELECT id,name,host,sdk_port,http_port,rtsp_port,channel,stream,protocol,created_at FROM devices_legacy""")
                db.execute("DROP TABLE devices_legacy")
                break
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS devices_host_sdk_channel ON devices(host,sdk_port,channel)")
    db.commit()
    return db


def load_devices():
    with database() as db:
        return [dict(row) for row in db.execute("SELECT * FROM devices ORDER BY host,channel,name")]


def _secret_tool(action: str, device, secret: str | None = None) -> str | None:
    executable = shutil.which("secret-tool")
    if not executable:
        raise CredentialStoreError("Установите libsecret-tools для системного хранения паролей")

    command = [executable, action]
    if action == "store":
        command.append(f"--label={SECRET_LABEL}")
    command.extend((
        "application", APP_ID,
        "host", str(device["host"]),
        "http_port", str(int(device.get("http_port", 80))),
    ))
    try:
        result = subprocess.run(
            command,
            input=secret if action == "store" else None,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CredentialStoreError("Истекло время ожидания системного хранилища секретов") from exc
    except OSError as exc:
        raise CredentialStoreError("Не удалось запустить secret-tool") from exc

    if action == "lookup":
        if result.returncode == 0:
            return result.stdout
        if result.returncode == 1 and not result.stderr.strip():
            return None
    elif action == "clear":
        if result.returncode == 0 or (result.returncode == 1 and not result.stderr.strip()):
            return None
    elif action == "store" and result.returncode == 0:
        return None

    raise CredentialStoreError(
        f"Системное хранилище секретов недоступно (secret-tool, код {result.returncode})"
    )


def _credential_payload(credentials) -> str:
    return json.dumps({
        "username": credentials["username"],
        "password": credentials.get("password", ""),
        "rtsp_url": credentials.get("rtsp_url", ""),
    }, ensure_ascii=False)


def _store_secret(device, credentials) -> None:
    _secret_tool("store", device, _credential_payload(credentials))


def _decode_secret(payload: str):
    try:
        credentials = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CredentialStoreError("В системном хранилище найдены некорректные данные Sentinel") from exc
    if not isinstance(credentials, dict) or not isinstance(credentials.get("username"), str):
        raise CredentialStoreError("В системном хранилище отсутствует имя пользователя устройства")
    return {
        "username": credentials["username"],
        "password": str(credentials.get("password", "")),
        "rtsp_url": str(credentials.get("rtsp_url", "")),
    }


def load_saved_credentials(device):
    key = (device["host"], int(device.get("http_port", 80)))
    with database() as db:
        row = db.execute(
            "SELECT username,password,rtsp_url FROM device_credentials WHERE host=? AND http_port=?",
            key,
        ).fetchone()

    if row:
        credentials = {
            "username": row["username"],
            "password": row["password"],
            "rtsp_url": row["rtsp_url"],
        }
        _store_secret(device, credentials)
        with database() as db:
            db.execute("DELETE FROM device_credentials WHERE host=? AND http_port=?", key)
        return credentials

    payload = _secret_tool("lookup", device)
    return _decode_secret(payload) if payload is not None else None


def save_credentials(device, credentials):
    if not credentials.get("username"):
        raise ValueError("Имя пользователя камеры не может быть пустым")
    _store_secret(device, credentials)
    key = (device["host"], int(device.get("http_port", 80)))
    with database() as db:
        db.execute("DELETE FROM device_credentials WHERE host=? AND http_port=?", key)


def migrate_legacy_credentials() -> int:
    with database() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT host,http_port,username,password,rtsp_url FROM device_credentials"
        )]

    for row in rows:
        device = {"host": row["host"], "http_port": row["http_port"]}
        _store_secret(device, row)
    if rows:
        with database() as db:
            db.executemany(
                "DELETE FROM device_credentials WHERE host=? AND http_port=?",
                ((row["host"], row["http_port"]) for row in rows),
            )
    return len(rows)


def delete_saved_credentials(device) -> None:
    _secret_tool("clear", device)
    with database() as db:
        db.execute(
            "DELETE FROM device_credentials WHERE host=? AND http_port=?",
            (device["host"], int(device.get("http_port", 80))),
        )
