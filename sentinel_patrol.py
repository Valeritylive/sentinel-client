"""Патрулирование (без GTK): маршруты из контрольных точек, расписание обходов,
отметки охранников и пропущенные точки.

Контрольная точка — дверь или считыватель контроллера доступа: охранник
отмечается картой (или лицом) на считывателе, отметка приходит событием
контроля доступа. Маршрут — точки по порядку с допустимым временем от начала
обхода, список охранников и время начала обходов по дням недели.

Для каждого обхода: каждая точка «вовремя», «рано», «поздно», «пропущена» или
«ожидается». Обход «выполнен», если все точки отмечены вовремя, «с
нарушениями», если что-то пропущено или не вовремя, «идёт», пока не прошло
время последней точки.
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta

import sentinel_access_rules as ar
import sentinel_storage as storage

ON_TIME, EARLY, LATE, MISSED, WAITING = "on_time", "early", "late", "missed", "waiting"
POINT_TITLES = {ON_TIME: "Вовремя", EARLY: "Рано", LATE: "Поздно", MISSED: "Пропущена", WAITING: "Ожидается"}
DONE, VIOLATION, RUNNING, PLANNED = "done", "violation", "running", "planned"
RUN_TITLES = {DONE: "Выполнен", VIOLATION: "С нарушениями", RUNNING: "Идёт", PLANNED: "Запланирован"}
FMT = "%Y-%m-%d %H:%M"


def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS patrol_routes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
        points TEXT NOT NULL DEFAULT '[]', guards TEXT NOT NULL DEFAULT '[]', starts TEXT NOT NULL DEFAULT '[]',
        days TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]', tolerance INTEGER NOT NULL DEFAULT 5)""")
    db.execute("""CREATE TABLE IF NOT EXISTS patrol_notified (
        route_id INTEGER NOT NULL, run_start TEXT NOT NULL, point_index INTEGER NOT NULL,
        PRIMARY KEY(route_id, run_start, point_index))""")


def _minutes(text):
    hours, minutes = str(text).strip().split(":")
    value = int(hours) * 60 + int(minutes)
    if not 0 <= int(minutes) < 60 or not 0 <= value < 24 * 60:
        raise ValueError
    return value


def validate_route(route):
    name = (route.get("name") or "").strip()
    if not name:
        raise ValueError("Введите название маршрута")
    points = []
    previous = -1
    for point in route.get("points") or []:
        offset = int(point.get("offset") or 0)
        if offset < previous:
            raise ValueError("Время точек должно расти по порядку маршрута")
        previous = offset
        points.append({"device_id": int(point["device_id"]), "door": max(1, int(point.get("door") or 1)),
                       "name": (point.get("name") or "").strip() or f"Точка {len(points) + 1}", "offset": offset})
    if len(points) < 2:
        raise ValueError("В маршруте должно быть минимум две контрольные точки")
    starts = []
    for start in route.get("starts") or []:
        try:
            starts.append(f"{_minutes(start) // 60:02d}:{_minutes(start) % 60:02d}")
        except (ValueError, AttributeError):
            raise ValueError(f"Время начала «{start}» в формате ЧЧ:ММ") from None
    if not starts:
        raise ValueError("Укажите время начала обходов")
    days = sorted({int(d) for d in route.get("days", range(7)) if 0 <= int(d) <= 6})
    if not days:
        raise ValueError("Выберите дни обходов")
    guards = sorted({int(g) for g in route.get("guards") or []})
    if not guards:
        raise ValueError("Выберите охранников маршрута")
    return {"name": name, "enabled": bool(route.get("enabled", True)), "points": points, "guards": guards,
            "starts": sorted(set(starts)), "days": days, "tolerance": max(1, int(route.get("tolerance") or 5))}


def _row(r):
    item = dict(r)
    for key in ("points", "guards", "starts", "days"):
        try:
            item[key] = json.loads(item[key])
        except (TypeError, json.JSONDecodeError):
            item[key] = []
    item["enabled"] = bool(item["enabled"])
    return item


def load_routes():
    with storage.database() as db:
        _ensure(db)
        return [_row(r) for r in db.execute("SELECT * FROM patrol_routes ORDER BY name")]


def get_route(route_id):
    return next((r for r in load_routes() if r["id"] == int(route_id)), None)


def save_route(route, route_id=None):
    clean = validate_route(route)
    values = (clean["name"], int(clean["enabled"]), json.dumps(clean["points"], ensure_ascii=False),
              json.dumps(clean["guards"]), json.dumps(clean["starts"]), json.dumps(clean["days"]),
              clean["tolerance"])
    with storage.database() as db:
        _ensure(db)
        if route_id is None:
            return db.execute("""INSERT INTO patrol_routes(name,enabled,points,guards,starts,days,tolerance)
                VALUES(?,?,?,?,?,?,?)""", values).lastrowid
        db.execute("""UPDATE patrol_routes SET name=?,enabled=?,points=?,guards=?,starts=?,days=?,tolerance=?
            WHERE id=?""", values + (int(route_id),))
        return route_id


def delete_route(route_id):
    with storage.database() as db:
        _ensure(db)
        db.execute("DELETE FROM patrol_routes WHERE id=?", (int(route_id),))
        db.execute("DELETE FROM patrol_notified WHERE route_id=?", (int(route_id),))


# ------------------------------------------------------------ отметки и обходы
def _local(value):
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def load_checkins(start, end, employees):
    """[(время, employee_no, device_id, door)] успешных отметок охранников за период."""
    wanted = set(employees.values())
    with storage.database() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM events WHERE event_type='access' AND time>=? AND time<=? ORDER BY time",
            ((start - timedelta(days=1)).isoformat()[:10], (end + timedelta(days=1)).isoformat()[:10] + "~"))]
    result = []
    for row in rows:
        who = ar.event_person(row)
        if who not in wanted or ar.event_outcome(row) != "granted":
            continue
        try:
            moment = _local(row["time"])
        except ValueError:
            continue
        if start <= moment <= end:
            result.append((moment, who, row.get("device_id"), ar.event_door(row) or 1))
    return result


def scheduled_runs(route, day):
    if day.weekday() not in route["days"]:
        return []
    return [datetime.combine(day, datetime.min.time()) + timedelta(minutes=_minutes(s)) for s in route["starts"]]


def evaluate_run(route, start, checkins, employees, now=None):
    """Итог одного обхода, начавшегося в start."""
    now = now or datetime.now()
    tolerance = timedelta(minutes=route["tolerance"])
    guards = {employees.get(g) for g in route["guards"]}
    last = route["points"][-1]["offset"]
    window_end = start + timedelta(minutes=last) + tolerance * 3
    mine = [c for c in checkins if c[1] in guards and start - tolerance * 3 <= c[0] <= window_end]
    used = set()
    points = []
    for index, point in enumerate(route["points"]):
        due = start + timedelta(minutes=point["offset"])
        candidates = [c for i, c in enumerate(mine) if i not in used and c[2] == point["device_id"]
                      and c[3] == point["door"] and due - tolerance * 3 <= c[0] <= due + tolerance * 3]
        hit = min(candidates, key=lambda c: abs((c[0] - due).total_seconds()), default=None)
        if hit:
            used.add(mine.index(hit))
            delta = hit[0] - due
            status = EARLY if delta < -tolerance else LATE if delta > tolerance else ON_TIME
        else:
            status = MISSED if now > due + tolerance * 3 else WAITING
        points.append({"index": index, "name": point["name"], "due": due, "time": hit[0] if hit else None,
                       "guard": hit[1] if hit else "", "status": status})
    statuses = {p["status"] for p in points}
    if start > now:
        run_status = PLANNED
    elif WAITING in statuses:
        run_status = RUNNING
    elif statuses == {ON_TIME}:
        run_status = DONE
    else:
        run_status = VIOLATION
    return {"route_id": route["id"], "route": route["name"], "start": start, "status": run_status,
            "points": points, "done": sum(1 for p in points if p["time"]), "total": len(points)}


def runs_for_period(routes, employees, start_day, end_day, now=None):
    if end_day < start_day:
        raise ValueError("Конец периода раньше начала")
    lo = datetime.combine(start_day, datetime.min.time()) - timedelta(hours=1)
    hi = datetime.combine(end_day, datetime.max.time()) + timedelta(hours=12)
    checkins = load_checkins(lo, hi, employees)
    runs = []
    for route in routes:
        if not route["enabled"]:
            continue
        day = start_day
        while day <= end_day:
            for start in scheduled_runs(route, day):
                runs.append(evaluate_run(route, start, checkins, employees, now))
            day += timedelta(days=1)
    return sorted(runs, key=lambda r: r["start"], reverse=True)


def new_missed_points(routes, employees, now=None):
    """Пропущенные точки, о которых ещё не сообщали: [(route, run, point)]. Отмечает их как сообщённые."""
    now = now or datetime.now()
    today = now.date()
    result = []
    runs = runs_for_period(routes, employees, today - timedelta(days=1), today, now)
    with storage.database() as db:
        _ensure(db)
        for run in runs:
            for point in run["points"]:
                if point["status"] != MISSED:
                    continue
                key = (run["route_id"], run["start"].strftime(FMT), point["index"])
                if db.execute("SELECT 1 FROM patrol_notified WHERE route_id=? AND run_start=? AND point_index=?",
                              key).fetchone():
                    continue
                db.execute("INSERT INTO patrol_notified(route_id,run_start,point_index) VALUES(?,?,?)", key)
                route = next(r for r in routes if r["id"] == run["route_id"])
                result.append((route, run, point))
    return result


def missed_event(route, run, point):
    """Событие для журнала и правил тревог."""
    return {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "device_id": None,
            "device_name": route["name"], "channel": "", "raw_type": "patrol", "event_type": "patrol_missed",
            "category": "event", "state": "active", "plate": "",
            "details": {"eventDescription": f"Пропущена точка «{point['name']}» обхода "
                                            f"{run['start']:%d.%m %H:%M}"}}


RUN_CSV = ("Начало", "Маршрут", "Статус", "Точка", "План", "Факт", "Охранник", "Отметка")


def export_runs_csv(runs, handle, names=None):
    names = names or {}
    writer = csv.writer(handle, delimiter=";")
    writer.writerow(RUN_CSV)
    for run in runs:
        for p in run["points"]:
            writer.writerow((run["start"].strftime(FMT), run["route"], RUN_TITLES[run["status"]], p["name"],
                             p["due"].strftime("%H:%M"), p["time"].strftime("%H:%M") if p["time"] else "",
                             names.get(p["guard"], p["guard"]), POINT_TITLES[p["status"]]))
