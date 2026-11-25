"""Пользователи клиента, роли, политика паролей, блокировка и резервная копия (без GTK).

Пароли пользователей клиента хранятся как PBKDF2-SHA256 (соль + 310 000 итераций).
Это пароли входа в Sentinel; пароли устройств по-прежнему лежат в Secret Service.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import sentinel_storage as storage

ITERATIONS = 310_000

PERMISSIONS = {
    "live": "Живой просмотр",
    "playback": "Воспроизведение и экспорт",
    "ptz": "Управление PTZ",
    "devices": "Управление устройствами",
    "persons": "Персоны и ТС",
    "access": "Контроль доступа и двери",
    "alarms": "Настройка событий и тревог",
    "alarm_ack": "Обработка тревог",
    "maps": "Карты",
    "reports": "Статистика и отчёты",
    "users": "Пользователи и безопасность",
    "system": "Системная конфигурация",
}
BUILTIN_ROLES = {
    "Администратор": tuple(PERMISSIONS),
    "Оператор": ("live", "playback", "ptz", "alarm_ack", "maps", "reports", "access"),
}

DEFAULT_SECURITY = {
    "password_validity_days": 180,   # 0 — без ограничения
    "warn_before_days": 14,
    "auto_lock_minutes": 0,          # 0 — не блокировать клиент
    "max_failed_attempts": 5,
    "lock_minutes": 30,
    "min_password_length": 8,
    "require_login": False,          # окно входа при запуске
}


# ---------------------------------------------------------------- хеши
def hash_password(password, salt=None, iterations=ITERATIONS):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt, digest = encoded.split("$")
    except (AttributeError, ValueError):
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(check.hex(), digest)


def password_strength(password):
    """0..3: слабый / средний / хороший / надёжный (как полоска в HikCentral)."""
    if not password:
        return 0
    classes = sum(bool(any(test(c) for c in password)) for test in
                  (str.islower, str.isupper, str.isdigit, lambda c: not c.isalnum()))
    score = 0
    if len(password) >= 8:
        score += 1
    if classes >= 2:
        score += 1
    if len(password) >= 12 and classes >= 3:
        score += 1
    return score


STRENGTH_TITLES = ("Слабый", "Средний", "Хороший", "Надёжный")


def validate_new_password(password, username="", policy=None):
    policy = dict(DEFAULT_SECURITY, **(policy or {}))
    if len(password) < int(policy["min_password_length"]):
        raise ValueError(f"Пароль должен быть не короче {policy['min_password_length']} символов")
    if username and username.lower() in password.lower():
        raise ValueError("Пароль не должен содержать имя пользователя")
    if password_strength(password) < 1:
        raise ValueError("Слишком простой пароль: добавьте буквы разного регистра и цифры")


# ---------------------------------------------------------------- роли и пользователи
def ensure_builtin_roles():
    with storage.database() as db:
        for name, perms in BUILTIN_ROLES.items():
            db.execute("INSERT OR IGNORE INTO roles(name,permissions,builtin) VALUES(?,?,1)",
                       (name, json.dumps(list(perms))))


def load_roles():
    ensure_builtin_roles()
    with storage.database() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM roles ORDER BY builtin DESC, name")]
    for row in rows:
        try:
            row["permissions"] = [p for p in json.loads(row["permissions"]) if p in PERMISSIONS]
        except (TypeError, json.JSONDecodeError):
            row["permissions"] = []
    return rows


def save_role(name, permissions, role_id=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("Введите название роли")
    perms = json.dumps([p for p in permissions if p in PERMISSIONS])
    with storage.database() as db:
        if role_id is None:
            return db.execute("INSERT INTO roles(name,permissions) VALUES(?,?)", (name, perms)).lastrowid
        db.execute("UPDATE roles SET name=?, permissions=? WHERE id=? AND builtin=0", (name, perms, role_id))
        return role_id


def delete_role(role_id):
    with storage.database() as db:
        if db.execute("SELECT 1 FROM users WHERE role_id=?", (role_id,)).fetchone():
            raise ValueError("Роль назначена пользователям")
        db.execute("DELETE FROM roles WHERE id=? AND builtin=0", (role_id,))


def load_users():
    with storage.database() as db:
        return [dict(r) for r in db.execute("""SELECT u.id,u.username,u.role_id,u.email,u.enabled,u.expires,
            u.password_changed_at,u.failed_attempts,u.locked_until,u.created_at,r.name AS role_name
            FROM users u LEFT JOIN roles r ON r.id=u.role_id ORDER BY u.id""")]


def has_users():
    with storage.database() as db:
        return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def _now():
    return datetime.now().astimezone()


def create_user(username, password, role_id, email="", expires="", policy=None):
    username = (username or "").strip()
    if not username or len(username) > 32 or not all(c.isalnum() or c in "._-" for c in username):
        raise ValueError("Имя пользователя: латиница/цифры/._- до 32 символов")
    validate_new_password(password, username, policy)
    with storage.database() as db:
        return db.execute("""INSERT INTO users(username,password_hash,role_id,email,expires,password_changed_at)
            VALUES(?,?,?,?,?,?)""", (username, hash_password(password), role_id, email, expires,
                                     _now().isoformat(timespec="seconds"))).lastrowid


def ensure_admin(password):
    """Первый запуск: создать admin (активация, как в HikCentral)."""
    ensure_builtin_roles()
    with storage.database() as db:
        role = db.execute("SELECT id FROM roles WHERE name='Администратор'").fetchone()["id"]
    return create_user("admin", password, role)


def update_user(user_id, role_id=None, email=None, enabled=None, expires=None):
    with storage.database() as db:
        row = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if row and row["username"] == "admin" and enabled is False:
            raise ValueError("Пользователя admin отключить нельзя")
        for column, value in (("role_id", role_id), ("email", email), ("expires", expires)):
            if value is not None:
                db.execute(f"UPDATE users SET {column}=? WHERE id=?", (value, user_id))
        if enabled is not None:
            db.execute("UPDATE users SET enabled=? WHERE id=?", (1 if enabled else 0, user_id))


def delete_user(user_id):
    with storage.database() as db:
        row = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if row and row["username"] == "admin":
            raise ValueError("Пользователя admin удалить нельзя")
        db.execute("DELETE FROM users WHERE id=?", (user_id,))


def change_password(username, old_password, new_password, policy=None):
    with storage.database() as db:
        row = db.execute("SELECT id,password_hash FROM users WHERE username=?", (username,)).fetchone()
    if not row or not verify_password(old_password, row["password_hash"]):
        raise ValueError("Неверный текущий пароль")
    reset_password(row["id"], new_password, username, policy)


def reset_password(user_id, new_password, username="", policy=None):
    validate_new_password(new_password, username, policy)
    with storage.database() as db:
        db.execute("""UPDATE users SET password_hash=?, password_changed_at=?, failed_attempts=0, locked_until=''
            WHERE id=?""", (hash_password(new_password), _now().isoformat(timespec="seconds"), user_id))


@dataclass
class Session:
    user_id: int
    username: str
    role: str
    permissions: frozenset
    password_expires_in: int | None   # дней; None — без ограничения

    def can(self, permission):
        return permission in self.permissions


class LoginError(ValueError):
    pass


def authenticate(username, password, policy=None, now=None):
    policy = dict(DEFAULT_SECURITY, **(policy or {}))
    now = now or _now()
    with storage.database() as db:
        row = db.execute("""SELECT u.*, r.name AS role_name, r.permissions FROM users u
            LEFT JOIN roles r ON r.id=u.role_id WHERE u.username=?""", ((username or "").strip(),)).fetchone()
    if not row:
        raise LoginError("Неверное имя пользователя или пароль")
    row = dict(row)
    if row["locked_until"] and now < datetime.fromisoformat(row["locked_until"]):
        raise LoginError(f"Учётная запись заблокирована до {row['locked_until'][11:16]}")
    if not verify_password(password, row["password_hash"]):
        attempts = row["failed_attempts"] + 1
        locked = ""
        if policy["max_failed_attempts"] and attempts >= int(policy["max_failed_attempts"]):
            locked = (now + timedelta(minutes=int(policy["lock_minutes"]))).isoformat(timespec="seconds")
            attempts = 0
        with storage.database() as db:
            db.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?", (attempts, locked, row["id"]))
        raise LoginError("Учётная запись временно заблокирована" if locked else "Неверное имя пользователя или пароль")
    if not row["enabled"]:
        raise LoginError("Учётная запись отключена")
    if row["expires"] and now.strftime("%Y-%m-%d") > row["expires"][:10]:
        raise LoginError("Срок действия учётной записи истёк")
    with storage.database() as db:
        db.execute("UPDATE users SET failed_attempts=0, locked_until='' WHERE id=?", (row["id"],))
    try:
        perms = frozenset(json.loads(row["permissions"] or "[]"))
    except json.JSONDecodeError:
        perms = frozenset()
    expires_in = None
    validity = int(policy["password_validity_days"])
    if validity and row["password_changed_at"]:
        changed = datetime.fromisoformat(row["password_changed_at"])
        expires_in = validity - (now - changed).days
    return Session(row["id"], row["username"], row["role_name"] or "", perms, expires_in)


def local_session():
    """Сеанс без входа (пользователи не заведены): все права."""
    return Session(0, "admin", "Администратор", frozenset(PERMISSIONS), None)


def load_policy():
    return storage.get_json_setting("security", dict(DEFAULT_SECURITY))


def save_policy(policy):
    clean = dict(DEFAULT_SECURITY)
    for key, default in DEFAULT_SECURITY.items():
        if key in policy:
            clean[key] = type(default)(policy[key])
    storage.set_json_setting("security", clean)
    return clean


# ---------------------------------------------------------------- резервная копия
BACKUP_SUFFIX = ".sentinel-backup"


def backup_database(target):
    """Согласованная копия SQLite (sqlite backup API). Пароли устройств в неё не входят."""
    target = Path(target)
    if target.suffix != BACKUP_SUFFIX:
        target = target.with_name(target.name + BACKUP_SUFFIX)
    storage._ensure_initialized()
    source = sqlite3.connect(storage.DB_PATH)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    os.chmod(target, 0o600)
    return target


def restore_database(source):
    """Заменить базу копией. Текущая база сохраняется рядом как *.before-restore."""
    source = Path(source)
    check = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Файл резервной копии повреждён")
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        check.close()
    if "devices" not in tables or "app_settings" not in tables:
        raise ValueError("Это не резервная копия Sentinel")
    storage._ensure_initialized()
    keep = storage.DB_PATH.with_name(storage.DB_PATH.name + ".before-restore")
    shutil.copy2(storage.DB_PATH, keep)
    os.chmod(keep, 0o600)
    with tempfile.NamedTemporaryFile(dir=storage.DATA, delete=False) as handle:
        temp = Path(handle.name)
    shutil.copyfile(source, temp)
    os.chmod(temp, 0o600)
    os.replace(temp, storage.DB_PATH)
    storage._initialized.discard(str(storage.DB_PATH))  # заново прогнать миграции схемы
    storage._ensure_initialized()
    return keep
