#!/usr/bin/env python3
"""Значок Sentinel Server в трее: состояние сервера и меню управления.

GTK 4 не умеет значки в трее, поэтому значок — отдельный процесс без GTK: он сам публикует
StatusNotifierItem и меню com.canonical.dbusmenu по D-Bus (Gio). Это тот же протокол, что у
libayatana-appindicator; в Ubuntu его показывает встроенное расширение «Ubuntu AppIndicators»,
в KDE и др. — штатный трей.

* двойной щелчок по значку — окно настроек сервера (в GNOME одиночный щелчок открывает меню);
* правая кнопка — меню: Перезапустить, Остановить, Запустить/возобновить, Выход;
* «Выход» останавливает службу (systemd завершает все её процессы, включая ffmpeg записи),
  закрывает окно настроек и сам значок.

Если сервер запущен, а администратора ещё нет, значок сам открывает окно «Задайте пароль».

Запуск: ``sentinel-server-tray`` (автозапуск при входе в систему); ``--show`` — сразу открыть
окно настроек (так значок запускается из меню приложений).
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gi.repository import Gio, GLib  # noqa: E402

import sentinel_server_ctl as ctl  # noqa: E402

ROOT = Path(__file__).resolve().parent
POLL_SECONDS = 3
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"
ICON_DIRS = (Path("/usr/share/sentinel-server/icons"), ROOT / "packaging" / "icons")
ICONS = {"running": "sentinel-server-running", "warning": "sentinel-server-warning",
         "starting": "sentinel-server-warning", "error": "sentinel-server-error",
         "stopped": "sentinel-server-stopped", "missing": "sentinel-server-error"}

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/>
    </method>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""

# пункты меню: id → (ключ действия или None, тип)
M_STATUS, M_SEP1, M_SETTINGS, M_SEP2, M_RESTART, M_STOP, M_START, M_SEP3, M_EXIT = range(1, 10)


def icon_theme_path():
    for directory in ICON_DIRS:
        if (directory / "sentinel-server-running.svg").exists():
            return str(directory)
    return ""


def menu_items(status, busy=""):
    """Пункты меню для состояния сервера (чистая функция — проверяется тестами)."""
    installed = status is not None and status.state != "missing"
    active = bool(status and status.active)
    text = busy or (status.text if status else "Проверяю состояние…")
    return [
        (M_STATUS, {"label": text, "enabled": False}),
        (M_SEP1, {"type": "separator"}),
        (M_SETTINGS, {"label": "Настройки сервера…"}),
        (M_SEP2, {"type": "separator"}),
        (M_RESTART, {"label": "Перезапустить", "enabled": installed and not busy}),
        (M_STOP, {"label": "Остановить", "enabled": installed and not busy and status.state != "stopped"}),
        (M_START, {"label": "Запустить / возобновить", "enabled": installed and not busy and not active}),
        (M_SEP3, {"type": "separator"}),
        (M_EXIT, {"label": "Выход (остановить сервер)", "enabled": not busy}),
    ]


def _variant_props(props):
    out = {}
    for key, value in props.items():
        if isinstance(value, bool):
            out[key] = GLib.Variant("b", value)
        else:
            out[key] = GLib.Variant("s", str(value))
    return out


def settings_command(*args):
    installed = shutil.which("sentinel-server-settings")
    if installed:
        return [installed, *args]
    return [sys.executable, str(ROOT / "sentinel_server_settings.py"), *args]


def notify(title, body="", urgency="normal"):
    if shutil.which("notify-send"):
        subprocess.Popen(["notify-send", "-a", "Sentinel Server", "-u", urgency, "-i", "io.sentinel.Server",
                          title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Tray:
    def __init__(self, loop, show_settings=False):
        self.loop = loop
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.status = None
        self.busy = ""
        self.revision = 1
        self.setup_opened = False
        self._polling = False
        self.theme_path = icon_theme_path()
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.bus.register_object(ITEM_PATH, sni, self._sni_call, self._sni_get, None)
        self.bus.register_object(MENU_PATH, menu, self._menu_call, self._menu_get, None)
        Gio.bus_own_name_on_connection(self.bus, self.bus_name, Gio.BusNameOwnerFlags.NONE, None, None)
        Gio.bus_watch_name_on_connection(self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE, self._watcher_up, None)
        if show_settings:
            self.open_settings()
        self.poll()
        GLib.timeout_add_seconds(POLL_SECONDS, self._tick)

    # ------------------------------------------------------------ StatusNotifierItem
    def _watcher_up(self, connection, _name, _owner):
        connection.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                        GLib.Variant("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE, -1, None, None)

    @property
    def icon_name(self):
        return ICONS.get(self.status.state if self.status else "starting", "sentinel-server-warning")

    def tooltip(self):
        if not self.status:
            return "Sentinel Server", "Проверяю состояние…"
        lines = [self.status.text]
        if self.status.detail:
            lines.append(self.status.detail.splitlines()[0])
        if self.status.active and self.status.urls:
            lines.append(self.status.urls[0])
        return "Sentinel Server", "\n".join(lines)

    def _sni_get(self, _conn, _sender, _path, _iface, name):
        title, description = self.tooltip()
        values = {
            "Category": GLib.Variant("s", "SystemServices"),
            "Id": GLib.Variant("s", "sentinel-server"),
            "Title": GLib.Variant("s", "Sentinel Server"),
            "Status": GLib.Variant("s", "NeedsAttention" if self.status and self.status.state == "error"
                                   else "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconThemePath": GLib.Variant("s", self.theme_path),
            "IconPixmap": GLib.Variant("a(iiay)", []),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", self.icon_name),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon_name, [], title, description)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name)

    def _sni_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            # GNOME (AppIndicators) вызывает Activate по двойному щелчку, KDE — по одиночному
            self.open_settings()
        invocation.return_value(None)

    def _emit_sni(self, *signals):
        for signal in signals:
            args = GLib.Variant("(s)", (self._sni_get(None, None, None, None, "Status").unpack(),)) \
                if signal == "NewStatus" else None
            self.bus.emit_signal(None, ITEM_PATH, "org.kde.StatusNotifierItem", signal, args)

    # ------------------------------------------------------------ dbusmenu
    def _items(self):
        return menu_items(self.status, self.busy)

    def _layout(self):
        children = [GLib.Variant("(ia{sv}av)", (item_id, _variant_props(props), []))
                    for item_id, props in self._items()]
        root = (0, {"children-display": GLib.Variant("s", "submenu")}, children)
        return root

    def _menu_get(self, _conn, _sender, _path, _iface, name):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"),
                "IconThemePath": GLib.Variant("as", [self.theme_path] if self.theme_path else [])}.get(name)

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        items = dict(self._items())
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            result = [(i, _variant_props(items[i])) for i in (ids or list(items)) if i in items]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (result,)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            value = items.get(item_id, {}).get(name)
            if value is None:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error", "Нет такого свойства")
                return
            invocation.return_value(GLib.Variant("(v)", (_variant_props({name: value})[name],)))
        elif method == "Event":
            item_id, event_id, _data, _ts = params.unpack()
            invocation.return_value(None)
            if event_id == "clicked":
                GLib.idle_add(lambda: self.on_menu(item_id) and False)
        elif method == "EventGroup":
            events = params.unpack()[0]
            invocation.return_value(GLib.Variant("(ai)", ([e[0] for e in events if e[0] not in items],)))
            for item_id, event_id, _data, _ts in events:
                if event_id == "clicked":
                    GLib.idle_add(lambda i=item_id: self.on_menu(i) and False)
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _menu_changed(self):
        self.revision += 1
        self.bus.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                             GLib.Variant("(ui)", (self.revision, 0)))

    # ------------------------------------------------------------ состояние
    def _tick(self):
        self.poll()
        return True

    def poll(self):
        if self._polling:
            return
        self._polling = True

        def worker():
            try:
                status = ctl.get_status()
            except Exception as exc:   # noqa: BLE001 — значок не должен падать
                status = ctl.Status("error", "Не удалось проверить сервер", str(exc))
            GLib.idle_add(lambda: self.apply_status(status) and False)
        threading.Thread(target=worker, daemon=True).start()

    def apply_status(self, status):
        self._polling = False
        old = self.status
        self.status = status
        changed = old is None or (old.state, old.text, old.detail, old.urls) != \
            (status.state, status.text, status.detail, status.urls)
        if changed:
            self._emit_sni("NewIcon", "NewToolTip", "NewStatus", "NewAttentionIcon")
            self._menu_changed()
            if old is not None and old.state != status.state:
                if status.state == "error":
                    notify("Sentinel Server: ошибка", f"{status.text}\n{status.detail}", "critical")
                elif old.state == "error" and status.state == "running":
                    notify("Sentinel Server снова работает")
        if status.setup_required and not self.setup_opened:
            self.setup_opened = True
            notify("Sentinel Server: первый запуск", "Задайте логин и пароль администратора")
            self.open_settings("--setup")

    # ------------------------------------------------------------ действия
    def open_settings(self, *args):
        try:
            subprocess.Popen(settings_command(*args), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            notify("Не удалось открыть настройки", str(exc), "critical")

    def on_menu(self, item_id):
        if item_id == M_SETTINGS:
            self.open_settings()
        elif item_id in (M_RESTART, M_STOP, M_START):
            action = {M_RESTART: "restart", M_STOP: "stop", M_START: "start"}[item_id]
            titles = {"restart": "Перезапуск…", "stop": "Остановка…", "start": "Запуск…"}
            self.run_action(titles[action], lambda: ctl.service(action))
        elif item_id == M_EXIT:
            self.run_action("Остановка сервера…", self.stop_everything, quit_after=True)

    def stop_everything(self):
        """Выход: служба (со всеми дочерними процессами) и окна настроек этого пользователя."""
        if self.status is None or self.status.state != "missing":
            props = ctl.unit_properties()
            if props.get("ActiveState") not in ("inactive", "", None) or props.get("SubState") == "auto-restart":
                ctl.service("stop")
        subprocess.run(["pkill", "-u", str(os.getuid()), "-f", "sentinel_server_settings|sentinel-server-settings"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def run_action(self, busy_text, func, quit_after=False):
        if self.busy:
            return
        self.busy = busy_text
        self._menu_changed()

        def worker():
            try:
                func()
                error = None
            except Exception as exc:   # noqa: BLE001
                error = exc
            GLib.idle_add(lambda: self._action_done(error, quit_after) and False)
        threading.Thread(target=worker, daemon=True).start()

    def _action_done(self, error, quit_after):
        self.busy = ""
        if error:
            notify("Sentinel Server", str(error), "critical")
        elif quit_after:
            self.loop.quit()
            return
        self._menu_changed()
        self.poll()


def acquire_lock():
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/sentinel-{os.getuid()}")
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = open(runtime / "sentinel-server-tray.lock", "w")   # noqa: SIM115 — держим до выхода
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0
    show = "--show" in argv
    lock = acquire_lock()
    if lock is None:
        # значок уже есть — повторный запуск из меню просто открывает настройки
        if show:
            subprocess.Popen(settings_command(), start_new_session=True)
        return 0
    loop = GLib.MainLoop()
    try:
        Tray(loop, show_settings=show)
    except GLib.Error as exc:
        print(f"Нет сеанса D-Bus для значка в трее: {exc.message}", file=sys.stderr)
        return 1
    for sig in (2, 15):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: loop.quit() or False)
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
