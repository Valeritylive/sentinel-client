"""События устройств, правила тревог и связанные действия (без GTK).

Поток событий Hikvision (ISAPI alertStream) нормализуется в словарь события,
сохраняется в журнал «Событие», затем проверяется правилами тревог. Правило,
которое сработало, создаёт запись тревоги (вкладка «Тревога») и план действий:
всплывающее окно, снимок, тревожный выход с автозакрытием, PTZ-пресет, email.
"""
from __future__ import annotations

import json
import re
import smtplib
import ssl
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.message import EmailMessage

import sentinel_storage as storage

PRIORITIES = {"high": "Высокий", "medium": "Средний", "low": "Низкий"}
STATUS_TITLES = {"unacknowledged": "Не подтверждена", "acknowledged": "Подтверждена"}
CATEGORIES = ("", "Ложная тревога", "Проверено", "Требует внимания", "Инцидент")

# Типы событий: ключ правила -> (название, категория, ISAPI eventType)
EVENT_TYPES = {
    "motion": ("Обнаружение движения", "event", ("VMD", "motion", "motiondetection")),
    "line": ("Пересечение линии", "event", ("linedetection",)),
    "intrusion": ("Вторжение в зону", "event", ("fielddetection",)),
    "region_entrance": ("Вход в зону", "event", ("regionEntrance",)),
    "region_exit": ("Выход из зоны", "event", ("regionExiting",)),
    "tamper": ("Закрытие объектива", "event", ("tamperdetection", "shelteralarm")),
    "video_loss": ("Потеря видео", "event", ("videoloss",)),
    "io": ("Тревожный вход", "event", ("IO",)),
    "face": ("Обнаружение лица", "person", ("facedetection", "faceSnap", "FaceDetection")),
    "human": ("Обнаружение человека", "person", ("humanRecognition", "PeopleDetection")),
    "anpr_any": ("ANPR: любой номер", "vehicle", ("ANPR", "vehicleDetection")),
    "anpr_matched": ("ANPR: номер из списка", "vehicle", ("ANPR", "vehicleDetection")),
    "anpr_mismatched": ("ANPR: номер не из списка", "vehicle", ("ANPR", "vehicleDetection")),
    "access": ("Событие контроля доступа", "event", ("AccessControllerEvent",)),
    "hdd": ("Ошибка диска", "event", ("diskfull", "diskerror", "hdError", "hdFull")),
    "offline": ("Устройство не в сети", "event", ("device_offline",)),
    "user": ("Пользовательское событие", "event", ("user_defined",)),
}
EVENT_GROUPS = (
    ("Видео", ("motion", "line", "intrusion", "region_entrance", "region_exit", "tamper", "video_loss", "io")),
    ("Персоны", ("face", "human")),
    ("ANPR", ("anpr_any", "anpr_matched", "anpr_mismatched")),
    ("Контроль доступа", ("access",)),
    ("Обслуживание", ("hdd", "offline")),
    ("Пользовательские", ("user",)),
)

DEFAULT_LINKAGE = {"popup": True, "capture": False, "record_seconds": 0, "alarm_outputs": [],
                   "ptz": None, "email": False, "email_to": ""}


# ---------------------------------------------------------------- нормализация событий
def plate_key(plate):
    """Ключ сравнения номеров: без пробелов/дефисов, латиница и кириллица-двойники к одному виду."""
    text = (plate or "").upper()
    twins = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")
    return re.sub(r"[^0-9A-ZА-ЯЁ]", "", text.translate(twins))


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def classify_event(raw_type, values=None):
    """Ключ типа события по ISAPI eventType ('' — неизвестное)."""
    lowered = (raw_type or "").lower()
    for key, (_title, _cat, types) in EVENT_TYPES.items():
        if key in ("anpr_matched", "anpr_mismatched"):
            continue
        if any(lowered == t.lower() for t in types):
            return key
    if values and (values.get("licensePlate") or values.get("plateNo")):
        return "anpr_any"
    return ""


def parse_event_xml(payload, device):
    """Разобрать XML/JSON из alertStream. Возвращает словарь события или None (heartbeat/мусор)."""
    values = {}
    text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return None

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, (dict, list)):
                        walk(v)
                    elif k not in values:
                        values[k] = "" if v is None else str(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(data)
    else:
        try:
            root = ET.fromstring(stripped)
        except ET.ParseError:
            return None
        for node in root.iter():
            name = _local(node.tag)
            if name not in values and node.text and node.text.strip():
                values[name] = node.text.strip()
    raw_type = values.get("eventType", "")
    state = values.get("eventState", "active")
    if raw_type.lower() == "videoloss" and state == "inactive":
        return None  # heartbeat Hikvision: videoloss/inactive раз в ~10 с
    if not raw_type:
        return None
    key = classify_event(raw_type, values)
    category = EVENT_TYPES[key][1] if key else "event"
    plate = values.get("licensePlate") or values.get("plateNo") or ""
    details = {k: values[k] for k in ("eventDescription", "licensePlate", "vehicleType", "color",
                                      "vehicleLogoRecog", "direction", "targetType", "employeeNoString",
                                      "cardNo", "doorNo", "major", "minor", "name", "ipAddress")
               if values.get(k)}
    channel = values.get("channelID") or values.get("dynChannelID") or values.get("doorNo") or ""
    return {
        "time": values.get("dateTime") or datetime.now().astimezone().isoformat(timespec="seconds"),
        "device_id": device.get("id"), "device_name": device.get("name", ""),
        "host": device.get("host", ""), "channel": channel,
        "raw_type": raw_type, "event_type": key or raw_type, "category": category,
        "state": state, "plate": plate, "details": details,
    }


def event_title(event_type):
    item = EVENT_TYPES.get(event_type)
    return item[0] if item else (event_type or "Событие")


# ---------------------------------------------------------------- журнал событий
def store_event(event, picture_path=""):
    details = dict(event.get("details") or {})
    if event.get("plate"):
        details["licensePlate"] = event["plate"]
    if event.get("matched_list"):
        details["vehicleList"] = event["matched_list"]
    with storage.database() as db:
        return db.execute("""INSERT INTO events(time,device_id,device_name,channel,event_type,category,state,
            details,picture_path) VALUES(?,?,?,?,?,?,?,?,?)""",
                          (event["time"], event.get("device_id"), event.get("device_name", ""),
                           str(event.get("channel", "")), event["event_type"], event.get("category", "event"),
                           event.get("state", ""), json.dumps(details, ensure_ascii=False), picture_path)).lastrowid


def load_events(limit=200, category=None, since=None, until=None, text=""):
    query = "SELECT * FROM events WHERE 1=1"
    args = []
    if category and category != "all":
        query += " AND category=?"
        args.append(category)
    if since:
        query += " AND time>=?"
        args.append(since)
    if until:
        query += " AND time<=?"
        args.append(until)
    if text:
        query += " AND (device_name LIKE ? OR event_type LIKE ? OR details LIKE ?)"
        args.extend([f"%{text}%"] * 3)
    query += " ORDER BY time DESC, id DESC LIMIT ?"
    args.append(int(limit))
    with storage.database() as db:
        return [dict(r) for r in db.execute(query, args)]


# ---------------------------------------------------------------- правила
def load_rules(enabled_only=False):
    with storage.database() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM alarm_rules" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY name")]
    for row in rows:
        for key, default in (("sources", "all"), ("vehicle_lists", "all"), ("recipients", ["admin"]),
                             ("linkage", {})):
            try:
                row[key] = json.loads(row[key])
            except (TypeError, json.JSONDecodeError):
                row[key] = default
        linkage = dict(DEFAULT_LINKAGE)
        linkage.update(row["linkage"] if isinstance(row["linkage"], dict) else {})
        row["linkage"] = linkage
    return rows


def save_rule(rule, rule_id=None):
    name = (rule.get("name") or "").strip()
    if not name:
        raise ValueError("Введите название тревоги")
    if rule.get("event_type") not in EVENT_TYPES:
        raise ValueError("Выберите тип события")
    priority = rule.get("priority", "medium")
    if priority not in PRIORITIES:
        raise ValueError("Неизвестный приоритет")
    linkage = dict(DEFAULT_LINKAGE)
    linkage.update(rule.get("linkage") or {})
    for output in linkage["alarm_outputs"]:
        if not isinstance(output, dict) or "device_id" not in output or "output" not in output:
            raise ValueError("Некорректный тревожный выход")
        output.setdefault("auto_close", 10)
    values = (name, 1 if rule.get("enabled", True) else 0, rule["event_type"],
              json.dumps(rule.get("sources", "all")), json.dumps(rule.get("vehicle_lists", "all")),
              priority, rule.get("schedule_id"), json.dumps(rule.get("recipients", ["admin"])),
              json.dumps(linkage, ensure_ascii=False))
    with storage.database() as db:
        if rule_id is None:
            return db.execute("""INSERT INTO alarm_rules(name,enabled,event_type,sources,vehicle_lists,priority,
                schedule_id,recipients,linkage) VALUES(?,?,?,?,?,?,?,?,?)""", values).lastrowid
        db.execute("""UPDATE alarm_rules SET name=?,enabled=?,event_type=?,sources=?,vehicle_lists=?,priority=?,
            schedule_id=?,recipients=?,linkage=? WHERE id=?""", values + (rule_id,))
        return rule_id


def delete_rule(rule_id):
    with storage.database() as db:
        db.execute("DELETE FROM alarm_rules WHERE id=?", (rule_id,))


def set_rule_enabled(rule_id, enabled):
    with storage.database() as db:
        db.execute("UPDATE alarm_rules SET enabled=? WHERE id=?", (1 if enabled else 0, rule_id))


def _event_matches_type(rule_type, event):
    if rule_type in ("anpr_any", "anpr_matched", "anpr_mismatched"):
        return event.get("category") == "vehicle" and bool(event.get("plate"))
    return event.get("event_type") == rule_type


def match_rules(event, rules, vehicle_index, schedule_lookup=None, now=None, holidays=None):
    """Правила, которые срабатывают на событие.

    vehicle_index: {plate_key: [list_id, ...]} — номера из списков ТС.
    schedule_lookup(schedule_id) -> недельный шаблон или None (весь день).
    """
    from sentinel_people import holiday_today, schedule_allows
    now = now or datetime.now().astimezone()
    # в праздник расписание проверяется как для воскресенья
    check_moment = now
    if holidays and holiday_today(holidays, now):
        check_moment = now + timedelta(days=6 - now.weekday())
    if event.get("state") == "inactive":
        return []
    result = []
    key = plate_key(event.get("plate", ""))
    lists_for_plate = set(vehicle_index.get(key, ())) if key else set()
    for rule in rules:
        if not rule.get("enabled", 1):
            continue
        if not _event_matches_type(rule["event_type"], event):
            continue
        sources = rule.get("sources", "all")
        if sources != "all" and event.get("device_id") not in set(sources or ()):
            continue
        if rule["event_type"] in ("anpr_matched", "anpr_mismatched"):
            wanted = rule.get("vehicle_lists", "all")
            pool = lists_for_plate if wanted == "all" else lists_for_plate & set(wanted or ())
            if rule["event_type"] == "anpr_matched" and not pool:
                continue
            if rule["event_type"] == "anpr_mismatched" and pool:
                continue
        if rule.get("schedule_id") and schedule_lookup:
            weekly = schedule_lookup(rule["schedule_id"])
            if weekly is not None and not schedule_allows(weekly, check_moment):
                continue
        result.append(rule)
    return result


# ---------------------------------------------------------------- записи тревог
def create_alarm_record(rule, event, event_id=None, picture_path=""):
    details = dict(event.get("details") or {})
    if event.get("plate"):
        details["licensePlate"] = event["plate"]
    if event.get("matched_list"):
        details["vehicleList"] = event["matched_list"]
    with storage.database() as db:
        return db.execute("""INSERT INTO alarm_records(rule_id,rule_name,event_id,time,device_id,device_name,
            channel,event_type,priority,details,picture_path) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                          (rule.get("id"), rule.get("name", ""), event_id, event["time"], event.get("device_id"),
                           event.get("device_name", ""), str(event.get("channel", "")), rule["event_type"],
                           rule.get("priority", "medium"), json.dumps(details, ensure_ascii=False),
                           picture_path)).lastrowid


def load_alarm_records(limit=200, status=None, priority=None, since=None):
    query, args = "SELECT * FROM alarm_records WHERE 1=1", []
    if status:
        query += " AND status=?"
        args.append(status)
    if priority:
        query += " AND priority=?"
        args.append(priority)
    if since:
        query += " AND time>=?"
        args.append(since)
    query += " ORDER BY time DESC, id DESC LIMIT ?"
    args.append(int(limit))
    with storage.database() as db:
        return [dict(r) for r in db.execute(query, args)]


def get_alarm_record(record_id):
    with storage.database() as db:
        row = db.execute("SELECT * FROM alarm_records WHERE id=?", (record_id,)).fetchone()
    return dict(row) if row else None


def update_alarm_record(record_id, **values):
    allowed = {"priority", "category", "remarks", "picture_path"}
    values = {k: v for k, v in values.items() if k in allowed}
    if not values:
        return
    sets = ",".join(f"{k}=?" for k in values)
    with storage.database() as db:
        db.execute(f"UPDATE alarm_records SET {sets} WHERE id=?", (*values.values(), record_id))


def acknowledge(record_ids, username, remarks=None):
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with storage.database() as db:
        for record_id in record_ids:
            if remarks is not None:
                db.execute("UPDATE alarm_records SET remarks=? WHERE id=?", (remarks, record_id))
            db.execute("""UPDATE alarm_records SET status='acknowledged', acknowledged_at=?, acknowledged_by=?
                WHERE id=? AND status!='acknowledged'""", (stamp, username, record_id))
    log_operation(username, "Подтверждение тревоги", ",".join(str(i) for i in record_ids))


def unacknowledged_count():
    with storage.database() as db:
        return db.execute("SELECT COUNT(*) FROM alarm_records WHERE status='unacknowledged'").fetchone()[0]


# ---------------------------------------------------------------- журнал операций
def log_operation(username, action, target="", details=""):
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with storage.database() as db:
        db.execute("INSERT INTO operation_log(time,username,action,target,details) VALUES(?,?,?,?,?)",
                   (stamp, username or "", action, str(target), str(details)))


def load_operation_log(limit=200, target=None):
    query, args = "SELECT * FROM operation_log", []
    if target is not None:
        query += " WHERE target=?"
        args.append(str(target))
    query += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with storage.database() as db:
        return [dict(r) for r in db.execute(query, args)]


# ---------------------------------------------------------------- хранение (сроки)
def purge_older_than(days_events=90, days_alarms=180, days_log=365, now=None):
    now = now or datetime.now().astimezone()
    removed = {}
    with storage.database() as db:
        for table, days in (("events", days_events), ("alarm_records", days_alarms),
                            ("operation_log", days_log)):
            if not days:
                continue
            limit = (now - timedelta(days=int(days))).isoformat(timespec="seconds")
            removed[table] = db.execute(f"DELETE FROM {table} WHERE time<?", (limit,)).rowcount
    return removed


# ---------------------------------------------------------------- действия (ISAPI)
def alarm_output_xml(active: bool):
    state = "high" if active else "low"
    return f"<IOPortData><outputState>{state}</outputState></IOPortData>"


def alarm_output_path(output_id):
    return f"/ISAPI/System/IO/outputs/{int(output_id)}/trigger"


def linkage_plan(rule, event):
    """Список действий в порядке выполнения: [(тип, параметры)]."""
    link = rule.get("linkage") or {}
    plan = []
    if link.get("capture"):
        plan.append(("capture", {"device_id": event.get("device_id"), "channel": event.get("channel")}))
    if link.get("record_seconds"):
        plan.append(("record", {"device_id": event.get("device_id"), "seconds": int(link["record_seconds"])}))
    for output in link.get("alarm_outputs") or []:
        plan.append(("alarm_output", {"device_id": output["device_id"], "output": int(output["output"]),
                                      "auto_close": int(output.get("auto_close") or 0)}))
    if link.get("ptz"):
        ptz = dict(link["ptz"])
        if not ptz.get("device_id"):
            ptz["device_id"] = event.get("device_id")
        plan.append(("ptz", ptz))
    if link.get("email") and link.get("email_to"):
        plan.append(("email", {"to": link["email_to"]}))
    if link.get("popup", True):
        plan.append(("popup", {}))
    return plan


# ---------------------------------------------------------------- email
DEFAULT_SMTP = {"server": "", "port": 587, "security": "starttls", "username": "", "sender": "",
                "sender_name": "Sentinel"}


def email_body(rule, event):
    lines = [f"Тревога: {rule.get('name', '')}",
             f"Тип: {event_title(rule.get('event_type'))}",
             f"Время: {event.get('time', '')}",
             f"Источник: {event.get('device_name', '')} · канал {event.get('channel', '')}",
             f"Приоритет: {PRIORITIES.get(rule.get('priority'), '')}"]
    if event.get("plate"):
        lines.append(f"Номер ТС: {event['plate']}")
    return "\n".join(lines)


def send_email(config, password, recipients, subject, body, attachment=None, timeout=20):
    """Отправить письмо по SMTP (вызывать из рабочего потока)."""
    config = dict(DEFAULT_SMTP, **(config or {}))
    if not config["server"]:
        raise ValueError("SMTP-сервер не настроен (Система → Конфигурация → Email)")
    recipients = [r.strip() for r in (recipients if isinstance(recipients, (list, tuple)) else
                                      str(recipients).replace(";", ",").split(",")) if r.strip()]
    if not recipients:
        raise ValueError("Не указан получатель")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{config['sender_name']} <{config['sender'] or config['username']}>"
    message["To"] = ", ".join(recipients)
    message.set_content(body)
    if attachment:
        with open(attachment, "rb") as handle:
            message.add_attachment(handle.read(), maintype="image", subtype="png",
                                   filename=attachment.rsplit("/", 1)[-1])
    port = int(config["port"])
    context = ssl.create_default_context()
    if config["security"] == "ssl":
        client = smtplib.SMTP_SSL(config["server"], port, timeout=timeout, context=context)
    else:
        client = smtplib.SMTP(config["server"], port, timeout=timeout)
    with client:
        if config["security"] == "starttls":
            client.starttls(context=context)
        if config["username"]:
            client.login(config["username"], password or "")
        client.send_message(message)
