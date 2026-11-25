"""Статистика и отчёты (левая панель «Статистика») — без GTK."""
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timedelta

import sentinel_storage as storage
from sentinel_alarms import PRIORITIES, STATUS_TITLES, event_title

REPORTS = {
    "device_status": "Состояние устройств",
    "alarms_by_type": "Тревоги по типам",
    "alarms_by_day": "Тревоги по дням",
    "events_by_category": "События по категориям",
    "vehicles": "Проезды ТС (ANPR)",
    "access_persons": "Персоны и учётные данные",
    "operations": "Журнал операций",
}
PERIODS = {"today": "Сегодня", "week": "7 дней", "month": "30 дней"}
STATUS_NAMES = {"online": "В сети", "offline": "Не в сети", "": "Не проверено"}


def period_start(period, now=None):
    now = now or datetime.now().astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {"today": today, "week": today - timedelta(days=6), "month": today - timedelta(days=29)}[period]


def build(report, period="week", now=None):
    """(заголовки, строки) отчёта."""
    since = period_start(period, now).isoformat(timespec="seconds")
    with storage.database() as db:
        if report == "device_status":
            rows = db.execute("""SELECT host, MAX(host_name) AS name, MAX(device_model) AS model, COUNT(*) AS channels,
                MAX(status) AS status, MAX(status_checked_at) AS checked FROM devices GROUP BY host ORDER BY host""")
            return (("Устройство", "Адрес", "Модель", "Каналов", "Состояние", "Проверено"),
                    [(r["name"] or r["host"], r["host"], r["model"], r["channels"],
                      STATUS_NAMES.get(r["status"], r["status"]), r["checked"][:16].replace("T", " "))
                     for r in rows])
        if report == "alarms_by_type":
            rows = db.execute("""SELECT event_type, priority, COUNT(*) AS n,
                SUM(status='acknowledged') AS ack FROM alarm_records WHERE time>=? GROUP BY event_type, priority
                ORDER BY n DESC""", (since,))
            return (("Тип", "Приоритет", "Всего", "Подтверждено"),
                    [(event_title(r["event_type"]), PRIORITIES.get(r["priority"], r["priority"]), r["n"], r["ack"])
                     for r in rows])
        if report == "alarms_by_day":
            rows = db.execute("""SELECT substr(time,1,10) AS day, COUNT(*) AS n,
                SUM(status='unacknowledged') AS open FROM alarm_records WHERE time>=? GROUP BY day ORDER BY day""",
                              (since,))
            return ("День", "Тревог", "Не подтверждено"), [(r["day"], r["n"], r["open"]) for r in rows]
        if report == "events_by_category":
            counter = Counter()
            for r in db.execute("SELECT category, event_type FROM events WHERE time>=?", (since,)):
                counter[(r["category"], event_title(r["event_type"]))] += 1
            names = {"event": "Событие", "person": "Персона", "vehicle": "ТС"}
            return (("Категория", "Тип", "Количество"),
                    [(names.get(c, c), t, n) for (c, t), n in counter.most_common()])
        if report == "vehicles":
            result = []
            for r in db.execute("""SELECT time, device_name, details FROM events WHERE category='vehicle' AND time>=?
                    ORDER BY time DESC LIMIT 1000""", (since,)):
                try:
                    details = json.loads(r["details"])
                except json.JSONDecodeError:
                    details = {}
                result.append((r["time"][:19].replace("T", " "), r["device_name"],
                               details.get("licensePlate", ""), details.get("vehicleList", "— нет в списках —")))
            return ("Время", "Камера", "Номер", "Список"), result
        if report == "access_persons":
            rows = db.execute("""SELECT p.employee_no, p.name, d.name AS department, p.cards, p.pin, p.valid_to
                FROM persons p LEFT JOIN departments d ON d.id=p.department_id ORDER BY p.name""")
            result = []
            for r in rows:
                try:
                    cards = len(json.loads(r["cards"]))
                except json.JSONDecodeError:
                    cards = 0
                result.append((r["employee_no"], r["name"], r["department"] or "", cards,
                               "да" if r["pin"] else "нет", r["valid_to"]))
            return ("ID", "Имя", "Отдел", "Карт", "PIN", "Действует до"), result
        if report == "operations":
            rows = db.execute("SELECT * FROM operation_log WHERE time>=? ORDER BY id DESC LIMIT 1000", (since,))
            return (("Время", "Пользователь", "Действие", "Объект"),
                    [(r["time"][:19].replace("T", " "), r["username"], r["action"], r["target"]) for r in rows])
    raise KeyError(report)


def summary(now=None):
    """Числа для плиток сводки."""
    since = period_start("today", now).isoformat(timespec="seconds")
    with storage.database() as db:
        devices = db.execute("SELECT COUNT(DISTINCT host) FROM devices").fetchone()[0]
        offline = db.execute("SELECT COUNT(DISTINCT host) FROM devices WHERE status='offline'").fetchone()[0]
        alarms = db.execute("SELECT COUNT(*) FROM alarm_records WHERE time>=?", (since,)).fetchone()[0]
        open_alarms = db.execute("SELECT COUNT(*) FROM alarm_records WHERE status='unacknowledged'").fetchone()[0]
        events = db.execute("SELECT COUNT(*) FROM events WHERE time>=?", (since,)).fetchone()[0]
        persons = db.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    return {"devices": devices, "offline": offline, "alarms_today": alarms, "open_alarms": open_alarms,
            "events_today": events, "persons": persons}


def export_csv(headers, rows, handle):
    writer = csv.writer(handle)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(["'" + str(v) if str(v)[:1] in ("=", "+", "-", "@") else v for v in row])


def status_label(code):
    return STATUS_TITLES.get(code, code)
