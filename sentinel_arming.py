"""Постановка на охрану и снятие с охраны (без GTK).

Охрану можно включать для всей системы, для области (группы устройств) и для
отдельного канала. Действует самое узкое правило: канал → область → система.
По умолчанию всё под охраной, поэтому до первого снятия поведение не меняется.

Снятая с охраны камера продолжает писать события в журнал, но правила тревог
для неё не срабатывают: тревоги не создаются, связанные действия не запускаются.
Пользовательское событие (кнопка «Запустить пользовательское событие») работает всегда.
"""
from __future__ import annotations

from datetime import datetime

import sentinel_storage as storage

SYSTEM, AREA, DEVICE = "system", "area", "device"
ALWAYS_ALLOWED = {"user"}   # типы событий, которые не зависят от охраны


def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS arming_state (
        scope TEXT NOT NULL, target INTEGER NOT NULL DEFAULT 0, armed INTEGER NOT NULL,
        changed_at TEXT NOT NULL DEFAULT '', changed_by TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (scope, target))""")


def load_states():
    """{(scope, target): {'armed', 'changed_at', 'changed_by'}}."""
    with storage.database() as db:
        _ensure(db)
        return {(r["scope"], r["target"]): {"armed": bool(r["armed"]), "changed_at": r["changed_at"],
                                            "changed_by": r["changed_by"]}
                for r in db.execute("SELECT * FROM arming_state")}


def set_armed(scope, target, armed, username=""):
    """Поставить (armed=True) или снять (False) с охраны.

    Для области и системы снимаются уточнения ниже: «поставить область на охрану»
    значит поставить все её камеры, даже если какую-то раньше снимали отдельно.
    """
    if scope not in (SYSTEM, AREA, DEVICE):
        raise ValueError("Неизвестная область охраны")
    target = int(target or 0)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with storage.database() as db:
        _ensure(db)
        if scope == SYSTEM:
            db.execute("DELETE FROM arming_state")
        elif scope == AREA:
            db.execute("DELETE FROM arming_state WHERE scope=? AND target IN "
                       "(SELECT id FROM devices WHERE group_id=?)", (DEVICE, target))
        db.execute("""INSERT INTO arming_state(scope, target, armed, changed_at, changed_by) VALUES(?,?,?,?,?)
            ON CONFLICT(scope, target) DO UPDATE SET armed=excluded.armed, changed_at=excluded.changed_at,
            changed_by=excluded.changed_by""", (scope, target, int(bool(armed)), now, username))


def set_devices_armed(device_ids, armed, username=""):
    for device_id in device_ids:
        set_armed(DEVICE, device_id, armed, username)


def effective(device, states=None):
    """Под охраной ли канал (dict устройства с id и group_id)."""
    states = load_states() if states is None else states
    if device is None:
        return states.get((SYSTEM, 0), {"armed": True})["armed"]
    for key in ((DEVICE, int(device.get("id") or 0)), (AREA, int(device.get("group_id") or 0))):
        if key in states:
            return states[key]["armed"]
    return states.get((SYSTEM, 0), {"armed": True})["armed"]


def device_armed(device_id, states=None):
    """Под охраной ли канал по id (для обработчиков событий)."""
    if not device_id:
        return effective(None, states)
    with storage.database() as db:
        row = db.execute("SELECT id, group_id FROM devices WHERE id=?", (int(device_id),)).fetchone()
    return effective(dict(row) if row else {"id": device_id}, states)


def event_raises_alarms(event, states=None):
    if event.get("event_type") in ALWAYS_ALLOWED:
        return True
    return device_armed(event.get("device_id"), states)


def summary(devices, states=None):
    """(под охраной, всего) по списку каналов."""
    states = load_states() if states is None else states
    armed = sum(1 for d in devices if effective(d, states))
    return armed, len(devices)


def area_state(group_id, devices, states=None):
    """'armed' / 'disarmed' / 'partial' для области (по её каналам)."""
    states = load_states() if states is None else states
    members = [d for d in devices if int(d.get("group_id") or 0) == int(group_id or 0)]
    if not members:
        return "armed" if effective({"group_id": group_id}, states) else "disarmed"
    values = {effective(d, states) for d in members}
    return "partial" if len(values) > 1 else ("armed" if values.pop() else "disarmed")


STATE_TITLES = {"armed": "под охраной", "disarmed": "снято с охраны", "partial": "частично под охраной"}
STATE_ICONS = {"armed": "🛡", "disarmed": "○", "partial": "◐"}
