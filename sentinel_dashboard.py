"""Панель мониторинга (стартовая страница) и видимость модулей по ролям — без GTK.

Набор виджетов хранится для каждого пользователя отдельно. Виджет виден, только
если у роли есть нужное право; по умолчанию набор зависит от роли.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import sentinel_storage as storage


@dataclass(frozen=True)
class Widget:
    key: str
    title: str
    permissions: tuple  # любое из прав; пусто — доступно всем


WIDGETS = (
    Widget("monitoring", "Мониторинг", ("live",)),
    Widget("alarms", "Тревоги", ("alarm_ack", "alarms")),
    Widget("health", "Состояние системы", ("system", "devices")),
    Widget("devices", "Устройства", ("devices", "live")),
    Widget("records", "Целостность записи", ("playback", "system")),
    Widget("map", "Карта", ("maps",)),
    Widget("events", "События сегодня", ("reports", "alarm_ack")),
)
BY_KEY = {w.key: w for w in WIDGETS}

ROLE_DEFAULTS = {
    "Администратор": ("health", "devices", "records", "alarms", "monitoring", "map"),
    "Оператор": ("monitoring", "alarms", "map", "events"),
}
FALLBACK_DEFAULT = ("monitoring", "alarms", "events", "devices")

# Модули главного меню -> права, любое из которых открывает модуль.
MODULE_PERMISSIONS = {
    "board": (),
    "main_view": ("live",), "overview": ("live",),
    "playback": ("playback",), "archive": ("playback",),
    "events": ("alarm_ack", "alarms"), "alarms": ("alarm_ack", "alarms"),
    "emap": ("maps",),
    "access": ("access",),
    "parking": ("persons", "access"), "wall": ("live",), "patrol": ("alarms", "access"),
    "evidence": ("playback",),
    "summary": ("reports",), "dashboard": ("reports",), "logs": ("reports",),
    "local_files": ("live", "playback"),
    "devices": ("devices",), "event_config": ("alarms",), "storage": ("system",),
    "accounts": ("users",), "settings": ("system",),
}


def _can(session, permissions):
    return not permissions or any(session.can(p) for p in permissions)


def module_allowed(session, key):
    return _can(session, MODULE_PERMISSIONS.get(key, ()))


def allowed_widgets(session):
    return [w for w in WIDGETS if _can(session, w.permissions)]


def default_widgets(session):
    keys = ROLE_DEFAULTS.get(session.role, FALLBACK_DEFAULT)
    allowed = {w.key for w in allowed_widgets(session)}
    return [k for k in keys if k in allowed]


def _setting_key(username):
    return f"dashboard_widgets:{username}"


def load_widgets(session):
    """Ключи виджетов пользователя в порядке показа (только разрешённые роли)."""
    saved = storage.get_json_setting(_setting_key(session.username), None)
    allowed = {w.key for w in allowed_widgets(session)}
    if not isinstance(saved, list):
        return default_widgets(session)
    return [k for k in saved if k in allowed]


def save_widgets(session, keys):
    allowed = {w.key for w in allowed_widgets(session)}
    storage.set_json_setting(_setting_key(session.username), [k for k in keys if k in allowed])


def start_on_dashboard():
    return storage.get_setting("start_on_dashboard", "0") == "1"


def set_start_on_dashboard(value):
    storage.set_setting("start_on_dashboard", "1" if value else "0")


# ---------------------------------------------------------------- состояние системы
def read_cpu_times(path="/proc/stat"):
    """(занято, всего) в тиках из первой строки /proc/stat; None, если недоступно."""
    try:
        with open(path, encoding="ascii") as handle:
            fields = handle.readline().split()
    except OSError:
        return None
    if not fields or fields[0] != "cpu":
        return None
    values = [int(v) for v in fields[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    total = sum(values[:8])
    return total - idle, total


class CpuMeter:
    """Загрузка CPU в процентах между двумя вызовами sample()."""

    def __init__(self, reader=read_cpu_times):
        self.reader = reader
        self.last = reader()

    def sample(self):
        current = self.reader()
        previous, self.last = self.last, current
        if not current or not previous:
            return None
        busy = current[0] - previous[0]
        total = current[1] - previous[1]
        if total <= 0:
            return None
        return max(0.0, min(100.0, busy * 100.0 / total))


def memory_usage(path="/proc/meminfo"):
    """(использовано байт, всего байт) или None."""
    values = {}
    try:
        with open(path, encoding="ascii") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    values[name] = int(parts[0]) * 1024
    except (OSError, ValueError):
        return None
    total = values.get("MemTotal")
    available = values.get("MemAvailable", values.get("MemFree"))
    if not total or available is None:
        return None
    return total - available, total


def disk_usage(path):
    """(занято, всего, свободно) байт для ближайшей существующей папки пути или None."""
    import shutil
    from pathlib import Path
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        disk = shutil.disk_usage(probe)
    except OSError:
        return None
    if not disk.total:
        return None
    return disk.total - disk.free, disk.total, disk.free


def load_average():
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return None


def device_counts():
    """{'total', 'online', 'offline', 'unknown', 'channels'} по регистраторам (адресам)."""
    with storage.database() as db:
        rows = db.execute("SELECT host, MAX(status) AS status, COUNT(*) AS channels FROM devices GROUP BY host").fetchall()
    result = {"total": len(rows), "online": 0, "offline": 0, "unknown": 0,
              "channels": sum(r["channels"] for r in rows)}
    for row in rows:
        status = row["status"] or ""
        result[status if status in ("online", "offline") else "unknown"] += 1
    return result


def record_integrity(now=None, root=None, plans=None, list_segments=None):
    """Проверка резервной записи на ПК: пишутся ли камеры, которым положено писать сейчас.

    Возвращает {'scheduled', 'ok', 'gaps': [device_id, ...]}. Камера с перебоем —
    по расписанию запись идёт, а последний сегмент старше двух длин сегмента.
    """
    import sentinel_recording as rec
    now = now or datetime.now().astimezone()
    root = root or rec.storage_root()
    plans = rec.enabled_plans() if plans is None else plans
    list_segments = list_segments or rec.list_segments
    limit = timedelta(seconds=rec.SEGMENT_SECONDS * 2)
    result = {"scheduled": 0, "ok": 0, "gaps": []}
    for plan in plans:
        if not rec.should_record(plan, now):
            continue
        result["scheduled"] += 1
        segments = list_segments(root, plan.device_id, now - limit, now + timedelta(seconds=1))
        if segments and now - segments[-1][1] <= limit:
            result["ok"] += 1
        else:
            result["gaps"].append(plan.device_id)
    return result


def percent_text(value):
    return "—" if value is None else f"{value:.0f}%"
