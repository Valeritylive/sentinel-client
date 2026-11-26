"""Доказательства (без GTK): дела, вложения, цепочка хранения, выгрузка.

Дело — номер, название, описание, статус (открыто / закрыто). Вложения:
пакеты экспорта записей, снимки, файлы, тревоги и заметки. При добавлении
файл копируется в хранилище дела и для него считается SHA-256; оригинал
не трогается. Каждое действие (создание, вложение, просмотр, выгрузка,
закрытие, проверка целостности) пишется в журнал дела. Закрытое дело
нельзя менять, только просматривать и выгружать. Выгрузка — ZIP с файлами,
manifest.json, журналом дела и SHA256SUMS.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import sentinel_storage as storage

OPEN, CLOSED = "open", "closed"
STATUS_TITLES = {OPEN: "Открыто", CLOSED: "Закрыто"}
KIND_TITLES = {"export": "Экспорт записи", "snapshot": "Снимок", "file": "Файл", "alarm": "Тревога",
               "note": "Заметка", "folder": "Папка"}
ACTIONS = {"create": "Создано", "add": "Добавлено", "remove": "Удалено", "view": "Просмотр", "export": "Выгрузка",
           "close": "Закрыто", "reopen": "Открыто повторно", "verify": "Проверка целостности", "edit": "Изменено"}


def root():
    return storage.DATA / "evidence"


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS evidence_cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open', tags TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, closed_at TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS evidence_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
        path TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0,
        meta TEXT NOT NULL DEFAULT '{}', added_by TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS evidence_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL, time TEXT NOT NULL,
        username TEXT NOT NULL DEFAULT '', action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')""")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _log(db, case_id, username, action, detail=""):
    db.execute("INSERT INTO evidence_log(case_id,time,username,action,detail) VALUES(?,?,?,?,?)",
               (int(case_id), _now(), username, action, detail))


def log(case_id, username, action, detail=""):
    with storage.database() as db:
        _ensure(db)
        _log(db, case_id, username, action, detail)


def next_number(now=None):
    year = (now or datetime.now()).year
    with storage.database() as db:
        _ensure(db)
        numbers = [r[0] for r in db.execute("SELECT number FROM evidence_cases WHERE number LIKE ?", (f"{year}-%",))]
    used = [int(n.split("-")[1]) for n in numbers if re.fullmatch(rf"{year}-\d+", n)]
    return f"{year}-{max(used, default=0) + 1:04d}"


def create_case(title, description="", username="", tags="", number=""):
    title = (title or "").strip()
    if not title:
        raise ValueError("Введите название дела")
    number = (number or "").strip() or next_number()
    with storage.database() as db:
        _ensure(db)
        if db.execute("SELECT 1 FROM evidence_cases WHERE number=?", (number,)).fetchone():
            raise ValueError(f"Дело № {number} уже есть")
        case_id = db.execute("""INSERT INTO evidence_cases(number,title,description,tags,created_by,created_at)
            VALUES(?,?,?,?,?,?)""", (number, title, description.strip(), tags.strip(), username, _now())).lastrowid
        _log(db, case_id, username, "create", f"№ {number}: {title}")
    return case_id


def load_cases(status=None, text=""):
    query, args = "SELECT * FROM evidence_cases WHERE 1=1", []
    if status:
        query += " AND status=?"
        args.append(status)
    if text:
        query += " AND (number LIKE ? OR title LIKE ? OR description LIKE ? OR tags LIKE ?)"
        args += [f"%{text}%"] * 4
    with storage.database() as db:
        _ensure(db)
        cases = [dict(r) for r in db.execute(query + " ORDER BY created_at DESC, id DESC", args)]
        counts = {r[0]: r[1] for r in db.execute("SELECT case_id, COUNT(*) FROM evidence_items GROUP BY case_id")}
    for case in cases:
        case["items"] = counts.get(case["id"], 0)
    return cases


def get_case(case_id):
    with storage.database() as db:
        _ensure(db)
        row = db.execute("SELECT * FROM evidence_cases WHERE id=?", (int(case_id),)).fetchone()
    return dict(row) if row else None


def _open_case(case_id):
    case = get_case(case_id)
    if case is None:
        raise ValueError("Дело не найдено")
    if case["status"] != OPEN:
        raise ValueError(f"Дело № {case['number']} закрыто — изменения запрещены")
    return case


def update_case(case_id, username, title=None, description=None, tags=None):
    case = _open_case(case_id)
    title = case["title"] if title is None else title.strip()
    if not title:
        raise ValueError("Введите название дела")
    with storage.database() as db:
        db.execute("UPDATE evidence_cases SET title=?, description=?, tags=? WHERE id=?",
                   (title, case["description"] if description is None else description.strip(),
                    case["tags"] if tags is None else tags.strip(), int(case_id)))
        _log(db, case_id, username, "edit", title)


def set_status(case_id, status, username, reason=""):
    case = get_case(case_id)
    if case is None:
        raise ValueError("Дело не найдено")
    if status not in STATUS_TITLES or case["status"] == status:
        return
    with storage.database() as db:
        db.execute("UPDATE evidence_cases SET status=?, closed_at=? WHERE id=?",
                   (status, _now() if status == CLOSED else "", int(case_id)))
        _log(db, case_id, username, "close" if status == CLOSED else "reopen", reason)


def case_folder(case):
    return root() / re.sub(r"[^\w.-]", "_", case["number"])


def _unique(folder, name):
    name = re.sub(r"[^\w.\- ()а-яА-ЯёЁ]", "_", name).strip() or "file"
    target = folder / name
    stem, suffix, n = target.stem, target.suffix, 2
    while target.exists():
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return target


def add_file(case_id, source, username, kind="file", title="", meta=None):
    """Скопировать файл или папку (пакет экспорта) в хранилище дела. Возвращает id вложения."""
    case = _open_case(case_id)
    source = Path(source)
    if not source.exists():
        raise ValueError(f"Файл не найден: {source}")
    folder = case_folder(case)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    target = _unique(folder, source.name)
    if source.is_dir():
        shutil.copytree(source, target)
        files = sorted(p for p in target.rglob("*") if p.is_file())
        digest = hashlib.sha256("".join(f"{sha256_file(p)}  {p.relative_to(target)}\n" for p in files)
                                .encode()).hexdigest()
        size = sum(p.stat().st_size for p in files)
        kind = "folder" if kind == "file" else kind
    else:
        shutil.copy2(source, target)
        digest, size = sha256_file(target), target.stat().st_size
    meta = dict(meta or {}, source=str(source))
    with storage.database() as db:
        item_id = db.execute("""INSERT INTO evidence_items(case_id,kind,title,path,sha256,size,meta,added_by,added_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (int(case_id), kind, title or source.name, str(target), digest, size,
                                           json.dumps(meta, ensure_ascii=False, default=str), username,
                                           _now())).lastrowid
        _log(db, case_id, username, "add", f"{KIND_TITLES.get(kind, kind)}: {title or source.name} · SHA-256 "
                                           f"{digest[:16]}…")
    return item_id


def add_record(case_id, username, kind, title, meta=None):
    """Вложение без файла: тревога, событие или заметка."""
    _open_case(case_id)
    if kind not in KIND_TITLES:
        raise ValueError("Неизвестный тип вложения")
    if not (title or "").strip():
        raise ValueError("Пустое вложение")
    body = json.dumps(meta or {}, ensure_ascii=False, default=str, sort_keys=True)
    digest = hashlib.sha256((title + body).encode()).hexdigest()
    with storage.database() as db:
        item_id = db.execute("""INSERT INTO evidence_items(case_id,kind,title,sha256,meta,added_by,added_at)
            VALUES(?,?,?,?,?,?,?)""", (int(case_id), kind, title.strip(), digest, body, username, _now())).lastrowid
        _log(db, case_id, username, "add", f"{KIND_TITLES[kind]}: {title.strip()}")
    return item_id


def remove_item(item_id, username, reason):
    if not (reason or "").strip():
        raise ValueError("Укажите причину удаления вложения")
    with storage.database() as db:
        _ensure(db)
        item = db.execute("SELECT * FROM evidence_items WHERE id=?", (int(item_id),)).fetchone()
    if item is None:
        return
    _open_case(item["case_id"])
    path = Path(item["path"]) if item["path"] else None
    if path and path.exists():
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    with storage.database() as db:
        db.execute("DELETE FROM evidence_items WHERE id=?", (int(item_id),))
        _log(db, item["case_id"], username, "remove", f"{item['title']} — {reason.strip()}")


def load_items(case_id):
    with storage.database() as db:
        _ensure(db)
        items = [dict(r) for r in db.execute("SELECT * FROM evidence_items WHERE case_id=? ORDER BY added_at, id",
                                             (int(case_id),))]
    for item in items:
        try:
            item["meta"] = json.loads(item["meta"])
        except (TypeError, json.JSONDecodeError):
            item["meta"] = {}
    return items


def load_log(case_id):
    with storage.database() as db:
        _ensure(db)
        return [dict(r) for r in db.execute("SELECT * FROM evidence_log WHERE case_id=? ORDER BY id",
                                            (int(case_id),))]


def _item_digest(item):
    path = Path(item["path"])
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file())
        return hashlib.sha256("".join(f"{sha256_file(p)}  {p.relative_to(path)}\n" for p in files)
                              .encode()).hexdigest()
    return sha256_file(path)


def verify(case_id, username):
    """Пересчитать суммы файлов. Возвращает [(вложение, 'ok'|'changed'|'missing')]."""
    result = []
    for item in load_items(case_id):
        if not item["path"]:
            continue
        path = Path(item["path"])
        if not path.exists():
            state = "missing"
        else:
            state = "ok" if _item_digest(item) == item["sha256"] else "changed"
        result.append((item, state))
    bad = [i["title"] for i, s in result if s != "ok"]
    log(case_id, username, "verify", "все файлы целы" if not bad else "нарушена целостность: " + ", ".join(bad))
    return result


def export_case(case_id, target_dir, username):
    """ZIP с делом: files/, manifest.json, custody_log.json, SHA256SUMS. Возвращает путь ZIP."""
    case = get_case(case_id)
    if case is None:
        raise ValueError("Дело не найдено")
    items = load_items(case_id)
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle = target_dir / f"case_{re.sub(r'[^\w.-]', '_', case['number'])}_{stamp}.zip"
    log(case_id, username, "export", str(bundle))
    sums, manifest_items = [], []
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in items:
            entry = {k: item[k] for k in ("id", "kind", "title", "sha256", "size", "added_by", "added_at", "meta")}
            path = Path(item["path"]) if item["path"] else None
            if path and path.exists():
                files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
                names = []
                for file in files:
                    name = f"files/{item['id']:04d}_{path.name}" + (
                        "" if file == path else f"/{file.relative_to(path)}")
                    archive.write(file, name)
                    sums.append(f"{sha256_file(file)}  {name}\n")
                    names.append(name)
                entry["files"] = names
            manifest_items.append(entry)
        manifest = {"case": {k: case[k] for k in ("number", "title", "description", "status", "tags", "created_by",
                                                  "created_at", "closed_at")},
                    "exported_by": username, "exported_at": _now(), "items": manifest_items}
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
        archive.writestr("custody_log.json", json.dumps(load_log(case_id), ensure_ascii=False, indent=2))
        archive.writestr("SHA256SUMS", "".join(sums))
        archive.writestr("README.txt", "Дело выгружено из Sentinel.\nmanifest.json — состав дела, custody_log.json — "
                                       "журнал действий,\nSHA256SUMS — суммы файлов (проверка: распакуйте и "
                                       "выполните sha256sum -c SHA256SUMS).\n")
    os.chmod(bundle, 0o600)
    return bundle
