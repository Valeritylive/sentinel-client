"""Видеостена (без GTK): стены из экранов, раскладки, сценарии и вывод тревог.

Стена — сетка физических мониторов, подключённых к компьютеру с Sentinel
(строки × колонки). У каждого экрана своя раскладка (1/4/6/9/16 окон) и
камеры в окнах. Сценарий — сохранённое содержимое всех экранов, его можно
вызвать одной кнопкой. Экран тревог: при тревоге с действием «На видеостену»
(или при любой тревоге, если так настроено — в том числе тревоге с сервера Sentinel)
камера события выводится в первое свободное окно этого экрана (или по кругу),
а через заданное время окно возвращается к прежнему содержимому.

Аппаратные декодеры видеостен не поддерживаются: изображение выводит сам
компьютер, по окну на монитор.
"""
from __future__ import annotations

import json

import sentinel_storage as storage

LAYOUTS = (1, 4, 6, 9, 16)


def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS video_walls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, rows INTEGER NOT NULL DEFAULT 1,
        cols INTEGER NOT NULL DEFAULT 2, screens TEXT NOT NULL DEFAULT '[]',
        alarm_screen INTEGER NOT NULL DEFAULT -1, alarm_seconds INTEGER NOT NULL DEFAULT 60,
        all_alarms INTEGER NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS video_wall_scenes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, wall_id INTEGER NOT NULL, name TEXT NOT NULL,
        screens TEXT NOT NULL DEFAULT '[]', UNIQUE(wall_id, name))""")


def empty_screen(index, layout=4):
    return {"monitor": index, "layout": layout, "cameras": [None] * layout}


def normalize_screens(screens, count):
    """Ровно count экранов; у каждого cameras длиной layout."""
    result = []
    for index in range(count):
        screen = dict(screens[index]) if index < len(screens or []) else empty_screen(index)
        layout = int(screen.get("layout") or 4)
        if layout not in LAYOUTS:
            layout = 4
        cameras = [int(c) if c else None for c in (screen.get("cameras") or [])][:layout]
        cameras += [None] * (layout - len(cameras))
        monitor = screen.get("monitor", index)
        result.append({"monitor": int(monitor) if monitor is not None else index, "layout": layout,
                       "cameras": cameras})
    return result


def _row(r):
    wall = dict(r)
    try:
        wall["screens"] = json.loads(wall["screens"])
    except (TypeError, json.JSONDecodeError):
        wall["screens"] = []
    wall["screens"] = normalize_screens(wall["screens"], wall["rows"] * wall["cols"])
    return wall


def load_walls():
    with storage.database() as db:
        _ensure(db)
        return [_row(r) for r in db.execute("SELECT * FROM video_walls ORDER BY name")]


def get_wall(wall_id):
    return next((w for w in load_walls() if w["id"] == int(wall_id)), None)


def save_wall(name, rows, cols, screens=None, alarm_screen=-1, alarm_seconds=60, wall_id=None, all_alarms=False):
    name = (name or "").strip()
    if not name:
        raise ValueError("Введите название стены")
    rows, cols = int(rows), int(cols)
    if not (1 <= rows <= 4 and 1 <= cols <= 6):
        raise ValueError("Стена: от 1 до 4 рядов и от 1 до 6 колонок экранов")
    count = rows * cols
    clean = normalize_screens(screens or [], count)
    alarm_screen = int(alarm_screen)
    if alarm_screen >= count:
        raise ValueError("Экран тревог вне стены")
    values = (name, rows, cols, json.dumps(clean), alarm_screen, max(5, int(alarm_seconds or 60)),
              int(bool(all_alarms)))
    with storage.database() as db:
        _ensure(db)
        if wall_id is None:
            return db.execute("""INSERT INTO video_walls(name,rows,cols,screens,alarm_screen,alarm_seconds,all_alarms)
                VALUES(?,?,?,?,?,?,?)""", values).lastrowid
        db.execute("""UPDATE video_walls SET name=?,rows=?,cols=?,screens=?,alarm_screen=?,alarm_seconds=?,all_alarms=?
            WHERE id=?""", values + (int(wall_id),))
        return wall_id


def save_screens(wall_id, screens):
    wall = get_wall(wall_id)
    if wall is None:
        raise ValueError("Стена не найдена")
    clean = normalize_screens(screens, wall["rows"] * wall["cols"])
    with storage.database() as db:
        db.execute("UPDATE video_walls SET screens=? WHERE id=?", (json.dumps(clean), int(wall_id)))
    return clean


def delete_wall(wall_id):
    with storage.database() as db:
        _ensure(db)
        db.execute("DELETE FROM video_wall_scenes WHERE wall_id=?", (int(wall_id),))
        db.execute("DELETE FROM video_walls WHERE id=?", (int(wall_id),))


# ------------------------------------------------------------ раскладка
def set_layout(screens, index, layout):
    if layout not in LAYOUTS:
        raise ValueError("Неизвестная раскладка")
    screens = [dict(s, cameras=list(s["cameras"])) for s in screens]
    cameras = [c for c in screens[index]["cameras"] if c][:layout]
    screens[index]["layout"] = layout
    screens[index]["cameras"] = cameras + [None] * (layout - len(cameras))
    return screens


def place_camera(screens, index, slot, device_id):
    screens = [dict(s, cameras=list(s["cameras"])) for s in screens]
    if not 0 <= slot < screens[index]["layout"]:
        raise ValueError("Нет такого окна на экране")
    screens[index]["cameras"][slot] = int(device_id) if device_id else None
    return screens


def place_cameras(screens, index, device_ids):
    """Заполнить экран камерами (например, из сохранённого вида); раскладка подбирается по числу камер."""
    ids = [int(d) for d in device_ids if d][:max(LAYOUTS)]
    layout = next((n for n in LAYOUTS if n >= len(ids)), max(LAYOUTS))
    screens = set_layout(screens, index, layout)
    screens[index]["cameras"] = ids + [None] * (layout - len(ids))
    return screens


def alarm_slot(wall, screens, rotation=0):
    """Окно экрана тревог для новой тревожной камеры: первое пустое, иначе по кругу. (экран, окно) или None."""
    index = wall["alarm_screen"]
    if index < 0 or index >= len(screens):
        return None
    cameras = screens[index]["cameras"]
    empty = next((i for i, c in enumerate(cameras) if not c), None)
    return index, empty if empty is not None else rotation % len(cameras)


# ------------------------------------------------------------ сценарии
def load_scenes(wall_id):
    with storage.database() as db:
        _ensure(db)
        rows = [dict(r) for r in db.execute("SELECT * FROM video_wall_scenes WHERE wall_id=? ORDER BY name",
                                            (int(wall_id),))]
    for row in rows:
        try:
            row["screens"] = json.loads(row["screens"])
        except (TypeError, json.JSONDecodeError):
            row["screens"] = []
    return rows


def save_scene(wall_id, name, screens):
    name = (name or "").strip()
    if not name:
        raise ValueError("Введите название сценария")
    with storage.database() as db:
        _ensure(db)
        db.execute("""INSERT INTO video_wall_scenes(wall_id,name,screens) VALUES(?,?,?)
            ON CONFLICT(wall_id,name) DO UPDATE SET screens=excluded.screens""",
                   (int(wall_id), name, json.dumps(screens)))


def delete_scene(scene_id):
    with storage.database() as db:
        _ensure(db)
        db.execute("DELETE FROM video_wall_scenes WHERE id=?", (int(scene_id),))


def apply_scene(wall_id, scene_id):
    scene = next((s for s in load_scenes(wall_id) if s["id"] == int(scene_id)), None)
    if scene is None:
        raise ValueError("Сценарий не найден")
    return save_screens(wall_id, scene["screens"])


def missing_cameras(screens, device_ids):
    known = set(device_ids)
    return sorted({c for s in screens for c in s["cameras"] if c and c not in known})
