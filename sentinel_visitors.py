"""Посетители (без GTK): заявки, согласование, регистрация входа и выхода,
пропуск, стоп-лист.

Жизненный цикл: заявка (ожидает согласования) → согласована → вход (выдан
пропуск) → выход. Отклонённую или просроченную заявку зарегистрировать нельзя.
При входе посетитель становится временной персоной с ID «V<номер>», картой
пропуска и выбранным уровнем доступа, срок действия — до конца визита. При
выходе временная персона удаляется, а её ID нужно стереть с контроллеров.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta

import sentinel_people as people
import sentinel_storage as storage

PENDING, APPROVED, REJECTED, INSIDE, LEFT = "pending", "approved", "rejected", "inside", "left"
STATUS_TITLES = {PENDING: "Ожидает согласования", APPROVED: "Согласован", REJECTED: "Отклонён",
                 INSIDE: "На объекте", LEFT: "Ушёл"}
PURPOSES = ("Деловая встреча", "Собеседование", "Доставка", "Подрядчик / ремонт", "Гость", "Другое")
FMT = "%Y-%m-%d %H:%M"


class WatchlistHit(Exception):
    def __init__(self, entry):
        super().__init__(f"Посетитель в стоп-листе: {entry['name']} ({entry['reason'] or 'без причины'})")
        self.entry = entry


def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS visitors (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
        company TEXT NOT NULL DEFAULT '', document TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL DEFAULT '',
        host_person_id INTEGER, host_name TEXT NOT NULL DEFAULT '',
        expected_from TEXT NOT NULL, expected_to TEXT NOT NULL,
        level_id INTEGER, status TEXT NOT NULL DEFAULT 'pending', badge TEXT NOT NULL DEFAULT '',
        person_id INTEGER, employee_no TEXT NOT NULL DEFAULT '',
        checked_in_at TEXT NOT NULL DEFAULT '', checked_out_at TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL DEFAULT '',
        remark TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS visitor_watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
        document TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")


def _norm_name(value):
    return " ".join(str(value or "").lower().replace("ё", "е").split())


def _norm_digits(value):
    return re.sub(r"\D", "", str(value or ""))


def _norm_doc(value):
    return re.sub(r"[\s\-]", "", str(value or "")).upper()


def _parse(value):
    return datetime.strptime(str(value)[:16].replace("T", " "), FMT)


def approval_required():
    return storage.get_setting("visitor_approval", "1") == "1"


def set_approval_required(value):
    storage.set_setting("visitor_approval", "1" if value else "0")


# ------------------------------------------------------------ стоп-лист
def load_watchlist():
    with storage.database() as db:
        _ensure(db)
        return [dict(r) for r in db.execute("SELECT * FROM visitor_watchlist ORDER BY name")]


def add_to_watchlist(name, phone="", document="", reason=""):
    if not (name or "").strip():
        raise ValueError("Введите имя для стоп-листа")
    with storage.database() as db:
        _ensure(db)
        return db.execute("INSERT INTO visitor_watchlist(name,phone,document,reason) VALUES(?,?,?,?)",
                          (name.strip(), phone.strip(), document.strip(), reason.strip())).lastrowid


def remove_from_watchlist(entry_id):
    with storage.database() as db:
        _ensure(db)
        db.execute("DELETE FROM visitor_watchlist WHERE id=?", (int(entry_id),))


def watchlist_match(visitor, entries=None):
    """Запись стоп-листа, совпадающая с посетителем (документ, телефон или полное имя), или None."""
    entries = load_watchlist() if entries is None else entries
    doc, phone, name = _norm_doc(visitor.get("document")), _norm_digits(visitor.get("phone")), \
        _norm_name(visitor.get("name"))
    for entry in entries:
        if doc and _norm_doc(entry["document"]) == doc:
            return entry
        if len(phone) >= 7 and _norm_digits(entry["phone"])[-10:] == phone[-10:]:
            return entry
        if name and _norm_name(entry["name"]) == name:
            return entry
    return None


# ------------------------------------------------------------ заявки
def register(visitor, username=""):
    """Новая заявка. Возвращает id. Статус «ожидает» или сразу «согласован», если согласование выключено."""
    name = (visitor.get("name") or "").strip()
    if not name:
        raise ValueError("Введите имя посетителя")
    start = _parse(visitor.get("expected_from") or datetime.now().strftime(FMT))
    end = _parse(visitor.get("expected_to") or (start + timedelta(hours=2)).strftime(FMT))
    if end <= start:
        raise ValueError("Окончание визита должно быть позже начала")
    if end - start > timedelta(days=31):
        raise ValueError("Визит не дольше 31 дня")
    status = PENDING if approval_required() else APPROVED
    host = visitor.get("host_name", "")
    if visitor.get("host_person_id") and not host:
        host = next((p["name"] for p in people.load_persons() if p["id"] == visitor["host_person_id"]), "")
    with storage.database() as db:
        _ensure(db)
        return db.execute("""INSERT INTO visitors(name,phone,company,document,purpose,host_person_id,host_name,
            expected_from,expected_to,level_id,status,created_by,remark) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (name, visitor.get("phone", "").strip(), visitor.get("company", "").strip(),
                           visitor.get("document", "").strip(), visitor.get("purpose", ""),
                           visitor.get("host_person_id"), host, start.strftime(FMT), end.strftime(FMT),
                           visitor.get("level_id"), status, username, visitor.get("remark", ""))).lastrowid


def get(visitor_id):
    with storage.database() as db:
        _ensure(db)
        row = db.execute("SELECT * FROM visitors WHERE id=?", (int(visitor_id),)).fetchone()
    return dict(row) if row else None


def load(status=None, day=None, text=""):
    query, args = "SELECT * FROM visitors WHERE 1=1", []
    if status:
        statuses = [status] if isinstance(status, str) else list(status)
        query += f" AND status IN ({','.join('?' for _ in statuses)})"
        args += statuses
    if day:
        query += " AND substr(expected_from,1,10)<=? AND substr(expected_to,1,10)>=?"
        args += [day, day]
    if text:
        query += " AND (name LIKE ? OR company LIKE ? OR phone LIKE ? OR host_name LIKE ? OR badge LIKE ?)"
        args += [f"%{text}%"] * 5
    with storage.database() as db:
        _ensure(db)
        return [dict(r) for r in db.execute(query + " ORDER BY expected_from DESC, id DESC", args)]


def _set(visitor_id, **fields):
    keys = ", ".join(f"{k}=?" for k in fields)
    with storage.database() as db:
        _ensure(db)
        db.execute(f"UPDATE visitors SET {keys} WHERE id=?", tuple(fields.values()) + (int(visitor_id),))


def decide(visitor_id, approved, username=""):
    visitor = get(visitor_id)
    if visitor is None:
        raise ValueError("Заявка не найдена")
    if visitor["status"] != PENDING:
        raise ValueError(f"Заявка уже: {STATUS_TITLES[visitor['status']].lower()}")
    _set(visitor_id, status=APPROVED if approved else REJECTED, decided_by=username)


def next_badge():
    with storage.database() as db:
        _ensure(db)
        rows = [r[0] for r in db.execute("SELECT badge FROM visitors WHERE badge!=''")]
    numbers = [int(b[1:]) for b in rows if b[:1] == "9" and b[1:].isdigit()]
    return "9" + str(max(numbers, default=100000) + 1)


def check_in(visitor_id, badge="", level_id=None, force=False, now=None):
    """Регистрация входа: создаёт временную персону. Возвращает её dict (для записи на контроллеры).

    WatchlistHit — посетитель в стоп-листе (force=True пропускает, решение оператора).
    """
    visitor = get(visitor_id)
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    if visitor is None:
        raise ValueError("Заявка не найдена")
    if visitor["status"] == PENDING:
        raise ValueError("Заявка ещё не согласована")
    if visitor["status"] != APPROVED:
        raise ValueError(f"Нельзя зарегистрировать вход: {STATUS_TITLES[visitor['status']].lower()}")
    end = _parse(visitor["expected_to"])
    if end <= now:
        raise ValueError("Время визита истекло — создайте новую заявку")
    entry = watchlist_match(visitor)
    if entry and not force:
        raise WatchlistHit(entry)
    badge = (badge or "").strip() or next_badge()
    employee_no = f"V{visitor['id']}"
    person = {"employee_no": employee_no, "name": visitor["name"], "phone": visitor["phone"],
              "valid_from": now.strftime(FMT), "valid_to": end.strftime(FMT), "cards": [badge],
              "remark": f"Посетитель · к {visitor['host_name'] or '—'}"}
    person_id = people.save_person(person)
    level = level_id or visitor["level_id"]
    if level:
        people.assign_levels(person_ids=[person_id], level_ids=[int(level)])
    _set(visitor_id, status=INSIDE, badge=badge, person_id=person_id, employee_no=employee_no,
         level_id=level, checked_in_at=now.strftime(FMT))
    return next(p for p in people.load_persons() if p["id"] == person_id)


def check_out(visitor_id, now=None):
    """Регистрация выхода. Возвращает (employee_no, {device_id}) — что стереть с контроллеров."""
    visitor = get(visitor_id)
    if visitor is None or visitor["status"] != INSIDE:
        raise ValueError("Посетитель не на объекте")
    devices = set()
    if visitor["person_id"]:
        person = next((p for p in people.load_persons() if p["id"] == visitor["person_id"]), None)
        if person:
            devices = set(people.person_doors(person))
        people.delete_persons([visitor["person_id"]])
    _set(visitor_id, status=LEFT, person_id=None,
         checked_out_at=(now or datetime.now()).strftime(FMT))
    return visitor["employee_no"], devices


def is_visitor(person):
    """Временная персона посетителя (ID «V<номер>»)."""
    number = str(person.get("employee_no") or "")
    return number[:1] == "V" and number[1:].isdigit()


def overdue(now=None):
    """На объекте после окончания визита."""
    stamp = (now or datetime.now()).strftime(FMT)
    return [v for v in load(INSIDE) if v["expected_to"] < stamp]


def counters(day=None, now=None):
    day = day or (now or datetime.now()).strftime("%Y-%m-%d")
    today = load(day=day)
    return {"pending": sum(1 for v in load(PENDING)),
            "expected": sum(1 for v in today if v["status"] == APPROVED),
            "inside": len(load(INSIDE)), "overdue": len(overdue(now)),
            "left_today": sum(1 for v in today if v["status"] == LEFT)}


def badge_html(visitor, site_name="Sentinel"):
    """Пропуск посетителя для печати (HTML, A6)."""
    e = lambda value: html.escape(str(value or "—"))
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Пропуск {e(visitor['badge'])}</title>
<style>@page{{size:A6;margin:8mm}}body{{font-family:sans-serif;margin:0}}
.card{{border:2px solid #222;border-radius:10px;padding:14px;max-width:360px}}
.top{{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #999;padding-bottom:6px}}
.kind{{background:#c62828;color:#fff;padding:3px 10px;border-radius:4px;font-weight:bold;letter-spacing:2px}}
h1{{font-size:22px;margin:12px 0 4px}}.row{{font-size:13px;margin:3px 0}}.muted{{color:#666}}
.badge{{font:bold 26px monospace;letter-spacing:3px;margin-top:10px;text-align:center;border:1px dashed #555;padding:6px}}
</style></head><body><div class="card">
<div class="top"><b>{e(site_name)}</b><span class="kind">ГОСТЬ</span></div>
<h1>{e(visitor['name'])}</h1>
<div class="row">{e(visitor['company'])}</div>
<div class="row"><span class="muted">К кому:</span> {e(visitor['host_name'])}</div>
<div class="row"><span class="muted">Цель:</span> {e(visitor['purpose'])}</div>
<div class="row"><span class="muted">Действует до:</span> {e(visitor['expected_to'])}</div>
<div class="badge">{e(visitor['badge'])}</div>
<div class="row muted" style="text-align:center">Верните пропуск при выходе</div>
</div><script>window.onload=()=>window.print()</script></body></html>"""
