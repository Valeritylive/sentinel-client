"""Вкладки видов, группы видов, ярлыки и адаптивная раскладка (без GTK).

Модель повторяет поведение клиента HikCentral Lite: верхние вкладки — это
живые виды (сетка + назначенные ресурсы), «+» создаёт «Новый вид N»,
изменённая вкладка помечается «*», её можно сохранить, сохранить как,
открыть в отдельном окне или закрыть.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import sentinel_storage as storage

# ---------------------------------------------------------------- ресурсы ячеек
KIND_CAMERA, KIND_WEB, KIND_DOOR, KIND_MAP = "camera", "web", "door", "map"
KINDS = (KIND_CAMERA, KIND_WEB, KIND_DOOR, KIND_MAP)


def normalize_mapping(mapping):
    """Привести сохранённое назначение к списку {slot, kind, ...}; мусор отбросить.

    Старые виды хранили только {"slot", "device_id"} — это камеры.
    """
    result = []
    seen = set()
    for item in mapping or []:
        if not isinstance(item, dict):
            continue
        try:
            slot = int(item.get("slot"))
        except (TypeError, ValueError):
            continue
        if slot < 0 or slot in seen:
            continue
        kind = item.get("kind") or KIND_CAMERA
        if kind not in KINDS:
            continue
        entry = {"slot": slot, "kind": kind}
        if kind in (KIND_CAMERA, KIND_DOOR):
            try:
                entry["device_id"] = int(item["device_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if kind == KIND_DOOR:
                entry["door"] = max(1, int(item.get("door") or 1))
        elif kind == KIND_WEB:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            entry["url"] = url
            entry["name"] = str(item.get("name") or url)
        elif kind == KIND_MAP:
            try:
                entry["map_id"] = int(item["map_id"])
            except (KeyError, TypeError, ValueError):
                continue
        seen.add(slot)
        result.append(entry)
    return sorted(result, key=lambda e: e["slot"])


def drop_devices(mapping, device_ids):
    ids = set(device_ids)
    return [m for m in normalize_mapping(mapping) if m.get("device_id") not in ids]


# ---------------------------------------------------------------- вкладки видов
@dataclass
class ViewTab:
    key: int
    name: str
    division: str = "s4"
    layout: int = 4
    grid_columns: int = 2
    grid_rows: int = 2
    mapping: list = field(default_factory=list)
    view_id: int | None = None
    dirty: bool = False
    server_view_id: int | None = None   # вид на сервере Sentinel (приватный или публичный)

    @property
    def title(self):
        return self.name + ("*" if self.dirty else "")


class ViewTabs:
    """Открытые вкладки видов. Всегда открыта хотя бы одна."""

    def __init__(self):
        self.tabs: list[ViewTab] = []
        self.active: int | None = None
        self._next_key = 1
        self._new_counter = 0

    # создание и поиск
    def new_tab(self, name=None, **values):
        if name is None:
            self._new_counter += 1
            name = f"Новый вид {self._new_counter}"
        tab = ViewTab(self._next_key, name, **values)
        self._next_key += 1
        self.tabs.append(tab)
        self.active = tab.key
        return tab

    def get(self, key):
        return next((t for t in self.tabs if t.key == key), None)

    def current(self):
        return self.get(self.active)

    def find_view(self, view_id):
        return next((t for t in self.tabs if view_id is not None and t.view_id == view_id), None)

    def open_saved(self, view):
        """Открыть сохранённый вид (dict из БД); уже открытый — активировать."""
        tab = self.find_view(view["id"])
        if tab:
            self.active = tab.key
            return tab, False
        return self.new_tab(view["name"], **saved_view_values(view)), True

    def activate(self, key):
        if self.get(key) is None:
            raise KeyError(key)
        self.active = key
        return self.current()

    def close(self, key):
        """Закрыть вкладку; вернуть новую активную (последнюю вкладку не закрываем — создаём пустую)."""
        tab = self.get(key)
        if tab is None:
            return self.current()
        index = self.tabs.index(tab)
        self.tabs.remove(tab)
        if not self.tabs:
            return self.new_tab()
        if self.active == key:
            self.active = self.tabs[max(0, index - 1)].key
        return self.current()

    def mark_dirty(self, key=None, dirty=True):
        tab = self.get(self.active if key is None else key)
        if tab:
            tab.dirty = dirty
        return tab


def saved_view_values(view):
    try:
        mapping = json.loads(view.get("mapping") or "[]")
    except (TypeError, json.JSONDecodeError):
        mapping = []
    layout = int(view.get("layout") or 4)
    cols = int(view.get("grid_columns") or 0)
    rows = int(view.get("grid_rows") or 0)
    return {"division": view.get("division") or "", "layout": layout,
            "grid_columns": cols, "grid_rows": rows,
            "mapping": normalize_mapping(mapping), "view_id": view["id"]}


# ---------------------------------------------------------------- хранение видов и групп
def load_view_groups():
    with storage.database() as db:
        return [dict(r) for r in db.execute("SELECT * FROM view_groups ORDER BY name COLLATE NOCASE")]


def add_view_group(name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Пустое название группы видов")
    with storage.database() as db:
        row = db.execute("SELECT id FROM view_groups WHERE name=?", (name,)).fetchone()
        if row:
            return row["id"]
        return db.execute("INSERT INTO view_groups(name) VALUES(?)", (name,)).lastrowid


def delete_view_group(group_id):
    with storage.database() as db:
        db.execute("UPDATE saved_views SET group_id=NULL WHERE group_id=?", (group_id,))
        db.execute("DELETE FROM view_groups WHERE id=?", (group_id,))


def load_views():
    with storage.database() as db:
        return [dict(r) for r in db.execute("SELECT * FROM saved_views ORDER BY name COLLATE NOCASE")]


def save_view(name, tab: ViewTab, group_id=None, view_id=None):
    """Создать (view_id=None) или перезаписать вид. Возвращает id. IntegrityError — имя занято."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Введите название вида")
    values = (name, tab.layout, json.dumps(normalize_mapping(tab.mapping), ensure_ascii=False),
              tab.grid_columns, tab.grid_rows, tab.division or "", group_id)
    with storage.database() as db:
        if view_id is None:
            return db.execute("""INSERT INTO saved_views(name,layout,mapping,grid_columns,grid_rows,division,group_id)
                VALUES(?,?,?,?,?,?,?)""", values).lastrowid
        db.execute("""UPDATE saved_views SET name=?,layout=?,mapping=?,grid_columns=?,grid_rows=?,division=?,
            group_id=COALESCE(?,group_id) WHERE id=?""", values + (view_id,))
        return view_id


def rename_view(view_id, name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Введите название вида")
    with storage.database() as db:
        db.execute("UPDATE saved_views SET name=? WHERE id=?", (name, view_id))


def move_view(view_id, group_id):
    with storage.database() as db:
        db.execute("UPDATE saved_views SET group_id=? WHERE id=?", (group_id, view_id))


def delete_view(view_id):
    with storage.database() as db:
        db.execute("DELETE FROM saved_views WHERE id=?", (view_id,))


def view_tree(groups, views):
    """[(группа|None, [виды])]: группы по имени, затем виды без группы (как в дереве «Вид»)."""
    by_group = {}
    for view in views:
        by_group.setdefault(view.get("group_id"), []).append(view)
    ids = {g["id"] for g in groups}
    tree = [(g, by_group.get(g["id"], [])) for g in groups]
    loose = [v for gid, items in by_group.items() if gid not in ids for v in items]
    tree.append((None, loose))
    return tree


# ---------------------------------------------------------------- веб-страницы
def normalize_url(url):
    url = (url or "").strip()
    if not url:
        raise ValueError("Введите адрес страницы")
    if "://" not in url:
        url = "https://" + url
    scheme = url.split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        raise ValueError("Поддерживаются только адреса http:// и https://")
    return url


def load_web_pages():
    with storage.database() as db:
        return [dict(r) for r in db.execute("SELECT * FROM web_pages ORDER BY name COLLATE NOCASE")]


def add_web_page(name, url):
    url = normalize_url(url)
    name = (name or "").strip() or url.split("://", 1)[1].split("/", 1)[0]
    with storage.database() as db:
        return db.execute("INSERT INTO web_pages(name,url) VALUES(?,?)", (name, url)).lastrowid


def delete_web_page(page_id):
    with storage.database() as db:
        db.execute("DELETE FROM web_pages WHERE id=?", (page_id,))


# ---------------------------------------------------------------- ярлыки
@dataclass(frozen=True)
class Shortcut:
    key: str
    title: str
    icon: str
    section: str


SHORTCUTS = (
    Shortcut("add_device", "Добавить устройство", "▭", "Система"),
    Shortcut("add_person", "Добавить персону", "☺", "Система"),
    Shortcut("import_persons", "Импорт персон из CSV", "⇩", "Система"),
    Shortcut("manage_vehicles", "Управление ТС", "⛟", "Система"),
    Shortcut("event_config", "Настройка событий и тревог", "⚑", "Система"),
    Shortcut("add_user", "Добавить пользователя", "☻", "Система"),
    Shortcut("system_storage", "Системное хранилище", "▤", "Система"),
    Shortcut("authorize_access", "Назначить уровень доступа", "▣", "Контроль доступа"),
    Shortcut("quick_lock", "Быстрая блокировка/разблокировка", "⚿", "Контроль доступа"),
    Shortcut("access_records", "Журнал проходов", "☰", "Контроль доступа"),
)
SHORTCUT_KEYS = {s.key: s for s in SHORTCUTS}
DEFAULT_SHORTCUTS = ("add_device", "add_person", "authorize_access", "quick_lock",
                     "manage_vehicles", "system_storage")


def load_shortcuts():
    with storage.database() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key='shortcuts'").fetchone()
    if not row:
        return list(DEFAULT_SHORTCUTS)
    try:
        keys = json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        return list(DEFAULT_SHORTCUTS)
    return [k for k in dict.fromkeys(keys) if k in SHORTCUT_KEYS]


def save_shortcuts(keys):
    keys = [k for k in dict.fromkeys(keys) if k in SHORTCUT_KEYS]
    storage.set_setting("shortcuts", json.dumps(keys))
    return keys


# ---------------------------------------------------------------- адаптивность
@dataclass(frozen=True)
class Adaptive:
    left: bool          # показывать левую панель
    right: bool         # показывать правую панель тревог
    compact_top: bool   # подписи кнопок верхней панели скрыты (только значки)
    compact_bar: bool   # нижняя полоса в одну строку без фильтров
    left_width: int
    right_width: int


def adaptive_layout(width, user_left=True, user_right=True):
    """Решить, какие панели помещаются при ширине окна width (px).

    Пользовательский выбор (скрыл/показал панель) уважается, но на узком окне
    панели убираются автоматически, чтобы видео не сжималось до полосы.
    """
    width = max(0, int(width or 0))
    left_width = 260 if width >= 1500 else 220
    right_width = 260 if width >= 1500 else 220
    left = user_left and width >= 900
    right = user_right and width >= 1240
    return Adaptive(left=left, right=right, compact_top=width < 1100, compact_bar=width < 820,
                    left_width=left_width, right_width=right_width)
