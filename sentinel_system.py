"""Окно «Система» в стиле HikCentral Lite.

Вкладки: Управление устройствами · Персона · Транспорт · Уровень доступа ·
Видеодомофон · Событие и тревога · Учётная запись и безопасность · Конфигурация системы.
"""
from __future__ import annotations

import io
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from gi.repository import GLib, Gtk

import sentinel_alarms as alarms
import sentinel_devmgmt as devmgmt
import sentinel_discovery as discovery
import sentinel_hikconnect as hikconnect
import sentinel_people as people
import sentinel_recording as recording
import sentinel_security as security
import sentinel_storage as storage
import sentinel_widgets as W

TABS = (("devices", "Управление устройствами", "devices"), ("person", "Персона", "persons"),
        ("vehicle", "Транспорт", "persons"), ("access", "Уровень доступа", "access"),
        ("intercom", "Видеодомофон", "access"), ("event", "Событие и тревога", "alarms"),
        ("accounts", "Учётная запись и безопасность", "users"), ("config", "Конфигурация системы", "system"))


def _field(grid, row, title, widget):
    grid.attach(W.label(title, "muted"), 0, row, 1, 1)
    widget.set_hexpand(True)
    grid.attach(widget, 1, row, 1, 1)
    return widget


def _form():
    grid = Gtk.Grid(column_spacing=12, row_spacing=9)
    W.margins(grid, 14)
    return grid


def _dialog(parent, title, accept="Сохранить", width=520, height=-1):
    dialog = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    dialog.set_default_size(width, height)
    dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
    if accept:
        dialog.add_button(accept, Gtk.ResponseType.ACCEPT).add_css_class("red-button")
    return dialog


def _dropdown(strings, selected=0):
    widget = Gtk.DropDown.new_from_strings(list(strings))
    if strings:
        widget.set_selected(max(0, min(selected, len(strings) - 1)))
    return widget


def _toolbar(*items):
    bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    for item in items:
        bar.append(item)
    return bar


def _spacer():
    box = Gtk.Box()
    box.set_hexpand(True)
    return box


def _scroll(child):
    scroll = Gtk.ScrolledWindow()
    scroll.set_vexpand(True)
    scroll.set_hexpand(True)
    scroll.set_child(child)
    return scroll


class SystemWindow(Gtk.Window):
    def __init__(self, owner):
        super().__init__(title="Система")
        self.owner = owner
        self.set_transient_for(owner)
        self.set_default_size(*owner._initial_window_size(1280, 820))
        self.set_hide_on_close(True)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        tabs.add_css_class("system-tabs")
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_vexpand(True)
        self.tab_buttons = {}
        first = None
        builders = {"devices": self._devices_page, "person": self._persons_page, "vehicle": self._vehicles_page,
                    "access": self._access_page, "intercom": self._intercom_page, "event": self._event_page,
                    "accounts": self._accounts_page, "config": self._config_page}
        for key, title, _perm in TABS:
            button = Gtk.ToggleButton(label=title)
            if first:
                button.set_group(first)
            else:
                first = button
            button.connect("toggled", lambda b, k=key: b.get_active() and self._show(k))
            tabs.append(button)
            self.tab_buttons[key] = button
            self.stack.add_named(builders[key](), key)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroller.set_child(tabs)
        root.append(scroller)
        root.append(self.stack)
        self.set_child(root)
        first.set_active(True)
        self._show("devices")

    # ------------------------------------------------------------------ навигация
    def _show(self, key):
        self.stack.set_visible_child_name(key)
        refresh = {"devices": self.refresh_devices, "person": self.refresh_persons,
                   "vehicle": self.refresh_vehicles, "access": self.refresh_access,
                   "event": self.refresh_rules, "accounts": self.refresh_accounts}.get(key)
        if refresh:
            refresh()

    def open(self, section=None, sub=None):
        allowed = {k for k, _t, perm in TABS if self.owner.session.can(perm)}
        for key, button in self.tab_buttons.items():
            button.set_sensitive(key in allowed)
        section = section or "devices"
        if section not in allowed:
            section = next(iter(allowed), None)
            if section is None:
                self.owner.notify("Нет прав на системную конфигурацию", error=True)
                return
        self.tab_buttons[section].set_active(True)
        self._show(section)
        if sub == "add" and section == "devices":
            self.show_add_device()
        elif sub == "hikconnect" and section == "devices":
            self.show_add_device("hikconnect")
        elif sub and sub.startswith("details:") and section == "devices":
            self.show_device_details(int(sub.split(":", 1)[1]))
        elif sub == "add" and section == "person":
            self.edit_person(None)
        elif sub == "import" and section == "person":
            self.import_persons()
        elif sub == "add" and section == "accounts":
            self.edit_user(None)
        elif sub and section == "config":
            self.config_nav[sub].set_active(True)
        self.present()

    # ================================================================== устройства
    def _devices_page(self):
        self.devices_stack = Gtk.Stack()
        list_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(list_page, 12)
        switch = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        switch.set_halign(Gtk.Align.CENTER)
        self.device_category = "video"
        first = None
        for key, title in (("video", "Видео"), ("access", "Контроль доступа"), ("areas", "Области")):
            button = Gtk.ToggleButton(label=title)
            button.add_css_class("filter-button")
            if first:
                button.set_group(first)
            else:
                first = button
                button.set_active(True)
            button.connect("toggled", lambda b, k=key: b.get_active() and self._device_category(k))
            switch.append(button)
        list_page.append(switch)
        self.device_body = Gtk.Stack()
        table_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.abnormal_label = W.label("● Неисправные (0)", "status-error")
        self.normal_label = W.label("● Нормальные (0)", "status-online")
        self.device_search = Gtk.SearchEntry(placeholder_text="Поиск")
        self.device_search.connect("search-changed", lambda *_: self.refresh_devices())
        table_box.append(_toolbar(self.abnormal_label, self.normal_label,
                                  W.button("⟳ Обновить всё", lambda: self.owner.check_all_devices(quiet=False)),
                                  W.button("Удалить", self.delete_selected_devices),
                                  W.button("Синхр. время", self.sync_selected_time),
                                  W.button("Экспорт CSV", self.owner.export_devices_csv),
                                  _spacer(), self.device_search))
        self.device_table = W.Table(("Имя", "IP-адрес", "Состояние", "Область", "Серийный номер", "Версия",
                                     "Каналы"), on_activate=self.show_device_details,
                                    on_context=self._device_context)
        table_box.append(_scroll(self.device_table))
        table_box.append(_toolbar(W.button("Добавить устройство", self.show_add_device, "red-button"),
                                  W.button("Пакетное обновление", lambda: self.owner.notify(
                                      "Обновление прошивки выполняется в веб-интерфейсе устройства или Hik-Tools: "
                                      "откройте «Удалённая настройка» в меню устройства."))))
        self.device_body.add_named(table_box, "table")
        self.device_body.add_named(self.owner._group_page(), "areas")
        self.device_body.set_vexpand(True)
        list_page.append(self.device_body)
        self.devices_stack.add_named(list_page, "list")
        self.devices_stack.add_named(self._add_device_page(), "add")
        self.details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.devices_stack.add_named(self.details_box, "details")
        return self.devices_stack

    def _device_category(self, key):
        if key == "areas":
            self.device_body.set_visible_child_name("areas")
            self.owner._render_groups()
            return
        self.device_category = key
        self.device_body.set_visible_child_name("table")
        self.refresh_devices()

    def _physical_rows(self):
        rows = devmgmt.physical_devices(self.owner.devices, self.owner.groups)
        by_id = {d["id"]: d for d in self.owner.devices}
        result = []
        for row in rows:
            first = by_id[row["ids"][0]]
            row["category"] = first.get("category") or "video"
            row["status"] = first.get("status", "")
            row["firmware"] = first.get("firmware", "")
            row["device"] = first
            result.append(row)
        return result

    def refresh_devices(self):
        rows = [r for r in self._physical_rows() if r["category"] == self.device_category]
        rows = devmgmt.filter_rows(rows, self.device_search.get_text())
        bad = sum(1 for r in rows if r["status"] == "offline")
        self.abnormal_label.set_text(f"● Неисправные ({bad})")
        self.normal_label.set_text(f"● Нормальные ({sum(1 for r in rows if r['status'] == 'online')})")
        table_rows = []
        for row in rows:
            badge = {"online": W.status_badge("В сети", "ok"), "offline": W.status_badge("Не в сети", "bad")}.get(
                row["status"], W.status_badge("Не проверено", "unknown"))
            table_rows.append((row["name"], row["host"], badge, row["group_name"] or "—",
                               row["serial_no"] or "—", row["firmware"] or "—", row["channels"]))
        self.device_rows = rows
        self.device_table.set_rows(table_rows, [r["ids"][0] for r in rows])

    def _selected_rows(self):
        keys = set(self.device_table.selected())
        return [r for r in self.device_rows if r["ids"][0] in keys]

    def delete_selected_devices(self):
        rows = self._selected_rows()
        if not rows:
            self.owner.notify("Отметьте устройства")
            return
        ids = [i for r in rows for i in r["ids"]]
        self.owner._confirm(f"Удалить устройств: {len(rows)} (каналов: {len(ids)})?",
                            lambda: (self.owner.delete_devices(ids), self.refresh_devices()))

    def sync_selected_time(self):
        rows = self._selected_rows() or self.device_rows
        for row in rows:
            device = row["device"]
            credentials = self.owner.session_credentials.get(device["id"])
            if credentials and device["protocol"] == "hikvision":
                threading.Thread(target=self.owner._sync_time_worker, args=(device, credentials), daemon=True).start()
        self.owner.notify(f"Синхронизация времени отправлена на {len(rows)} устройств")

    def _device_context(self, key, widget, x, y):
        row = next((r for r in self.device_rows if r["ids"][0] == key), None)
        if not row:
            return
        device = row["device"]
        W.popover_menu(widget, [
            ("Подробности", lambda: self.show_device_details(key)),
            ("Удалённая настройка", lambda: self.owner.open_remote_configuration(device)),
            ("Настройка тревог", lambda: self.tab_buttons["event"].set_active(True)),
            ("Журнал устройства (последние события)", lambda: (self.owner._show_module("alarms"))),
            ("Переименовать", lambda: self.owner._ask_text("Название", row["name"], lambda n: (
                self.owner.rename_physical_device(row, n), self.refresh_devices()))),
            ("Обновить состояние", lambda: self.owner.check_all_devices([device], quiet=False)),
            None, ("Удалить", lambda: self.owner._confirm(f"Удалить {row['name']}?", lambda: (
                self.owner.delete_devices(row["ids"]), self.refresh_devices())))], x, y)

    # ------------------------------------------------------------------ добавление
    def _add_device_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(page, 12)
        page.append(_toolbar(W.button("‹ Назад", lambda: self.devices_stack.set_visible_child_name("list"), flat=True),
                             W.label("Система › Управление устройствами › Добавить устройство", "muted")))
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        methods = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        methods.set_size_request(240, -1)
        methods.append(W.label("1. Способ добавления", "section-title"))
        self.add_methods = Gtk.Stack()
        self.add_method_cards = {}
        first = None
        for key, title, text in (
                ("auto", "Автопоиск", "Устройство и клиент в одной локальной сети (поиск SADP)."),
                ("batch", "Пакетный импорт", "IP, порты и учётные данные нескольких устройств из CSV."),
                ("manual", "Вручную", "Известен адрес одного устройства или диапазон IP."),
                ("hikconnect", "Hik-Connect", "Устройства, привязанные к облачному аккаунту Hikvision (P2P).")):
            card = Gtk.ToggleButton()
            card.add_css_class("method-card")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            inner.append(W.label(title, "section-title"))
            inner.append(W.label(text, "muted", wrap=True))
            card.set_child(inner)
            if first:
                card.set_group(first)
            else:
                first = card
            card.connect("toggled", lambda b, k=key: b.get_active() and self._select_add_method(k))
            self.add_method_cards[key] = card
            methods.append(card)
        body.append(methods)

        auto = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.scan_status = W.label("Нажмите «Обновить», чтобы найти устройства в сети.", "muted")
        auto.append(_toolbar(W.label("2. Выбор устройств", "section-title"), _spacer(), self.scan_status,
                             W.button("⟳ Обновить", self.scan_network)))
        self.scan_table = W.Table(("Адрес", "Серийный номер", "Модель", "Порт", "HTTP", "Прошивка", "Активация"))
        auto.append(_scroll(self.scan_table))
        auto.append(W.label("3. Данные для входа", "section-title"))
        form = _form()
        self.scan_user = _field(form, 0, "Имя пользователя *", Gtk.Entry(text="admin"))
        self.scan_password = _field(form, 1, "Пароль *", Gtk.PasswordEntry(show_peek_icon=True))
        self.scan_type = _field(form, 2, "Тип", _dropdown(["Видео (NVR/DVR/камера)", "Контроль доступа"]))
        self.scan_area = _field(form, 3, "Область", Gtk.Entry(placeholder_text="Необязательно"))
        auto.append(form)
        auto.append(_toolbar(_spacer(), W.button("Добавить", lambda: self.add_scanned(False), "red-button"),
                             W.button("Добавить и продолжить", lambda: self.add_scanned(True)),
                             W.button("Отмена", lambda: self.devices_stack.set_visible_child_name("list"))))
        self.scan_results = []
        self.add_methods.add_named(auto, "auto")

        batch = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        batch.append(W.label("Формат CSV (первая строка — заголовок):", "section-title"))
        batch.append(W.label("Название,Адрес,HTTP-порт,RTSP-порт,Пользователь,Пароль,Тип,Область\n"
                             "Склад,192.168.1.64,80,554,admin,пароль,video,Склад", "muted"))
        batch.append(W.label("Тип: video или access. Пароли сохраняются в системном хранилище Linux.",
                             "muted", wrap=True))
        batch.append(_toolbar(W.button("Скачать шаблон", self.save_batch_template),
                              W.button("Выбрать CSV и импортировать…", self.batch_import, "red-button")))
        self.batch_status = W.label("", "muted", wrap=True)
        batch.append(self.batch_status)
        self.add_methods.add_named(batch, "batch")

        manual = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        manual.append(W.label("Ручное добавление открывает форму с проверкой подключения и "
                              "автоматическим определением каналов (поддерживается диапазон IP).", "muted", wrap=True))
        manual.append(_toolbar(W.button("Открыть форму добавления…", self.owner.add_device, "red-button"),
                               W.button("Добавить контроллер доступа…", self.add_access_controller)))
        self.add_methods.add_named(manual, "manual")
        from sentinel_cloud_ui import HikConnectPanel
        self.hikconnect_panel = HikConnectPanel(self.owner)
        cloud_scroll = Gtk.ScrolledWindow()
        cloud_scroll.set_vexpand(True)
        cloud_scroll.set_child(self.hikconnect_panel)
        self.add_methods.add_named(cloud_scroll, "hikconnect")
        self.add_methods.set_hexpand(True)
        body.append(self.add_methods)
        body.set_vexpand(True)
        page.append(body)
        first.set_active(True)
        return page

    def show_add_device(self, method=None):
        self.devices_stack.set_visible_child_name("add")
        if method and method in self.add_method_cards:
            self.add_method_cards[method].set_active(True)
            self._select_add_method(method)

    def _select_add_method(self, key):
        self.add_methods.set_visible_child_name(key)
        if key == "hikconnect":
            self.hikconnect_panel.refresh()

    def scan_network(self):
        self.scan_status.set_text("Поиск…")

        def worker():
            try:
                found = discovery.discover(3.0)
                error = None
            except OSError as exc:
                found, error = [], str(exc)
            GLib.idle_add(self._scan_done, found, error)

        threading.Thread(target=worker, daemon=True).start()

    def _scan_done(self, found, error):
        known = {d["host"] for d in self.owner.devices}
        self.scan_results = found
        self.scan_status.set_text(f"Ошибка: {error}" if error else f"Найдено устройств: {len(found)}")
        self.scan_table.set_rows([(d["host"] + ("  (добавлено)" if d["host"] in known else ""), d["serial_no"],
                                   d["model"], d["sdk_port"], d["http_port"], d["firmware"],
                                   "Активировано" if d["activated"] else "Не активировано") for d in found])
        return False

    def add_scanned(self, keep_open):
        selected = [self.scan_results[i] for i in self.scan_table.selected()]
        if not selected:
            self.owner.notify("Отметьте устройства в списке")
            return
        if any(not d["activated"] for d in selected):
            self.owner.notify("Неактивированные устройства сначала активируйте (задайте пароль) в SADP или веб-"
                              "интерфейсе", error=True)
            return
        credentials = {"username": self.scan_user.get_text().strip(), "password": self.scan_password.get_text()}
        if not credentials["username"]:
            self.owner.notify("Введите имя пользователя", error=True)
            return
        category = "access" if self.scan_type.get_selected() == 1 else "video"
        area = self.scan_area.get_text().strip()
        templates = [{"name": d["model"] or d["host"], "group_name": area, "host": d["host"], "protocol": "hikvision",
                      "sdk_port": d["sdk_port"], "http_port": d["http_port"], "rtsp_port": 554, "stream": "main",
                      "category": category, "serial_no": d["serial_no"], "device_model": d["model"],
                      "firmware": d["firmware"]} for d in selected]
        self.scan_status.set_text("Подключение…")
        threading.Thread(target=self._add_worker, args=(templates, credentials, keep_open), daemon=True).start()

    def _add_worker(self, templates, credentials, keep_open):
        added, errors = 0, []
        for device in templates:
            try:
                self._probe_and_store(device, credentials)
                added += 1
            except sqlite3.IntegrityError:
                errors.append(f"{device['host']}: уже добавлено")
            except Exception as exc:
                errors.append(f"{device['host']}: {exc}")
        GLib.idle_add(self._add_done, added, errors, keep_open)

    def _probe_and_store(self, device, credentials):
        owner = self.owner
        if device.get("category") == "access":
            doors = 1
            try:
                payload = owner._isapi_request(device, credentials, "/ISAPI/AccessControl/AcsWorkStatus?format=json")
                doors = max(1, len(people.parse_door_status(payload)))
            except Exception as exc:
                if "401" in str(exc):
                    raise RuntimeError("неверный логин или пароль") from exc
            device.update(owner._probe_device_extras(device, credentials))
            device["door_count"] = doors
            channels = [1]
        else:
            channels = owner._discover_hikvision_channels(device, credentials)
            device.update(owner._probe_device_extras(device, credentials))
        return owner._store_probed_device(device, credentials, channels)

    def _add_done(self, added, errors, keep_open):
        self.owner.refresh()
        self.owner.sync_event_subscriptions()
        message = f"Добавлено устройств: {added}" + (f". Ошибки: {'; '.join(errors[:3])}" if errors else "")
        self.scan_status.set_text(message)
        self.owner.notify(message, error=bool(errors))
        if added and not keep_open:
            self.devices_stack.set_visible_child_name("list")
            self.refresh_devices()
        return False

    def save_batch_template(self):
        path = Path.home() / "sentinel_devices_template.csv"
        path.write_text("Название,Адрес,HTTP-порт,RTSP-порт,Пользователь,Пароль,Тип,Область\n"
                        "Склад,192.168.1.64,80,554,admin,,video,Склад\n", encoding="utf-8-sig")
        self.batch_status.set_text(f"Шаблон сохранён: {path}")

    def batch_import(self):
        chooser = Gtk.FileChooserNative.new("CSV со списком устройств", self, Gtk.FileChooserAction.OPEN,
                                            "Импорт", "Отмена")
        self._chooser = chooser

        def done(native, response):
            file = native.get_file()
            native.destroy()
            if response != Gtk.ResponseType.ACCEPT or not file:
                return
            try:
                text = Path(file.get_path()).read_text(encoding="utf-8-sig")
            except OSError as exc:
                self.batch_status.set_text(str(exc))
                return
            import csv
            rows = list(csv.DictReader(io.StringIO(text)))
            self.batch_status.set_text(f"Импорт {len(rows)} устройств…")

            def worker():
                added, errors = 0, []
                for line, row in enumerate(rows, start=2):
                    try:
                        device = {"name": row.get("Название") or row["Адрес"], "host": row["Адрес"].strip(),
                                  "group_name": (row.get("Область") or "").strip(), "protocol": "hikvision",
                                  "sdk_port": 8000, "http_port": int(row.get("HTTP-порт") or 80),
                                  "rtsp_port": int(row.get("RTSP-порт") or 554), "stream": "main",
                                  "category": "access" if (row.get("Тип") or "").strip() == "access" else "video"}
                        self._probe_and_store(device, {"username": row.get("Пользователь", "admin"),
                                                       "password": row.get("Пароль", "")})
                        added += 1
                    except Exception as exc:
                        errors.append(f"строка {line}: {exc}")
                GLib.idle_add(lambda: (self.batch_status.set_text(
                    f"Добавлено: {added}. Ошибок: {len(errors)}. " + "; ".join(errors[:5])),
                    self.owner.refresh(), self.owner.sync_event_subscriptions()) and False)

            threading.Thread(target=worker, daemon=True).start()

        chooser.connect("response", done)
        chooser.show()

    def add_access_controller(self):
        dialog = _dialog(self, "Контроллер доступа", "Добавить")
        form = _form()
        name = _field(form, 0, "Название", Gtk.Entry(placeholder_text="Например, Домофон у входа"))
        host = _field(form, 1, "IP-адрес", Gtk.Entry(placeholder_text="192.168.1.70"))
        port = _field(form, 2, "HTTP-порт", Gtk.SpinButton.new_with_range(1, 65535, 1))
        port.set_value(80)
        user = _field(form, 3, "Пользователь", Gtk.Entry(text="admin"))
        password = _field(form, 4, "Пароль", Gtk.PasswordEntry(show_peek_icon=True))
        area = _field(form, 5, "Область", Gtk.Entry())
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                device = {"name": name.get_text().strip() or host.get_text().strip(), "host": host.get_text().strip(),
                          "group_name": area.get_text().strip(), "protocol": "hikvision", "sdk_port": 8000,
                          "http_port": port.get_value_as_int(), "rtsp_port": 554, "stream": "main",
                          "category": "access"}
                if not device["host"]:
                    self.owner.notify("Введите адрес", error=True)
                    return
                threading.Thread(target=self._add_worker, args=([device], {
                    "username": user.get_text().strip(), "password": password.get_text()}, False), daemon=True).start()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    # ------------------------------------------------------------------ подробности устройства
    def show_device_details(self, device_id):
        rows = self._physical_rows()
        row = next((r for r in rows if device_id in r["ids"]), None)
        if not row:
            return
        W.clear(self.details_box)
        device = row["device"]
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        W.margins(page, 12)
        page.append(_toolbar(W.button("‹ Назад", lambda: self.devices_stack.set_visible_child_name("list"), flat=True),
                             W.label("Система › Устройство › Подробности", "muted")))
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        head.append(W.label("▣" if row["category"] == "access" else "▤", "page-title"))
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        titles.append(W.label(row["name"], "page-title"))
        titles.append(W.label(f"Серийный №: {row['serial_no'] or '—'}   Версия: {row['firmware'] or '—'}   "
                              f"Модель: {row['device_model'] or '—'}", "muted"))
        status = {"online": ("В сети", "ok"), "offline": ("Не в сети", "bad")}.get(row["status"],
                                                                                   ("Не проверено", "unknown"))
        titles.append(W.status_badge(*status))
        titles.set_hexpand(True)
        head.append(titles)
        head.append(W.button("Удалённая настройка", lambda: self.owner.open_remote_configuration(device)))
        head.append(W.button("Журнал", lambda: self.owner.open_report("operations")))
        head.append(W.button("Перезагрузить", lambda: self._reboot(device)))
        page.append(head)
        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)

        form = _form()
        user = _field(form, 0, "Имя пользователя *", Gtk.Entry())
        password = _field(form, 1, "Пароль *", Gtk.PasswordEntry(show_peek_icon=True))
        strength = _field(form, 2, "Надёжность", W.label("", "muted"))
        password.connect("changed", lambda e: strength.set_text(
            security.STRENGTH_TITLES[security.password_strength(e.get_text())]))
        known = self.owner.session_credentials.get(device["id"]) or {}
        user.set_text(known.get("username", ""))
        password.set_text(known.get("password", ""))
        host = _field(form, 3, "IP-адрес *", Gtk.Entry(text=row["host"]))
        http_port = _field(form, 4, "HTTP-порт", Gtk.SpinButton.new_with_range(1, 65535, 1))
        http_port.set_value(row["http_port"])
        rtsp_port = _field(form, 5, "RTSP-порт", Gtk.SpinButton.new_with_range(1, 65535, 1))
        rtsp_port.set_value(row["rtsp_port"])
        name = _field(form, 6, "Название устройства *", Gtk.Entry(text=row["name"]))
        groups = self.owner.groups
        area = _field(form, 7, "Область", _dropdown(["—"] + [g["name"] for g in groups],
                                                     next((i + 1 for i, g in enumerate(groups)
                                                           if g["id"] == row["group_id"]), 0)))
        doors = None
        if row["category"] == "access":
            doors = _field(form, 8, "Количество дверей", Gtk.SpinButton.new_with_range(1, 64, 1))
            doors.set_value(int(device.get("door_count") or 1))
        if device.get("cloud_serial"):
            note = ("Устройство добавлено из Hik-Connect. Логин и пароль здесь — от самого регистратора, "
                    "а не от аккаунта Hik-Connect. Без них и без прямого доступа видео идёт через облако.")
            if hikconnect.is_private_host(row["host"]):
                note += (f" Адрес {row['host']} — локальная сеть объекта: напрямую он доступен через VPN или "
                         "после проброса RTSP-порта и смены адреса на внешний.")
            form.attach(W.label(note, "muted", wrap=True), 0, 10, 2, 1)
        form.attach(W.label("Если часовой пояс устройства и ПК различаются, резервная запись на ПК недоступна — "
                            "синхронизируйте время.", "muted", wrap=True), 0, 9, 2, 1)

        def save():
            with storage.database() as db:
                group_id = groups[area.get_selected() - 1]["id"] if area.get_selected() > 0 else None
                for device_id_item in row["ids"]:
                    db.execute("""UPDATE devices SET host=?, http_port=?, rtsp_port=?, host_name=?, group_id=?
                        WHERE id=?""", (host.get_text().strip(), http_port.get_value_as_int(),
                                        rtsp_port.get_value_as_int(), name.get_text().strip()[:120], group_id,
                                        device_id_item))
                    if doors is not None:
                        db.execute("UPDATE devices SET door_count=? WHERE id=?", (doors.get_value_as_int(),
                                                                                    device_id_item))
            credentials = {"username": user.get_text().strip(), "password": password.get_text()}
            if credentials["username"]:
                updated = dict(device, host=host.get_text().strip(), http_port=http_port.get_value_as_int())
                try:
                    storage.save_credentials(updated, credentials)
                except (storage.CredentialStoreError, sqlite3.Error) as exc:
                    self.owner.notify(f"Пароль не сохранён: {exc}", error=True)
                for device_id_item in row["ids"]:
                    self.owner.session_credentials[device_id_item] = dict(credentials)
            if device.get("cloud_serial"):
                # Новый адрес или пароль: снова пробуем прямое подключение, облако — запасной путь.
                storage.set_setting(f"cloud_video:{device['cloud_serial']}", "0")
            alarms.log_operation(self.owner.session.username, "Изменение устройства", row["host"])
            self.owner.refresh()
            self.owner.sync_event_subscriptions()
            self.owner.notify("Устройство сохранено")

        basic = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        basic.append(form)
        basic.append(_toolbar(_spacer(), W.button("Сохранить", save, "red-button"),
                              W.button("Синхронизировать время", lambda: self.owner.sync_device_time(device))))
        notebook.append_page(_scroll(basic), W.label("Основные параметры"))

        resources = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        W.margins(resources, 10)
        channels = [d for d in self.owner.devices if d["id"] in row["ids"]]
        if row["category"] == "access":
            for door in range(1, int(device.get("door_count") or 1) + 1):
                line = _toolbar(W.label(f"🚪 Дверь {door}"), _spacer(),
                                *[W.button(people.DOOR_TITLES[c], lambda c=c, n=door: self.owner.door_command(
                                    device, n, c)) for c in ("open", "close", "always_open", "always_close")])
                resources.append(line)
        else:
            for channel in channels:
                recording_plan = recording.load_plan(channel["id"])
                state = "резервная запись ПК: вкл" if recording_plan.enabled else "запись на устройстве"
                resources.append(_toolbar(W.label(f"◉ {channel['name']}  ·  канал {channel['channel']}"),
                                          W.label(state, "muted"), _spacer(),
                                          W.button("Настройка записи",
                                                   lambda c=channel: self.show_recording_config(c)),
                                          W.button("Живой просмотр", lambda c=channel: self.owner.open_live_dialog(
                                              c, self.owner.selected_slot))))
        notebook.append_page(_scroll(resources), W.label("Ресурсы"))

        advanced = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(advanced, 12)
        advanced.append(W.label(f"Протокол: {'Hikvision ISAPI/RTSP' if device['protocol'] == 'hikvision' else 'RTSP'}",
                                "muted"))
        advanced.append(W.label(f"SDK-порт: {row['sdk_port']}   ·   Каналов: {row['channels']}", "muted"))
        advanced.append(W.label(f"Последняя проверка: {(device.get('status_checked_at') or '—')[:19]}", "muted"))
        notebook.append_page(advanced, W.label("Дополнительно"))
        page.append(notebook)
        self.details_box.append(page)
        self.devices_stack.set_visible_child_name("details")

    def _reboot(self, device):
        credentials = self.owner.session_credentials.get(device["id"])
        if not credentials:
            self.owner.notify("Нет сохранённого пароля устройства", error=True)
            return

        def go():
            threading.Thread(target=self.owner._isapi_quiet, args=(device, credentials, "/ISAPI/System/reboot",
                                                                   "PUT", None), daemon=True).start()
            alarms.log_operation(self.owner.session.username, "Перезагрузка устройства", device["host"])
            self.owner.notify(f"Команда перезагрузки отправлена на {device['host']}")

        self.owner._confirm(f"Перезагрузить {device['host']}? Видео прервётся на 1–3 минуты.", go, "Перезагрузить")

    # ------------------------------------------------------------------ настройка записи (резервное хранилище ПК)
    def show_recording_config(self, channel):
        plan = recording.load_plan(channel["id"])
        dialog = _dialog(self, f"Настройка записи · {channel['name']}", "Сохранить", 820)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        W.margins(box, 14)
        enabled = Gtk.Switch()
        enabled.set_active(plan.enabled)
        box.append(_toolbar(W.label("Резервная запись на этот ПК", "section-title"), enabled, _spacer()))
        box.append(W.label("Основная запись остаётся на регистраторе. Резервная копия пишется сегментами по 10 мин "
                           f"в {recording.storage_root()} и показывается зелёным на шкале воспроизведения.",
                           "muted", wrap=True))
        form = Gtk.Grid(column_spacing=12, row_spacing=9)
        stream = _field(form, 0, "Поток", _dropdown(["Основной", "Дополнительный", "Третий"],
                                                     ("main", "sub", "third").index(plan.stream)))
        retention = _field(form, 1, "Срок хранения, дней", Gtk.SpinButton.new_with_range(1, 3650, 1))
        retention.set_value(plan.retention_days)
        overwrite = Gtk.CheckButton(label="Перезаписывать старые записи при нехватке места")
        overwrite.set_active(plan.overwrite)
        form.attach(overwrite, 0, 2, 2, 1)
        box.append(form)
        box.append(W.label("Расписание записи", "section-title"))
        editor = W.ScheduleEditor(people.cells_from_week(plan.schedule))
        box.append(editor)
        others = [d for d in self.owner.devices if d["id"] != channel["id"] and d.get("category", "video") == "video"]
        copy_checks = []
        if others:
            expander = Gtk.Expander(label="Копировать настройки на другие камеры")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for other in others:
                check = Gtk.CheckButton(label=f"{other['name']} · {other['host']}")
                copy_checks.append((check, other["id"]))
                inner.append(check)
            expander.set_child(inner)
            box.append(expander)
        dialog.get_content_area().append(_scroll(box))
        dialog.set_default_size(860, 640)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                new_plan = recording.Plan(channel["id"], enabled.get_active(),
                                          ("main", "sub", "third")[stream.get_selected()],
                                          people.week_from_cells(editor.cells), retention.get_value_as_int(),
                                          overwrite.get_active())
                try:
                    recording.save_plan(new_plan)
                    recording.copy_plan(new_plan, [i for c, i in copy_checks if c.get_active()])
                except ValueError as exc:
                    self.owner.notify(str(exc), error=True)
                    return
                self.owner._recorder_tick()
                self.owner.notify("Настройки записи сохранены")
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    # ================================================================== персоны
    def _persons_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        W.margins(page, 12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_size_request(230, -1)
        left.append(_toolbar(W.label("Отделы", "section-title"), _spacer(),
                             W.button("＋", self.add_department, flat=True, tooltip="Добавить отдел")))
        self.department_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        left.append(_scroll(self.department_list))
        page.append(left)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_hexpand(True)
        self.person_search = Gtk.SearchEntry(placeholder_text="Имя, ID, карта, телефон")
        self.person_search.connect("search-changed", lambda *_: self.refresh_persons())
        right.append(_toolbar(W.button("＋ Добавить", lambda: self.edit_person(None), "red-button"),
                              W.button("Удалить", self.delete_persons),
                              W.button("Импорт", self.import_persons), W.button("Экспорт", self.export_persons),
                              W.button("Назначить уровень доступа", self.assign_selected_persons),
                              _spacer(), self.person_search))
        self.person_table = W.Table(("Имя", "ID", "Отдел", "Карты", "PIN", "Телефон", "Действует до"),
                                    on_activate=lambda pid: self.edit_person(pid))
        right.append(_scroll(self.person_table))
        page.append(right)
        self.selected_department = None
        return page

    def refresh_persons(self):
        departments = people.load_departments()
        W.clear(self.department_list)
        root_id = departments[0]["id"]
        for department in departments:
            selected = (self.selected_department or root_id) == department["id"]
            button = W.button(("▾ " if department["id"] == root_id else "   ▸ ") + department["name"],
                              lambda d=department["id"]: self._select_department(d), "tree-row")
            if selected:
                button.add_css_class("selected")
            if department["id"] != root_id:
                W.on_right_click(button, lambda w, x, y, d=department: W.popover_menu(w, [
                    ("Переименовать", lambda: self.owner._ask_text("Отдел", d["name"], lambda n: (
                        people.rename_department(d["id"], n), self.refresh_persons()))),
                    ("Удалить", lambda: self.owner._confirm(f"Удалить отдел «{d['name']}»? Персоны перейдут в корень.",
                                                            lambda: (people.delete_department(d["id"]),
                                                                     self.refresh_persons())))], x, y))
            self.department_list.append(button)
        department = None if (self.selected_department in (None, root_id)) else self.selected_department
        persons = people.load_persons(department, self.person_search.get_text().strip())
        names = {d["id"]: d["name"] for d in departments}
        self.persons = {p["id"]: p for p in persons}
        self.person_table.set_rows([(p["name"], p["employee_no"], names.get(p["department_id"], "—"),
                                     " ".join(p["cards"]) or "—", "есть" if p["pin"] else "—", p["phone"] or "—",
                                     p["valid_to"]) for p in persons], [p["id"] for p in persons])

    def _select_department(self, department_id):
        self.selected_department = department_id
        self.refresh_persons()

    def add_department(self):
        self.owner._ask_text("Новый отдел", "", lambda name: (people.add_department(name), self.refresh_persons()))

    def edit_person(self, person_id):
        person = (self.persons if hasattr(self, "persons") else {}).get(person_id) if person_id else None
        departments = people.load_departments()
        dialog = _dialog(self, "Персона" if person else "Добавить персону", "Сохранить", 560)
        form = _form()
        name = _field(form, 0, "Имя *", Gtk.Entry(text=person["name"] if person else ""))
        number = _field(form, 1, "ID персоны", Gtk.Entry(text=person["employee_no"] if person else
                                                          people.next_employee_no()))
        default_department = person["department_id"] if person else (self.selected_department or departments[0]["id"])
        department = _field(form, 2, "Отдел", _dropdown([d["name"] for d in departments],
                                                        next((i for i, d in enumerate(departments)
                                                              if d["id"] == default_department), 0)))
        gender = _field(form, 3, "Пол", _dropdown(["—", "Мужской", "Женский"],
                                                  ["", "Мужской", "Женский"].index(person["gender"])
                                                  if person and person["gender"] in ("Мужской", "Женский") else 0))
        phone = _field(form, 4, "Телефон", Gtk.Entry(text=person["phone"] if person else ""))
        email = _field(form, 5, "Email", Gtk.Entry(text=person["email"] if person else ""))
        start_default, end_default = people._default_validity()
        valid_from = _field(form, 6, "Действует с", Gtk.Entry(text=person["valid_from"] if person else start_default))
        valid_to = _field(form, 7, "Действует до", Gtk.Entry(text=person["valid_to"] if person else end_default))
        form.attach(W.label("Учётные данные", "section-title"), 0, 8, 2, 1)
        cards = _field(form, 9, "Карты (через пробел)", Gtk.Entry(text=" ".join(person["cards"]) if person else ""))
        pin = _field(form, 10, "PIN-код", Gtk.PasswordEntry(show_peek_icon=True))
        if person:
            pin.set_text(person["pin"])
        form.attach(W.label("Номер карты можно ввести вручную или считать считывателем, работающим как клавиатура. "
                            "Отпечатки и радужка регистрируются на самом терминале.", "muted", wrap=True), 0, 11, 2, 1)
        levels = people.load_access_levels()
        current = {lvl["id"] for lvl in people.effective_levels(person)} if person else set()
        level_checks = []
        if levels:
            form.attach(W.label("Уровни доступа", "section-title"), 0, 12, 2, 1)
            for index, level in enumerate(levels):
                check = Gtk.CheckButton(label=level["name"])
                check.set_active(level["id"] in current)
                level_checks.append((check, level["id"]))
                form.attach(check, 0, 13 + index, 2, 1)
        remark = _field(form, 14 + len(levels), "Примечание", Gtk.Entry(text=person["remark"] if person else ""))
        dialog.get_content_area().append(_scroll(form))
        dialog.set_default_size(560, 680)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                data = {"name": name.get_text(), "employee_no": number.get_text(),
                        "department_id": departments[department.get_selected()]["id"],
                        "gender": ["", "Мужской", "Женский"][gender.get_selected()], "phone": phone.get_text(),
                        "email": email.get_text(), "valid_from": valid_from.get_text().strip(),
                        "valid_to": valid_to.get_text().strip(), "cards": cards.get_text().split(),
                        "pin": pin.get_text(), "remark": remark.get_text()}
                try:
                    saved_id = people.save_person(data, person_id)
                except (ValueError, sqlite3.IntegrityError) as exc:
                    self.owner.notify(f"Не сохранено: {exc}", error=True)
                    return
                chosen = [lid for check, lid in level_checks if check.get_active()]
                people.assign_levels(person_ids=[saved_id], level_ids=chosen, replace=True)
                alarms.log_operation(self.owner.session.username, "Сохранение персоны", data["name"])
                self.refresh_persons()
                if chosen:
                    person_row = next(p for p in people.load_persons() if p["id"] == saved_id)
                    self.owner.apply_access_to_devices([person_row], self.refresh_access)
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def delete_persons(self):
        ids = self.person_table.selected()
        if not ids:
            self.owner.notify("Отметьте персон")
            return
        self.owner._confirm(f"Удалить персон: {len(ids)}? С контроллеров их нужно удалить отдельно "
                            "(повторное «Применить» не создаст их снова).",
                            lambda: (people.delete_persons(ids), self.refresh_persons()))

    def import_persons(self):
        chooser = Gtk.FileChooserNative.new("Импорт персон (CSV)", self, Gtk.FileChooserAction.OPEN, "Импорт", "Отмена")
        self._chooser = chooser

        def done(native, response):
            file = native.get_file()
            native.destroy()
            if response == Gtk.ResponseType.ACCEPT and file:
                with open(file.get_path(), encoding="utf-8-sig", newline="") as handle:
                    added, errors = people.import_persons_csv(handle)
                self.refresh_persons()
                self.owner.notify(f"Импортировано: {added}" + (f". Ошибки: {'; '.join(errors[:3])}" if errors else ""),
                                  error=bool(errors))

        chooser.connect("response", done)
        chooser.show()

    def export_persons(self):
        path = Path.home() / datetime.now().strftime("sentinel_persons_%Y%m%d.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            people.export_persons_csv(people.load_persons(), people.load_departments(), handle)
        self.owner.notify(f"Сохранено: {path}")

    def assign_selected_persons(self):
        ids = self.person_table.selected()
        if not ids:
            self.owner.notify("Отметьте персон")
            return
        self._assign_levels_dialog(person_ids=ids)

    # ================================================================== транспорт
    def _vehicles_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        W.margins(page, 12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_size_request(230, -1)
        left.append(_toolbar(W.label("Списки ТС", "section-title"), _spacer(),
                             W.button("＋", lambda: self.owner._ask_text("Новый список ТС", "", lambda n: (
                                 people.add_vehicle_list(n), self.refresh_vehicles())), flat=True)))
        self.vehicle_lists_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        left.append(_scroll(self.vehicle_lists_box))
        page.append(left)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_hexpand(True)
        self.vehicle_search = Gtk.SearchEntry(placeholder_text="Номер, владелец, марка")
        self.vehicle_search.connect("search-changed", lambda *_: self.refresh_vehicles())
        right.append(_toolbar(W.button("＋ Добавить ТС", lambda: self.edit_vehicle(None), "red-button"),
                              W.button("Удалить", self.delete_vehicles),
                              W.button("Переместить в список…", self.move_vehicles),
                              W.button("Импорт", self.import_vehicles), W.button("Экспорт", self.export_vehicles),
                              _spacer(), self.vehicle_search))
        right.append(W.label("Для правил «ANPR: номер из списка / не из списка» нужны камеры с распознаванием номеров. "
                             "Номера сравниваются без пробелов и дефисов, кириллица = латиница.", "muted", wrap=True))
        self.vehicle_table = W.Table(("Номер", "Список", "Марка", "Тип", "Цвет", "Владелец", "Персона"),
                                     on_activate=lambda vid: self.edit_vehicle(vid))
        right.append(_scroll(self.vehicle_table))
        page.append(right)
        self.selected_vehicle_list = None
        return page

    def refresh_vehicles(self):
        lists = people.load_vehicle_lists()
        W.clear(self.vehicle_lists_box)
        all_button = W.button("▾ Все списки", lambda: self._select_vehicle_list(None), "tree-row")
        if self.selected_vehicle_list is None:
            all_button.add_css_class("selected")
        self.vehicle_lists_box.append(all_button)
        for item in lists:
            button = W.button("   ▸ " + item["name"], lambda i=item["id"]: self._select_vehicle_list(i), "tree-row")
            if item["id"] == self.selected_vehicle_list:
                button.add_css_class("selected")
            W.on_right_click(button, lambda w, x, y, i=item: W.popover_menu(w, [
                ("Удалить список и его ТС", lambda: self.owner._confirm(f"Удалить список «{i['name']}»?", lambda: (
                    people.delete_vehicle_list(i["id"]), self._select_vehicle_list(None))))], x, y))
            self.vehicle_lists_box.append(button)
        vehicles = people.load_vehicles(self.selected_vehicle_list, self.vehicle_search.get_text().strip())
        self.vehicles = {v["id"]: v for v in vehicles}
        self.vehicle_table.set_rows([(v["plate"], v["list_name"], v["brand"] or "—", v["vehicle_type"] or "—",
                                      v["color"] or "—", v["owner"] or "—", v["person_name"] or "—")
                                     for v in vehicles], [v["id"] for v in vehicles])

    def _select_vehicle_list(self, list_id):
        self.selected_vehicle_list = list_id
        self.refresh_vehicles()

    def edit_vehicle(self, vehicle_id):
        vehicle = self.vehicles.get(vehicle_id) if vehicle_id else None
        lists = people.load_vehicle_lists()
        persons = people.load_persons()
        dialog = _dialog(self, "Транспортное средство", "Сохранить")
        form = _form()
        plate = _field(form, 0, "Номер *", Gtk.Entry(text=vehicle["plate"] if vehicle else ""))
        default_list = vehicle["list_id"] if vehicle else (self.selected_vehicle_list or lists[0]["id"])
        list_choice = _field(form, 1, "Список *", _dropdown([item["name"] for item in lists],
                                                            next((i for i, item in enumerate(lists)
                                                                  if item["id"] == default_list), 0)))
        brand = _field(form, 2, "Марка", Gtk.Entry(text=vehicle["brand"] if vehicle else ""))
        kind = _field(form, 3, "Тип", Gtk.Entry(text=vehicle["vehicle_type"] if vehicle else "",
                                               placeholder_text="седан, грузовик…"))
        color = _field(form, 4, "Цвет", Gtk.Entry(text=vehicle["color"] if vehicle else ""))
        owner = _field(form, 5, "Владелец", Gtk.Entry(text=vehicle["owner"] if vehicle else ""))
        person = _field(form, 6, "Связанная персона", _dropdown(["—"] + [f"{p['name']} ({p['employee_no']})"
                                                                         for p in persons],
                                                                next((i + 1 for i, p in enumerate(persons)
                                                                      if vehicle and p["id"] == vehicle["person_id"]), 0)))
        valid_from = _field(form, 7, "Действует с", Gtk.Entry(text=vehicle["valid_from"] if vehicle else "",
                                                               placeholder_text="ГГГГ-ММ-ДД ЧЧ:ММ (необязательно)"))
        valid_to = _field(form, 8, "Действует до", Gtk.Entry(text=vehicle["valid_to"] if vehicle else ""))
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                data = {"plate": plate.get_text(), "list_id": lists[list_choice.get_selected()]["id"],
                        "brand": brand.get_text(), "vehicle_type": kind.get_text(), "color": color.get_text(),
                        "owner": owner.get_text(), "valid_from": valid_from.get_text().strip(),
                        "valid_to": valid_to.get_text().strip(),
                        "person_id": persons[person.get_selected() - 1]["id"] if person.get_selected() > 0 else None}
                try:
                    people.save_vehicle(data, vehicle_id)
                except ValueError as exc:
                    self.owner.notify(str(exc), error=True)
                    return
                except sqlite3.IntegrityError:
                    self.owner.notify("Этот номер уже есть в списке", error=True)
                    return
                self.refresh_vehicles()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def delete_vehicles(self):
        ids = self.vehicle_table.selected()
        if ids:
            self.owner._confirm(f"Удалить ТС: {len(ids)}?", lambda: (people.delete_vehicles(ids),
                                                                    self.refresh_vehicles()))

    def move_vehicles(self):
        ids = self.vehicle_table.selected()
        if not ids:
            self.owner.notify("Отметьте ТС")
            return
        lists = people.load_vehicle_lists()
        dialog = _dialog(self, "Переместить в список", "Переместить")
        form = _form()
        choice = _field(form, 0, "Список", _dropdown([item["name"] for item in lists]))
        dialog.get_content_area().append(form)
        dialog.connect("response", lambda dlg, r: (dlg.close(), r == Gtk.ResponseType.ACCEPT and (
            people.move_vehicles(ids, lists[choice.get_selected()]["id"]), self.refresh_vehicles())))
        dialog.present()

    def import_vehicles(self):
        chooser = Gtk.FileChooserNative.new("Импорт ТС (CSV)", self, Gtk.FileChooserAction.OPEN, "Импорт", "Отмена")
        self._chooser = chooser

        def done(native, response):
            file = native.get_file()
            native.destroy()
            if response == Gtk.ResponseType.ACCEPT and file:
                default = self.selected_vehicle_list or people.load_vehicle_lists()[0]["id"]
                with open(file.get_path(), encoding="utf-8-sig", newline="") as handle:
                    added, errors = people.import_vehicles_csv(handle, default)
                self.refresh_vehicles()
                self.owner.notify(f"Импортировано: {added}" + (f". Ошибки: {'; '.join(errors[:3])}" if errors else ""),
                                  error=bool(errors))

        chooser.connect("response", done)
        chooser.show()

    def export_vehicles(self):
        path = Path.home() / datetime.now().strftime("sentinel_vehicles_%Y%m%d.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            people.export_vehicles_csv(people.load_vehicles(), handle)
        self.owner.notify(f"Сохранено: {path}")

    # ================================================================== уровни доступа
    def _access_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        W.margins(page, 12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_size_request(260, -1)
        left.append(_toolbar(W.label("Уровни доступа", "section-title"), _spacer(),
                             W.button("＋", lambda: self.edit_level(None), flat=True, tooltip="Новый уровень"),
                             W.button("⏲", self.manage_schedules, flat=True, tooltip="Расписания доступа")))
        self.level_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        left.append(_scroll(self.level_list))
        page.append(left)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_hexpand(True)
        self.access_summary = W.label("", "section-title")
        right.append(_toolbar(self.access_summary, _spacer(),
                              W.button("Назначить отделам…", lambda: self._assign_levels_dialog(departments=True)),
                              W.button("Назначить персонам…", lambda: self._assign_levels_dialog()),
                              W.button("Применить всё", self.apply_all, "red-button")))
        right.append(W.label("После назначения по отделу или уровню нажмите «Применить всё» — персоны и карты будут "
                             "записаны на контроллеры. При назначении в карточке персоны это делается сразу.",
                             "muted", wrap=True))
        self.apply_table = W.Table(("Персона", "Устройство", "Статус", "Сообщение"), selectable=False)
        right.append(_scroll(self.apply_table))
        page.append(right)
        return page

    def refresh_access(self):
        W.clear(self.level_list)
        levels = people.load_access_levels()
        schedules = {s["id"]: s["name"] for s in people.load_schedules()}
        if not levels:
            self.level_list.append(W.label("Уровней нет — нажмите ＋", "muted"))
        for level in levels:
            text = f"▣ {level['name']}\n    дверей: {len(level['doors'])} · {schedules.get(level['schedule_id'], 'Весь день')}"
            button = W.button(text, lambda l=level: self.edit_level(l), "tree-row")
            W.on_right_click(button, lambda w, x, y, l=level: W.popover_menu(w, [
                ("Изменить", lambda: self.edit_level(l)),
                ("Удалить", lambda: self.owner._confirm(f"Удалить уровень «{l['name']}»?", lambda: (
                    people.delete_access_level(l["id"]), self.refresh_access())))], x, y))
            self.level_list.append(button)
        summary = people.apply_summary()
        self.access_summary.set_text(f"С доступом: {summary['authorized']}   ·   Применено: {summary['applied']}   ·   "
                                     f"Ошибки: {summary['failed']}   ·   Ожидают: {summary['pending']}")
        with storage.database() as db:
            rows = [dict(r) for r in db.execute("""SELECT s.*, p.name AS person FROM access_apply_status s
                JOIN persons p ON p.id=s.person_id ORDER BY s.updated_at DESC LIMIT 300""")]
        devices = {d["id"]: d.get("host_name") or d["host"] for d in self.owner.devices}
        titles = {"applied": "Применено", "failed": "Ошибка", "pending": "Ожидает"}
        self.apply_table.set_rows([(r["person"], devices.get(r["device_id"], r["device_id"]),
                                    titles.get(r["status"], r["status"]), r["message"]) for r in rows])

    def _doors(self):
        result = []
        for device in self.owner.devices:
            if device.get("category") == "access":
                for door in range(1, int(device.get("door_count") or 1) + 1):
                    result.append((device, door))
        return result

    def edit_level(self, level):
        schedules = people.load_schedules()
        doors = self._doors()
        dialog = _dialog(self, "Уровень доступа", "Сохранить")
        form = _form()
        name = _field(form, 0, "Название *", Gtk.Entry(text=level["name"] if level else ""))
        schedule = _field(form, 1, "Расписание доступа", _dropdown(
            [s["name"] for s in schedules], next((i for i, s in enumerate(schedules)
                                                 if level and s["id"] == level["schedule_id"]), 0)))
        form.attach(W.label("Точки доступа (двери)", "section-title"), 0, 2, 2, 1)
        checks = []
        current = {(d["device_id"], d["door"]) for d in (level["doors"] if level else [])}
        if not doors:
            form.attach(W.label("Нет контроллеров доступа. Добавьте устройство с типом «Контроль доступа».",
                                "muted", wrap=True), 0, 3, 2, 1)
        for index, (device, door) in enumerate(doors):
            check = Gtk.CheckButton(label=f"{device.get('host_name') or device['host']} · Дверь {door}")
            check.set_active((device["id"], door) in current)
            checks.append((check, device["id"], door))
            form.attach(check, 0, 3 + index, 2, 1)
        dialog.get_content_area().append(_scroll(form))
        dialog.set_default_size(520, 520)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    people.save_access_level(name.get_text(), [{"device_id": d, "door": n}
                                                               for c, d, n in checks if c.get_active()],
                                             schedules[schedule.get_selected()]["id"], level["id"] if level else None)
                except (ValueError, sqlite3.IntegrityError) as exc:
                    self.owner.notify(f"Не сохранено: {exc}", error=True)
                    return
                self.refresh_access()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def manage_schedules(self):
        schedules = people.load_schedules()
        dialog = _dialog(self, "Расписания доступа", "Сохранить", 820)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(box, 12)
        choice = _dropdown(["＋ Новое расписание"] + [s["name"] + (" (шаблон)" if s["kind"] == "template" else "")
                                                       for s in schedules])
        name = Gtk.Entry(placeholder_text="Название")
        box.append(_toolbar(choice, name))
        holder = Gtk.Box()
        box.append(holder)
        state = {"editor": None}

        def load(*_args):
            W.clear(holder)
            index = choice.get_selected() - 1
            weekly = schedules[index]["weekly"] if index >= 0 else people.weekdays_only()
            name.set_text(schedules[index]["name"] if index >= 0 else "")
            name.set_sensitive(index < 0 or schedules[index]["kind"] != "template")
            state["editor"] = W.ScheduleEditor(people.cells_from_week(weekly))
            holder.append(state["editor"])

        choice.connect("notify::selected", load)
        load()
        box.append(W.label("Шаблоны изменить нельзя — создайте новое расписание.", "muted"))
        dialog.get_content_area().append(box)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                index = choice.get_selected() - 1
                if index >= 0 and schedules[index]["kind"] == "template":
                    self.owner.notify("Шаблон не изменяется", error=True)
                    return
                try:
                    people.save_schedule(name.get_text(), people.week_from_cells(state["editor"].cells),
                                         schedules[index]["id"] if index >= 0 else None)
                except (ValueError, sqlite3.IntegrityError) as exc:
                    self.owner.notify(str(exc), error=True)
                    return
                self.refresh_access()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def _assign_levels_dialog(self, person_ids=None, departments=False):
        levels = people.load_access_levels()
        if not levels:
            self.owner.notify("Сначала создайте уровень доступа")
            return
        dialog = _dialog(self, "Назначить уровень доступа", "Назначить")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        W.margins(box, 14)
        box.append(W.label("Уровни", "section-title"))
        level_checks = []
        for level in levels:
            check = Gtk.CheckButton(label=level["name"])
            level_checks.append((check, level["id"]))
            box.append(check)
        target_checks = []
        if person_ids is None:
            box.append(W.label("Отделы" if departments else "Персоны", "section-title"))
            targets = people.load_departments() if departments else people.load_persons()
            for target in targets:
                check = Gtk.CheckButton(label=target["name"])
                target_checks.append((check, target["id"]))
                box.append(check)
        replace = Gtk.CheckButton(label="Заменить текущие назначения")
        box.append(replace)
        dialog.get_content_area().append(_scroll(box))
        dialog.set_default_size(440, 520)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                chosen = [lid for c, lid in level_checks if c.get_active()]
                targets = person_ids or [tid for c, tid in target_checks if c.get_active()]
                if departments:
                    people.assign_levels(department_ids=targets, level_ids=chosen, replace=replace.get_active())
                else:
                    people.assign_levels(person_ids=targets, level_ids=chosen, replace=replace.get_active())
                self.refresh_access()
                self.owner.notify("Назначено. Нажмите «Применить всё», чтобы записать на контроллеры.")
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def apply_all(self):
        self.owner.apply_access_to_devices(people.load_persons(), self.refresh_access)

    # ================================================================== видеодомофон
    def _intercom_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        W.margins(page, 16)
        settings = storage.get_json_setting("intercom", {"ring_client": True, "max_ring": 30, "max_talk": 120})
        page.append(W.label("Вызов с вызывной панели", "section-title"))
        form = _form()
        ring = Gtk.CheckButton(label="Показывать вызов в клиенте (всплывающее окно с видео панели)")
        ring.set_active(settings["ring_client"])
        form.attach(ring, 0, 0, 2, 1)
        max_ring = _field(form, 1, "Макс. длительность вызова, с", Gtk.SpinButton.new_with_range(10, 120, 5))
        max_ring.set_value(settings["max_ring"])
        max_talk = _field(form, 2, "Макс. длительность разговора, с", Gtk.SpinButton.new_with_range(30, 600, 10))
        max_talk.set_value(settings["max_talk"])
        page.append(form)
        page.append(W.label("Вызовы приходят как события контроля доступа и открывают окно тревоги с видео; "
                            "двустороннее аудио доступно в веб-интерфейсе панели.", "muted", wrap=True))
        page.append(_toolbar(W.button("Сохранить", lambda: (storage.set_json_setting("intercom", {
            "ring_client": ring.get_active(), "max_ring": max_ring.get_value_as_int(),
            "max_talk": max_talk.get_value_as_int()}), self.owner.notify("Сохранено")), "red-button")))
        return page

    # ================================================================== события и тревоги
    def _event_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(page, 12)
        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)
        subscription = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(subscription, 12)
        self.receive_events = Gtk.CheckButton(label="Получать события со всех устройств Hikvision (поток ISAPI)")
        self.receive_events.set_active(storage.get_setting("receive_events", "1") == "1")
        self.receive_events.connect("toggled", lambda b: (storage.set_setting("receive_events",
                                                                              "1" if b.get_active() else "0"),
                                                          self.owner.sync_event_subscriptions(),
                                                          self.refresh_rules()))
        subscription.append(self.receive_events)
        subscription.append(W.label("События приходят только от устройств, чей пароль сохранён. Включите нужные "
                                    "детекторы (движение, пересечение линии, ANPR…) и «Уведомить центр наблюдения» "
                                    "в настройках событий самого устройства.", "muted", wrap=True))
        self.subscription_table = W.Table(("Устройство", "Адрес", "Подписка"), selectable=False)
        subscription.append(_scroll(self.subscription_table))
        notebook.append_page(subscription, W.label("Подписка на события"))

        rules = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(rules, 12)
        rules.append(_toolbar(W.button("＋ Добавить", lambda: self.edit_rule(None), "red-button"),
                              W.button("Удалить", self.delete_rules),
                              W.button("Вкл/выкл", self.toggle_rules)))
        self.rule_table = W.Table(("Название", "Событие", "Источники", "Приоритет", "Действия", "Включено"),
                                  on_activate=lambda rid: self.edit_rule(rid))
        rules.append(_scroll(self.rule_table))
        notebook.append_page(rules, W.label("Настройка тревог"))
        page.append(notebook)
        notebook.set_current_page(1)
        return page

    def refresh_rules(self):
        rules = alarms.load_rules()
        self.rules = {r["id"]: r for r in rules}
        rows = []
        for rule in rules:
            sources = "Все" if rule["sources"] == "all" else f"{len(rule['sources'])} источн."
            link = rule["linkage"]
            actions = [name for key, name in (("popup", "окно"), ("capture", "снимок"), ("email", "email"))
                       if link.get(key)]
            if link.get("alarm_outputs"):
                actions.append(f"выходы: {len(link['alarm_outputs'])}")
            if link.get("record_seconds"):
                actions.append("запись")
            rows.append((rule["name"], alarms.event_title(rule["event_type"]), sources,
                         alarms.PRIORITIES[rule["priority"]], ", ".join(actions) or "—",
                         "да" if rule["enabled"] else "нет"))
        self.rule_table.set_rows(rows, [r["id"] for r in rules])
        endpoints = self.owner._endpoints()
        self.subscription_table.set_rows([(d.get("host_name") or d["name"], f"{host}:{port}",
                                           "активна" if (host, port) in self.owner.event_workers else
                                           ("нет пароля" if d["id"] not in self.owner.session_credentials else "выкл"))
                                          for (host, port), d in endpoints.items()])

    def delete_rules(self):
        ids = self.rule_table.selected()
        if ids:
            self.owner._confirm(f"Удалить правил: {len(ids)}?", lambda: (
                [alarms.delete_rule(i) for i in ids], self.refresh_rules()))

    def toggle_rules(self):
        for rule_id in self.rule_table.selected():
            alarms.set_rule_enabled(rule_id, not self.rules[rule_id]["enabled"])
        self.refresh_rules()

    def edit_rule(self, rule_id):
        rule = self.rules.get(rule_id) if rule_id else None
        dialog = _dialog(self, "Тревога" if rule else "Добавить тревогу", "Готово", 720)
        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)

        # 1 — событие
        event_box = _form()
        name = _field(event_box, 0, "Название *", Gtk.Entry(text=rule["name"] if rule else ""))
        type_keys = [key for _group, keys in alarms.EVENT_GROUPS for key in keys]
        type_titles = [f"{group} · {alarms.EVENT_TYPES[key][0]}" for group, keys in alarms.EVENT_GROUPS for key in keys]
        event_type = _field(event_box, 1, "Событие *", _dropdown(type_titles, type_keys.index(rule["event_type"])
                                                                 if rule else 0))
        priority = _field(event_box, 2, "Приоритет", _dropdown(list(alarms.PRIORITIES.values()),
                                                               list(alarms.PRIORITIES).index(rule["priority"])
                                                               if rule else 1))
        lists = people.load_vehicle_lists()
        list_label = W.label("Списки ТС (для ANPR)", "section-title")
        event_box.attach(list_label, 0, 3, 2, 1)
        list_checks = []
        for index, item in enumerate(lists):
            check = Gtk.CheckButton(label=item["name"])
            check.set_active(not rule or rule["vehicle_lists"] == "all" or item["id"] in rule["vehicle_lists"])
            list_checks.append((check, item["id"]))
            event_box.attach(check, 0, 4 + index, 2, 1)
        row = 4 + len(lists)
        event_box.attach(W.label("Источники", "section-title"), 0, row, 2, 1)
        all_sources = Gtk.CheckButton(label="Все источники")
        all_sources.set_active(not rule or rule["sources"] == "all")
        event_box.attach(all_sources, 0, row + 1, 2, 1)
        source_checks = []
        for index, device in enumerate(self.owner.devices):
            check = Gtk.CheckButton(label=f"{device['name']} · {device['host']}")
            check.set_active(bool(rule) and rule["sources"] != "all" and device["id"] in rule["sources"])
            source_checks.append((check, device["id"]))
            event_box.attach(check, 0, row + 2 + index, 2, 1)

        def update_sources(*_args):
            for check, _id in source_checks:
                check.set_sensitive(not all_sources.get_active())
            is_anpr = type_keys[event_type.get_selected()] in ("anpr_matched", "anpr_mismatched")
            list_label.set_visible(is_anpr)
            for check, _id in list_checks:
                check.set_visible(is_anpr)

        all_sources.connect("toggled", update_sources)
        event_type.connect("notify::selected", update_sources)
        update_sources()
        notebook.append_page(_scroll(event_box), W.label("1. Событие"))

        # 2 — расписание
        schedules = people.load_schedules()
        schedule_box = _form()
        schedule = _field(schedule_box, 0, "Активно по расписанию", _dropdown(
            [s["name"] for s in schedules], next((i for i, s in enumerate(schedules)
                                                 if rule and s["id"] == rule["schedule_id"]), 0)))
        schedule_box.attach(W.label("Расписания создаются в «Уровень доступа» → ⏲.", "muted"), 0, 1, 2, 1)
        notebook.append_page(schedule_box, W.label("2. Расписание"))

        # 3 — получатели
        users = security.load_users() or [{"username": "admin"}]
        recipient_box = _form()
        recipient_checks = []
        for index, user in enumerate(users):
            check = Gtk.CheckButton(label=user["username"])
            check.set_active(not rule or user["username"] in rule["recipients"])
            recipient_checks.append((check, user["username"]))
            recipient_box.attach(check, 0, index, 2, 1)
        recipient_box.attach(W.label("Новых пользователей добавьте в существующие тревоги вручную.", "muted"),
                             0, len(users), 2, 1)
        notebook.append_page(recipient_box, W.label("3. Получатели"))

        # 4 — действия
        link = dict(alarms.DEFAULT_LINKAGE, **(rule["linkage"] if rule else {}))
        actions = _form()
        popup = Gtk.CheckButton(label="Всплывающее окно")
        popup.set_active(link["popup"])
        capture = Gtk.CheckButton(label="Снимок с камеры-источника")
        capture.set_active(link["capture"])
        actions.attach(popup, 0, 0, 2, 1)
        actions.attach(capture, 0, 1, 2, 1)
        record = _field(actions, 2, "Запись тревожного видео, с (0 — нет)", Gtk.SpinButton.new_with_range(0, 600, 5))
        record.set_value(link["record_seconds"])
        email = Gtk.CheckButton(label="Отправить email")
        email.set_active(link["email"])
        actions.attach(email, 0, 3, 2, 1)
        email_to = _field(actions, 4, "Адреса (через запятую)", Gtk.Entry(text=link["email_to"]))
        ptz_preset = _field(actions, 5, "PTZ: перейти к пресету источника (0 — нет)",
                            Gtk.SpinButton.new_with_range(0, 300, 1))
        ptz_preset.set_value((link.get("ptz") or {}).get("preset", 0))
        actions.attach(W.label("Тревожные выходы (шлагбаум, сирена)", "section-title"), 0, 6, 2, 1)
        outputs = [dict(o) for o in link["alarm_outputs"]]
        outputs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        actions.attach(outputs_box, 0, 7, 2, 1)
        hosts = []
        seen = set()
        for device in self.owner.devices:
            if device["protocol"] == "hikvision" and device["host"] not in seen:
                seen.add(device["host"])
                hosts.append(device)

        def render_outputs():
            W.clear(outputs_box)
            names = {d["id"]: d.get("host_name") or d["host"] for d in self.owner.devices}
            for index, output in enumerate(outputs):
                closing = f"автозакрытие {output['auto_close']} с" if output["auto_close"] else "закрыть вручную"
                outputs_box.append(_toolbar(W.label(f"{names.get(output['device_id'], '?')} · выход "
                                                    f"{output['output']} · {closing}"), _spacer(),
                                            W.button("✕", lambda i=index: (outputs.pop(i), render_outputs()), flat=True)))
            if hosts:
                device_choice = _dropdown([d.get("host_name") or d["host"] for d in hosts])
                number = Gtk.SpinButton.new_with_range(1, 16, 1)
                auto_close = Gtk.SpinButton.new_with_range(0, 600, 1)
                auto_close.set_value(10)
                outputs_box.append(_toolbar(device_choice, W.label("выход"), number, W.label("автозакр., с"),
                                            auto_close, W.button("Добавить", lambda: (outputs.append({
                                                "device_id": hosts[device_choice.get_selected()]["id"],
                                                "output": number.get_value_as_int(),
                                                "auto_close": auto_close.get_value_as_int()}), render_outputs()))))

        render_outputs()
        notebook.append_page(_scroll(actions), W.label("4. Действия"))
        dialog.get_content_area().append(notebook)
        dialog.set_default_size(720, 600)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                key = type_keys[event_type.get_selected()]
                sources = "all" if all_sources.get_active() else [i for c, i in source_checks if c.get_active()]
                chosen_lists = [i for c, i in list_checks if c.get_active()]
                data = {"name": name.get_text(), "event_type": key, "enabled": rule["enabled"] if rule else True,
                        "priority": list(alarms.PRIORITIES)[priority.get_selected()], "sources": sources,
                        "vehicle_lists": "all" if len(chosen_lists) == len(lists) else chosen_lists,
                        "schedule_id": schedules[schedule.get_selected()]["id"],
                        "recipients": [u for c, u in recipient_checks if c.get_active()],
                        "linkage": {"popup": popup.get_active(), "capture": capture.get_active(),
                                    "record_seconds": record.get_value_as_int(), "alarm_outputs": outputs,
                                    "email": email.get_active(), "email_to": email_to.get_text().strip(),
                                    "ptz": ({"device_id": sources[0] if sources != "all" and sources else None,
                                             "preset": ptz_preset.get_value_as_int()}
                                            if ptz_preset.get_value_as_int() else None)}}
                try:
                    alarms.save_rule(data, rule_id)
                except (ValueError, sqlite3.IntegrityError) as exc:
                    self.owner.notify(f"Не сохранено: {exc}", error=True)
                    return
                alarms.log_operation(self.owner.session.username, "Сохранение правила тревоги", data["name"])
                self.refresh_rules()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    # ================================================================== учётные записи
    def _accounts_page(self):
        notebook = Gtk.Notebook()
        W.margins(notebook, 12)
        users = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(users, 10)
        users.append(_toolbar(W.button("＋ Добавить", lambda: self.edit_user(None), "red-button"),
                              W.button("Удалить", self.delete_users), W.button("Сбросить пароль", self.reset_user)))
        self.user_table = W.Table(("Пользователь", "Роль", "Email", "Состояние", "Срок действия"),
                                  on_activate=lambda uid: self.edit_user(uid))
        users.append(_scroll(self.user_table))
        notebook.append_page(users, W.label("Пользователи"))
        roles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(roles, 10)
        roles.append(_toolbar(W.button("＋ Добавить роль", lambda: self.edit_role(None), "red-button"),
                              W.button("Удалить", self.delete_roles)))
        self.role_table = W.Table(("Роль", "Права"), on_activate=lambda rid: self.edit_role(rid))
        roles.append(_scroll(self.role_table))
        notebook.append_page(roles, W.label("Роли"))
        notebook.append_page(self._security_settings(), W.label("Настройки безопасности"))
        return notebook

    def refresh_accounts(self):
        users = security.load_users()
        self.users = {u["id"]: u for u in users}
        if not users:
            self.user_table.set_rows([("admin (без пароля)", "Администратор", "—",
                                       "Задайте пароль через 🔒 или «Сменить пароль»", "—")], [0])
        else:
            self.user_table.set_rows([(u["username"], u["role_name"] or "—", u["email"] or "—",
                                       "включён" if u["enabled"] else "отключён", u["expires"] or "бессрочно")
                                      for u in users], [u["id"] for u in users])
        roles = security.load_roles()
        self.roles = {r["id"]: r for r in roles}
        self.role_table.set_rows([(r["name"] + (" (встроенная)" if r["builtin"] else ""),
                                   ", ".join(security.PERMISSIONS[p] for p in r["permissions"])) for r in roles],
                                 [r["id"] for r in roles])

    def edit_user(self, user_id):
        if not security.has_users():
            self.owner.notify("Сначала задайте пароль администратора (admin)")
            self.owner._show_lock_screen("setup")
            return
        user = self.users.get(user_id) if user_id else None
        roles = security.load_roles()
        dialog = _dialog(self, "Пользователь", "Сохранить")
        form = _form()
        username = _field(form, 0, "Имя пользователя *", Gtk.Entry(text=user["username"] if user else ""))
        username.set_sensitive(user is None)
        password = None
        if user is None:
            password = _field(form, 1, "Пароль *", Gtk.PasswordEntry(show_peek_icon=True))
        role = _field(form, 2, "Роль", _dropdown([r["name"] for r in roles],
                                                 next((i for i, r in enumerate(roles)
                                                       if user and r["id"] == user["role_id"]), 1)))
        email = _field(form, 3, "Email", Gtk.Entry(text=user["email"] if user else ""))
        expires = _field(form, 4, "Действует до (ГГГГ-ММ-ДД)", Gtk.Entry(text=user["expires"] if user else ""))
        enabled = Gtk.CheckButton(label="Включён")
        enabled.set_active(bool(user["enabled"]) if user else True)
        form.attach(enabled, 0, 5, 2, 1)
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    if user is None:
                        security.create_user(username.get_text(), password.get_text(),
                                             roles[role.get_selected()]["id"], email.get_text(),
                                             expires.get_text().strip(), security.load_policy())
                    else:
                        security.update_user(user_id, roles[role.get_selected()]["id"], email.get_text(),
                                             enabled.get_active(), expires.get_text().strip())
                except (ValueError, sqlite3.IntegrityError) as exc:
                    self.owner.notify(f"Не сохранено: {exc}", error=True)
                    return
                alarms.log_operation(self.owner.session.username, "Изменение пользователя", username.get_text())
                self.refresh_accounts()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def delete_users(self):
        ids = [i for i in self.user_table.selected() if i]
        if ids:
            def go():
                for user_id in ids:
                    try:
                        security.delete_user(user_id)
                    except ValueError as exc:
                        self.owner.notify(str(exc), error=True)
                self.refresh_accounts()
            self.owner._confirm(f"Удалить пользователей: {len(ids)}?", go)

    def reset_user(self):
        ids = [i for i in self.user_table.selected() if i]
        if len(ids) != 1:
            self.owner.notify("Отметьте одного пользователя")
            return
        user = self.users[ids[0]]
        dialog = _dialog(self, f"Новый пароль · {user['username']}", "Сохранить")
        form = _form()
        password = _field(form, 0, "Новый пароль", Gtk.PasswordEntry(show_peek_icon=True))
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    security.reset_password(user["id"], password.get_text(), user["username"], security.load_policy())
                except ValueError as exc:
                    self.owner.notify(str(exc), error=True)
                    return
                alarms.log_operation(self.owner.session.username, "Сброс пароля", user["username"])
                self.owner.notify("Пароль изменён")
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def edit_role(self, role_id):
        role = self.roles.get(role_id) if role_id else None
        if role and role["builtin"]:
            self.owner.notify("Встроенные роли не изменяются — создайте новую")
            return
        dialog = _dialog(self, "Роль", "Сохранить")
        form = _form()
        name = _field(form, 0, "Название *", Gtk.Entry(text=role["name"] if role else ""))
        checks = []
        for index, (key, title) in enumerate(security.PERMISSIONS.items()):
            check = Gtk.CheckButton(label=title)
            check.set_active(bool(role) and key in role["permissions"])
            checks.append((check, key))
            form.attach(check, 0, 1 + index, 2, 1)
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    security.save_role(name.get_text(), [k for c, k in checks if c.get_active()], role_id)
                except (ValueError, sqlite3.IntegrityError) as exc:
                    self.owner.notify(f"Не сохранено: {exc}", error=True)
                    return
                self.refresh_accounts()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def delete_roles(self):
        for role_id in self.role_table.selected():
            try:
                security.delete_role(role_id)
            except ValueError as exc:
                self.owner.notify(str(exc), error=True)
        self.refresh_accounts()

    def _security_settings(self):
        policy = security.load_policy()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        form = _form()
        validity = _field(form, 0, "Срок действия пароля, дней (0 — без ограничения)",
                          Gtk.SpinButton.new_with_range(0, 3650, 1))
        validity.set_value(policy["password_validity_days"])
        warn = _field(form, 1, "Предупреждать за, дней", Gtk.SpinButton.new_with_range(0, 90, 1))
        warn.set_value(policy["warn_before_days"])
        lock = _field(form, 2, "Автоблокировка клиента после бездействия, мин (0 — выкл)",
                      Gtk.SpinButton.new_with_range(0, 240, 1))
        lock.set_value(policy["auto_lock_minutes"])
        attempts = _field(form, 3, "Неудачных попыток входа до блокировки", Gtk.SpinButton.new_with_range(0, 20, 1))
        attempts.set_value(policy["max_failed_attempts"])
        lock_minutes = _field(form, 4, "Длительность блокировки, мин", Gtk.SpinButton.new_with_range(1, 1440, 1))
        lock_minutes.set_value(policy["lock_minutes"])
        min_length = _field(form, 5, "Минимальная длина пароля", Gtk.SpinButton.new_with_range(6, 64, 1))
        min_length.set_value(policy["min_password_length"])
        login = Gtk.CheckButton(label="Запрашивать вход при запуске клиента")
        login.set_active(policy["require_login"])
        form.attach(login, 0, 6, 2, 1)
        box.append(form)

        def save():
            if login.get_active() and not security.has_users():
                self.owner.notify("Сначала задайте пароль администратора", error=True)
                self.owner._show_lock_screen("setup")
                return
            security.save_policy({"password_validity_days": validity.get_value_as_int(),
                                  "warn_before_days": warn.get_value_as_int(),
                                  "auto_lock_minutes": lock.get_value_as_int(),
                                  "max_failed_attempts": attempts.get_value_as_int(),
                                  "lock_minutes": lock_minutes.get_value_as_int(),
                                  "min_password_length": min_length.get_value_as_int(),
                                  "require_login": login.get_active()})
            alarms.log_operation(self.owner.session.username, "Изменение настроек безопасности")
            self.owner.notify("Настройки безопасности сохранены")

        box.append(_toolbar(W.button("Сохранить", save, "red-button")))
        return box

    # ================================================================== конфигурация системы
    def _config_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        nav = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        nav.add_css_class("system-nav")
        nav.set_size_request(220, -1)
        stack = Gtk.Stack()
        stack.set_hexpand(True)
        self.config_nav = {}
        first = None
        for key, title, builder in (("email", "Email", self._email_config),
                                    ("storage", "Системное хранилище", self._storage_config),
                                    ("retention", "Срок хранения данных", self._retention_config),
                                    ("holiday", "Праздники", self._holiday_config),
                                    ("network", "Время и сеть", self._network_config),
                                    ("capability", "Возможности сервиса", self._capability_config)):
            button = Gtk.ToggleButton(label=title)
            if first:
                button.set_group(first)
            else:
                first = button
            button.connect("toggled", lambda b, k=key: b.get_active() and stack.set_visible_child_name(k))
            nav.append(button)
            self.config_nav[key] = button
            stack.add_named(_scroll(builder()), key)
        first.set_active(True)
        page.append(nav)
        page.append(stack)
        return page

    def _email_config(self):
        config = storage.get_json_setting("smtp", dict(alarms.DEFAULT_SMTP))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(box, 16)
        box.append(W.label("Почта для отправки тревог", "section-title"))
        form = Gtk.Grid(column_spacing=12, row_spacing=9)
        server = _field(form, 0, "SMTP-сервер", Gtk.Entry(text=config["server"]))
        port = _field(form, 1, "Порт", Gtk.SpinButton.new_with_range(1, 65535, 1))
        port.set_value(config["port"])
        mode = _field(form, 2, "Шифрование", _dropdown(["STARTTLS", "SSL/TLS", "Нет"],
                                                       ["starttls", "ssl", "none"].index(config["security"])))
        user = _field(form, 3, "Пользователь", Gtk.Entry(text=config["username"]))
        password = _field(form, 4, "Пароль", Gtk.PasswordEntry(show_peek_icon=True))
        sender = _field(form, 5, "Адрес отправителя", Gtk.Entry(text=config["sender"]))
        name = _field(form, 6, "Имя отправителя", Gtk.Entry(text=config["sender_name"]))
        test_to = _field(form, 7, "Тестовое письмо на", Gtk.Entry())
        box.append(form)

        def collect():
            return {"server": server.get_text().strip(), "port": port.get_value_as_int(),
                    "security": ["starttls", "ssl", "none"][mode.get_selected()], "username": user.get_text().strip(),
                    "sender": sender.get_text().strip(), "sender_name": name.get_text().strip() or "Sentinel"}

        def save():
            data = collect()
            storage.set_json_setting("smtp", data)
            if password.get_text():
                try:
                    storage.save_credentials({"host": "smtp:" + data["server"], "http_port": 0},
                                             {"username": data["username"] or "smtp", "password": password.get_text()})
                except (storage.CredentialStoreError, ValueError) as exc:
                    self.owner.notify(f"Пароль SMTP не сохранён: {exc}", error=True)
                    return
            self.owner.notify("Настройки почты сохранены")

        def test():
            data = collect()
            secret = password.get_text()

            def worker():
                try:
                    alarms.send_email(data, secret, test_to.get_text(), "Sentinel: тест", "Тестовое письмо Sentinel.")
                    GLib.idle_add(self.owner.notify, "Тестовое письмо отправлено")
                except Exception as exc:
                    GLib.idle_add(self.owner.notify, f"Ошибка отправки: {exc}", True)

            threading.Thread(target=worker, daemon=True).start()

        box.append(_toolbar(W.button("Сохранить", save, "red-button"), W.button("Отправить тест", test)))
        return box

    def _storage_config(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        W.margins(box, 16)
        box.append(W.label("Пространство хранения", "section-title"))
        root = Gtk.Entry(text=str(recording.storage_root()))
        box.append(_toolbar(W.label("Папка резервной записи", "muted"), root,
                            W.button("Сохранить", lambda: (storage.set_setting("storage_root", root.get_text().strip()),
                                                           refresh(), self.owner.notify("Папка сохранена")),
                                     "red-button")))
        level = Gtk.LevelBar()
        level.set_min_value(0)
        level.set_max_value(1)
        level.set_size_request(-1, 16)
        box.append(level)
        info = W.label("", "muted", wrap=True)
        box.append(info)
        box.append(W.button("Обновить", lambda: refresh()))
        box.append(W.label("Какие камеры записывать на ПК, задаётся в «Управление устройствами» → устройство → "
                           "«Ресурсы» → «Настройка записи». Рекомендуется оставлять основной архив на регистраторе: "
                           "ПК — резервная копия.", "muted", wrap=True))

        def refresh():
            data = recording.usage(recording.storage_root())
            total = data["total"] or 1
            used = total - data["free"]
            level.set_value(used / total)
            days = max(data["days"].values(), default=0)
            estimate = recording.estimate_days_left(data["video_bytes"], days, data["free"])
            info.set_text(f"Диск: занято {recording.human_bytes(used)} из {recording.human_bytes(total)} · "
                          f"свободно {recording.human_bytes(data['free'])}\n"
                          f"Видео Sentinel: {recording.human_bytes(data['video_bytes'])} · камер: {len(data['days'])} · "
                          f"записано дней: {days}" + (f" · хватит ещё примерно на {estimate} дн." if estimate else ""))

        refresh()
        return box

    def _retention_config(self):
        settings = recording.retention_settings()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(box, 16)
        form = Gtk.Grid(column_spacing=12, row_spacing=9)
        fields = {}
        for row, (key, title, maximum) in enumerate((("recording_days", "Видео на ПК, дней (по умолчанию)", 3650),
                                                     ("events_days", "События, дней", 3650),
                                                     ("alarms_days", "Тревоги, дней", 3650),
                                                     ("log_days", "Журнал операций, дней", 3650),
                                                     ("min_free_gb", "Держать свободными на диске, ГБ", 10000))):
            spin = _field(form, row, title, Gtk.SpinButton.new_with_range(1, maximum, 1))
            spin.set_value(settings[key])
            fields[key] = spin
        box.append(form)
        box.append(_toolbar(W.button("Сохранить", lambda: (storage.set_json_setting("data_retention", {
            k: s.get_value_as_int() for k, s in fields.items()}), self.owner.notify("Сохранено")), "red-button")))
        return box

    def _holiday_config(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(box, 16)
        table = W.Table(("Название", "С", "По"))
        name = Gtk.Entry(placeholder_text="Новый год")
        start = Gtk.Entry(placeholder_text="2027-01-01")
        end = Gtk.Entry(placeholder_text="2027-01-08")

        def refresh():
            with storage.database() as db:
                rows = [dict(r) for r in db.execute("SELECT * FROM holidays ORDER BY start_date")]
            table.set_rows([(r["name"], r["start_date"], r["end_date"]) for r in rows], [r["id"] for r in rows])

        def add():
            try:
                for value in (start.get_text(), end.get_text() or start.get_text()):
                    datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                self.owner.notify("Даты в формате ГГГГ-ММ-ДД", error=True)
                return
            with storage.database() as db:
                db.execute("INSERT INTO holidays(name,start_date,end_date) VALUES(?,?,?)",
                           (name.get_text().strip() or "Праздник", start.get_text(), end.get_text() or start.get_text()))
            refresh()

        def delete():
            with storage.database() as db:
                db.executemany("DELETE FROM holidays WHERE id=?", [(i,) for i in table.selected()])
            refresh()

        box.append(_toolbar(name, start, end, W.button("Добавить", add, "red-button"), W.button("Удалить", delete)))
        box.append(W.label("В праздничные дни правила тревог и расписания доступа действуют как в выходной.",
                           "muted", wrap=True))
        box.append(table)
        refresh()
        return box

    def _network_config(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(box, 16)
        box.append(W.label("Синхронизация времени устройств", "section-title"))
        settings = storage.get_json_setting("time_sync", {"enabled": False, "hours": 24})
        enabled = Gtk.CheckButton(label="Периодически устанавливать на устройствах время этого ПК")
        enabled.set_active(settings["enabled"])
        hours = Gtk.SpinButton.new_with_range(1, 168, 1)
        hours.set_value(settings["hours"])
        box.append(enabled)
        box.append(_toolbar(W.label("Интервал, ч", "muted"), hours))
        box.append(_toolbar(W.button("Сохранить", lambda: (storage.set_json_setting("time_sync", {
            "enabled": enabled.get_active(), "hours": hours.get_value_as_int()}), self.owner.notify("Сохранено")),
                                     "red-button"),
                            W.button("Синхронизировать сейчас", self.sync_selected_time)))
        box.append(W.label("Протоколы доступа", "section-title"))
        insecure = Gtk.CheckButton(label="Разрешить HTTP Basic по http:// (только для старых камер без Digest)")
        insecure.set_active(self.owner.allow_insecure_basic)
        insecure.connect("toggled", lambda b: self.owner._set_insecure_basic(b.get_active()))
        box.append(insecure)
        box.append(W.label("Удалённый доступ к клиенту и мобильное приложение Hik-Connect не поддерживаются: Sentinel "
                           "работает локально и подключается к устройствам напрямую.", "muted", wrap=True))
        return box

    def _capability_config(self):
        settings = storage.get_json_setting("capability", {"video": True, "access": True, "alarm": True})
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(box, 16)
        checks = {}
        for key, title in (("video", "Видео"), ("access", "Контроль доступа"), ("alarm", "Обнаружение тревог")):
            check = Gtk.CheckButton(label=title)
            check.set_active(settings[key])
            checks[key] = check
            box.append(check)
        checks["video"].set_sensitive(False)
        box.append(W.label("Отключённые модули скрываются из ярлыков и меню.", "muted"))

        def save():
            storage.set_json_setting("capability", {k: c.get_active() for k, c in checks.items()})
            self.owner.apply_capabilities()
            self.owner.notify("Сохранено")

        box.append(_toolbar(W.button("Сохранить", save, "red-button")))
        return box
