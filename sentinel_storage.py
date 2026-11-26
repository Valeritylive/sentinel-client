"""SQLite persistence and Secret Service-backed device credentials."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
from pathlib import Path

APP_ID = "io.sentinel.VideoClient"
SECRET_LABEL = "Sentinel camera credentials"
# SENTINEL_DATA_DIR позволяет серверу (служба systemd) держать свою базу отдельно от клиента.
DATA = (Path(os.environ["SENTINEL_DATA_DIR"]).expanduser() if os.environ.get("SENTINEL_DATA_DIR")
        else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "sentinel")
DB_PATH = DATA / "devices.sqlite3"
# "secret-service" (по умолчанию) или "file" — для сервера без D-Bus-сеанса пользователя.
CREDENTIAL_STORE_ENV = "SENTINEL_CREDENTIAL_STORE"
_file_secret_lock = threading.Lock()

_initialized: set[str] = set()
_init_lock = threading.Lock()


class CredentialStoreError(RuntimeError):
    """Raised when device credentials cannot be safely read or written."""


class _ClosingConnection(sqlite3.Connection):
    """Commits/rolls back *and closes* when its ``with`` block ends.

    Plain sqlite3 ``__exit__`` only ends the transaction, so every
    ``with database() as db`` used to leave a connection open.
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _create_schema(db: sqlite3.Connection) -> None:
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
    _migrate_ivms_model(db)
    db.commit()


def _add_column(db: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_ivms_model(db: sqlite3.Connection) -> None:
    """Группы устройств, сведения об устройстве и пользовательские деления окон."""
    db.execute("""CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    _add_column(db, "devices", "group_id", "INTEGER REFERENCES groups(id) ON DELETE SET NULL")
    _add_column(db, "devices", "serial_no", "TEXT NOT NULL DEFAULT ''")
    _add_column(db, "devices", "device_model", "TEXT NOT NULL DEFAULT ''")
    _add_column(db, "devices", "use_tls", "INTEGER NOT NULL DEFAULT 0")
    _add_column(db, "devices", "host_name", "TEXT NOT NULL DEFAULT ''")
    _add_column(db, "saved_views", "division", "TEXT NOT NULL DEFAULT ''")
    db.execute("""CREATE TABLE IF NOT EXISTS custom_divisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        cols INTEGER NOT NULL, rows INTEGER NOT NULL, cells TEXT NOT NULL)""")
    _migrate_hcl_model(db)


def _migrate_hcl_model(db: sqlite3.Connection) -> None:
    """Таблицы модулей в стиле HikCentral Lite: виды, персоны, ТС, доступ, тревоги, пользователи."""
    db.execute("""CREATE TABLE IF NOT EXISTS view_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)""")
    # Виды на сервере: приватные (owner) и публичные; камеры — по адресу и каналу.
    db.execute("""CREATE TABLE IF NOT EXISTS shared_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, owner TEXT NOT NULL,
        public INTEGER NOT NULL DEFAULT 0, layout TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL DEFAULT '', updated_by TEXT NOT NULL DEFAULT '')""")
    _add_column(db, "saved_views", "group_id", "INTEGER REFERENCES view_groups(id) ON DELETE SET NULL")
    _add_column(db, "devices", "area", "TEXT NOT NULL DEFAULT ''")
    _add_column(db, "devices", "firmware", "TEXT NOT NULL DEFAULT ''")
    _add_column(db, "devices", "time_zone", "TEXT NOT NULL DEFAULT ''")
    _add_column(db, "devices", "category", "TEXT NOT NULL DEFAULT 'video'")
    _add_column(db, "devices", "status", "TEXT NOT NULL DEFAULT ''")
    _add_column(db, "devices", "status_checked_at", "TEXT NOT NULL DEFAULT ''")
    _add_column(db, "devices", "door_count", "INTEGER NOT NULL DEFAULT 0")
    _add_column(db, "devices", "alarm_outputs", "INTEGER NOT NULL DEFAULT 0")
    db.execute("""CREATE TABLE IF NOT EXISTS web_pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS departments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        parent_id INTEGER REFERENCES departments(id) ON DELETE SET NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS persons (
        id INTEGER PRIMARY KEY AUTOINCREMENT, employee_no TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL, department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL,
        gender TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
        valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
        cards TEXT NOT NULL DEFAULT '[]', pin TEXT NOT NULL DEFAULT '', remark TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS vehicle_lists (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)""")
    db.execute("""CREATE TABLE IF NOT EXISTS vehicles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, plate TEXT NOT NULL, plate_key TEXT NOT NULL,
        list_id INTEGER NOT NULL REFERENCES vehicle_lists(id) ON DELETE CASCADE,
        brand TEXT NOT NULL DEFAULT '', vehicle_type TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '',
        owner TEXT NOT NULL DEFAULT '', person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL,
        valid_from TEXT NOT NULL DEFAULT '', valid_to TEXT NOT NULL DEFAULT '',
        remark TEXT NOT NULL DEFAULT '', UNIQUE(plate_key, list_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL DEFAULT 'access', weekly TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS access_levels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        schedule_id INTEGER REFERENCES schedules(id) ON DELETE SET NULL,
        doors TEXT NOT NULL DEFAULT '[]', remark TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS person_access_levels (
        person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
        level_id INTEGER NOT NULL REFERENCES access_levels(id) ON DELETE CASCADE,
        PRIMARY KEY(person_id, level_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS department_access_levels (
        department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
        level_id INTEGER NOT NULL REFERENCES access_levels(id) ON DELETE CASCADE,
        PRIMARY KEY(department_id, level_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS access_apply_status (
        person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
        device_id INTEGER NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(person_id, device_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS alarm_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        enabled INTEGER NOT NULL DEFAULT 1, event_type TEXT NOT NULL,
        sources TEXT NOT NULL DEFAULT '"all"', vehicle_lists TEXT NOT NULL DEFAULT '"all"',
        priority TEXT NOT NULL DEFAULT 'medium', schedule_id INTEGER REFERENCES schedules(id) ON DELETE SET NULL,
        recipients TEXT NOT NULL DEFAULT '["admin"]', linkage TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, device_id INTEGER,
        device_name TEXT NOT NULL DEFAULT '', channel TEXT NOT NULL DEFAULT '',
        event_type TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'event', state TEXT NOT NULL DEFAULT '',
        details TEXT NOT NULL DEFAULT '{}', picture_path TEXT NOT NULL DEFAULT '')""")
    db.execute("CREATE INDEX IF NOT EXISTS events_time ON events(time)")
    db.execute("""CREATE TABLE IF NOT EXISTS alarm_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER, rule_name TEXT NOT NULL DEFAULT '',
        event_id INTEGER, time TEXT NOT NULL, device_id INTEGER, device_name TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL DEFAULT '', event_type TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'medium',
        status TEXT NOT NULL DEFAULT 'unacknowledged', category TEXT NOT NULL DEFAULT '',
        remarks TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '{}',
        picture_path TEXT NOT NULL DEFAULT '', acknowledged_at TEXT NOT NULL DEFAULT '',
        acknowledged_by TEXT NOT NULL DEFAULT '')""")
    db.execute("CREATE INDEX IF NOT EXISTS alarm_records_time ON alarm_records(time)")
    db.execute("""CREATE TABLE IF NOT EXISTS roles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        permissions TEXT NOT NULL DEFAULT '[]', builtin INTEGER NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL, role_id INTEGER REFERENCES roles(id) ON DELETE SET NULL,
        email TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
        expires TEXT NOT NULL DEFAULT '', password_changed_at TEXT NOT NULL DEFAULT '',
        failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS operation_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS recording_plans (
        device_id INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL DEFAULT 0, stream TEXT NOT NULL DEFAULT 'sub',
        schedule TEXT NOT NULL DEFAULT '', retention_days INTEGER NOT NULL DEFAULT 30,
        overwrite INTEGER NOT NULL DEFAULT 1)""")
    db.execute("""CREATE TABLE IF NOT EXISTS holidays (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        start_date TEXT NOT NULL, end_date TEXT NOT NULL)""")
    _migrate_cloud_model(db)


def _migrate_cloud_model(db: sqlite3.Connection) -> None:
    """Облачные аккаунты (Hik-Connect) и снимок привязанных к ним устройств."""
    db.execute("""CREATE TABLE IF NOT EXISTS cloud_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL DEFAULT 'hikconnect',
        account TEXT NOT NULL, api_url TEXT NOT NULL DEFAULT '', remember_password INTEGER NOT NULL DEFAULT 0,
        auto_sync INTEGER NOT NULL DEFAULT 1, last_sync TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(provider, account))""")
    db.execute("""CREATE TABLE IF NOT EXISTS cloud_devices (
        account_id INTEGER NOT NULL REFERENCES cloud_accounts(id) ON DELETE CASCADE,
        serial TEXT NOT NULL, full_serial TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '', firmware TEXT NOT NULL DEFAULT '', online INTEGER NOT NULL DEFAULT 0,
        local_ip TEXT NOT NULL DEFAULT '', wan_ip TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL DEFAULT '',
        http_port INTEGER NOT NULL DEFAULT 80, rtsp_port INTEGER NOT NULL DEFAULT 554,
        sdk_port INTEGER NOT NULL DEFAULT 8000, wan_http_port INTEGER NOT NULL DEFAULT 0,
        wan_rtsp_port INTEGER NOT NULL DEFAULT 0, wan_sdk_port INTEGER NOT NULL DEFAULT 0,
        category TEXT NOT NULL DEFAULT 'video', cameras TEXT NOT NULL DEFAULT '[]',
        synced_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(account_id, serial))""")
    _add_column(db, "devices", "cloud_serial", "TEXT NOT NULL DEFAULT ''")


def get_setting(key: str, default: str | None = None) -> str | None:
    with database() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    with database() as db:
        db.execute("INSERT INTO app_settings(key,value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_json_setting(key: str, default=None):
    raw = get_setting(key)
    if raw is None:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default
    if isinstance(default, dict) and isinstance(value, dict):
        merged = dict(default)
        merged.update(value)
        return merged
    return value


def set_json_setting(key: str, value) -> None:
    set_setting(key, json.dumps(value, ensure_ascii=False))


def _ensure_initialized() -> None:
    """Create/migrate the schema once per database file, not on every query."""
    key = str(DB_PATH)
    if key in _initialized and DB_PATH.exists():
        return
    with _init_lock:
        if key in _initialized and DB_PATH.exists():
            return
        DATA.mkdir(parents=True, exist_ok=True)
        os.chmod(DATA, 0o700)
        db = sqlite3.connect(DB_PATH, factory=_ClosingConnection, timeout=10)
        os.chmod(DB_PATH, 0o600)
        with db:
            _create_schema(db)
        _initialized.add(key)


def database():
    """Short-lived connection; use as ``with database() as db:`` (closed on exit)."""
    _ensure_initialized()
    db = sqlite3.connect(DB_PATH, factory=_ClosingConnection, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA secure_delete=ON")
    return db


def load_devices():
    with database() as db:
        return [dict(row) for row in db.execute("SELECT * FROM devices ORDER BY host,channel,name")]


def load_groups():
    with database() as db:
        return [dict(row) for row in db.execute("SELECT * FROM groups ORDER BY name COLLATE NOCASE")]


def add_group(name: str) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Пустое имя группы")
    with database() as db:
        row = db.execute("SELECT id FROM groups WHERE name=?", (name,)).fetchone()
        if row:
            return row["id"]
        return db.execute("INSERT INTO groups(name) VALUES(?)", (name,)).lastrowid


def rename_group(group_id: int, name: str) -> None:
    name = name.strip()
    if not name:
        raise ValueError("Пустое имя группы")
    with database() as db:
        db.execute("UPDATE groups SET name=? WHERE id=?", (name, group_id))


def delete_group(group_id: int) -> None:
    """Устройства группы остаются, но становятся «без группы»."""
    with database() as db:
        db.execute("UPDATE devices SET group_id=NULL WHERE group_id=?", (group_id,))
        db.execute("DELETE FROM groups WHERE id=?", (group_id,))


def set_device_group(device_ids, group_id) -> None:
    with database() as db:
        db.executemany("UPDATE devices SET group_id=? WHERE id=?", [(group_id, int(i)) for i in device_ids])


def load_custom_divisions():
    import json
    with database() as db:
        rows = db.execute("SELECT * FROM custom_divisions ORDER BY name COLLATE NOCASE").fetchall()
    return [{"id": r["id"], "name": r["name"], "cols": r["cols"], "rows": r["rows"],
             "cells": [tuple(c) for c in json.loads(r["cells"])]} for r in rows]


def save_custom_division(name: str, cols: int, rows: int, cells) -> None:
    import json
    from sentinel_layouts import validate_cells
    name = name.strip()
    cells = [tuple(int(v) for v in c) for c in cells]
    if not name or not validate_cells(cols, rows, cells):
        raise ValueError("Некорректное пользовательское деление")
    with database() as db:
        db.execute("""INSERT INTO custom_divisions(name,cols,rows,cells) VALUES(?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET cols=excluded.cols, rows=excluded.rows, cells=excluded.cells""",
                   (name, cols, rows, json.dumps(cells)))


def delete_custom_division(name: str) -> None:
    with database() as db:
        db.execute("DELETE FROM custom_divisions WHERE name=?", (name,))


def credential_backend() -> str:
    return "file" if os.environ.get(CREDENTIAL_STORE_ENV, "").strip().lower() == "file" else "secret-service"


def _secret_key(device) -> str:
    return f"{device['host']}|{int(device.get('http_port', 80))}"


def _file_secret(action: str, device, secret: str | None = None) -> str | None:
    """Файловое хранилище секретов (0600) для службы без Secret Service.

    Файл не шифруется: защищён только правами доступа учётной записи службы,
    как обычные конфигурационные файлы демонов. Включается явно переменной
    SENTINEL_CREDENTIAL_STORE=file.
    """
    path = DATA / "secrets.json"
    with _file_secret_lock:
        DATA.mkdir(parents=True, exist_ok=True)
        try:
            items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(items, dict):
                raise ValueError
        except (OSError, ValueError) as exc:
            raise CredentialStoreError(f"Файл секретов повреждён или недоступен: {path}") from exc
        key = _secret_key(device)
        if action == "lookup":
            return items.get(key)
        if action == "store":
            items[key] = secret
        elif action == "clear":
            if items.pop(key, None) is None:
                return None
        else:
            raise CredentialStoreError(f"Неизвестная операция: {action}")
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(items, handle, ensure_ascii=False)
        os.replace(temp, path)
        os.chmod(path, 0o600)
    return None


def _secret_tool(action: str, device, secret: str | None = None) -> str | None:
    if credential_backend() == "file":
        return _file_secret(action, device, secret)
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


# ------------------------------------------------------------------ произвольные секреты
def save_json_secret(namespace: str, name: str, value: dict) -> None:
    """Секрет не-устройства (облачный аккаунт, SMTP и т.п.) в том же хранилище."""
    _secret_tool("store", {"host": f"{namespace}:{name}", "http_port": 0}, json.dumps(value, ensure_ascii=False))


def load_json_secret(namespace: str, name: str):
    payload = _secret_tool("lookup", {"host": f"{namespace}:{name}", "http_port": 0})
    if payload is None:
        return None
    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CredentialStoreError("В хранилище секретов найдены некорректные данные") from exc
    return value if isinstance(value, dict) else None


def delete_json_secret(namespace: str, name: str) -> None:
    _secret_tool("clear", {"host": f"{namespace}:{name}", "http_port": 0})
