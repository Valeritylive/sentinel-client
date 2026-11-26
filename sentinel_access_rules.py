"""Расширенные правила доступа (без GTK): анти-passback, шлюз (взаимная блокировка)
и «первый вошедший».

Правило хранится локально со статусом «не применено», пока его не отправят на
контроллер кнопкой «Применить». Анти-passback и шлюз записываются в контроллер
через ISAPI и дальше работают без Sentinel. «Первый вошедший» выполняет сам
Sentinel: когда первый человек из списка проходит через дверь, дверь переводится
в режим «Всегда открыта» до заданного времени, затем возвращается в обычный режим.

Нумерация считывателей как у контроллеров Hikvision: у двери N считыватель
входа 2N-1 и считыватель выхода 2N.
"""
from __future__ import annotations

import json
from datetime import datetime

import sentinel_storage as storage

ANTIPASSBACK, INTERLOCK, FIRST_PERSON = "antipassback", "interlock", "first_person"
KIND_TITLES = {ANTIPASSBACK: "Анти-passback", INTERLOCK: "Шлюз (взаимная блокировка)",
               FIRST_PERSON: "Первый вошедший"}
KIND_HINTS = {
    ANTIPASSBACK: "Нельзя пройти повторно, не выйдя: карта, вошедшая через вход, должна выйти через выход.",
    INTERLOCK: "В группе дверей открыта может быть только одна: пока одна открыта, остальные заблокированы.",
    FIRST_PERSON: "Дверь остаётся открытой, когда пришёл первый человек из списка (например, начальник смены).",
}
SOFTWARE_KINDS = {FIRST_PERSON}      # выполняются Sentinel, а не контроллером
PENDING, APPLIED, FAILED = "pending", "applied", "failed"
STATUS_TITLES = {PENDING: "Не применено", APPLIED: "Применено", FAILED: "Ошибка"}

# Коды Hikvision (major 5) для успешного прохода: карта, карта+пароль, отпечаток, лицо, ID+пароль и т. п.
GRANTED_MINORS = {1, 2, 38, 75, 104, 105, 106, 107, 108}
DENIED_MINORS = {3, 4, 6, 7, 8, 9, 10, 39, 76, 109}


def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS access_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL,
        device_id INTEGER NOT NULL, config TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending', message TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '', applied_at TEXT NOT NULL DEFAULT '')""")


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def door_readers(door):
    """(вход, выход) для двери."""
    door = int(door)
    return 2 * door - 1, 2 * door


def reader_door(reader):
    return (int(reader) + 1) // 2


def reader_direction(reader):
    return "in" if int(reader) % 2 == 1 else "out"


# ------------------------------------------------------------ проверка
def validate(kind, config, door_count=None):
    """Вернуть очищенный config или бросить ValueError."""
    door_count = int(door_count or 0) or None

    def check_door(door):
        door = int(door)
        if door < 1 or (door_count and door > door_count):
            raise ValueError(f"У контроллера нет двери {door}")
        return door

    if kind == ANTIPASSBACK:
        doors = [check_door(d) for d in config.get("doors") or []]
        doors = list(dict.fromkeys(doors))
        if not doors:
            raise ValueError("Выберите хотя бы одну дверь для анти-passback")
        mode = config.get("mode", "in_out")
        if mode not in ("in_out", "route"):
            raise ValueError("Неизвестный режим анти-passback")
        if mode == "route" and len(doors) < 2:
            raise ValueError("Для маршрута нужны минимум две двери в порядке прохода")
        return {"doors": doors, "mode": mode}
    if kind == INTERLOCK:
        groups, seen = [], set()
        for group in config.get("groups") or []:
            doors = list(dict.fromkeys(check_door(d) for d in group))
            if len(doors) < 2:
                raise ValueError("В группе шлюза должно быть минимум две двери")
            if seen & set(doors):
                raise ValueError("Дверь не может входить в две группы шлюза")
            seen |= set(doors)
            groups.append(doors)
        if not groups:
            raise ValueError("Добавьте группу дверей")
        return {"groups": groups}
    if kind == FIRST_PERSON:
        door = check_door(config.get("door") or 1)
        persons = sorted({int(p) for p in config.get("person_ids") or []})
        if not persons:
            raise ValueError("Выберите людей, после прихода которых дверь открывается")
        until = str(config.get("until") or "18:00")
        try:
            datetime.strptime(until, "%H:%M")
        except ValueError:
            raise ValueError("Время окончания в формате ЧЧ:ММ") from None
        return {"door": door, "person_ids": persons, "until": until}
    raise ValueError("Неизвестный тип правила")


# ------------------------------------------------------------ хранение
def _row(r):
    item = dict(r)
    try:
        item["config"] = json.loads(item["config"])
    except (TypeError, json.JSONDecodeError):
        item["config"] = {}
    item["enabled"] = bool(item["enabled"])
    return item


def load_rules(kind=None, device_id=None):
    query, args = "SELECT * FROM access_rules WHERE 1=1", []
    if kind:
        query += " AND kind=?"
        args.append(kind)
    if device_id:
        query += " AND device_id=?"
        args.append(int(device_id))
    with storage.database() as db:
        _ensure(db)
        return [_row(r) for r in db.execute(query + " ORDER BY kind, name", args)]


def get_rule(rule_id):
    with storage.database() as db:
        _ensure(db)
        row = db.execute("SELECT * FROM access_rules WHERE id=?", (int(rule_id),)).fetchone()
    return _row(row) if row else None


def save_rule(kind, name, device_id, config, rule_id=None, enabled=True, door_count=None):
    name = (name or "").strip() or KIND_TITLES.get(kind, "Правило")
    clean = validate(kind, config, door_count)
    if kind == INTERLOCK:
        for other in load_rules(INTERLOCK, device_id):
            if other["id"] != rule_id and other["enabled"]:
                busy = {d for g in other["config"]["groups"] for d in g}
                if busy & {d for g in clean["groups"] for d in g}:
                    raise ValueError(f"Дверь уже в шлюзе «{other['name']}»")
    status = APPLIED if kind in SOFTWARE_KINDS else PENDING
    with storage.database() as db:
        _ensure(db)
        values = (kind, name, int(device_id), json.dumps(clean), int(bool(enabled)), status, _now())
        if rule_id is None:
            return db.execute("""INSERT INTO access_rules(kind,name,device_id,config,enabled,status,updated_at)
                VALUES(?,?,?,?,?,?,?)""", values).lastrowid
        db.execute("""UPDATE access_rules SET kind=?,name=?,device_id=?,config=?,enabled=?,status=?,updated_at=?,
            message='' WHERE id=?""", values + (int(rule_id),))
        return rule_id


def set_enabled(rule_id, enabled):
    rule = get_rule(rule_id)
    if rule is None:
        return
    status = APPLIED if rule["kind"] in SOFTWARE_KINDS else PENDING
    with storage.database() as db:
        db.execute("UPDATE access_rules SET enabled=?, status=?, updated_at=? WHERE id=?",
                   (int(bool(enabled)), status, _now(), int(rule_id)))


def delete_rule(rule_id):
    """Удалённое аппаратное правило надо снять с контроллера: оставляем «выключенную» запись до «Применить»."""
    rule = get_rule(rule_id)
    if rule is None:
        return
    with storage.database() as db:
        if rule["kind"] in SOFTWARE_KINDS or rule["status"] == PENDING and not rule["applied_at"]:
            db.execute("DELETE FROM access_rules WHERE id=?", (int(rule_id),))
        else:
            db.execute("UPDATE access_rules SET enabled=0, status='pending', name=name||' (удаляется)', "
                       "message='deleted', updated_at=? WHERE id=?", (_now(), int(rule_id)))


def set_status(rule_id, status, message=""):
    with storage.database() as db:
        rule = db.execute("SELECT message, enabled FROM access_rules WHERE id=?", (int(rule_id),)).fetchone()
        if rule and status == APPLIED and rule["message"] == "deleted" and not rule["enabled"]:
            db.execute("DELETE FROM access_rules WHERE id=?", (int(rule_id),))
            return
        db.execute("UPDATE access_rules SET status=?, message=?, applied_at=CASE WHEN ?='applied' THEN ? "
                   "ELSE applied_at END WHERE id=?",
                   (status, message if message != "deleted" else "", status, _now(), int(rule_id)))


def pending_rules():
    return [r for r in load_rules() if r["status"] != APPLIED and r["kind"] not in SOFTWARE_KINDS]


def pending_devices():
    """id контроллеров, на которые надо отправить правила."""
    return sorted({r["device_id"] for r in pending_rules()})


# ------------------------------------------------------------ ISAPI
def device_payloads(device_id, door_count):
    """Полная конфигурация анти-passback и шлюза контроллера: [(путь, тело JSON)].

    Отправляется целиком по контроллеру: так выключенное или удалённое правило
    корректно снимается, а не остаётся в устройстве.
    """
    door_count = max(1, int(door_count or 1))
    rules = [r for r in load_rules(device_id=device_id) if r["enabled"] and r["kind"] not in SOFTWARE_KINDS]
    passback = [r for r in rules if r["kind"] == ANTIPASSBACK]
    follow = {reader: [] for door in range(1, door_count + 1) for reader in door_readers(door)}
    first_reader = 0
    for rule in passback:
        doors = rule["config"]["doors"]
        if rule["config"]["mode"] == "route":
            readers = [door_readers(d)[0] for d in doors]
            for index, reader in enumerate(readers):
                follow[reader] = sorted(set(follow[reader]) | {readers[(index + 1) % len(readers)]})
            first_reader = first_reader or readers[0]
        else:
            for door in doors:
                entry, exit_ = door_readers(door)
                follow[entry] = sorted(set(follow[entry]) | {exit_})
                follow[exit_] = sorted(set(follow[exit_]) | {entry})
            first_reader = first_reader or door_readers(doors[0])[0]
    payloads = [("/ISAPI/AccessControl/AntiSneakCfg?format=json",
                 {"AntiSneakCfg": {"enable": bool(passback), "startCardReaderNo": first_reader or 1}})]
    if passback:
        for reader, after in sorted(follow.items()):
            payloads.append((f"/ISAPI/AccessControl/CardReaderAntiSneakCfg/{reader}?format=json",
                             {"CardReaderAntiSneakCfg": {"enable": bool(after), "followUpCardReader": after}}))
    groups = [g for r in rules if r["kind"] == INTERLOCK for g in r["config"]["groups"]]
    payloads.append(("/ISAPI/AccessControl/MultiDoorInterLockCfg?format=json",
                     {"MultiDoorInterLockCfg": {"enable": bool(groups), "MultiDoorGroup": [
                         {"id": index, "doorNoList": doors} for index, doors in enumerate(groups, start=1)]}}))
    return payloads


# ------------------------------------------------------------ события прохода
def _details(event):
    details = event.get("details") or {}
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except json.JSONDecodeError:
            details = {}
    return details if isinstance(details, dict) else {}


def event_person(event):
    details = _details(event)
    return str(details.get("employeeNoString") or details.get("employeeNo") or "")


def event_outcome(event):
    """'granted' / 'denied' / '' для события контроля доступа."""
    details = _details(event)
    try:
        minor = int(details.get("minor") or 0)
    except (TypeError, ValueError):
        minor = 0
    if minor in GRANTED_MINORS:
        return "granted"
    if minor in DENIED_MINORS:
        return "denied"
    if event_person(event) and not minor:
        return "granted"
    return ""


def event_door(event):
    details = _details(event)
    try:
        return int(details.get("doorNo") or event.get("channel") or 0)
    except (TypeError, ValueError):
        return 0


class FirstPersonTracker:
    """Выполнение правил «первый вошедший» (держит состояние за день)."""

    def __init__(self):
        self.opened = {}     # rule_id -> дата, когда дверь оставлена открытой

    def on_event(self, event, rules, employee_ids, now=None):
        """Команды для дверей: [(device_id, door, command, rule)].

        employee_ids — {id персоны: employee_no}.
        """
        if event_outcome(event) != "granted":
            return []
        now = now or datetime.now()
        who, door = event_person(event), event_door(event)
        commands = []
        for rule in rules:
            if rule["kind"] != FIRST_PERSON or not rule["enabled"] or rule["device_id"] != event.get("device_id"):
                continue
            config = rule["config"]
            if config["door"] != door or now.strftime("%H:%M") >= config["until"]:
                continue
            allowed = {employee_ids.get(pid) for pid in config["person_ids"]}
            if who in allowed and self.opened.get(rule["id"]) != now.date():
                self.opened[rule["id"]] = now.date()
                commands.append((rule["device_id"], door, "always_open", rule))
        return commands

    def due_resume(self, rules, now=None):
        """Двери, которые пора вернуть в обычный режим."""
        now = now or datetime.now()
        result = []
        for rule in rules:
            opened = self.opened.get(rule["id"])
            if rule["kind"] != FIRST_PERSON or opened is None:
                continue
            if opened != now.date() or now.strftime("%H:%M") >= rule["config"]["until"] or not rule["enabled"]:
                self.opened.pop(rule["id"], None)
                result.append((rule["device_id"], rule["config"]["door"], "resume", rule))
        return result


class PassbackMonitor:
    """Программная проверка анти-passback по журналу (подсветка нарушений в мониторинге)."""

    def __init__(self):
        self.last = {}       # (device_id, employee) -> 'in' / 'out'

    def check(self, event, rules):
        """True, если проход нарушает анти-passback (повторный вход без выхода и т. п.)."""
        if event_outcome(event) != "granted":
            return False
        who = event_person(event)
        if not who:
            return False
        details = _details(event)
        door = event_door(event)
        reader = int(details.get("cardReaderNo") or 0)
        if not reader and door:
            reader = door_readers(door)[0]
        if not reader:
            return False
        for rule in rules:
            if rule["kind"] != ANTIPASSBACK or not rule["enabled"] or rule["device_id"] != event.get("device_id"):
                continue
            if rule["config"]["mode"] != "in_out" or reader_door(reader) not in rule["config"]["doors"]:
                continue
            key = (rule["device_id"], who)
            direction = reader_direction(reader)
            violated = self.last.get(key) == direction
            self.last[key] = direction
            return violated
        return False
