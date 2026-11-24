"""Каталог модулей и модель вкладок оболочки в стиле iVMS-4200 (без GTK)."""
from dataclasses import dataclass

HOME = "home"


@dataclass(frozen=True)
class Module:
    key: str          # ключ вкладки
    title: str
    icon: str
    section: str      # группа плиток на домашнем экране
    page: str | None  # имя страницы Gtk.Stack; None — заглушка «в разработке»


MODULES = (
    Module("main_view", "Главный просмотр", "▦", "Операции", "overview"),
    Module("playback", "Удалённое воспроизведение", "◷", "Операции", "archive"),
    Module("events", "Центр событий", "⚑", "Операции", "alarms"),
    Module("emap", "Электронная карта", "⌖", "Операции", "emap"),
    Module("access", "Контроль доступа", "▣", "Операции", "access"),
    Module("summary", "Обзор системы", "◈", "Операции", "dashboard"),
    Module("devices", "Управление устройствами", "◉", "Управление", "devices"),
    Module("event_config", "Настройка событий", "⚙", "Управление", None),
    Module("storage", "Расписание записи", "▤", "Управление", None),
    Module("accounts", "Учётные записи", "☺", "Управление", None),
    Module("logs", "Журнал системы", "☰", "Обслуживание", None),
    Module("settings", "Системные настройки", "✦", "Обслуживание", None),
)
BY_KEY = {m.key: m for m in MODULES}


def module(key):
    return BY_KEY.get(key)


def page_name(key):
    """Имя страницы Gtk.Stack для вкладки."""
    if key == HOME:
        return HOME
    item = BY_KEY.get(key)
    if item is None:
        return None
    return item.page or f"stub:{key}"


def sections():
    """[(название группы, [Module, ...]), ...] в порядке каталога."""
    result = []
    for item in MODULES:
        if not result or result[-1][0] != item.section:
            result.append((item.section, []))
        result[-1][1].append(item)
    return result


class TabModel:
    """Открытые вкладки; «Главная» всегда есть и закрыть её нельзя."""

    def __init__(self):
        self.tabs = []
        self.active = HOME

    def open(self, key):
        if key != HOME and key not in BY_KEY:
            raise KeyError(key)
        if key != HOME and key not in self.tabs:
            self.tabs.append(key)
        self.active = key
        return key

    def close(self, key):
        """Закрыть вкладку; вернуть ключ, который нужно показать."""
        if key == HOME or key not in self.tabs:
            return self.active
        index = self.tabs.index(key)
        self.tabs.remove(key)
        if self.active == key:
            self.active = self.tabs[index - 1] if index > 0 else (self.tabs[0] if self.tabs else HOME)
        return self.active
