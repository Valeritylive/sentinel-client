"""Парковка (без GTK): стоянки, въезд и выезд по распознанному номеру, шлагбаум,
занятость, история проездов.

Стоянка — набор камер ANPR на въезде и на выезде, вместимость и шлагбаум
(тревожный выход устройства с автозакрытием). Решение на въезде:
номер из чёрного списка — отказ; режим «только списки» — пропуск номеров из
разрешённых списков; режим «все» — пропуск всех, пока есть свободные места
(номера из разрешённых списков проезжают и на полную стоянку). На выезде
открытая сессия с тем же номером закрывается (допускается одна ошибка
распознавания), шлагбаум открывается всегда.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta

import sentinel_storage as storage
from sentinel_alarms import plate_key

MODE_LISTS, MODE_ALL = "lists", "all"
MODE_TITLES = {MODE_LISTS: "Только номера из разрешённых списков", MODE_ALL: "Все, пока есть места"}
ALLOW, DENY = "allow", "deny"
REASONS = {"list": "в разрешённом списке", "open": "свободный въезд", "blacklist": "в чёрном списке",
           "full": "нет свободных мест", "not_listed": "нет в разрешённых списках", "exit": "выезд",
           "exit_unknown": "выезд без въезда", "manual": "вручную оператором"}
FMT = "%Y-%m-%d %H:%M:%S"


def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS parking_lots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, capacity INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL DEFAULT 'lists', entry_cameras TEXT NOT NULL DEFAULT '[]',
        exit_cameras TEXT NOT NULL DEFAULT '[]', allow_lists TEXT NOT NULL DEFAULT '[]',
        deny_lists TEXT NOT NULL DEFAULT '[]', barrier TEXT NOT NULL DEFAULT '{}',
        exit_barrier TEXT NOT NULL DEFAULT '{}', overstay_hours INTEGER NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS parking_passes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id INTEGER NOT NULL, plate TEXT NOT NULL,
        plate_key TEXT NOT NULL, direction TEXT NOT NULL, time TEXT NOT NULL, decision TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '', device_id INTEGER, event_id INTEGER, username TEXT NOT NULL DEFAULT '',
        session_id INTEGER)""")
    db.execute("""CREATE TABLE IF NOT EXISTS parking_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id INTEGER NOT NULL, plate TEXT NOT NULL,
        plate_key TEXT NOT NULL, entered_at TEXT NOT NULL, exited_at TEXT NOT NULL DEFAULT '',
        lists TEXT NOT NULL DEFAULT '')""")
    db.execute("CREATE INDEX IF NOT EXISTS parking_open ON parking_sessions(lot_id, exited_at)")


def _json(value, default):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _lot(row):
    lot = dict(row)
    for key in ("entry_cameras", "exit_cameras", "allow_lists", "deny_lists"):
        lot[key] = [int(v) for v in _json(lot[key], [])]
    for key in ("barrier", "exit_barrier"):
        lot[key] = _json(lot[key], {}) or {}
    return lot


def _clean_barrier(barrier):
    if not barrier or not barrier.get("device_id"):
        return {}
    return {"device_id": int(barrier["device_id"]), "output": max(1, int(barrier.get("output") or 1)),
            "auto_close": max(0, int(barrier.get("auto_close", 5) or 0))}


def load_lots():
    with storage.database() as db:
        _ensure(db)
        return [_lot(r) for r in db.execute("SELECT * FROM parking_lots ORDER BY name")]


def get_lot(lot_id):
    return next((lot for lot in load_lots() if lot["id"] == int(lot_id)), None)


def save_lot(lot, lot_id=None):
    name = (lot.get("name") or "").strip()
    if not name:
        raise ValueError("Введите название стоянки")
    entry = sorted({int(d) for d in lot.get("entry_cameras") or []})
    exit_ = sorted({int(d) for d in lot.get("exit_cameras") or []})
    if not entry and not exit_:
        raise ValueError("Выберите камеры въезда или выезда")
    if set(entry) & set(exit_):
        raise ValueError("Одна камера не может быть и на въезде, и на выезде")
    mode = lot.get("mode", MODE_LISTS)
    if mode not in MODE_TITLES:
        raise ValueError("Неизвестный режим въезда")
    allow = sorted({int(i) for i in lot.get("allow_lists") or []})
    deny = sorted({int(i) for i in lot.get("deny_lists") or []})
    if set(allow) & set(deny):
        raise ValueError("Список не может быть одновременно разрешённым и чёрным")
    if mode == MODE_LISTS and not allow:
        raise ValueError("Для режима «только списки» выберите разрешённые списки")
    capacity = max(0, int(lot.get("capacity") or 0))
    for other in load_lots():
        if other["id"] != lot_id and set(other["entry_cameras"] + other["exit_cameras"]) & set(entry + exit_):
            raise ValueError(f"Камера уже используется стоянкой «{other['name']}»")
    values = (name, capacity, mode, json.dumps(entry), json.dumps(exit_), json.dumps(allow), json.dumps(deny),
              json.dumps(_clean_barrier(lot.get("barrier"))), json.dumps(_clean_barrier(lot.get("exit_barrier"))),
              max(0, int(lot.get("overstay_hours") or 0)))
    with storage.database() as db:
        _ensure(db)
        if lot_id is None:
            return db.execute("""INSERT INTO parking_lots(name,capacity,mode,entry_cameras,exit_cameras,allow_lists,
                deny_lists,barrier,exit_barrier,overstay_hours) VALUES(?,?,?,?,?,?,?,?,?,?)""", values).lastrowid
        db.execute("""UPDATE parking_lots SET name=?,capacity=?,mode=?,entry_cameras=?,exit_cameras=?,allow_lists=?,
            deny_lists=?,barrier=?,exit_barrier=?,overstay_hours=? WHERE id=?""", values + (int(lot_id),))
        return lot_id


def delete_lot(lot_id):
    with storage.database() as db:
        _ensure(db)
        db.execute("DELETE FROM parking_sessions WHERE lot_id=?", (int(lot_id),))
        db.execute("DELETE FROM parking_passes WHERE lot_id=?", (int(lot_id),))
        db.execute("DELETE FROM parking_lots WHERE id=?", (int(lot_id),))


def lot_for_camera(device_id, lots=None):
    """(стоянка, 'in'|'out') для камеры или (None, None)."""
    for lot in load_lots() if lots is None else lots:
        if device_id in lot["entry_cameras"]:
            return lot, "in"
        if device_id in lot["exit_cameras"]:
            return lot, "out"
    return None, None


# ------------------------------------------------------------ решения
def open_sessions(lot_id=None):
    query, args = "SELECT * FROM parking_sessions WHERE exited_at=''", []
    if lot_id is not None:
        query += " AND lot_id=?"
        args.append(int(lot_id))
    with storage.database() as db:
        _ensure(db)
        return [dict(r) for r in db.execute(query + " ORDER BY entered_at DESC", args)]


def occupancy(lot):
    used = len(open_sessions(lot["id"]))
    return {"used": used, "capacity": lot["capacity"],
            "free": max(0, lot["capacity"] - used) if lot["capacity"] else None}


def entry_decision(lot, plate, index):
    """(allow|deny, причина). index — {plate_key: [list_id]} (people.vehicle_index())."""
    lists = set(index.get(plate_key(plate), []))
    if lists & set(lot["deny_lists"]):
        return DENY, "blacklist"
    if lists & set(lot["allow_lists"]):
        return ALLOW, "list"
    if lot["mode"] == MODE_LISTS:
        return DENY, "not_listed"
    if lot["capacity"] and occupancy(lot)["used"] >= lot["capacity"]:
        return DENY, "full"
    return ALLOW, "open"


def _near(a, b):
    """Номера совпадают с точностью до одного символа (ошибка распознавания)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 4:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def find_session(lot_id, plate):
    key = plate_key(plate)
    sessions = open_sessions(lot_id)
    exact = next((s for s in sessions if s["plate_key"] == key), None)
    if exact:
        return exact
    near = [s for s in sessions if _near(s["plate_key"], key)]
    return near[0] if len(near) == 1 else None


def _record(db, lot_id, plate, direction, moment, decision, reason, device_id, event_id, username, session_id):
    return db.execute("""INSERT INTO parking_passes(lot_id,plate,plate_key,direction,time,decision,reason,device_id,
        event_id,username,session_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (lot_id, plate, plate_key(plate), direction, moment, decision, reason, device_id, event_id,
                       username, session_id)).lastrowid


def handle_plate(device_id, plate, index, moment=None, event_id=None, list_names=None, lots=None):
    """Обработать номер с камеры. Возвращает dict решения или None, если камера не на стоянке.

    {'lot', 'direction', 'decision', 'reason', 'barrier' (что открыть или None), 'session_id', 'duration'}
    """
    plate = (plate or "").strip().upper()
    lot, direction = lot_for_camera(device_id, lots)
    if lot is None or not plate_key(plate):
        return None
    stamp = (moment or datetime.now()).strftime(FMT)
    result = {"lot": lot, "direction": direction, "plate": plate, "session_id": None, "duration": None}
    with storage.database() as db:
        _ensure(db)
        if direction == "in":
            decision, reason = entry_decision(lot, plate, index)
            if decision == ALLOW and find_session(lot["id"], plate) is None:
                names = ", ".join((list_names or {}).get(i, str(i)) for i in index.get(plate_key(plate), []))
                result["session_id"] = db.execute(
                    "INSERT INTO parking_sessions(lot_id,plate,plate_key,entered_at,lists) VALUES(?,?,?,?,?)",
                    (lot["id"], plate, plate_key(plate), stamp, names)).lastrowid
            barrier = lot["barrier"] if decision == ALLOW else None
        else:
            session = find_session(lot["id"], plate)
            decision, reason = ALLOW, "exit" if session else "exit_unknown"
            if session:
                db.execute("UPDATE parking_sessions SET exited_at=? WHERE id=?", (stamp, session["id"]))
                result["session_id"] = session["id"]
                result["duration"] = duration_minutes(session["entered_at"], stamp)
            barrier = lot["exit_barrier"] or lot["barrier"]
        _record(db, lot["id"], plate, direction, stamp, decision, reason, device_id, event_id, "",
                result["session_id"])
    result.update(decision=decision, reason=reason, barrier=barrier or None)
    return result


def manual_pass(lot_id, plate, direction, username, moment=None):
    """Ручной въезд/выезд (оператор открыл шлагбаум или исправил номер)."""
    lot = get_lot(lot_id)
    if lot is None:
        raise ValueError("Стоянка не найдена")
    plate = (plate or "").strip().upper()
    if not plate_key(plate):
        raise ValueError("Введите номер")
    stamp = (moment or datetime.now()).strftime(FMT)
    session_id = None
    with storage.database() as db:
        _ensure(db)
        if direction == "in":
            if find_session(lot_id, plate) is None:
                session_id = db.execute(
                    "INSERT INTO parking_sessions(lot_id,plate,plate_key,entered_at) VALUES(?,?,?,?)",
                    (lot_id, plate, plate_key(plate), stamp)).lastrowid
        else:
            session = find_session(lot_id, plate)
            if session:
                db.execute("UPDATE parking_sessions SET exited_at=? WHERE id=?", (stamp, session["id"]))
                session_id = session["id"]
        _record(db, lot_id, plate, direction, stamp, ALLOW, "manual", None, None, username, session_id)
    return lot["barrier"] if direction == "in" else (lot["exit_barrier"] or lot["barrier"])


def close_session(session_id, username, moment=None):
    """Закрыть «зависшую» сессию (машина уехала, выезд не распознан)."""
    with storage.database() as db:
        _ensure(db)
        row = db.execute("SELECT * FROM parking_sessions WHERE id=?", (int(session_id),)).fetchone()
        if row is None or row["exited_at"]:
            return
        stamp = (moment or datetime.now()).strftime(FMT)
        db.execute("UPDATE parking_sessions SET exited_at=? WHERE id=?", (stamp, int(session_id)))
        _record(db, row["lot_id"], row["plate"], "out", stamp, ALLOW, "manual", None, None, username, session_id)


def duration_minutes(start, end):
    return max(0, int((datetime.strptime(end[:19], FMT) - datetime.strptime(start[:19], FMT)).total_seconds() // 60))


def overstays(lot, now=None):
    if not lot["overstay_hours"]:
        return []
    limit = ((now or datetime.now()) - timedelta(hours=lot["overstay_hours"])).strftime(FMT)
    return [s for s in open_sessions(lot["id"]) if s["entered_at"] < limit]


def load_passes(lot_id=None, start=None, end=None, text="", limit=1000):
    query, args = "SELECT * FROM parking_passes WHERE 1=1", []
    if lot_id:
        query += " AND lot_id=?"
        args.append(int(lot_id))
    if start:
        query += " AND time>=?"
        args.append(str(start))
    if end:
        query += " AND time<=?"
        args.append(str(end))
    if text:
        query += " AND plate_key LIKE ?"
        args.append(f"%{plate_key(text)}%")
    with storage.database() as db:
        _ensure(db)
        return [dict(r) for r in db.execute(query + " ORDER BY time DESC, id DESC LIMIT ?", args + [limit])]


def day_stats(lot_id, day=None):
    day = day or datetime.now().strftime("%Y-%m-%d")
    passes = load_passes(lot_id, day, day + " 23:59:59")
    return {"in": sum(1 for p in passes if p["direction"] == "in" and p["decision"] == ALLOW),
            "out": sum(1 for p in passes if p["direction"] == "out"),
            "denied": sum(1 for p in passes if p["decision"] == DENY)}


PASS_CSV = ("Время", "Стоянка", "Номер", "Направление", "Решение", "Причина", "Оператор")


def export_passes_csv(passes, lots, handle):
    names = {lot["id"]: lot["name"] for lot in lots}
    writer = csv.writer(handle, delimiter=";")
    writer.writerow(PASS_CSV)
    for p in passes:
        writer.writerow((p["time"], names.get(p["lot_id"], p["lot_id"]), p["plate"],
                         "Въезд" if p["direction"] == "in" else "Выезд",
                         "Пропущен" if p["decision"] == ALLOW else "Отказ", REASONS.get(p["reason"], p["reason"]),
                         p["username"]))
