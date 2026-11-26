"""Персоны, отделы, списки ТС, расписания и уровни доступа (без GTK).

Уровень доступа = набор дверей (контроллер + номер двери) + расписание.
Персона получает уровни лично или через отдел. «Применить» отправляет
персону и её карты на контроллеры через ISAPI (UserInfo / CardInfo).
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta

import sentinel_storage as storage
from sentinel_alarms import plate_key

DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MINUTES_PER_DAY = 24 * 60


# ---------------------------------------------------------------- расписания
def full_week():
    return [[[0, MINUTES_PER_DAY]] for _ in range(7)]


def weekdays_only(start=8 * 60, end=18 * 60):
    return [[[start, end]] if day < 5 else [] for day in range(7)]


def weekends_only():
    return [[[0, MINUTES_PER_DAY]] if day >= 5 else [] for day in range(7)]


BUILTIN_SCHEDULES = {"Весь день": full_week, "Будни 8:00–18:00": weekdays_only,
                     "Выходные": weekends_only}


def normalize_week(weekly):
    """7 дней, в каждом отсортированные непересекающиеся интервалы [start, end) в минутах."""
    if not isinstance(weekly, (list, tuple)) or len(weekly) != 7:
        raise ValueError("Недельное расписание должно содержать 7 дней")
    result = []
    for day in weekly:
        spans = []
        for span in day or []:
            start, end = int(span[0]), int(span[1])
            start, end = max(0, start), min(MINUTES_PER_DAY, end)
            if end > start:
                spans.append([start, end])
        spans.sort()
        merged = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        result.append(merged)
    return result


def week_from_cells(cells):
    """cells[day][hour] -> bool (сетка 7×24, как в редакторе расписания)."""
    week = []
    for day in range(7):
        spans = []
        for hour in range(24):
            if cells[day][hour]:
                if spans and spans[-1][1] == hour * 60:
                    spans[-1][1] = (hour + 1) * 60
                else:
                    spans.append([hour * 60, (hour + 1) * 60])
        week.append(spans)
    return week


def cells_from_week(weekly):
    cells = [[False] * 24 for _ in range(7)]
    for day, spans in enumerate(normalize_week(weekly)):
        for start, end in spans:
            for hour in range(24):
                if start < (hour + 1) * 60 and end > hour * 60:
                    cells[day][hour] = True
    return cells


def schedule_allows(weekly, moment):
    if weekly is None:
        return True
    day = moment.weekday()
    minute = moment.hour * 60 + moment.minute
    return any(start <= minute < end for start, end in normalize_week(weekly)[day])


def holiday_today(holidays, moment):
    day = moment.date().isoformat()
    return any(h["start_date"] <= day <= h["end_date"] for h in holidays)


def ensure_builtin_schedules():
    with storage.database() as db:
        for name, factory in BUILTIN_SCHEDULES.items():
            db.execute("INSERT OR IGNORE INTO schedules(name,kind,weekly) VALUES(?,?,?)",
                       (name, "template", json.dumps(factory())))


def load_schedules():
    ensure_builtin_schedules()
    with storage.database() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM schedules ORDER BY id")]
    for row in rows:
        try:
            row["weekly"] = normalize_week(json.loads(row["weekly"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            row["weekly"] = full_week()
    return rows


def get_schedule(schedule_id):
    return next((s["weekly"] for s in load_schedules() if s["id"] == schedule_id), None)


def save_schedule(name, weekly, schedule_id=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("Введите название расписания")
    payload = json.dumps(normalize_week(weekly))
    with storage.database() as db:
        if schedule_id is None:
            return db.execute("INSERT INTO schedules(name,kind,weekly) VALUES(?,?,?)",
                              (name, "access", payload)).lastrowid
        db.execute("UPDATE schedules SET name=?, weekly=? WHERE id=? AND kind!='template'",
                   (name, payload, schedule_id))
        return schedule_id


def delete_schedule(schedule_id):
    with storage.database() as db:
        db.execute("UPDATE access_levels SET schedule_id=NULL WHERE schedule_id=?", (schedule_id,))
        db.execute("UPDATE alarm_rules SET schedule_id=NULL WHERE schedule_id=?", (schedule_id,))
        db.execute("DELETE FROM schedules WHERE id=? AND kind!='template'", (schedule_id,))


# ---------------------------------------------------------------- отделы
def load_departments():
    with storage.database() as db:
        if not db.execute("SELECT 1 FROM departments LIMIT 1").fetchone():
            db.execute("INSERT INTO departments(name) VALUES('Все')")
        return [dict(r) for r in db.execute("SELECT * FROM departments ORDER BY id")]


def add_department(name, parent_id=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("Введите название отдела")
    with storage.database() as db:
        row = db.execute("SELECT id FROM departments WHERE name=?", (name,)).fetchone()
        if row:
            return row["id"]
        return db.execute("INSERT INTO departments(name,parent_id) VALUES(?,?)", (name, parent_id)).lastrowid


def rename_department(department_id, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Введите название отдела")
    with storage.database() as db:
        db.execute("UPDATE departments SET name=? WHERE id=?", (name, department_id))


def delete_department(department_id):
    with storage.database() as db:
        root = db.execute("SELECT MIN(id) FROM departments").fetchone()[0]
        if department_id == root:
            raise ValueError("Корневой отдел удалить нельзя")
        db.execute("UPDATE persons SET department_id=? WHERE department_id=?", (root, department_id))
        db.execute("UPDATE departments SET parent_id=NULL WHERE parent_id=?", (department_id,))
        db.execute("DELETE FROM department_access_levels WHERE department_id=?", (department_id,))
        db.execute("DELETE FROM departments WHERE id=?", (department_id,))


# ---------------------------------------------------------------- персоны
def _default_validity(now=None):
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    return now.strftime("%Y-%m-%d %H:%M"), now.replace(year=now.year + 10).strftime("%Y-%m-%d %H:%M")


def next_employee_no():
    with storage.database() as db:
        numbers = [r[0] for r in db.execute("SELECT employee_no FROM persons")]
    numeric = [int(n) for n in numbers if str(n).isdigit()]
    return str(max(numeric, default=0) + 1)


def _clean_cards(cards):
    result = []
    for card in cards or []:
        card = str(card).strip()
        if card and card not in result:
            if not card.isalnum() or len(card) > 32:
                raise ValueError(f"Некорректный номер карты: {card}")
            result.append(card)
    return result


def save_person(person, person_id=None):
    name = (person.get("name") or "").strip()
    if not name:
        raise ValueError("Введите имя персоны")
    employee_no = (str(person.get("employee_no") or "")).strip() or next_employee_no()
    if len(employee_no) > 32:
        raise ValueError("ID персоны не длиннее 32 символов")
    start, end = _default_validity()
    valid_from = person.get("valid_from") or start
    valid_to = person.get("valid_to") or end
    for value in (valid_from, valid_to):
        datetime.fromisoformat(value)
    if valid_to <= valid_from:
        raise ValueError("Окончание срока действия должно быть позже начала")
    pin = (person.get("pin") or "").strip()
    if pin and (not pin.isdigit() or not 4 <= len(pin) <= 8):
        raise ValueError("PIN-код: от 4 до 8 цифр")
    values = (employee_no, name, person.get("department_id"), person.get("gender", ""),
              person.get("phone", ""), person.get("email", ""), valid_from, valid_to,
              json.dumps(_clean_cards(person.get("cards"))), pin, person.get("remark", ""))
    with storage.database() as db:
        if person_id is None:
            return db.execute("""INSERT INTO persons(employee_no,name,department_id,gender,phone,email,valid_from,
                valid_to,cards,pin,remark) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", values).lastrowid
        db.execute("""UPDATE persons SET employee_no=?,name=?,department_id=?,gender=?,phone=?,email=?,valid_from=?,
            valid_to=?,cards=?,pin=?,remark=? WHERE id=?""", values + (person_id,))
        return person_id


def load_persons(department_id=None, text=""):
    query, args = "SELECT * FROM persons WHERE 1=1", []
    if department_id:
        query += " AND department_id=?"
        args.append(department_id)
    if text:
        query += " AND (name LIKE ? OR employee_no LIKE ? OR cards LIKE ? OR phone LIKE ?)"
        args.extend([f"%{text}%"] * 4)
    query += " ORDER BY id DESC"
    with storage.database() as db:
        rows = [dict(r) for r in db.execute(query, args)]
    for row in rows:
        try:
            row["cards"] = json.loads(row["cards"])
        except (TypeError, json.JSONDecodeError):
            row["cards"] = []
    return rows


def delete_persons(person_ids):
    ids = [int(i) for i in person_ids]
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    with storage.database() as db:
        db.execute(f"DELETE FROM person_access_levels WHERE person_id IN ({marks})", ids)
        db.execute(f"DELETE FROM access_apply_status WHERE person_id IN ({marks})", ids)
        db.execute(f"UPDATE vehicles SET person_id=NULL WHERE person_id IN ({marks})", ids)
        db.execute(f"DELETE FROM persons WHERE id IN ({marks})", ids)


PERSON_CSV = ("ID", "Имя", "Отдел", "Телефон", "Email", "Карты", "Начало", "Окончание")


def export_persons_csv(persons, departments, handle):
    names = {d["id"]: d["name"] for d in departments}
    writer = csv.writer(handle)
    writer.writerow(PERSON_CSV)
    for p in persons:
        writer.writerow([_safe(v) for v in (p["employee_no"], p["name"], names.get(p["department_id"], ""),
                                            p["phone"], p["email"], " ".join(p["cards"]),
                                            p["valid_from"], p["valid_to"])])


def import_persons_csv(handle):
    """Импорт из CSV с заголовком PERSON_CSV. Возвращает (добавлено, ошибки)."""
    reader = csv.DictReader(handle)
    added, errors = 0, []
    for line, row in enumerate(reader, start=2):
        try:
            department = (row.get("Отдел") or "").strip()
            department_id = add_department(department) if department else None
            existing = None
            employee_no = (row.get("ID") or "").strip().lstrip("'")
            if employee_no:
                with storage.database() as db:
                    found = db.execute("SELECT id FROM persons WHERE employee_no=?", (employee_no,)).fetchone()
                existing = found["id"] if found else None
            save_person({"employee_no": employee_no, "name": row.get("Имя", ""), "department_id": department_id,
                         "phone": row.get("Телефон", ""), "email": row.get("Email", ""),
                         "cards": (row.get("Карты") or "").split(), "valid_from": row.get("Начало") or None,
                         "valid_to": row.get("Окончание") or None}, existing)
            added += 1
        except (ValueError, KeyError) as exc:
            errors.append(f"строка {line}: {exc}")
    return added, errors


def _safe(value):
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


# ---------------------------------------------------------------- ТС
def load_vehicle_lists():
    with storage.database() as db:
        if not db.execute("SELECT 1 FROM vehicle_lists LIMIT 1").fetchone():
            db.execute("INSERT INTO vehicle_lists(name) VALUES('Основной список')")
        return [dict(r) for r in db.execute("SELECT * FROM vehicle_lists ORDER BY id")]


def add_vehicle_list(name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Введите название списка")
    with storage.database() as db:
        row = db.execute("SELECT id FROM vehicle_lists WHERE name=?", (name,)).fetchone()
        return row["id"] if row else db.execute("INSERT INTO vehicle_lists(name) VALUES(?)", (name,)).lastrowid


def delete_vehicle_list(list_id):
    with storage.database() as db:
        db.execute("DELETE FROM vehicles WHERE list_id=?", (list_id,))
        db.execute("DELETE FROM vehicle_lists WHERE id=?", (list_id,))


def save_vehicle(vehicle, vehicle_id=None):
    plate = (vehicle.get("plate") or "").strip().upper()
    key = plate_key(plate)
    if not key:
        raise ValueError("Введите номер ТС")
    if not vehicle.get("list_id"):
        raise ValueError("Выберите список ТС")
    values = (plate, key, vehicle["list_id"], vehicle.get("brand", ""), vehicle.get("vehicle_type", ""),
              vehicle.get("color", ""), vehicle.get("owner", ""), vehicle.get("person_id"),
              vehicle.get("valid_from", ""), vehicle.get("valid_to", ""), vehicle.get("remark", ""))
    with storage.database() as db:
        if vehicle_id is None:
            return db.execute("""INSERT INTO vehicles(plate,plate_key,list_id,brand,vehicle_type,color,owner,person_id,
                valid_from,valid_to,remark) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", values).lastrowid
        db.execute("""UPDATE vehicles SET plate=?,plate_key=?,list_id=?,brand=?,vehicle_type=?,color=?,owner=?,
            person_id=?,valid_from=?,valid_to=?,remark=? WHERE id=?""", values + (vehicle_id,))
        return vehicle_id


def load_vehicles(list_id=None, text=""):
    query = """SELECT v.*, p.name AS person_name, l.name AS list_name FROM vehicles v
        LEFT JOIN persons p ON p.id=v.person_id JOIN vehicle_lists l ON l.id=v.list_id WHERE 1=1"""
    args = []
    if list_id:
        query += " AND v.list_id=?"
        args.append(list_id)
    if text:
        query += " AND (v.plate LIKE ? OR v.owner LIKE ? OR v.brand LIKE ?)"
        args.extend([f"%{text}%"] * 3)
    with storage.database() as db:
        return [dict(r) for r in db.execute(query + " ORDER BY v.plate", args)]


def move_vehicles(vehicle_ids, list_id):
    with storage.database() as db:
        for vid in vehicle_ids:
            row = db.execute("SELECT plate_key FROM vehicles WHERE id=?", (vid,)).fetchone()
            if not row:
                continue
            clash = db.execute("SELECT id FROM vehicles WHERE plate_key=? AND list_id=? AND id!=?",
                               (row["plate_key"], list_id, vid)).fetchone()
            if clash:
                db.execute("DELETE FROM vehicles WHERE id=?", (vid,))
            else:
                db.execute("UPDATE vehicles SET list_id=? WHERE id=?", (list_id, vid))


def delete_vehicles(vehicle_ids):
    with storage.database() as db:
        db.executemany("DELETE FROM vehicles WHERE id=?", [(int(i),) for i in vehicle_ids])


def vehicle_index(now=None):
    """{plate_key: [list_id]} для действующих номеров."""
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    index = {}
    with storage.database() as db:
        for row in db.execute("SELECT plate_key,list_id,valid_from,valid_to FROM vehicles"):
            if row["valid_from"] and stamp < row["valid_from"]:
                continue
            if row["valid_to"] and stamp > row["valid_to"]:
                continue
            index.setdefault(row["plate_key"], []).append(row["list_id"])
    return index


VEHICLE_CSV = ("Номер", "Список", "Марка", "Тип", "Цвет", "Владелец")


def export_vehicles_csv(vehicles, handle):
    writer = csv.writer(handle)
    writer.writerow(VEHICLE_CSV)
    for v in vehicles:
        writer.writerow([_safe(x) for x in (v["plate"], v["list_name"], v["brand"], v["vehicle_type"],
                                            v["color"], v["owner"])])


def import_vehicles_csv(handle, default_list_id):
    reader = csv.DictReader(handle)
    added, errors = 0, []
    for line, row in enumerate(reader, start=2):
        try:
            list_name = (row.get("Список") or "").strip()
            list_id = add_vehicle_list(list_name) if list_name else default_list_id
            save_vehicle({"plate": row.get("Номер", ""), "list_id": list_id, "brand": row.get("Марка", ""),
                          "vehicle_type": row.get("Тип", ""), "color": row.get("Цвет", ""),
                          "owner": row.get("Владелец", "")})
            added += 1
        except (ValueError, KeyError) as exc:
            errors.append(f"строка {line}: {exc}")
        except Exception as exc:  # sqlite IntegrityError — дубликат номера в списке
            errors.append(f"строка {line}: {exc}")
    return added, errors


# ---------------------------------------------------------------- уровни доступа
def load_access_levels():
    with storage.database() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM access_levels ORDER BY name")]
    for row in rows:
        try:
            row["doors"] = json.loads(row["doors"])
        except (TypeError, json.JSONDecodeError):
            row["doors"] = []
    return rows


def save_access_level(name, doors, schedule_id=None, level_id=None, remark=""):
    name = (name or "").strip()
    if not name:
        raise ValueError("Введите название уровня доступа")
    clean = []
    for door in doors or []:
        item = {"device_id": int(door["device_id"]), "door": max(1, int(door.get("door", 1)))}
        if item not in clean:
            clean.append(item)
    with storage.database() as db:
        if level_id is None:
            return db.execute("INSERT INTO access_levels(name,schedule_id,doors,remark) VALUES(?,?,?,?)",
                              (name, schedule_id, json.dumps(clean), remark)).lastrowid
        db.execute("UPDATE access_levels SET name=?,schedule_id=?,doors=?,remark=? WHERE id=?",
                   (name, schedule_id, json.dumps(clean), remark, level_id))
    mark_pending(level_ids=[level_id])
    return level_id


def delete_access_level(level_id):
    with storage.database() as db:
        db.execute("DELETE FROM person_access_levels WHERE level_id=?", (level_id,))
        db.execute("DELETE FROM department_access_levels WHERE level_id=?", (level_id,))
        db.execute("DELETE FROM access_levels WHERE id=?", (level_id,))


def assign_levels(person_ids=(), department_ids=(), level_ids=(), replace=False):
    with storage.database() as db:
        for pid in person_ids:
            if replace:
                db.execute("DELETE FROM person_access_levels WHERE person_id=?", (pid,))
            for lid in level_ids:
                db.execute("INSERT OR IGNORE INTO person_access_levels(person_id,level_id) VALUES(?,?)", (pid, lid))
            db.execute("UPDATE access_apply_status SET status='pending' WHERE person_id=?", (pid,))
        for did in department_ids:
            if replace:
                db.execute("DELETE FROM department_access_levels WHERE department_id=?", (did,))
            for lid in level_ids:
                db.execute("INSERT OR IGNORE INTO department_access_levels(department_id,level_id) VALUES(?,?)",
                           (did, lid))
    mark_pending(person_ids, department_ids)


def clear_levels(person_ids=(), department_ids=()):
    with storage.database() as db:
        for pid in person_ids:
            db.execute("DELETE FROM person_access_levels WHERE person_id=?", (pid,))
        for did in department_ids:
            db.execute("DELETE FROM department_access_levels WHERE department_id=?", (did,))


def effective_levels(person):
    """Уровни персоны: личные + уровни её отдела."""
    with storage.database() as db:
        ids = {r[0] for r in db.execute("SELECT level_id FROM person_access_levels WHERE person_id=?",
                                        (person["id"],))}
        if person.get("department_id"):
            ids |= {r[0] for r in db.execute(
                "SELECT level_id FROM department_access_levels WHERE department_id=?", (person["department_id"],))}
    return [lvl for lvl in load_access_levels() if lvl["id"] in ids]


def person_doors(person):
    """{device_id: set(door)} — куда персона должна иметь доступ."""
    doors = {}
    for level in effective_levels(person):
        for door in level["doors"]:
            doors.setdefault(door["device_id"], set()).add(door["door"])
    return doors


def apply_plan(persons):
    """[(person, device_id, doors)] — что отправить на контроллеры при «Применить всё»."""
    plan = []
    for person in persons:
        for device_id, doors in sorted(person_doors(person).items()):
            plan.append((person, device_id, sorted(doors)))
    return plan


def set_apply_status(person_id, device_id, status, message=""):
    with storage.database() as db:
        db.execute("""INSERT INTO access_apply_status(person_id,device_id,status,message,updated_at)
            VALUES(?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(person_id,device_id) DO UPDATE
            SET status=excluded.status, message=excluded.message, updated_at=excluded.updated_at""",
                   (person_id, device_id, status, message))


def mark_pending(person_ids=(), department_ids=(), level_ids=()):
    """Отметить «не применено» у персон, чьи права изменились (индикатор и «Применить изменения»)."""
    ids = {int(i) for i in person_ids}
    with storage.database() as db:
        for did in department_ids:
            ids |= {r[0] for r in db.execute("SELECT id FROM persons WHERE department_id=?", (did,))}
        for lid in level_ids:
            ids |= {r[0] for r in db.execute("SELECT person_id FROM person_access_levels WHERE level_id=?", (lid,))}
            ids |= {r[0] for r in db.execute("""SELECT p.id FROM persons p JOIN department_access_levels d
                ON d.department_id=p.department_id WHERE d.level_id=?""", (lid,))}
    if not ids:
        return 0
    persons = [p for p in load_persons() if p["id"] in ids]
    for person, device_id, _doors in apply_plan(persons):
        set_apply_status(person["id"], device_id, "pending", "права изменены, не применено")
    return len(persons)


def pending_persons():
    """Персоны, у которых есть неприменённые или неудачные записи."""
    with storage.database() as db:
        ids = {r[0] for r in db.execute("SELECT DISTINCT person_id FROM access_apply_status WHERE status!='applied'")}
    return [p for p in load_persons() if p["id"] in ids]


def apply_summary():
    with storage.database() as db:
        rows = db.execute("SELECT status, COUNT(*) AS n FROM access_apply_status GROUP BY status").fetchall()
        authorized = db.execute("""SELECT COUNT(DISTINCT p.id) FROM persons p WHERE
            EXISTS(SELECT 1 FROM person_access_levels a WHERE a.person_id=p.id) OR
            EXISTS(SELECT 1 FROM department_access_levels d WHERE d.department_id=p.department_id)""").fetchone()[0]
    counts = {r["status"]: r["n"] for r in rows}
    return {"authorized": authorized, "applied": counts.get("applied", 0),
            "failed": counts.get("failed", 0), "pending": counts.get("pending", 0)}


def _isapi_time(value):
    return datetime.fromisoformat(value).strftime("%Y-%m-%dT%H:%M:%S")


def user_info_payload(person, doors):
    """JSON для PUT /ISAPI/AccessControl/UserInfo/SetUp?format=json."""
    right_plan = [{"doorNo": int(door), "planTemplateNo": "1"} for door in doors]
    user = {"employeeNo": str(person["employee_no"]), "name": person["name"][:32], "userType": "normal",
            "Valid": {"enable": True, "beginTime": _isapi_time(person["valid_from"]),
                      "endTime": _isapi_time(person["valid_to"]), "timeType": "local"},
            "doorRight": ",".join(str(d) for d in doors) or "1", "RightPlan": right_plan}
    if person.get("pin"):
        user["password"] = person["pin"]
    return {"UserInfo": user}


def card_payload(person, card):
    """JSON для PUT /ISAPI/AccessControl/CardInfo/SetUp?format=json."""
    return {"CardInfo": {"employeeNo": str(person["employee_no"]), "cardNo": str(card), "cardType": "normalCard"}}


def user_delete_payload(employee_nos):
    return {"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": str(n)} for n in employee_nos]}}


DOOR_COMMANDS = {"open": "open", "close": "close", "always_open": "alwaysOpen", "always_close": "alwaysClose",
                 "resume": "resume"}
DOOR_TITLES = {"open": "Открыть", "close": "Закрыть", "always_open": "Всегда открыта",
               "always_close": "Всегда закрыта", "resume": "Обычный режим"}


def door_command_xml(command):
    if command not in DOOR_COMMANDS:
        raise ValueError("Неизвестная команда двери")
    return f"<RemoteControlDoor><cmd>{DOOR_COMMANDS[command]}</cmd></RemoteControlDoor>"


def parse_door_status(payload):
    """Состояние дверей из /ISAPI/AccessControl/AcsWorkStatus?format=json -> {door: state}."""
    try:
        data = json.loads(payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload)
    except (json.JSONDecodeError, AttributeError):
        return {}
    status = data.get("AcsWorkStatus", data)
    locks = status.get("doorLockStatus") or []
    names = {0: "Закрыта", 1: "Открыта", 2: "Короткое замыкание", 3: "Обрыв", 4: "Неизвестно"}
    return {index + 1: names.get(value, "Неизвестно") for index, value in enumerate(locks)}


def door_counters(states):
    """Счётчики для полосы над видом с дверями (как в HikCentral)."""
    counters = {"Открыта": 0, "Закрыта": 0, "Неизвестно": 0}
    for value in states.values():
        counters[value if value in counters else "Неизвестно"] += 1
    return counters


def expiring_persons(days=7, now=None):
    now = now or datetime.now()
    limit = (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    stamp = now.strftime("%Y-%m-%d %H:%M")
    return [p for p in load_persons() if stamp <= p["valid_to"] <= limit]
