"""Учёт рабочего времени (без GTK).

Источник — проходы персон через двери, отмеченные как точки учёта (вход, выход
или оба направления). Если точки не выбраны, считаются все двери. Проходы
берутся из журнала событий Sentinel; пропущенные (клиент был выключен) можно
догрузить из памяти контроллера.

Для каждой персоны и дня: первый вход, последний выход, отработано (минус
перерыв), опоздание, ранний уход, переработка и статус. Смена назначается
персоне, иначе её отделу, иначе берётся смена по умолчанию. Ночная смена
(конец раньше начала) заканчивается на следующий день. Праздники и
нерабочие дни смены — выходные. Ручные корректировки (забыл отметиться,
отпуск, больничный) перекрывают расчёт и попадают в журнал.
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta

import sentinel_access_rules as rules
import sentinel_people as people
import sentinel_storage as storage

SETTING = "attendance"
DEFAULT_SHIFT = {"id": 1, "name": "Дневная 9:00–18:00", "start": "09:00", "end": "18:00", "break": 60,
                 "late": 10, "early": 10, "days": [0, 1, 2, 3, 4]}
DEFAULT_CONFIG = {"shifts": [DEFAULT_SHIFT], "default_shift": 1, "department_shifts": {}, "person_shifts": {},
                  "points": {}}
DIRECTIONS = {"both": "Вход и выход", "in": "Только вход", "out": "Только выход"}
LEAVE_KINDS = {"vacation": "Отпуск", "sick": "Больничный", "trip": "Командировка", "off": "Отгул"}
STATUS_TITLES = {"ok": "Норма", "late": "Опоздание", "early": "Ранний уход", "late_early": "Опоздание и уход",
                 "no_out": "Нет выхода", "absent": "Отсутствие", "day_off": "Выходной", "leave": "Отсутствие (уваж.)",
                 "extra": "Работа в выходной", "future": ""}
WINDOW_BEFORE = timedelta(hours=4)
WINDOW_AFTER = timedelta(hours=6)


def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS attendance_corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER NOT NULL, day TEXT NOT NULL,
        check_in TEXT NOT NULL DEFAULT '', check_out TEXT NOT NULL DEFAULT '', leave TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(person_id, day))""")


# ------------------------------------------------------------ настройки
def load_config():
    config = storage.get_json_setting(SETTING, None) or {}
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update({k: v for k, v in config.items() if k in merged})
    if not merged["shifts"]:
        merged["shifts"] = [dict(DEFAULT_SHIFT)]
    return merged


def save_config(config):
    storage.set_json_setting(SETTING, config)


def _minutes(text):
    hours, minutes = str(text).split(":")
    value = int(hours) * 60 + int(minutes)
    if not 0 <= value < 24 * 60 or not 0 <= int(minutes) < 60:
        raise ValueError
    return value


def validate_shift(shift):
    name = (shift.get("name") or "").strip()
    if not name:
        raise ValueError("Введите название смены")
    try:
        start, end = _minutes(shift["start"]), _minutes(shift["end"])
    except (KeyError, ValueError):
        raise ValueError("Начало и конец смены в формате ЧЧ:ММ") from None
    if start == end:
        raise ValueError("Начало и конец смены совпадают")
    length = (end - start) % (24 * 60)
    pause = int(shift.get("break") or 0)
    if not 0 <= pause < length:
        raise ValueError("Перерыв должен быть короче смены")
    days = sorted({int(d) for d in shift.get("days", []) if 0 <= int(d) <= 6})
    if not days:
        raise ValueError("Выберите рабочие дни")
    return {"id": int(shift.get("id") or 0), "name": name, "start": shift["start"], "end": shift["end"],
            "break": pause, "late": max(0, int(shift.get("late") or 0)), "early": max(0, int(shift.get("early") or 0)),
            "days": days}


def save_shift(shift):
    config = load_config()
    clean = validate_shift(shift)
    if not clean["id"]:
        clean["id"] = max((s["id"] for s in config["shifts"]), default=0) + 1
        config["shifts"].append(clean)
    else:
        config["shifts"] = [clean if s["id"] == clean["id"] else s for s in config["shifts"]]
    save_config(config)
    return clean["id"]


def delete_shift(shift_id):
    config = load_config()
    if len(config["shifts"]) <= 1:
        raise ValueError("Нужна хотя бы одна смена")
    config["shifts"] = [s for s in config["shifts"] if s["id"] != shift_id]
    if config["default_shift"] == shift_id:
        config["default_shift"] = config["shifts"][0]["id"]
    for key in ("department_shifts", "person_shifts"):
        config[key] = {k: v for k, v in config[key].items() if v != shift_id}
    save_config(config)


def shift_for(person, config=None):
    config = config or load_config()
    shifts = {s["id"]: s for s in config["shifts"]}
    for shift_id in (config["person_shifts"].get(str(person["id"])),
                     config["department_shifts"].get(str(person.get("department_id") or "")),
                     config["default_shift"]):
        if shift_id in shifts:
            return shifts[shift_id]
    return config["shifts"][0]


def point_key(device_id, door):
    return f"{int(device_id)}:{int(door)}"


def point_direction(config, device_id, door):
    """Направление точки учёта или None, если дверь не участвует."""
    points = config["points"]
    if not points:
        return "both"
    return points.get(point_key(device_id, door))


# ------------------------------------------------------------ проходы
def _local(value):
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def load_passes(start, end, config=None):
    """{employee_no: [(время, направление, device_id, door)]} за период (успешные проходы через точки учёта)."""
    config = config or load_config()
    lo = (start - timedelta(days=1)).isoformat()
    hi = (end + timedelta(days=1)).isoformat()
    with storage.database() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM events WHERE event_type='access' AND time>=? AND time<=? ORDER BY time",
            (lo[:10], hi[:10] + "~"))]
    result = {}
    for row in rows:
        if rules.event_outcome(row) != "granted":
            continue
        who = rules.event_person(row)
        try:
            moment = _local(row["time"])
        except ValueError:
            continue
        if not who or not start <= moment <= end:
            continue
        door = rules.event_door(row) or 1
        direction = point_direction(config, row.get("device_id") or 0, door)
        if direction is None:
            continue
        result.setdefault(who, []).append((moment, direction, row.get("device_id"), door))
    for items in result.values():
        items.sort(key=lambda item: item[0])
    return result


def import_controller_events(device, records):
    """Сохранить события из /ISAPI/AccessControl/AcsEvent в журнал (без дублей). Возвращает число новых."""
    added = 0
    with storage.database() as db:
        for record in records:
            when = record.get("time")
            if not when:
                continue
            who = str(record.get("employeeNoString") or "")
            details = {k: record[k] for k in ("employeeNoString", "cardNo", "doorNo", "cardReaderNo", "major",
                                               "minor", "name", "attendanceStatus", "currentVerifyMode")
                       if record.get(k) not in (None, "")}
            details["source"] = "controller"
            exists = db.execute("""SELECT 1 FROM events WHERE device_id=? AND time=? AND event_type='access'
                AND details LIKE ?""", (device["id"], when, f'%"employeeNoString": "{who}"%' if who else "%")).fetchone()
            if exists:
                continue
            db.execute("""INSERT INTO events(time,device_id,device_name,channel,event_type,category,state,details)
                VALUES(?,?,?,?,?,?,?,?)""", (when, device["id"], device.get("host_name") or device.get("name", ""),
                                             str(record.get("doorNo") or ""), "access", "event", "active",
                                             json.dumps(details, ensure_ascii=False)))
            added += 1
    return added


def acs_event_query(start, end, position=0, size=100):
    return {"AcsEventCond": {"searchID": f"sentinel-{start:%Y%m%d%H%M}", "searchResultPosition": position,
                             "maxResults": size, "major": 5, "minor": 0,
                             "startTime": start.astimezone().isoformat(timespec="seconds"),
                             "endTime": end.astimezone().isoformat(timespec="seconds")}}


# ------------------------------------------------------------ корректировки
def load_corrections(start_day=None, end_day=None):
    query, args = "SELECT * FROM attendance_corrections WHERE 1=1", []
    if start_day:
        query += " AND day>=?"
        args.append(str(start_day))
    if end_day:
        query += " AND day<=?"
        args.append(str(end_day))
    with storage.database() as db:
        _ensure(db)
        return {(r["person_id"], r["day"]): dict(r) for r in db.execute(query, args)}


def save_correction(person_id, day, check_in="", check_out="", leave="", reason="", username=""):
    day = date.fromisoformat(str(day)).isoformat()
    for value in (check_in, check_out):
        if value:
            _minutes(value)
    if leave and leave not in LEAVE_KINDS:
        raise ValueError("Неизвестный вид отсутствия")
    if not (check_in or check_out or leave):
        raise ValueError("Укажите время входа/выхода или вид отсутствия")
    if not (reason or "").strip():
        raise ValueError("Укажите причину корректировки")
    with storage.database() as db:
        _ensure(db)
        db.execute("""INSERT INTO attendance_corrections(person_id,day,check_in,check_out,leave,reason,username)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(person_id, day) DO UPDATE SET check_in=excluded.check_in,
            check_out=excluded.check_out, leave=excluded.leave, reason=excluded.reason,
            username=excluded.username, created_at=CURRENT_TIMESTAMP""",
                   (int(person_id), day, check_in, check_out, leave, reason.strip(), username))


def delete_correction(person_id, day):
    with storage.database() as db:
        _ensure(db)
        db.execute("DELETE FROM attendance_corrections WHERE person_id=? AND day=?", (int(person_id), str(day)))


# ------------------------------------------------------------ расчёт
def shift_bounds(shift, day):
    start = datetime.combine(day, datetime.min.time()) + timedelta(minutes=_minutes(shift["start"]))
    end = datetime.combine(day, datetime.min.time()) + timedelta(minutes=_minutes(shift["end"]))
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _at(day, text, after=None):
    moment = datetime.combine(day, datetime.min.time()) + timedelta(minutes=_minutes(text))
    if after is not None and moment < after:
        moment += timedelta(days=1)
    return moment


def compute_day(person, day, passes, shift, holidays=(), correction=None, now=None):
    """Итог дня одной персоны (dict)."""
    now = now or datetime.now()
    start, end = shift_bounds(shift, day)
    workday = day.weekday() in shift["days"] and not people.holiday_today(
        list(holidays), datetime.combine(day, datetime.min.time()))
    window = [p for p in passes if start - WINDOW_BEFORE <= p[0] <= end + WINDOW_AFTER]
    ins = [p[0] for p in window if p[1] in ("in", "both")]
    outs = [p[0] for p in window if p[1] in ("out", "both")]
    first_in = ins[0] if ins else None
    last_out = next((t for t in reversed(outs) if first_in is None or t > first_in), None)
    record = {"person_id": person["id"], "employee_no": person["employee_no"], "name": person["name"],
              "day": day.isoformat(), "shift": shift["name"], "workday": workday, "passes": len(window),
              "first_in": first_in, "last_out": last_out, "worked": 0, "late": 0, "early": 0, "overtime": 0,
              "status": "", "corrected": False, "leave": "", "note": ""}
    if correction:
        record["corrected"] = True
        record["note"] = correction["reason"]
        if correction["leave"]:
            record.update(leave=correction["leave"], status="leave")
            return record
        if correction["check_in"]:
            record["first_in"] = first_in = _at(day, correction["check_in"])
            if start - first_in > timedelta(hours=12):
                record["first_in"] = first_in = first_in + timedelta(days=1)
        if correction["check_out"]:
            record["last_out"] = last_out = _at(day, correction["check_out"], first_in or start)
    if first_in is None and last_out is None:
        if not workday:
            record["status"] = "day_off"
        elif start > now:
            record["status"] = "future"
        else:
            record["status"] = "absent"
        return record
    shift_length = (end - start).total_seconds() // 60 - shift["break"]
    if first_in is not None and last_out is not None:
        span = (last_out - first_in).total_seconds() // 60
        worked = span - shift["break"] if span > shift["break"] * 2 else span
        record["worked"] = int(max(0, worked))
        record["overtime"] = int(max(0, record["worked"] - shift_length)) if workday else record["worked"]
    if not workday:
        record["status"] = "extra"
        return record
    if first_in is not None:
        late = (first_in - start).total_seconds() // 60
        record["late"] = int(late) if late > shift["late"] else 0
    if last_out is not None:
        early = (end - last_out).total_seconds() // 60
        record["early"] = int(early) if early > shift["early"] else 0
    if last_out is None:
        record["status"] = "no_out" if end + WINDOW_AFTER < now else ("late" if record["late"] else "ok")
    elif record["late"] and record["early"]:
        record["status"] = "late_early"
    elif record["late"]:
        record["status"] = "late"
    elif record["early"]:
        record["status"] = "early"
    else:
        record["status"] = "ok"
    return record


def compute(persons, start_day, end_day, now=None):
    """Записи по всем персонам и дням периода (включительно)."""
    if end_day < start_day:
        raise ValueError("Конец периода раньше начала")
    if (end_day - start_day).days > 92:
        raise ValueError("Период не больше 3 месяцев")
    config = load_config()
    lo = datetime.combine(start_day, datetime.min.time()) - WINDOW_BEFORE
    hi = datetime.combine(end_day, datetime.max.time()) + timedelta(days=1) + WINDOW_AFTER
    passes = load_passes(lo, hi, config)
    corrections = load_corrections(start_day, end_day)
    with storage.database() as db:
        holidays = [dict(r) for r in db.execute("SELECT * FROM holidays")]
    records = []
    for person in persons:
        shift = shift_for(person, config)
        mine = passes.get(str(person["employee_no"]), [])
        day = start_day
        while day <= end_day:
            records.append(compute_day(person, day, mine, shift, holidays,
                                       corrections.get((person["id"], day.isoformat())), now))
            day += timedelta(days=1)
    return records


def summarize(records):
    """Итоги по персонам за период."""
    result = {}
    for r in records:
        item = result.setdefault(r["person_id"], {
            "person_id": r["person_id"], "employee_no": r["employee_no"], "name": r["name"], "workdays": 0,
            "present": 0, "absent": 0, "leave": 0, "late_count": 0, "late_minutes": 0, "early_count": 0,
            "early_minutes": 0, "no_out": 0, "worked": 0, "overtime": 0})
        if r["workday"] and r["status"] != "future":
            item["workdays"] += 1
        if r["status"] in ("ok", "late", "early", "late_early", "no_out", "extra"):
            item["present"] += 1
        item["absent"] += r["status"] == "absent"
        item["leave"] += r["status"] == "leave"
        item["no_out"] += r["status"] == "no_out"
        item["late_count"] += bool(r["late"])
        item["late_minutes"] += r["late"]
        item["early_count"] += bool(r["early"])
        item["early_minutes"] += r["early"]
        item["worked"] += r["worked"]
        item["overtime"] += r["overtime"]
    return sorted(result.values(), key=lambda i: i["name"])


def hm(minutes):
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"


def _t(value):
    return value.strftime("%H:%M") if value else ""


DAILY_CSV = ("Дата", "ID", "Имя", "Смена", "Вход", "Выход", "Отработано", "Опоздание, мин", "Ранний уход, мин",
             "Переработка", "Статус", "Корректировка")
SUMMARY_CSV = ("ID", "Имя", "Рабочих дней", "Присутствовал", "Отсутствовал", "Уважительно", "Опозданий",
               "Опоздания, мин", "Ранних уходов", "Ранний уход, мин", "Без выхода", "Отработано", "Переработка")


def status_text(record):
    if record["status"] == "leave":
        return LEAVE_KINDS.get(record["leave"], "Отсутствие")
    return STATUS_TITLES.get(record["status"], record["status"])


def daily_row(r):
    return (r["day"], r["employee_no"], r["name"], r["shift"], _t(r["first_in"]), _t(r["last_out"]),
            hm(r["worked"]) if r["worked"] else "", r["late"] or "", r["early"] or "",
            hm(r["overtime"]) if r["overtime"] else "", status_text(r), r["note"] if r["corrected"] else "")


def summary_row(s):
    return (s["employee_no"], s["name"], s["workdays"], s["present"], s["absent"], s["leave"], s["late_count"],
            s["late_minutes"], s["early_count"], s["early_minutes"], s["no_out"], hm(s["worked"]), hm(s["overtime"]))


def export_csv(records, handle, summary=False):
    writer = csv.writer(handle, delimiter=";")
    if summary:
        writer.writerow(SUMMARY_CSV)
        writer.writerows(summary_row(s) for s in summarize(records))
    else:
        writer.writerow(DAILY_CSV)
        writer.writerows(daily_row(r) for r in records)
