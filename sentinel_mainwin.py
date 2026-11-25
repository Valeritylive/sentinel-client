"""Главное окно в стиле HikCentral Lite (примесь к SentinelWindow).

  ┌ логотип │ вкладки видов … + │ Поиск  Система  ▦  ⤓  ?  admin▾  🔒 ┐
  │ левая панель │      видеосетка вида            │ правая панель     │
  │ ресурсы,     │                                  │ Тревога / Событие │
  │ карта, …     ├──────────────────────────────────┤                   │
  │ ярлыки       │ Живой просмотр | Воспроизведение, шкала времени     │
  └ строка состояния ─────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from gi.repository import Gdk, GLib, GObject, Gtk

import sentinel_alarms as alarms
import sentinel_devmgmt as devmgmt
import sentinel_layouts as layouts
import sentinel_people as people
import sentinel_recording as recording
import sentinel_reports as reports
import sentinel_security as security
import sentinel_shell as shell
import sentinel_storage as storage
import sentinel_timeline as tl
import sentinel_views as views
import sentinel_widgets as W

SPEEDS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
MODULE_PAGES = {
    "main_view": "overview", "overview": "overview",
    "playback": "archive", "archive": "archive",
    "events": "alarms", "alarms": "alarms",
    "emap": "emap", "access": "access",
    "summary": "dashboard", "dashboard": "dashboard", "logs": "dashboard",
    "local_files": "local_files",
}
PAGE_TITLES = {"archive": "Поиск записей", "alarms": "Центр событий", "emap": "Электронная карта",
               "access": "Контроль доступа", "dashboard": "Статистика", "local_files": "Локальные файлы"}
SYSTEM_MODULES = {"devices": "devices", "event_config": "event", "storage": "config:storage",
                  "accounts": "accounts", "settings": "config"}


class MainWindowMixin:
    # ================================================================== построение
    def _build_window(self):
        self.view_tabs = views.ViewTabs()
        self.slot_resources = {}
        self._applying_tab = False
        self.hide_offline = False
        self.user_left_panel = True
        self.user_right_panel = True
        self._last_width = 0
        self.bar_mode = "live"
        self.bar_collapsed = False
        self.notifications = []
        self.timeline_cache = {}
        self.timeline_token = None
        self.session = security.local_session()
        self.system_window = None
        self.alarm_details_window = None
        self.door_states = {}
        self.set_titlebar(self._build_header())

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)
        self.stack.add_named(Gtk.Box(), "overview")
        self.stack.add_named(self._archive_page(), "archive")
        self.stack.add_named(self._alarms_page(), "alarms")
        self.stack.add_named(self._emap_page(), "emap")
        self.stack.add_named(self._access_page(), "access")
        self.stack.add_named(self._dashboard_page(), "dashboard")
        self.stack.add_named(self._local_files_page(), "local_files")
        self.stack.set_visible_child_name("overview")
        self.group_list = None

        workspace = self._overview_page()
        self.inner_window = W.InnerWindow(self._close_inner)
        self.inner_window.content.add_named(self.stack, "modules")
        self.inner_window.set_visible(False)
        self.toast = W.Toast()
        center = Gtk.Overlay()
        center.set_child(workspace)
        center.add_overlay(self.inner_window)
        center.add_overlay(self.toast)
        center.set_hexpand(True)
        center.set_vexpand(True)
        self.center_overlay = center

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.left_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self.left_revealer.set_child(self._build_left_panel())
        self.left_revealer.set_reveal_child(True)
        body.append(self.left_revealer)
        self.left_handle = W.button("‹", self._toggle_left_panel, "panel-handle", "Скрыть/показать панель ресурсов")
        body.append(self.left_handle)
        body.append(center)
        self.right_handle = W.button("›", self._toggle_right_panel, "panel-handle", "Скрыть/показать панель тревог")
        body.append(self.right_handle)
        self.right_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.right_revealer.set_child(self._build_right_panel())
        self.right_revealer.set_reveal_child(True)
        body.append(self.right_revealer)
        body.set_vexpand(True)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main.append(body)
        main.append(self._status_bar())
        root = Gtk.Overlay()
        root.set_child(main)
        self.lock_layer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.lock_layer.add_css_class("lock-screen")
        self.lock_layer.set_visible(False)
        root.add_overlay(self.lock_layer)
        self.set_child(root)

        self.connect("close-request", self._on_close)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)
        activity = Gtk.EventControllerMotion()
        activity.connect("motion", lambda *_: self._note_activity())
        self.add_controller(activity)
        GLib.timeout_add_seconds(1, self._tick_clock)
        GLib.timeout_add(400, self._adaptive_tick)
        GLib.timeout_add(500, self._timeline_tick)
        self._new_view_tab()

    def _build_header(self):
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        logo = W.label("⬢", "hcl-logo")
        logo.set_tooltip_text("Sentinel")
        header.pack_start(logo)
        self.view_tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        header.pack_start(self.view_tab_box)
        header.pack_start(W.button("＋", self._new_view_tab, tooltip="Новый вид", flat=True))
        header.set_title_widget(Gtk.Label(label=""))
        self.header_buttons = {}
        lock = W.button("🔒", self.lock_client, tooltip="Заблокировать клиент")
        header.pack_end(lock)
        self.user_button = Gtk.MenuButton(label="admin ▾")
        self.user_button.set_popover(self._user_popover())
        header.pack_end(self.user_button)
        header.pack_end(W.button("?", self.show_about, tooltip="Справка и сочетания клавиш"))
        header.pack_end(W.button("⤓", lambda: self._show_module("local_files"),
                                 tooltip="Локальные файлы: снимки, записи, экспорт"))
        apps = Gtk.MenuButton(label="▦")
        apps.set_tooltip_text("Модули")
        apps.set_popover(self._modules_popover(apps))
        header.pack_end(apps)
        for key, text, tooltip, callback in (
                ("system", "⚙ Система", "Конфигурация системы", lambda: self.open_system()),
                ("search", "⌕ Поиск", "Поиск записей и событий", lambda: self._show_module("archive"))):
            widget = W.button(text, callback, tooltip=tooltip)
            self.header_buttons[key] = widget
            header.pack_end(widget)
        self.header = header
        return header

    def _modules_popover(self, menu_button):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        W.margins(box, 6)
        for title, key in (("Статистика и сводка", "summary"), ("Центр событий", "events"),
                           ("Поиск записей", "playback"), ("Электронная карта", "emap"),
                           ("Контроль доступа", "access"), ("Локальные файлы", "local_files")):
            item = W.button(title, lambda k=key: (popover.popdown(), self._show_module(k)), flat=True)
            box.append(item)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        box.append(W.button("Окно просмотра на другом экране…",
                            lambda: (popover.popdown(), self.open_view_window()), flat=True))
        box.append(W.button("Настройка устройств…", lambda: (popover.popdown(), self.open_system("devices")),
                            flat=True))
        popover.set_child(box)
        return popover

    def _user_popover(self):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        W.margins(box, 8)
        self.user_title = W.label("admin", "section-title")
        box.append(self.user_title)
        box.append(W.label("Локальный клиент Sentinel", "muted"))
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        for title, callback in (("Лицензии", self.show_license_info),
                                ("Резервное копирование и восстановление", self.backup_restore_dialog),
                                ("Локальная конфигурация", self.open_local_settings),
                                ("Сменить пароль", self.change_password_dialog),
                                ("О программе", self.show_about)):
            box.append(W.button(title, lambda cb=callback: (popover.popdown(), cb()), flat=True))
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        box.append(W.button("Выйти", lambda: (popover.popdown(), self.log_out())))
        popover.set_child(box)
        return popover

    # ------------------------------------------------------------------ левая панель
    def _build_left_panel(self):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        panel.add_css_class("side-panel")
        panel.set_size_request(240, -1)
        self.left_panel = panel
        self.camera_search = Gtk.SearchEntry(placeholder_text="Поиск")
        W.margins(self.camera_search, 6)
        self.camera_search.connect("search-changed", lambda *_: self._fill_resource_list())
        panel.append(self.camera_search)

        sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.resource_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.resource_list.set_margin_start(4)
        self.resource_section = W.section("Sentinel", "⌂", self.resource_list, True,
                                          (("ⓘ", "Условные обозначения", self.show_resource_legend),
                                           ("⋯", "Действия", self._resource_root_menu)))
        sections.append(self.resource_section)

        self.left_maps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        sections.append(W.section("Карта", "⌖", self.left_maps, False,
                                  (("＋", "Добавить карту", self.add_map),)))
        stats = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        stats.append(W.button("◈ Сводка", lambda: self.open_report(None), "tree-row"))
        for key, title in reports.REPORTS.items():
            stats.append(W.button("▤ " + title, lambda k=key: self.open_report(k), "tree-row"))
        sections.append(W.section("Статистика", "▤", stats))
        self.left_web = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        sections.append(W.section("Веб-страница", "◍", self.left_web, False,
                                  (("＋", "Добавить веб-страницу", self.add_web_page_dialog),)))
        files = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        for title, kind in (("Снимки", "captures"), ("Видеозаписи и экспорт", "records"),
                            ("Резервное хранилище", "storage")):
            files.append(W.button("▸ " + title, lambda k=kind: self.open_local_files(k), "tree-row"))
        sections.append(W.section("Локальные файлы", "▢", files))
        self.custom_view_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        sections.append(W.section("Вид", "▦", self.custom_view_list, False,
                                  (("＋", "Новая группа видов", self.add_view_group_dialog),)))
        sections.append(W.section("PTZ", "✥", self._ptz_panel()))

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(sections)
        panel.append(scroll)
        self.resources_panel = scroll

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        W.margins(head, 6)
        head.append(W.label("➤", "muted"))
        title = W.label("Ярлыки", "section-title")
        title.set_hexpand(True)
        head.append(title)
        head.append(W.button("✎", self.edit_shortcuts_dialog, flat=True, tooltip="Настроить ярлыки"))
        self.shortcut_revealer = Gtk.Revealer()
        self.shortcut_revealer.set_reveal_child(True)
        toggle = W.button("˄", None, flat=True, tooltip="Свернуть/развернуть ярлыки")
        toggle.connect("clicked", lambda b: (self.shortcut_revealer.set_reveal_child(
            not self.shortcut_revealer.get_reveal_child()),
            b.set_label("˄" if self.shortcut_revealer.get_reveal_child() else "˅")))
        head.append(toggle)
        panel.append(head)
        self.shortcut_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.shortcut_box.set_margin_bottom(6)
        self.shortcut_revealer.set_child(self.shortcut_box)
        panel.append(self.shortcut_revealer)
        self._render_shortcuts()
        return panel

    def _ptz_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        ptz = Gtk.Grid(column_spacing=3, row_spacing=3)
        ptz.set_halign(Gtk.Align.CENTER)
        self.ptz_buttons = []
        for x, y, text, direction in ((1, 0, "↑", "up"), (0, 1, "←", "left"), (1, 1, "■", "stop"),
                                      (2, 1, "→", "right"), (1, 2, "↓", "down"), (0, 0, "↖", "up_left"),
                                      (2, 0, "↗", "up_right"), (0, 2, "↙", "down_left"),
                                      (2, 2, "↘", "down_right"), (0, 3, "−", "zoom_out"), (2, 3, "+", "zoom_in")):
            item = Gtk.Button(label=text)
            item.set_sensitive(False)
            gesture = Gtk.GestureClick()
            gesture.connect("pressed", lambda *_a, d=direction: self._ptz_press(d))
            gesture.connect("released", lambda *_a, d=direction: self._ptz_release(d))
            item.add_controller(gesture)
            ptz.attach(item, x, y, 1, 1)
            self.ptz_buttons.append(item)
        box.append(ptz)
        speed = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        speed.append(W.label("Скорость", "muted"))
        self.ptz_speed = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 7, 1)
        self.ptz_speed.set_value(4)
        self.ptz_speed.set_hexpand(True)
        speed.append(self.ptz_speed)
        box.append(speed)
        box.append(W.button("Пресеты · маршруты · паттерны…", self.open_ptz_tools))
        W.margins(box, 6)
        return box

    # ------------------------------------------------------------------ правая панель
    def _build_right_panel(self):
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        panel.add_css_class("side-panel")
        panel.add_css_class("right")
        panel.set_size_request(240, -1)
        self.right_panel = panel
        tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        W.margins(tabs, 4)
        self.right_stack = Gtk.Stack()
        self.alarm_tab = Gtk.ToggleButton(label="⚑ Тревога")
        self.event_tab = Gtk.ToggleButton(label="☰ Событие")
        self.event_tab.set_group(self.alarm_tab)
        for widget, page in ((self.alarm_tab, "alarm"), (self.event_tab, "event")):
            widget.add_css_class("filter-button")
            widget.connect("toggled", lambda b, p=page: b.get_active() and self.right_stack.set_visible_child_name(p))
            tabs.append(widget)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        tabs.append(spacer)
        tabs.append(W.button("✓", self.batch_acknowledge, flat=True, tooltip="Подтвердить все тревоги"))
        self.bell_button = Gtk.MenuButton(label="🔔")
        self.bell_button.set_tooltip_text("Уведомления")
        self.bell_popover = Gtk.Popover()
        self.bell_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        W.margins(self.bell_list, 8)
        bell_scroll = Gtk.ScrolledWindow()
        bell_scroll.set_min_content_height(240)
        bell_scroll.set_min_content_width(320)
        bell_scroll.set_child(self.bell_list)
        self.bell_popover.set_child(bell_scroll)
        self.bell_button.set_popover(self.bell_popover)
        tabs.append(self.bell_button)
        panel.append(tabs)

        alarm_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        filters.set_margin_start(6)
        filters.set_margin_end(6)
        self.alarm_priority_filter = Gtk.DropDown.new_from_strings(["Любой приоритет", "Высокий", "Средний", "Низкий"])
        self.alarm_status_filter = Gtk.DropDown.new_from_strings(["Все", "Не подтверждённые"])
        for widget in (self.alarm_priority_filter, self.alarm_status_filter):
            widget.connect("notify::selected", lambda *_: self._refresh_alarm_panel())
            widget.set_hexpand(True)
            filters.append(widget)
        alarm_page.append(filters)
        trigger = W.button("⚡ Запустить пользовательское событие", self.trigger_user_event, flat=True)
        trigger.set_margin_start(6)
        alarm_page.append(trigger)
        self.alarm_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.alarm_list)
        alarm_page.append(scroll)
        self.right_stack.add_named(alarm_page, "alarm")

        event_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        ev_filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        ev_filters.set_margin_start(6)
        self.event_category = "all"
        first = None
        for key, text in (("all", "Все"), ("event", "▣"), ("person", "☺"), ("vehicle", "⛟")):
            widget = Gtk.ToggleButton(label=text)
            widget.add_css_class("filter-button")
            widget.set_tooltip_text({"all": "Все события", "event": "События", "person": "Персоны",
                                     "vehicle": "Транспорт"}[key])
            if first:
                widget.set_group(first)
            else:
                first = widget
                widget.set_active(True)
            widget.connect("toggled", lambda b, k=key: b.get_active() and self._set_event_category(k))
            ev_filters.append(widget)
        event_page.append(ev_filters)
        self.event_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        ev_scroll = Gtk.ScrolledWindow()
        ev_scroll.set_vexpand(True)
        ev_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        ev_scroll.set_child(self.event_list)
        event_page.append(ev_scroll)
        self.right_stack.add_named(event_page, "event")
        self.right_stack.set_vexpand(True)
        panel.append(self.right_stack)
        self.alarm_tab.set_active(True)
        return panel

    # ------------------------------------------------------------------ рабочая область
    def _overview_page(self):
        workspace = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        workspace.add_css_class("workspace")
        self.door_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.door_bar.add_css_class("door-bar")
        self.door_bar_label = W.label("")
        self.door_bar.append(self.door_bar_label)
        self.door_bar.set_visible(False)
        workspace.append(self.door_bar)

        self.camera_grid = Gtk.Grid(column_spacing=1, row_spacing=1)
        self.camera_grid.set_hexpand(True)
        self.camera_grid.set_column_homogeneous(True)
        self.camera_grid.set_valign(Gtk.Align.START)
        camera_scroll = Gtk.ScrolledWindow()
        camera_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        camera_scroll.set_hexpand(True)
        camera_scroll.set_vexpand(True)
        camera_scroll.set_child(self.camera_grid)
        self.camera_scroll = camera_scroll
        self.camera_area = Gtk.Overlay()
        self.camera_area.set_child(camera_scroll)
        self.camera_area.set_hexpand(True)
        self.camera_area.set_vexpand(True)
        workspace.append(self.camera_area)
        self.overview_monitor = workspace
        self.camera_grid_toggle = Gtk.Box()  # совместимость: кнопку заменила ручка левой панели
        workspace.append(self._build_bottom_bar())

        self.division = "s4"
        self.stream_mode = layouts.STREAM_AUTO
        self.layout_count = 4
        self.grid_columns, self.grid_rows = 2, 2
        self.cell_layout = None
        self.selected_slot = 0
        self.video_slots = []
        self._render_video_grid()
        return workspace

    def _build_bottom_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        bar.add_css_class("playback-bar")
        self.playback_bar = bar
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.live_mode_button = Gtk.ToggleButton(label="● Живой просмотр")
        self.playback_mode_button = Gtk.ToggleButton(label="● Воспроизведение")
        self.playback_mode_button.set_group(self.live_mode_button)
        self.live_mode_button.set_active(True)
        for widget, mode in ((self.live_mode_button, "live"), (self.playback_mode_button, "playback")):
            widget.add_css_class("mode-button")
            widget.connect("toggled", lambda b, m=mode: b.get_active() and self.set_bar_mode(m))
            top.append(widget)
        self.sync_playback = Gtk.CheckButton(label="Синхронное воспроизведение")
        self.sync_playback.set_tooltip_text("Воспроизводить все камеры вида с одного момента времени")
        self.sync_playback.set_visible(False)
        top.append(self.sync_playback)
        left_spacer = Gtk.Box()
        left_spacer.set_hexpand(True)
        top.append(left_spacer)
        self.bar_collapse_button = W.button("⌄", self.toggle_bar_collapsed, flat=True, tooltip="Свернуть шкалу")
        top.append(self.bar_collapse_button)
        right_spacer = Gtk.Box()
        right_spacer.set_hexpand(True)
        top.append(right_spacer)
        self.timeline_filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        first = None
        for key, text in (("all", "Все"), ("event", "■ Событие"), ("person", "☺ Персона"), ("vehicle", "⛟ ТС")):
            widget = Gtk.ToggleButton(label=text)
            widget.add_css_class("filter-button")
            if first:
                widget.set_group(first)
            else:
                first = widget
                widget.set_active(True)
            widget.connect("toggled", lambda b, k=key: b.get_active() and self._set_timeline_filter(k))
            self.timeline_filters.append(widget)
        top.append(self.timeline_filters)
        bar.append(top)

        self.timeline_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.timeline = W.Timeline(self.timeline_seek, self._load_timeline_segments)
        line.append(self.timeline)
        zoom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        zoom.set_valign(Gtk.Align.CENTER)
        zoom.append(W.button("−", lambda: self.zoom_timeline(-1), flat=True, tooltip="Уменьшить масштаб"))
        self.zoom_label = W.label("24ч", xalign=0.5)
        self.zoom_label.set_size_request(42, -1)
        zoom.append(self.zoom_label)
        zoom.append(W.button("+", lambda: self.zoom_timeline(+1), flat=True, tooltip="Увеличить масштаб"))
        line.append(zoom)
        self.timeline_area.append(line)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.bar_stop_button = W.button("■", self.stop_playback_mode, flat=True, tooltip="Остановить воспроизведение")
        controls.append(self.bar_stop_button)
        self.bar_play_button = W.button("Ⅱ", self.bar_play_pause, "round-button", "Пауза / продолжить")
        controls.append(self.bar_play_button)
        controls.append(W.button("↺30", lambda: self.bar_jump(-30), flat=True, tooltip="Назад на 30 секунд"))
        controls.append(W.button("30↻", lambda: self.bar_jump(30), flat=True, tooltip="Вперёд на 30 секунд"))
        controls.append(W.button("|▷", self.bar_step, flat=True, tooltip="Следующий кадр (на паузе)"))
        controls.append(W.button("«", lambda: self.bar_speed(-1), flat=True, tooltip="Медленнее"))
        self.bar_speed_label = W.label("1x", xalign=0.5)
        self.bar_speed_label.set_size_request(36, -1)
        controls.append(self.bar_speed_label)
        controls.append(W.button("»", lambda: self.bar_speed(+1), flat=True, tooltip="Быстрее"))
        controls.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        controls.append(self._live_toolbar())
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        controls.append(spacer)
        self.bar_date_button = Gtk.MenuButton(label=tl.format_position(datetime.now()) + " ⌄")
        self.bar_date_button.set_tooltip_text("Перейти к дате и времени")
        self.bar_date_button.set_popover(self._date_popover())
        controls.append(self.bar_date_button)
        self.timeline_area.append(controls)
        bar.append(self.timeline_area)
        self._update_bar_controls()
        return bar

    def _live_toolbar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.live_toolbar = box
        capture = W.button("📷", self.capture_selected, flat=True, tooltip="Снимок выбранного окна")
        box.append(capture)
        self.record_button = W.button("●", self.toggle_recording, flat=True, tooltip="Начать/остановить запись")
        self.compact_record_button = True
        box.append(self.record_button)
        self.audio_button = self._audio_control_menu()
        self.compact_audio_button = True
        self.audio_button.set_label("🔇")
        box.append(self.audio_button)
        instant = Gtk.MenuButton(label="⏪")
        instant.set_tooltip_text("Мгновенный повтор")
        popover = Gtk.Popover()
        items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for seconds in (30, 60, 180, 300, 600):
            text = "Последние 30 сек" if seconds < 60 else f"Последние {seconds // 60} мин"
            items.append(W.button(text, lambda s=seconds: (popover.popdown(), self.instant_playback(s)), flat=True))
        popover.set_child(items)
        instant.set_popover(popover)
        box.append(instant)
        box.append(self._layout_menu())
        box.append(self._stream_menu())
        self.auto_switch_button = W.button("⟳", self.toggle_auto_switch, flat=True, tooltip="Автопереключение камер")
        box.append(self.auto_switch_button)
        self.auto_interval_choice = Gtk.DropDown.new_from_strings(["5 с", "10 с", "20 с", "30 с", "60 с"])
        self.auto_interval_choice.set_selected(1)
        self.auto_interval_choice.set_tooltip_text("Интервал автопереключения")
        self.auto_interval_choice.connect("notify::selected", self._set_auto_switch_interval)
        box.append(self.auto_interval_choice)
        box.append(W.button("▶▶", self.start_all_channels, flat=True, tooltip="Открыть все камеры"))
        self.stop_button = W.button("■■", self.stop_all_streams, flat=True, tooltip="Остановить весь живой просмотр")
        box.append(self.stop_button)
        box.append(W.button("⛶", self.toggle_fullscreen, flat=True, tooltip="Полный экран (F11)"))
        self.stream_count_label = W.label("0 потоков", "muted")
        self.stream_count_label.set_margin_start(4)
        box.append(self.stream_count_label)
        return box

    def _layout_menu(self):
        menu = Gtk.MenuButton(label="▦")
        menu.set_tooltip_text("Деление окон")
        popover = Gtk.Popover()
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        W.margins(actions, 6)
        for title, pool in (("Стандартное", layouts.STANDARD_DIVISIONS), ("Широкое", layouts.WIDE_DIVISIONS)):
            actions.append(W.label(title, "muted"))
            flow = Gtk.FlowBox()
            flow.set_selection_mode(Gtk.SelectionMode.NONE)
            flow.set_max_children_per_line(5)
            flow.set_min_children_per_line(3)
            for division in pool:
                action = Gtk.Button(label=str(division.count))
                action.connect("clicked", lambda _b, k=division.key: (popover.popdown(), self._set_division(k)))
                flow.append(action)
            actions.append(flow)
        actions.append(W.button("Настроить сетку…", lambda: (popover.popdown(), self.customize_layout())))
        popover.set_child(actions)
        menu.set_popover(popover)
        return menu

    def _stream_menu(self):
        menu = Gtk.MenuButton(label="⇅")
        menu.set_tooltip_text("Тип потока")
        popover = Gtk.Popover()
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for title, mode in (("Основной", layouts.STREAM_MAIN), ("Дополнительный", layouts.STREAM_SUB),
                            ("Третий", layouts.STREAM_THIRD), ("Автоматически", layouts.STREAM_AUTO)):
            actions.append(W.button(title, lambda m=mode: (popover.popdown(), self._set_stream_mode(m)), flat=True))
        popover.set_child(actions)
        menu.set_popover(popover)
        return menu

    def _date_popover(self):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        W.margins(box, 8)
        self.bar_calendar = Gtk.Calendar()
        box.append(self.bar_calendar)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.append(W.label("Время", "muted"))
        self.bar_time_entry = Gtk.Entry(text="00:00:00")
        self.bar_time_entry.set_width_chars(9)
        row.append(self.bar_time_entry)
        box.append(row)
        box.append(W.button("Перейти", lambda: (popover.popdown(), self._goto_picked_date()), "red-button"))
        popover.set_child(box)
        return popover

    # ================================================================== адаптивность
    def _adaptive_tick(self):
        if getattr(self, "_closing", False):
            return False
        width = self.get_width()
        if not isinstance(width, int) or width == self._last_width:
            return True
        self._last_width = width
        self._apply_adaptive(width)
        return True

    def _apply_adaptive(self, width):
        decision = views.adaptive_layout(width, self.user_left_panel, self.user_right_panel)
        self.left_revealer.set_reveal_child(decision.left)
        self.right_revealer.set_reveal_child(decision.right)
        self.left_panel.set_size_request(decision.left_width, -1)
        self.right_panel.set_size_request(decision.right_width, -1)
        self.left_handle.set_label("‹" if decision.left else "›")
        self.right_handle.set_label("›" if decision.right else "‹")
        self.timeline_filters.set_visible(not decision.compact_bar)
        self.auto_interval_choice.set_visible(not decision.compact_bar)
        self.stream_count_label.set_visible(not decision.compact_bar)
        for key, widget in self.header_buttons.items():
            full = {"system": "⚙ Система", "search": "⌕ Поиск"}[key]
            widget.set_label(full.split(" ", 1)[0] if decision.compact_top else full)
        return decision

    def _toggle_left_panel(self):
        self.user_left_panel = not self.left_revealer.get_reveal_child()
        self._apply_adaptive(max(self._last_width, 900) if self.user_left_panel else self._last_width)

    def _toggle_right_panel(self):
        self.user_right_panel = not self.right_revealer.get_reveal_child()
        self._apply_adaptive(max(self._last_width, 1240) if self.user_right_panel else self._last_width)

    def _on_key_pressed(self, _controller, keyval, _keycode, state):
        self._note_activity()
        if keyval == Gdk.KEY_Escape and self.is_fullscreen():
            self.unfullscreen()
            return True
        if keyval == Gdk.KEY_F11:
            self.toggle_fullscreen()
            return True
        if not hasattr(self, "inner_window"):  # дополнительные окна просмотра
            return False
        if keyval == Gdk.KEY_Escape and self.inner_window.get_visible():
            self._close_inner()
            return True
        if keyval == Gdk.KEY_F11:
            self.toggle_fullscreen()
            return True
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval in (Gdk.KEY_t, Gdk.KEY_T):
            self._new_view_tab()
            return True
        if ctrl and keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self.save_view_tab()
            return True
        if ctrl and keyval in (Gdk.KEY_l, Gdk.KEY_L):
            self.lock_client()
            return True
        if keyval == Gdk.KEY_space and self.bar_mode == "playback":
            self.bar_play_pause()
            return True
        return False

    # ================================================================== модули
    def _show_module(self, key):
        if key in SYSTEM_MODULES:
            section, _, sub = SYSTEM_MODULES[key].partition(":")
            self.open_system(section, sub or None)
            return
        page = MODULE_PAGES.get(key, key if key in PAGE_TITLES else "overview")
        if page == "overview":
            self._close_inner()
            return
        self.stack.set_visible_child_name(page)
        self.inner_window.title.set_text(PAGE_TITLES.get(page, ""))
        self.inner_window.set_visible(True)
        if page == "archive":
            self._refresh_archive_choices()
        elif page == "emap":
            self._refresh_maps()
        elif page == "access":
            self._refresh_archive_choices()
        elif page == "dashboard":
            self._render_report()
        elif page == "alarms":
            self._render_event_center()
        elif page == "local_files":
            self._render_local_files()

    def _activate_tab(self, key):  # совместимость со старым API вкладок модулей
        self._show_module(key)

    def open_system(self, section=None, sub=None):
        from sentinel_system import SystemWindow
        if self.system_window is None:
            self.system_window = SystemWindow(self)
        if sub and sub.startswith("recording:"):
            device = next((d for d in self.devices if d["id"] == int(sub.split(":", 1)[1])), None)
            self.system_window.open("devices")
            if device:
                self.system_window.show_recording_config(device)
            return
        self.system_window.open(section, sub)

    def _set_insecure_basic(self, value):
        from sentinel_protocols import set_allow_insecure_basic
        self.allow_insecure_basic = bool(value)
        set_allow_insecure_basic(self.allow_insecure_basic)
        storage.set_setting("allow_insecure_basic", "1" if value else "0")

    def capabilities(self):
        return storage.get_json_setting("capability", {"video": True, "access": True, "alarm": True})

    def apply_capabilities(self):
        self._render_shortcuts()
        self.right_panel.set_visible(self.capabilities().get("alarm", True))
        self._fill_resource_list()

    def _close_inner(self):
        self.inner_window.set_visible(False)
        self.stack.set_visible_child_name("overview")

    def _place_camera_area_in_archive(self, _in_archive):
        """Воспроизведение теперь идёт прямо в видеосетке — переносить её не нужно."""

    # ================================================================== вкладки видов
    def _render_view_tabs(self):
        W.clear(self.view_tab_box)
        for tab in self.view_tabs.tabs:
            item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            item.add_css_class("view-tab")
            if tab.key == self.view_tabs.active:
                item.add_css_class("active")
            title = W.button(tab.title, lambda k=tab.key: self.switch_view_tab(k), flat=True)
            W.on_right_click(title, lambda w, x, y, k=tab.key: self._view_tab_menu(k, w, x, y))
            item.append(title)
            item.append(W.button("✕", lambda k=tab.key: self.close_view_tab(k), "tab-close", "Закрыть вид",
                                 flat=True))
            self.view_tab_box.append(item)

    def _view_tab_menu(self, key, widget, x, y):
        if key != self.view_tabs.active:
            self.switch_view_tab(key)
        W.popover_menu(widget, [("Сохранить вид", self.save_view_tab),
                                ("Сохранить как…", lambda: self.save_view_tab(save_as=True)),
                                ("Открыть в новом окне", self.open_tab_in_window),
                                None,
                                ("Закрыть", lambda: self.close_view_tab(key))], x, y)

    def _new_view_tab(self, *_args):
        if self.view_tabs.tabs:
            self._capture_current_tab()
        tab = self.view_tabs.new_tab(division="s4", layout=4, grid_columns=2, grid_rows=2)
        self._apply_tab(tab)
        return tab

    def switch_view_tab(self, key):
        if key == self.view_tabs.active:
            return
        self._capture_current_tab()
        tab = self.view_tabs.activate(key)
        self._apply_tab(tab)

    def close_view_tab(self, key):
        active = self.view_tabs.active
        tab = self.view_tabs.get(key)
        if tab is None:
            return
        if key != active:
            self.view_tabs.tabs.remove(tab)
            self._render_view_tabs()
            return
        self._capture_current_tab()
        self._apply_tab(self.view_tabs.close(key))

    def _current_mapping(self):
        mapping = []
        for slot, state in sorted(self.streams.items()):
            if not state.get("playback"):
                mapping.append({"slot": slot, "kind": views.KIND_CAMERA, "device_id": state["base_device"]["id"]})
        for slot, entry in self.slot_resources.items():
            mapping.append(dict(entry, slot=slot))
        return views.normalize_mapping(mapping)

    def _capture_current_tab(self):
        tab = self.view_tabs.current()
        if tab is None:
            return
        tab.mapping = self._current_mapping()
        tab.division = getattr(self, "division", "") or ""
        tab.layout = self.layout_count
        tab.grid_columns, tab.grid_rows = self.grid_columns, self.grid_rows

    def _apply_tab(self, tab):
        self._applying_tab = True
        try:
            self.stop_all_streams()
            for slot in list(self.slot_resources):
                self._clear_resource(slot)
            division = self._lookup_division(tab.division) if tab.division else None
            if self.maximized_slot is not None:
                self._restore_maximized_slot()
            if division and division.count == tab.layout:
                self.division = division.key
                self.layout_count = division.count
                self.grid_columns, self.grid_rows = division.cols, division.rows
                self.cell_layout = list(division.cells)
            else:
                self.division = ""
                self.layout_count = max(1, int(tab.layout))
                cols, rows = tab.grid_columns, tab.grid_rows
                if cols < 1 or rows < 1 or cols * rows != self.layout_count:
                    cols, rows = self._default_grid_shape(self.layout_count)
                self.grid_columns, self.grid_rows = cols, rows
                self.cell_layout = None
            self._render_video_grid()
            cameras = [m for m in tab.mapping if m["kind"] == views.KIND_CAMERA and m["slot"] < self.layout_count]
            for entry in tab.mapping:
                if entry["kind"] != views.KIND_CAMERA and entry["slot"] < self.layout_count:
                    self._place_resource(entry["slot"], entry)
        finally:
            self._applying_tab = False
        self._render_view_tabs()
        if cameras:
            self._load_view_cameras(cameras, 0)
        self._update_door_bar()

    def _mark_view_dirty(self):
        if getattr(self, "_applying_tab", True) or not hasattr(self, "view_tabs"):
            return
        tab = self.view_tabs.current()
        if tab and not tab.dirty:
            tab.dirty = True
            self._render_view_tabs()

    def save_view_tab(self, save_as=False):
        tab = self.view_tabs.current()
        if tab is None:
            return
        self._capture_current_tab()
        if tab.view_id and not save_as:
            views.save_view(tab.name, tab, view_id=tab.view_id)
            tab.dirty = False
            self._render_view_tabs()
            self._fill_custom_view_list()
            self.notify(f"Вид «{tab.name}» сохранён")
            return
        groups = views.load_view_groups()

        def store(name, group_index):
            group_id = groups[group_index]["id"] if 0 <= group_index < len(groups) else None
            try:
                view_id = views.save_view(name, tab, group_id)
            except ValueError as exc:
                self.notify(str(exc), error=True)
                return
            except Exception:
                self.notify("Вид с таким названием уже существует", error=True)
                return
            tab.view_id, tab.name, tab.dirty = view_id, name, False
            self._render_view_tabs()
            self._fill_custom_view_list()
            self.notify(f"Вид «{name}» сохранён")

        self._ask_name_and_group("Сохранить вид", tab.name if save_as else tab.name, groups, store)

    def _ask_name_and_group(self, title, initial, groups, callback):
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("OK", Gtk.ResponseType.ACCEPT)
        form = Gtk.Grid(column_spacing=10, row_spacing=10)
        W.margins(form, 16)
        entry = Gtk.Entry(text=initial)
        group = Gtk.DropDown.new_from_strings(["— без группы —"] + [g["name"] for g in groups])
        self._field(form, "Название вида", entry, 0)
        self._field(form, "Группа видов", group, 1)
        dialog.get_content_area().append(form)

        def response(dlg, result):
            name = entry.get_text().strip()
            index = group.get_selected() - 1
            dlg.close()
            if result == Gtk.ResponseType.ACCEPT and name:
                callback(name, index)

        dialog.connect("response", response)
        dialog.present()

    def open_tab_in_window(self):
        self._capture_current_tab()
        tab = self.view_tabs.current()
        cameras = [m for m in tab.mapping if m["kind"] == views.KIND_CAMERA]
        if not cameras:
            self.notify("В виде нет камер для отдельного окна")
            return
        devices = {d["id"]: d for d in self.devices}
        capacity = max(tab.layout, max(m["slot"] for m in cameras) + 1)
        mapping = [None] * capacity
        for m in cameras:
            mapping[m["slot"]] = m["device_id"]
        name = tab.name
        with storage.database() as db:
            existing = {r[0] for r in db.execute("SELECT name FROM saved_windows")}
            suffix = 2
            while name in existing:
                name = f"{tab.name} ({suffix})"
                suffix += 1
            saved_id = db.execute("""INSERT INTO saved_windows(name,capacity,screen_index,mapping,use_substream,
                fullscreen) VALUES(?,?,?,?,1,0)""", (name, capacity, 0, json.dumps(mapping))).lastrowid
        from app import MonitorWindow  # отложенный импорт: app импортирует этот модуль
        self.monitor_sequence += 1
        window = MonitorWindow(self, self.monitor_sequence, name, 1, capacity, 0, saved_id)
        self.monitor_windows.append(window)
        window.present()
        window.start_assignments([(m["slot"], devices[m["device_id"]]) for m in cameras
                                  if m["device_id"] in devices], {}, True)

    # ------------------------------------------------------------------ дерево «Вид»
    def _fill_custom_view_list(self):
        if not hasattr(self, "custom_view_list"):
            return
        W.clear(self.custom_view_list)
        groups = views.load_view_groups()
        saved = views.load_views()
        if not saved and not groups:
            self.custom_view_list.append(W.label("Нет сохранённых видов", "muted"))
        for group, items in views.view_tree(groups, saved):
            target = self.custom_view_list
            if group is not None:
                expander = Gtk.Expander(label=f"▣ {group['name']}  ({len(items)})")
                expander.set_expanded(True)
                inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
                inner.set_margin_start(12)
                expander.set_child(inner)
                W.on_right_click(expander, lambda w, x, y, g=group: W.popover_menu(w, [
                    ("Удалить группу видов", lambda: (views.delete_view_group(g["id"]),
                                                      self._fill_custom_view_list()))], x, y))
                self.custom_view_list.append(expander)
                target = inner
            for view in items:
                row = W.button("▦ " + view["name"], lambda v=view: self.load_custom_view(v), "tree-row")
                W.on_right_click(row, lambda w, x, y, v=view: self._view_menu(v, w, x, y))
                target.append(row)

    def _view_menu(self, view, widget, x, y):
        groups = views.load_view_groups()
        items = [("Открыть", lambda: self.load_custom_view(view)),
                 ("Переименовать", lambda: self._ask_text("Название вида", view["name"],
                                                          lambda n: (views.rename_view(view["id"], n),
                                                                     self._fill_custom_view_list())))]
        for group in groups:
            items.append((f"Переместить в «{group['name']}»",
                          lambda g=group: (views.move_view(view["id"], g["id"]), self._fill_custom_view_list())))
        items += [None, ("Удалить вид", lambda: self.delete_custom_view(view["id"]))]
        W.popover_menu(widget, items, x, y)

    def load_custom_view(self, view):
        self._capture_current_tab()
        tab, _created = self.view_tabs.open_saved(view)
        self._apply_tab(tab)

    def delete_custom_view(self, view_id):
        views.delete_view(view_id)
        tab = self.view_tabs.find_view(view_id)
        if tab:
            tab.view_id = None
            tab.dirty = True
            self._render_view_tabs()
        self._fill_custom_view_list()

    def save_custom_view(self):
        self.save_view_tab(save_as=True)

    def add_view_group_dialog(self):
        self._ask_text("Новая группа видов", "", lambda name: (views.add_view_group(name),
                                                               self._fill_custom_view_list()))

    # ================================================================== ресурсы
    def _fill_resource_list(self):
        if not hasattr(self, "resource_list"):
            return
        W.clear(self.resource_list)
        query = self.camera_search.get_text().strip().casefold()
        devices = [d for d in self.devices
                   if (not self.hide_offline or d.get("status") != "offline")
                   and (query in d["name"].casefold() or query in d["host"].casefold()
                        or query in (d.get("host_name") or "").casefold())]
        if not devices:
            self.resource_list.append(W.label("Нет ресурсов. Добавьте устройство: Система → Устройства.",
                                              "muted", wrap=True))
            return
        groups = getattr(self, "groups", [])
        rows = devmgmt.physical_devices(devices, groups)
        by_id = {d["id"]: d for d in devices}
        tree = devmgmt.group_tree(groups, rows)
        for group, members in tree:
            if not members:
                continue
            target = self.resource_list
            if group is not None or len(tree) > 1:
                group_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
                group_box.set_margin_start(10)
                expander = Gtk.Expander(label=f"◫ {group['name'] if group else 'Без области'}")
                expander.set_expanded(True)
                expander.set_child(group_box)
                self.resource_list.append(expander)
                target = group_box
            for member in members:
                channels = [by_id[i] for i in member["ids"]]
                first = channels[0]
                status = first.get("status", "")
                icon = "▣" if first.get("category") == "access" else "▤"
                dot = {"online": "●", "offline": "○"}.get(status, "◌")
                expander = Gtk.Expander(label=f"{icon} {member['name']}  {dot}")
                expander.set_expanded(bool(query) or len(members) <= 2)
                W.on_right_click(expander, lambda w, x, y, m=member, d=first: self._device_menu(m, d, w, x, y))
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
                box.set_margin_start(12)
                if first.get("category") == "access":
                    for door in range(1, max(1, int(first.get("door_count") or 1)) + 1):
                        box.append(self._door_row(first, door))
                else:
                    for device in channels:
                        box.append(self._camera_row(device))
                expander.set_child(box)
                target.append(expander)

    def _camera_row(self, device):
        row = Gtk.Button()
        row.add_css_class("tree-row")
        row.set_has_frame(False)
        status = device.get("status", "")
        mark = {"online": "status-online", "offline": "status-offline"}.get(status, "")
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = W.label("◉" if status != "offline" else "◎", mark)
        content.append(icon)
        name = W.label(device["name"])
        name.set_ellipsize(3)
        name.set_hexpand(True)
        content.append(name)
        row.set_child(content)
        row.set_tooltip_text(f"{device['host']} · канал {device['channel']}\n"
                             "Щелчок — в выбранное окно; перетащите в ячейку; правая кнопка — действия")
        row.connect("clicked", lambda _b, d=device: self.open_live_dialog(d, self.selected_slot))
        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.COPY)
        source.connect("prepare", lambda _s, _x, _y, did=device["id"]:
                       Gdk.ContentProvider.new_for_value(f"camera:{did}"))
        row.add_controller(source)
        W.on_right_click(row, lambda w, x, y, d=device: self._show_camera_menu(d, w, x, y))
        return row

    def _door_row(self, device, door):
        row = W.button(f"🚪 Дверь {door}", lambda: self._place_door(device, door), "tree-row")
        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.COPY)
        source.connect("prepare", lambda _s, _x, _y: Gdk.ContentProvider.new_for_value(
            f"door:{device['id']}:{door}"))
        row.add_controller(source)
        W.on_right_click(row, lambda w, x, y: W.popover_menu(w, [
            (people.DOOR_TITLES[c], lambda c=c: self.door_command(device, door, c)) for c in people.DOOR_COMMANDS
        ] + [None, ("Показать в выбранном окне", lambda: self._place_door(device, door))], x, y))
        return row

    def _resource_root_menu(self):
        W.popover_menu(self.resource_section, [
            ("Показать офлайн-ресурсы" if self.hide_offline else "Скрыть офлайн-ресурсы", self._toggle_offline),
            ("Добавить устройство", lambda: self.open_system("devices", "add")),
            ("Создать область", lambda: self._ask_text("Новая область", "", lambda n: (
                storage.add_group(n), self.refresh()))),
            ("Проверить состояние устройств", self.check_all_devices),
            ("Перейти к управлению устройствами", lambda: self.open_system("devices"))])

    def _toggle_offline(self):
        self.hide_offline = not self.hide_offline
        self._fill_resource_list()

    def _device_menu(self, member, device, widget, x, y):
        W.popover_menu(widget, [
            ("Подробности", lambda: self.open_system("devices", f"details:{device['id']}")),
            ("Удалённая настройка (веб)", lambda: self.open_remote_configuration(device)),
            ("Настройка тревог", lambda: self.open_system("event")),
            ("Синхронизировать время", lambda: self.sync_device_time(device)),
            ("Журнал операций", lambda: self.open_report("operations")),
            ("Переименовать", lambda: self._ask_text("Название устройства", member["name"],
                                                     lambda n: self.rename_physical_device(member, n))),
            ("Проверить состояние", lambda: self.check_all_devices([device])),
            None,
            ("Удалить", lambda: self._confirm_delete_devices(None, member["ids"], f"устройство {member['name']}"))],
            x, y)

    def rename_physical_device(self, member, name):
        with storage.database() as db:
            db.executemany("UPDATE devices SET host_name=? WHERE id=?", [(name[:120], i) for i in member["ids"]])
            if member["channels"] == 1:
                db.execute("UPDATE devices SET name=? WHERE id=?", (name[:120], member["ids"][0]))
        self.refresh()

    def show_resource_legend(self):
        self.notify("Обозначения: ▤ устройство видео · ▣ контроллер доступа · ◉ камера · 🚪 дверь\n"
                    "● в сети · ○ не в сети · ◌ не проверено")

    # ------------------------------------------------------------------ карты, веб, файлы
    def _fill_left_maps(self):
        if not hasattr(self, "left_maps"):
            return
        W.clear(self.left_maps)
        with storage.database() as db:
            maps = [dict(r) for r in db.execute("SELECT * FROM maps ORDER BY name")]
        if not maps:
            self.left_maps.append(W.label("Карт нет — нажмите ＋", "muted"))
        for item in maps:
            self.left_maps.append(W.button("⌖ " + item["name"], lambda m=item: self.open_map(m["id"]), "tree-row"))

    def open_map(self, map_id):
        self._show_module("emap")
        for index, item in enumerate(getattr(self, "maps", [])):
            if item["id"] == map_id:
                self.map_choice.set_selected(index)
                break

    def _fill_web_pages(self):
        if not hasattr(self, "left_web"):
            return
        W.clear(self.left_web)
        pages = views.load_web_pages()
        if not pages:
            self.left_web.append(W.label("Страниц нет — нажмите ＋", "muted"))
        for page in pages:
            entry = {"kind": views.KIND_WEB, "url": page["url"], "name": page["name"]}
            row = W.button("◍ " + page["name"], lambda e=entry: self._place_resource(self.selected_slot, e) or
                           self._mark_view_dirty(), "tree-row")
            row.set_tooltip_text(page["url"] + "\nЩелчок — в выбранное окно; можно перетащить в ячейку")
            source = Gtk.DragSource()
            source.set_actions(Gdk.DragAction.COPY)
            source.connect("prepare", lambda _s, _x, _y, pid=page["id"]:
                           Gdk.ContentProvider.new_for_value(f"web:{pid}"))
            row.add_controller(source)
            W.on_right_click(row, lambda w, x, y, p=page: W.popover_menu(w, [
                ("Открыть в браузере", lambda: self._open_uri(p["url"])),
                ("Удалить", lambda: (views.delete_web_page(p["id"]), self._fill_web_pages()))], x, y))
            self.left_web.append(row)

    def add_web_page_dialog(self):
        dialog = Gtk.Dialog(title="Добавить веб-страницу", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Добавить", Gtk.ResponseType.ACCEPT)
        form = Gtk.Grid(column_spacing=10, row_spacing=10)
        W.margins(form, 16)
        name = Gtk.Entry(placeholder_text="Например, Погода")
        url = Gtk.Entry(placeholder_text="https://…")
        self._field(form, "Название", name, 0)
        self._field(form, "Адрес", url, 1)
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    views.add_web_page(name.get_text(), url.get_text())
                except ValueError as exc:
                    self.notify(str(exc), error=True)
                    return
                self._fill_web_pages()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def _open_uri(self, uri):
        try:
            Gtk.show_uri(self, uri, Gdk.CURRENT_TIME)
        except Exception as exc:
            self.notify(f"Не удалось открыть {uri}: {exc}", error=True)

    def _local_files_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(page, 14)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.local_kind = Gtk.DropDown.new_from_strings(["Снимки", "Видеозаписи и экспорт", "Резервное хранилище"])
        self.local_kind.connect("notify::selected", lambda *_: self._render_local_files())
        bar.append(self.local_kind)
        bar.append(W.button("Открыть папку", lambda: self._open_uri(self._local_folder().as_uri())))
        bar.append(W.button("Обновить", self._render_local_files))
        page.append(bar)
        self.local_table = W.Table(("Файл", "Размер", "Изменён", ""), selectable=False)
        page.append(self.local_table)
        return page

    def _local_folder(self):
        kind = self.local_kind.get_selected() if hasattr(self, "local_kind") else 0
        return [self.capture_folder, self.record_folder, recording.storage_root()][min(kind, 2)]

    def open_local_files(self, kind):
        self._show_module("local_files")
        self.local_kind.set_selected({"captures": 0, "records": 1, "storage": 2}[kind])
        self._render_local_files()

    def _render_local_files(self):
        folder = self._local_folder()
        files = []
        if folder.is_dir():
            pattern = "**/*" if folder == recording.storage_root() else "*"
            for path in folder.glob(pattern):
                if path.is_file():
                    try:
                        files.append((path.stat().st_mtime, path))
                    except OSError:
                        pass
        files.sort(reverse=True)
        rows = []
        for mtime, path in files[:300]:
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            actions.append(W.button("Открыть", lambda p=path: self._open_uri(p.as_uri()), flat=True))
            actions.append(W.button("Удалить", lambda p=path: self._confirm(
                f"Удалить файл {p.name}?", lambda: (p.unlink(missing_ok=True), self._render_local_files())),
                flat=True))
            rows.append((str(path.relative_to(folder)), recording.human_bytes(path.stat().st_size),
                         datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"), actions))
        self.local_table.set_rows(rows)

    def _confirm(self, text, callback, accept="Удалить"):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                                   buttons=Gtk.ButtonsType.CANCEL, text=text)
        dialog.add_button(accept, Gtk.ResponseType.ACCEPT)
        dialog.connect("response", lambda dlg, r: (dlg.close(), callback() if r == Gtk.ResponseType.ACCEPT else None))
        dialog.present()

    # ------------------------------------------------------------------ ярлыки
    def _render_shortcuts(self):
        W.clear(self.shortcut_box)
        access_enabled = self.capabilities().get("access", True)
        for key in views.load_shortcuts():
            item = views.SHORTCUT_KEYS[key]
            if item.section == "Контроль доступа" and not access_enabled:
                continue
            self.shortcut_box.append(W.button(f"{item.icon}   {item.title}", lambda k=key: self.run_shortcut(k),
                                              "shortcut"))

    def edit_shortcuts_dialog(self):
        current = set(views.load_shortcuts())
        dialog = Gtk.Dialog(title="Ярлыки", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("OK", Gtk.ResponseType.ACCEPT)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        W.margins(box, 16)
        checks = {}
        section = None
        for item in views.SHORTCUTS:
            if item.section != section:
                section = item.section
                box.append(W.label(section, "muted"))
            check = Gtk.CheckButton(label=f"{item.icon}  {item.title}")
            check.set_active(item.key in current)
            checks[item.key] = check
            box.append(check)
        dialog.get_content_area().append(box)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                views.save_shortcuts([k for k, c in checks.items() if c.get_active()])
                self._render_shortcuts()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def run_shortcut(self, key):
        actions = {
            "add_device": lambda: self.open_system("devices", "add"),
            "add_person": lambda: self.open_system("person", "add"),
            "import_persons": lambda: self.open_system("person", "import"),
            "manage_vehicles": lambda: self.open_system("vehicle"),
            "event_config": lambda: self.open_system("event"),
            "add_user": lambda: self.open_system("accounts", "add"),
            "system_storage": lambda: self.open_system("config", "storage"),
            "authorize_access": lambda: self.open_system("access"),
            "quick_lock": self.quick_lock_dialog,
            "access_records": lambda: self._show_module("access"),
        }
        actions[key]()

    # ================================================================== ресурсы в ячейках
    def _place_resource(self, slot, entry):
        if slot >= len(self.video_slots):
            return
        if slot in self.streams:
            self._stop_stream(slot)
        self._clear_resource(slot)
        entry = dict(entry)
        entry.pop("slot", None)
        kind = entry.get("kind")
        if kind == views.KIND_WEB:
            widget = self._web_tile(entry)
        elif kind == views.KIND_DOOR:
            widget = self._door_tile(entry)
        elif kind == views.KIND_MAP:
            widget = self._map_tile(entry)
        else:
            return
        holder = self.video_slots[slot]
        holder["placeholder"].set_visible(False)
        holder["pane"].add_overlay(widget)
        holder["resource_widget"] = widget
        self.slot_resources[slot] = entry
        self._update_door_bar()

    def _clear_resource(self, slot):
        self.slot_resources.pop(slot, None)
        if slot < len(self.video_slots):
            holder = self.video_slots[slot]
            widget = holder.pop("resource_widget", None)
            if widget is not None:
                holder["pane"].remove_overlay(widget)
            if slot not in self.streams:
                holder["placeholder"].set_visible(True)
        self._update_door_bar()

    def _web_tile(self, entry):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_hexpand(True)
        box.set_vexpand(True)
        view = None
        try:
            import gi
            gi.require_version("WebKit", "6.0")
            from gi.repository import WebKit
            view = WebKit.WebView()
            view.load_uri(entry["url"])
            view.set_vexpand(True)
            view.set_hexpand(True)
        except (ImportError, ValueError, AttributeError):
            view = None
        if view is not None:
            box.append(view)
        else:
            box.set_valign(Gtk.Align.CENTER)
            box.append(W.label("◍ " + entry.get("name", ""), "section-title", 0.5))
            box.append(W.label(entry["url"], "muted", 0.5))
            hint = W.label("Встроенный просмотр требует WebKitGTK 6 (gir1.2-webkit-6.0).", "muted", 0.5, True)
            box.append(hint)
            open_button = W.button("Открыть в браузере", lambda: self._open_uri(entry["url"]))
            open_button.set_halign(Gtk.Align.CENTER)
            box.append(open_button)
        return box

    def _door_tile(self, entry):
        device = next((d for d in self.devices if d["id"] == entry["device_id"]), None)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        name = f"{(device or {}).get('host_name') or (device or {}).get('name', 'Контроллер')} · Дверь {entry['door']}"
        box.append(W.label("🚪", "page-title", 0.5))
        box.append(W.label(name, "section-title", 0.5))
        state = self.door_states.get((entry["device_id"], entry["door"]), "Неизвестно")
        status = W.label(f"Состояние: {state}", "muted", 0.5)
        box.append(status)
        actions = Gtk.FlowBox()
        actions.set_selection_mode(Gtk.SelectionMode.NONE)
        actions.set_max_children_per_line(4)
        if device:
            for command in ("open", "close", "always_open", "always_close"):
                actions.append(W.button(people.DOOR_TITLES[command],
                                        lambda c=command: self.door_command(device, entry["door"], c)))
        box.append(actions)
        return box

    def _map_tile(self, entry):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_valign(Gtk.Align.CENTER)
        box.append(W.label("⌖ Карта", "section-title", 0.5))
        box.append(W.button("Открыть", lambda: self.open_map(entry["map_id"])))
        return box

    def _place_door(self, device, door):
        self._place_resource(self.selected_slot, {"kind": views.KIND_DOOR, "device_id": device["id"], "door": door})
        self._mark_view_dirty()
        self.refresh_door_states([device])

    def _update_door_bar(self):
        if not hasattr(self, "door_bar"):
            return
        doors = [(e["device_id"], e["door"]) for e in self.slot_resources.values() if e["kind"] == views.KIND_DOOR]
        self.door_bar.set_visible(bool(doors))
        if doors:
            counters = people.door_counters({i: self.door_states.get(d, "Неизвестно") for i, d in enumerate(doors)})
            self.door_bar_label.set_text(
                f"🚪 Открыто: {counters['Открыта']}    Закрыто: {counters['Закрыта']}    "
                f"Неизвестно: {counters['Неизвестно']}")

    # ================================================================== нижняя полоса
    def set_bar_mode(self, mode):
        if mode == self.bar_mode:
            return
        self.bar_mode = mode
        self.sync_playback.set_visible(mode == "playback")
        self._update_bar_controls()
        if mode == "live":
            for slot, state in list(self.streams.items()):
                if state.get("playback"):
                    device, credentials = state["base_device"], state["credentials"]
                    try:
                        self._start_preview(slot, device, credentials)
                    except Exception as exc:
                        self.notify(str(exc), error=True)
            self.timeline.model.cursor = datetime.now().astimezone()
            self.timeline.model.center = self.timeline.model.cursor
        else:
            if not self.session.can("playback"):
                self.notify("Нет прав на воспроизведение", error=True)
                self.live_mode_button.set_active(True)
                return
            moment = datetime.now().astimezone() - timedelta(minutes=5)
            self.timeline.model.cursor = moment
            if self._playback_targets():
                self.timeline_seek(moment)
        self._load_timeline_segments()
        self.timeline.queue_draw()

    def _update_bar_controls(self):
        playback = self.bar_mode == "playback"
        for widget in (self.bar_stop_button, self.bar_play_button, self.bar_speed_label):
            widget.set_sensitive(playback)
        if self.live_mode_button.get_active() == playback:
            (self.playback_mode_button if playback else self.live_mode_button).set_active(True)

    def toggle_bar_collapsed(self):
        self.bar_collapsed = not self.bar_collapsed
        self.timeline_area.set_visible(not self.bar_collapsed)
        self.bar_collapse_button.set_label("⌃" if self.bar_collapsed else "⌄")

    def _set_timeline_filter(self, key):
        self.timeline.model.filter = key
        self.timeline.queue_draw()

    def zoom_timeline(self, direction):
        self.timeline.model.zoom(direction, self.timeline.model.cursor)
        self.zoom_label.set_text(tl.ZOOM_TITLES[self.timeline.model.span])
        self.timeline.queue_draw()
        self._load_timeline_segments()

    def _goto_picked_date(self):
        date = self.bar_calendar.get_date()
        try:
            hours, minutes, seconds = (int(p) for p in self.bar_time_entry.get_text().split(":"))
            moment = datetime(date.get_year(), date.get_month(), date.get_day_of_month(),
                              hours, minutes, seconds).astimezone()
        except (ValueError, TypeError) as exc:
            self.notify(f"Неверное время: {exc}", error=True)
            return
        self.timeline.model.center = moment
        self.timeline.model.cursor = moment
        if self.bar_mode != "playback":
            self.playback_mode_button.set_active(True)
        self.timeline_seek(moment)

    def _playback_targets(self):
        """[(slot, device, credentials)] для воспроизведения: выбранное окно или все при синхронном режиме."""
        slots = range(self.layout_count) if self.sync_playback.get_active() else [self.selected_slot]
        targets = []
        tab = self.view_tabs.current() if hasattr(self, "view_tabs") else None
        planned = {m["slot"]: m["device_id"] for m in (tab.mapping if tab else []) if m["kind"] == views.KIND_CAMERA}
        for slot in slots:
            state = self.streams.get(slot)
            device = state["base_device"] if state else next(
                (d for d in self.devices if d["id"] == planned.get(slot)), None)
            if device is None:
                continue
            credentials = state["credentials"] if state else self._get_device_credentials(device)
            if credentials:
                targets.append((slot, device, dict(credentials)))
        return targets

    def timeline_seek(self, moment):
        if self.bar_mode != "playback":
            self.bar_mode = "playback"
            self.sync_playback.set_visible(True)
            self.playback_mode_button.set_active(True)
            self._update_bar_controls()
        targets = self._playback_targets()
        if not targets:
            self.notify("Выберите окно с камерой для воспроизведения")
            return
        moment = moment.astimezone()
        self.timeline.model.cursor = moment
        for slot, device, credentials in targets:
            try:
                self._play_slot_at(slot, device, credentials, moment)
            except Exception as exc:
                self.notify(f"{device['name']}: {exc}", error=True)
        self.bar_play_button.set_label("Ⅱ")
        self.timeline.queue_draw()

    def _play_slot_at(self, slot, device, credentials, moment):
        _start, day_end = tl.day_bounds(moment)
        end = max(day_end, moment + timedelta(minutes=10))
        local_path, offset = (None, 0.0)
        model = self.timeline.model
        segment = model.segment_at(moment) if self._timeline_device() is device or (
            self._timeline_device() or {}).get("id") == device["id"] else None
        prefer_local = segment is not None and segment.kind == "local" and not any(
            s.kind == "continuous" and s.contains(moment) for s in model.segments)
        if device.get("protocol") != "hikvision" or prefer_local:
            local_path, offset = recording.segment_for(recording.storage_root(), device["id"], moment)
            if local_path is None:
                raise ValueError("Для этой камеры нет записи на выбранное время")
        if local_path is not None:
            self._start_preview(slot, device, credentials, direct_uri=local_path.as_uri(), stream_override="main")
            state = self.streams[slot]
            if offset > 1:
                GLib.timeout_add(700, self._seek_local, state, offset)
        else:
            interval = (moment.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"))
            self._start_preview(slot, device, credentials, interval, stream_override="main")
            state = self.streams[slot]
        state.update({"archive_start": moment, "archive_end": end, "archive_elapsed": 0.0,
                      "archive_speed": 1.0, "archive_clock": time.monotonic(),
                      "archive_paused": False, "local_file": local_path})
        self.bar_speed_label.set_text("1x")

    def _seek_local(self, state, offset):
        try:
            from gi.repository import Gst
            state["pipeline"].seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                                          int(offset * Gst.SECOND))
        except Exception:
            pass
        return False

    def _slot_position(self, state):
        elapsed = state.get("archive_elapsed", 0.0)
        if not state.get("archive_paused"):
            elapsed += (time.monotonic() - state.get("archive_clock", 0)) * state.get("archive_speed", 1.0)
        return state["archive_start"] + timedelta(seconds=max(0.0, elapsed))

    def _active_playback_slots(self):
        if self.sync_playback.get_active():
            return [s for s, st in self.streams.items() if st.get("playback") and st.get("archive_start")]
        state = self.streams.get(self.selected_slot)
        return [self.selected_slot] if state and state.get("playback") and state.get("archive_start") else []

    def bar_play_pause(self):
        from gi.repository import Gst
        slots = self._active_playback_slots()
        if not slots:
            self.timeline_seek(self.timeline.model.cursor)
            return
        pause = not self.streams[slots[0]].get("archive_paused")
        now = time.monotonic()
        for slot in slots:
            state = self.streams[slot]
            if pause and not state.get("archive_paused"):
                state["archive_elapsed"] += (now - state["archive_clock"]) * state.get("archive_speed", 1.0)
                state["pipeline"].set_state(Gst.State.PAUSED)
                state["archive_paused"] = True
            elif not pause and state.get("archive_paused"):
                state["pipeline"].set_state(Gst.State.PLAYING)
                state["archive_clock"] = now
                state["archive_paused"] = False
        self.bar_play_button.set_label("▶" if pause else "Ⅱ")

    def bar_jump(self, seconds):
        slots = self._active_playback_slots()
        if not slots:
            return
        moment = self._slot_position(self.streams[slots[0]]) + timedelta(seconds=seconds)
        self.timeline_seek(moment)

    def bar_step(self):
        from gi.repository import Gst
        for slot in self._active_playback_slots():
            state = self.streams[slot]
            if not state.get("archive_paused"):
                self.bar_play_pause()
            state["pipeline"].send_event(Gst.Event.new_step(Gst.Format.BUFFERS, 1, 1.0, True, False))
            state["archive_elapsed"] += 0.04

    def bar_speed(self, direction):
        from gi.repository import Gst
        slots = self._active_playback_slots()
        if not slots:
            return
        current = self.streams[slots[0]].get("archive_speed", 1.0)
        index = SPEEDS.index(current) if current in SPEEDS else 2
        rate = SPEEDS[max(0, min(len(SPEEDS) - 1, index + direction))]
        if rate == current:
            return
        now = time.monotonic()
        for slot in slots:
            state = self.streams[slot]
            try:
                ok = state["pipeline"].seek(rate, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                            Gst.SeekType.NONE, 0, Gst.SeekType.NONE, 0)
            except Exception:
                ok = False
            if not ok:
                self.notify("Устройство не поддерживает изменение скорости", error=True)
                return
            if not state.get("archive_paused"):
                state["archive_elapsed"] += (now - state["archive_clock"]) * state.get("archive_speed", 1.0)
                state["archive_clock"] = now
            state["archive_speed"] = rate
        self.bar_speed_label.set_text(f"{rate:g}x")

    def stop_playback_mode(self):
        self.live_mode_button.set_active(True)

    def _timeline_tick(self):
        if getattr(self, "_closing", False):
            return False
        model = self.timeline.model
        state = self.streams.get(self.selected_slot)
        if state and state.get("playback") and state.get("archive_start"):
            model.cursor = self._slot_position(state)
        elif self.bar_mode == "live":
            model.cursor = datetime.now().astimezone().replace(microsecond=0)
        if not (model.start < model.cursor < model.end) and self.timeline._drag_center is None:
            model.center = model.cursor
        self.bar_date_button.set_label(tl.format_position(model.cursor) + " ⌄")
        self.timeline.queue_draw()
        return True

    # ------------------------------------------------------------------ записи для шкалы
    def _timeline_device(self):
        state = self.streams.get(self.selected_slot)
        if state:
            return state["base_device"]
        tab = self.view_tabs.current() if hasattr(self, "view_tabs") else None
        planned = {m["slot"]: m["device_id"] for m in (tab.mapping if tab else []) if m["kind"] == views.KIND_CAMERA}
        return next((d for d in self.devices if d["id"] == planned.get(self.selected_slot)), None)

    def _load_timeline_segments(self):
        if not hasattr(self, "timeline"):
            return
        device = self._timeline_device()
        model = self.timeline.model
        if device is None:
            model.segments = []
            self.timeline.set_label("")
            self.timeline.queue_draw()
            return
        self.timeline.set_label(device["name"])
        day_start, day_end = tl.day_bounds(model.center)
        key = (device["id"], day_start.date().isoformat())
        cached = self.timeline_cache.get(key)
        if cached and time.monotonic() - cached[0] < 60:
            model.segments = cached[1]
            self.timeline.queue_draw()
            return
        token = object()
        self.timeline_token = token
        credentials = None
        try:
            credentials = self._get_device_credentials(device)
        except Exception:
            credentials = None
        threading.Thread(target=self._timeline_worker, args=(device, credentials, day_start, day_end, key, token),
                         daemon=True).start()

    def _timeline_worker(self, device, credentials, day_start, day_end, key, token):
        segments = []
        if device.get("protocol") == "hikvision" and credentials:
            try:
                for start, end, uri in self.isapi_search_recordings(device, credentials, day_start, day_end):
                    segments.append(tl.Segment(start, end, "continuous", uri))
            except Exception:
                pass
        for began, finish, path in recording.list_segments(recording.storage_root(), device["id"], day_start, day_end):
            segments.append(tl.Segment(began, finish, "local", path))
        try:
            for event in alarms.load_events(limit=2000, since=day_start.isoformat(timespec="seconds"),
                                            until=day_end.isoformat(timespec="seconds")):
                if event["device_id"] != device["id"] and event["device_name"] != device.get("host_name"):
                    continue
                moment = datetime.fromisoformat(event["time"].replace("Z", "+00:00")).astimezone()
                segments.append(tl.Segment(moment - timedelta(seconds=5), moment + timedelta(seconds=10),
                                           event["category"] if event["category"] in ("person", "vehicle") else "event"))
        except Exception:
            pass
        merged = tl.merge_segments(segments)
        GLib.idle_add(self._timeline_loaded, key, merged, token)

    def _timeline_loaded(self, key, segments, token):
        self.timeline_cache[key] = (time.monotonic(), segments)
        if token is self.timeline_token:
            self.timeline.model.segments = segments
            self.timeline.queue_draw()
        return False

    # ================================================================== правая панель
    def _set_event_category(self, key):
        self.event_category = key
        self._refresh_event_panel()

    def _refresh_alarm_panel(self):
        if not hasattr(self, "alarm_list"):
            return
        W.clear(self.alarm_list)
        priority = [None, "high", "medium", "low"][min(3, self.alarm_priority_filter.get_selected())]
        status = "unacknowledged" if self.alarm_status_filter.get_selected() == 1 else None
        records = alarms.load_alarm_records(100, status=status, priority=priority)
        if not records:
            empty = W.label("⚐\nНет тревог", "empty-state", 0.5)
            empty.set_justify(Gtk.Justification.CENTER)
            self.alarm_list.append(empty)
        for record in records:
            row = Gtk.Button()
            row.add_css_class("alarm-row")
            row.set_has_frame(False)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            head.append(W.label("●", f"prio-{record['priority']}"))
            title = W.label(record["rule_name"] or alarms.event_title(record["event_type"]), "section-title")
            title.set_ellipsize(3)
            title.set_hexpand(True)
            head.append(title)
            if record["status"] == "unacknowledged":
                head.append(W.label("new", "badge"))
            content.append(head)
            content.append(W.label(record["device_name"] or "—", "muted"))
            content.append(W.label(record["time"][:19].replace("T", " "), "muted"))
            row.set_child(content)
            row.connect("clicked", lambda _b, r=record: self.show_alarm_details(r["id"]))
            self.alarm_list.append(row)
        open_count = alarms.unacknowledged_count()
        self.alarm_tab.set_label(f"⚑ Тревога ({open_count})" if open_count else "⚑ Тревога")

    def _refresh_event_panel(self):
        if not hasattr(self, "event_list"):
            return
        W.clear(self.event_list)
        events = alarms.load_events(100, category=self.event_category)
        if not events:
            self.event_list.append(W.label("Нет событий", "empty-state", 0.5))
        icons = {"event": "▣", "person": "☺", "vehicle": "⛟"}
        for event in events:
            try:
                details = json.loads(event["details"])
            except json.JSONDecodeError:
                details = {}
            row = Gtk.Button()
            row.add_css_class("alarm-row")
            row.set_has_frame(False)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            line = f"{icons.get(event['category'], '▣')} {alarms.event_title(event['event_type'])}"
            if details.get("licensePlate"):
                line += f" · {details['licensePlate']}"
            box.append(W.label(line, "section-title"))
            box.append(W.label(f"{event['device_name']} · CH {event['channel']}", "muted"))
            box.append(W.label(event["time"][:19].replace("T", " "), "muted"))
            row.set_child(box)
            item = {"device_id": event["device_id"], "time": event["time"], "device": event["device_name"],
                    "channel": event["channel"]}
            row.connect("clicked", lambda _b, e=item: self._open_alarm_video(e))
            self.event_list.append(row)

    def batch_acknowledge(self):
        records = alarms.load_alarm_records(1000, status="unacknowledged")
        if not records:
            self.notify("Нет неподтверждённых тревог")
            return
        self._confirm(f"Подтвердить все тревоги ({len(records)})?", lambda: (
            alarms.acknowledge([r["id"] for r in records], self.session.username),
            self._refresh_alarm_panel(), self._update_status_bar()), "Подтвердить")

    def trigger_user_event(self):
        event = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "device_id": None,
                 "device_name": f"Оператор {self.session.username}", "channel": "", "raw_type": "user_defined",
                 "event_type": "user", "category": "event", "state": "active", "plate": "", "details": {}}
        self.handle_event(event)
        self.notify("Пользовательское событие отправлено в обработку тревог")

    # ------------------------------------------------------------------ окно «Подробности тревоги»
    def show_alarm_details(self, record_id):
        record = alarms.get_alarm_record(record_id)
        if not record:
            return
        if self.alarm_details_window is not None:
            self.alarm_details_window.destroy()
        window = Gtk.Window(title="Подробности тревоги", transient_for=self)
        window.set_default_size(*self._initial_window_size(860, 560))
        self.alarm_details_window = window
        try:
            details = json.loads(record["details"])
        except json.JSONDecodeError:
            details = {}
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        W.margins(body, 14)
        info = Gtk.Grid(column_spacing=10, row_spacing=7)
        info.attach(W.label("⚑ " + (record["rule_name"] or alarms.event_title(record["event_type"])),
                            "section-title"), 0, 0, 2, 1)
        rows = [("Тип события", alarms.event_title(record["event_type"])),
                ("Источник", record["device_name"] or "—"), ("Канал", record["channel"] or "—"),
                ("Время", record["time"][:19].replace("T", " "))]
        if details.get("licensePlate"):
            rows.append(("Номер ТС", details["licensePlate"]))
        if details.get("vehicleList"):
            rows.append(("Список ТС", details["vehicleList"]))
        for key in ("vehicleType", "color", "employeeNoString", "cardNo"):
            if details.get(key):
                rows.append(({"vehicleType": "Тип ТС", "color": "Цвет", "employeeNoString": "ID персоны",
                              "cardNo": "Карта"}[key], details[key]))
        rows.append(("Статус", alarms.STATUS_TITLES.get(record["status"], record["status"])))
        if record["acknowledged_by"]:
            rows.append(("Подтвердил", f"{record['acknowledged_by']} · {record['acknowledged_at'][:16]}"))
        for index, (title, value) in enumerate(rows, start=1):
            info.attach(W.label(title, "muted"), 0, index, 1, 1)
            info.attach(W.label(str(value)), 1, index, 1, 1)
        base = len(rows) + 1
        priority = Gtk.DropDown.new_from_strings(list(alarms.PRIORITIES.values()))
        priority.set_selected(list(alarms.PRIORITIES).index(record["priority"])
                              if record["priority"] in alarms.PRIORITIES else 1)
        category = Gtk.DropDown.new_from_strings([c or "—" for c in alarms.CATEGORIES])
        category.set_selected(alarms.CATEGORIES.index(record["category"]) if record["category"] in alarms.CATEGORIES else 0)
        remarks = Gtk.TextView()
        remarks.get_buffer().set_text(record["remarks"] or "")
        remarks.set_size_request(240, 70)
        info.attach(W.label("Приоритет", "muted"), 0, base, 1, 1)
        info.attach(priority, 1, base, 1, 1)
        info.attach(W.label("Категория", "muted"), 0, base + 1, 1, 1)
        info.attach(category, 1, base + 1, 1, 1)
        info.attach(W.label("Примечание", "muted"), 0, base + 2, 1, 1)
        info.attach(remarks, 1, base + 2, 1, 1)
        body.append(info)

        notebook = Gtk.Notebook()
        notebook.set_hexpand(True)
        notebook.set_vexpand(True)
        picture_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        if record["picture_path"] and Path(record["picture_path"]).is_file():
            picture = Gtk.Picture.new_for_filename(record["picture_path"])
            W.fit_contain(picture)
            picture.set_vexpand(True)
            picture_page.append(picture)
        else:
            picture_page.append(W.label("Нет снимка. Включите «Снимок» в действиях правила тревоги.",
                                        "empty-state", 0.5, True))
        notebook.append_page(picture_page, W.label("Изображение"))
        video_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        video_page.set_valign(Gtk.Align.CENTER)
        event_item = {"device_id": record["device_id"], "time": record["time"], "device": record["device_name"],
                      "channel": record["channel"]}
        video_page.append(W.label("Видео ±30 секунд вокруг тревоги откроется в выбранном окне просмотра.",
                                  "muted", 0.5, True))
        play = W.button("▶ Воспроизвести", lambda: (self._open_alarm_video(event_item)), "red-button")
        play.set_halign(Gtk.Align.CENTER)
        video_page.append(play)
        notebook.append_page(video_page, W.label("Видео"))
        log_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for entry in alarms.load_operation_log(50, target=str(record_id)):
            log_page.append(W.label(f"{entry['time'][:19].replace('T', ' ')} · {entry['username']} · {entry['action']}",
                                    "muted"))
        if record["acknowledged_by"]:
            log_page.append(W.label(f"{record['acknowledged_at'][:19].replace('T', ' ')} · "
                                    f"{record['acknowledged_by']} · Подтверждение", "muted"))
        notebook.append_page(log_page, W.label("Журнал операций"))
        body.append(notebook)
        root.append(body)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        W.margins(footer, 10)

        def save_fields():
            buffer = remarks.get_buffer()
            text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
            alarms.update_alarm_record(record_id, priority=list(alarms.PRIORITIES)[priority.get_selected()],
                                       category=alarms.CATEGORIES[category.get_selected()], remarks=text)
            return text

        def acknowledge():
            text = save_fields()
            alarms.acknowledge([record_id], self.session.username, text)
            self._refresh_alarm_panel()
            self._update_status_bar()
            nxt = alarms.load_alarm_records(1, status="unacknowledged")
            window.destroy()
            self.alarm_details_window = None
            if nxt:
                self.show_alarm_details(nxt[0]["id"])

        footer.append(W.button("Подтвердить", acknowledge, "red-button"))
        footer.append(W.button("Тревожный выход…", lambda: self.alarm_output_dialog(record)))
        footer.append(W.button("Сохранить", lambda: (save_fields(), self.notify("Сохранено"))))
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        footer.append(spacer)
        popup = Gtk.CheckButton(label="Всплывающее окно при тревоге")
        popup.set_active(storage.get_setting("alarm_popup", "1") == "1")
        popup.connect("toggled", lambda b: storage.set_setting("alarm_popup", "1" if b.get_active() else "0"))
        footer.append(popup)
        sound = Gtk.CheckButton(label="Звук")
        sound.set_active(storage.get_setting("alarm_sound", "1") == "1")
        sound.connect("toggled", lambda b: storage.set_setting("alarm_sound", "1" if b.get_active() else "0"))
        footer.append(sound)
        open_records = alarms.load_alarm_records(200, status="unacknowledged")
        ids = [r["id"] for r in open_records]
        if record_id in ids and len(ids) > 1:
            position = ids.index(record_id)
            footer.append(W.button("‹", lambda: self.show_alarm_details(ids[max(0, position - 1)]), flat=True))
            footer.append(W.label(f"{position + 1}/{len(ids)}"))
            footer.append(W.button("›", lambda: self.show_alarm_details(ids[min(len(ids) - 1, position + 1)]),
                                   flat=True))
        root.append(footer)
        window.set_child(root)
        window.connect("close-request", lambda *_: setattr(self, "alarm_details_window", None) or False)
        window.present()

    def alarm_output_dialog(self, record):
        devices = [d for d in self.devices if d.get("protocol") == "hikvision"]
        hosts = {}
        for device in devices:
            hosts.setdefault((device["host"], device.get("http_port", 80)), device)
        choices = list(hosts.values())
        if not choices:
            self.notify("Нет устройств Hikvision с тревожными выходами")
            return
        dialog = Gtk.Dialog(title="Тревожный выход", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Включить", Gtk.ResponseType.ACCEPT)
        form = Gtk.Grid(column_spacing=10, row_spacing=10)
        W.margins(form, 16)
        device_choice = Gtk.DropDown.new_from_strings([f"{d.get('host_name') or d['host']}" for d in choices])
        preferred = next((i for i, d in enumerate(choices) if d["id"] == record.get("device_id")), 0)
        device_choice.set_selected(preferred)
        output = Gtk.SpinButton.new_with_range(1, 16, 1)
        seconds = Gtk.SpinButton.new_with_range(0, 600, 1)
        seconds.set_value(10)
        self._field(form, "Устройство", device_choice, 0)
        self._field(form, "Выход №", output, 1)
        self._field(form, "Автозакрытие, с (0 — вручную)", seconds, 2)
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                device = choices[device_choice.get_selected()]
                self.trigger_alarm_output(device["id"], output.get_value_as_int(), seconds.get_value_as_int())
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    # ================================================================== статистика и центр событий
    def _dashboard_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        W.margins(page, 14)
        metrics = Gtk.FlowBox()
        metrics.set_selection_mode(Gtk.SelectionMode.NONE)
        metrics.set_max_children_per_line(6)
        metrics.set_min_children_per_line(2)
        self.dashboard_device_count = Gtk.Label(label="0")
        self.dashboard_stream_count = Gtk.Label(label="0")
        self.dashboard_event_count = Gtk.Label(label="0")
        self.dashboard_map_count = Gtk.Label(label="0")
        self.dashboard_alarm_count = Gtk.Label(label="0")
        self.dashboard_offline_count = Gtk.Label(label="0")
        for title, widget, caption in (("УСТРОЙСТВА", self.dashboard_device_count, "в каталоге"),
                                       ("НЕ В СЕТИ", self.dashboard_offline_count, "по последней проверке"),
                                       ("ПОТОКИ", self.dashboard_stream_count, "активных сейчас"),
                                       ("ТРЕВОГИ", self.dashboard_alarm_count, "не подтверждены"),
                                       ("СОБЫТИЯ", self.dashboard_event_count, "за сегодня"),
                                       ("КАРТЫ", self.dashboard_map_count, "планов объекта")):
            metrics.append(self._stat(title, widget, caption))
        page.append(metrics)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.report_keys = list(reports.REPORTS)
        self.report_choice = Gtk.DropDown.new_from_strings(list(reports.REPORTS.values()))
        self.report_period = Gtk.DropDown.new_from_strings(list(reports.PERIODS.values()))
        self.report_period.set_selected(1)
        for widget in (self.report_choice, self.report_period):
            widget.connect("notify::selected", lambda *_: self._render_report())
            controls.append(widget)
        controls.append(W.button("Экспорт CSV", self.export_report))
        page.append(controls)
        self.report_table = W.Table(("—",), selectable=False)
        page.append(self.report_table)
        self.dashboard_event_list = Gtk.Box()
        self.dashboard_event_snapshot = None
        return page

    def open_report(self, key):
        self._show_module("dashboard")
        if key:
            self.report_choice.set_selected(self.report_keys.index(key))
        self._render_report()

    def _render_report(self):
        if not hasattr(self, "report_table"):
            return
        self._update_dashboard(update_maps=True)
        key = self.report_keys[min(self.report_choice.get_selected(), len(self.report_keys) - 1)]
        period = list(reports.PERIODS)[min(self.report_period.get_selected(), 2)]
        headers, rows = reports.build(key, period)
        self.report_table.columns = headers
        self.report_table.set_rows(rows)
        self.report_rows = (headers, rows)

    def export_report(self):
        headers, rows = getattr(self, "report_rows", ((), []))
        if not rows:
            self.notify("Отчёт пуст")
            return
        folder = self.record_folder.parent / "Reports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / datetime.now().strftime("report_%Y%m%d_%H%M%S.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            reports.export_csv(headers, rows, handle)
        self.notify(f"Отчёт сохранён: {path}")

    def _update_dashboard(self, update_maps=False):
        if not hasattr(self, "dashboard_device_count"):
            return
        summary = reports.summary()
        self.dashboard_device_count.set_text(str(summary["devices"]))
        self.dashboard_offline_count.set_text(str(summary["offline"]))
        self.dashboard_alarm_count.set_text(str(summary["open_alarms"]))
        self.dashboard_event_count.set_text(str(summary["events_today"]))
        streams = len(getattr(self, "streams", {})) + sum(len(w.streams) for w in getattr(self, "monitor_windows", []))
        self.dashboard_stream_count.set_text(str(streams))
        if update_maps:
            with storage.database() as db:
                self.dashboard_map_count.set_text(str(db.execute("SELECT COUNT(*) FROM maps").fetchone()[0]))

    def _alarms_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(page, 14)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.event_center_kind = Gtk.DropDown.new_from_strings(["Тревоги", "Все события", "Персоны", "Транспорт"])
        self.event_center_search = Gtk.SearchEntry(placeholder_text="Устройство, номер, тип…")
        self.event_center_search.set_hexpand(True)
        self.event_center_kind.connect("notify::selected", lambda *_: self._render_event_center())
        self.event_center_search.connect("search-changed", lambda *_: self._render_event_center())
        bar.append(self.event_center_kind)
        bar.append(self.event_center_search)
        self.event_status = W.label("", "muted")
        bar.append(self.event_status)
        bar.append(W.button("Подписки на события…", lambda: self.open_system("event")))
        page.append(bar)
        self.event_center_table = W.Table(("Время",), selectable=False)
        page.append(self.event_center_table)
        # старые атрибуты монитора тревог
        self.alarm_device = Gtk.DropDown.new_from_strings([])
        self.alarm_event_list = Gtk.Box()
        self.alarm_status = self.event_status
        return page

    def _render_event_center(self):
        if not hasattr(self, "event_center_table"):
            return
        kind = self.event_center_kind.get_selected()
        text = self.event_center_search.get_text().strip()
        if kind == 0:
            records = [r for r in alarms.load_alarm_records(500)
                       if not text or text.lower() in (r["device_name"] + r["rule_name"] + r["details"]).lower()]
            self.event_center_table.columns = ("Время", "Тревога", "Источник", "Приоритет", "Статус")
            self.event_center_table.on_activate = lambda rid: self.show_alarm_details(rid)
            self.event_center_table.set_rows(
                [(r["time"][:19].replace("T", " "), r["rule_name"], r["device_name"],
                  alarms.PRIORITIES.get(r["priority"], ""), alarms.STATUS_TITLES.get(r["status"], ""))
                 for r in records], [r["id"] for r in records])
            return
        category = {1: "all", 2: "person", 3: "vehicle"}.get(kind, "all")
        events = alarms.load_events(500, category=category, text=text)
        self.event_center_table.columns = ("Время", "Тип", "Источник", "Канал", "Подробности")
        self.event_center_table.on_activate = lambda index: self._open_alarm_video(
            {"device_id": events[index]["device_id"], "time": events[index]["time"]})
        rows = []
        for event in events:
            try:
                details = json.loads(event["details"])
            except json.JSONDecodeError:
                details = {}
            rows.append((event["time"][:19].replace("T", " "), alarms.event_title(event["event_type"]),
                         event["device_name"], event["channel"],
                         " ".join(f"{v}" for v in details.values())[:80]))
        self.event_center_table.set_rows(rows)

    # ================================================================== уведомления и шапка
    def notify(self, message, error=False):
        stamp = datetime.now().strftime("%H:%M:%S")
        if hasattr(self, "notifications"):
            self.notifications.insert(0, (stamp, message, error))
            del self.notifications[50:]
            W.clear(self.bell_list)
            for when, text, bad in self.notifications:
                item = W.label(f"{when}  {text}", "status-error" if bad else None, wrap=True)
                item.set_max_width_chars(48)
                self.bell_list.append(item)
            self.bell_button.set_label(f"🔔 {len(self.notifications)}")
        toast = getattr(self, "toast", None)
        if toast is not None:
            toast.show_message(message, error)
            return
        # Окна просмотра без панели уведомлений: одно окно на одинаковый текст, без стопок.
        shown = self.__dict__.setdefault("_open_messages", {})
        if message in shown:
            shown[message].present()
            return
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                                   buttons=Gtk.ButtonsType.CLOSE, text=message)

        def closed(instance, *_args):
            shown.pop(message, None)
            instance.destroy()

        dialog.connect("response", closed)
        shown[message] = dialog
        dialog.present()

    def show_about(self):
        from app import APP_VERSION
        self.notify(f"Sentinel {APP_VERSION} — клиент видеонаблюдения для Linux в стиле HikCentral Lite / iVMS-4200.\n"
                    "Клавиши: F11 — полный экран, Esc — закрыть окно модуля, Ctrl+T — новый вид, "
                    "Ctrl+S — сохранить вид, Ctrl+L — заблокировать, Пробел — пауза воспроизведения.")

    def show_license_info(self):
        self.notify("Sentinel не требует лицензий: каналы, двери и ANPR не ограничены. "
                    "Возможности зависят только от моделей и прошивок устройств.")

    def backup_restore_dialog(self):
        if not self.session.can("system"):
            self.notify("Нет прав на системную конфигурацию", error=True)
            return
        dialog = Gtk.Dialog(title="Резервное копирование и восстановление", transient_for=self, modal=True)
        dialog.add_button("Закрыть", Gtk.ResponseType.CLOSE)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        W.margins(box, 16)
        box.append(W.label("В копию входят устройства, виды, карты, персоны, ТС, уровни доступа, правила тревог, "
                           "пользователи и настройки. Пароли устройств остаются в системном хранилище и в копию "
                           "не попадают.", "muted", wrap=True))
        folder = Gtk.Entry(text=str(Path.home() / "Documents"))
        box.append(folder)

        def backup():
            target = Path(folder.get_text()).expanduser() / datetime.now().strftime("sentinel_%Y%m%d_%H%M%S")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                path = security.backup_database(target)
            except (OSError, ValueError) as exc:
                self.notify(f"Ошибка резервного копирования: {exc}", error=True)
                return
            alarms.log_operation(self.session.username, "Резервное копирование", str(path))
            self.notify(f"Копия сохранена: {path}")

        def restore():
            chooser = Gtk.FileChooserNative.new("Файл резервной копии", self, Gtk.FileChooserAction.OPEN,
                                                "Восстановить", "Отмена")
            self._restore_chooser = chooser

            def done(native, response):
                file = native.get_file()
                native.destroy()
                if response != Gtk.ResponseType.ACCEPT or not file:
                    return
                self._confirm("Заменить текущую базу резервной копией? Текущая база сохранится рядом "
                              "с расширением .before-restore.", lambda: self._restore_from(file.get_path()),
                              "Восстановить")

            chooser.connect("response", done)
            chooser.show()

        box.append(W.button("Создать резервную копию", backup, "red-button"))
        box.append(W.button("Восстановить из файла…", restore))
        dialog.get_content_area().append(box)
        dialog.connect("response", lambda dlg, *_: dlg.close())
        dialog.present()

    def _restore_from(self, path):
        try:
            self.stop_all_streams()
            keep = security.restore_database(path)
        except (OSError, ValueError, Exception) as exc:
            self.notify(f"Не удалось восстановить: {exc}", error=True)
            return
        self.refresh()
        self.notify(f"База восстановлена. Прежняя версия: {keep}")

    # ================================================================== вход и блокировка
    def _note_activity(self):
        self.last_activity = time.monotonic()

    def _show_lock_screen(self, mode):
        """mode: 'login' | 'unlock' | 'setup'."""
        W.clear(self.lock_layer)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.set_halign(Gtk.Align.CENTER)
        card.set_valign(Gtk.Align.CENTER)
        card.set_size_request(320, -1)
        card.append(W.label("⬢", "hcl-logo", 0.5))
        card.append(W.label("Sentinel", "page-title", 0.5))
        subtitle = {"login": "Вход в клиент", "unlock": "Клиент заблокирован",
                    "setup": "Задайте пароль администратора (admin)"}[mode]
        card.append(W.label(subtitle, "muted", 0.5))
        username = Gtk.Entry(text="admin" if mode != "unlock" else self.session.username)
        username.set_sensitive(mode == "login")
        password = Gtk.PasswordEntry(show_peek_icon=True)
        password.set_property("placeholder-text", "Пароль")
        confirm = Gtk.PasswordEntry(show_peek_icon=True)
        confirm.set_property("placeholder-text", "Повторите пароль")
        error = W.label("", "status-error", 0.5, True)
        card.append(username)
        card.append(password)
        if mode == "setup":
            card.append(confirm)
            card.append(W.label("Не короче 8 символов, буквы и цифры.", "muted", 0.5, True))

        def submit(*_args):
            try:
                if mode == "setup":
                    if password.get_text() != confirm.get_text():
                        raise ValueError("Пароли не совпадают")
                    security.ensure_admin(password.get_text())
                    session = security.authenticate("admin", password.get_text(), security.load_policy())
                else:
                    session = security.authenticate(username.get_text(), password.get_text(), security.load_policy())
            except ValueError as exc:
                error.set_text(str(exc))
                return
            self._begin_session(session)
            self.lock_layer.set_visible(False)

        password.connect("activate", submit)
        confirm.connect("activate", submit)
        card.append(W.button({"login": "Войти", "unlock": "Разблокировать", "setup": "Сохранить и войти"}[mode],
                             submit, "red-button"))
        if mode == "setup":
            card.append(W.button("Отмена", lambda: self.lock_layer.set_visible(False), flat=True))
        card.append(error)
        self.lock_layer.append(card)
        self.lock_layer.set_visible(True)
        password.grab_focus()

    def _begin_session(self, session):
        previous = self.session.username
        self.session = session
        self.user_button.set_label(f"{session.username} ▾")
        self.user_title.set_text(f"{session.username} · {session.role}")
        self.header_buttons["system"].set_sensitive(any(session.can(p) for p in
                                                        ("devices", "persons", "access", "alarms", "users", "system")))
        if session.username != previous:
            alarms.log_operation(session.username, "Вход в клиент")
        if session.password_expires_in is not None:
            policy = security.load_policy()
            if session.password_expires_in <= 0:
                self.notify("Срок действия пароля истёк — смените пароль", error=True)
                GLib.idle_add(self.change_password_dialog)
            elif session.password_expires_in <= policy["warn_before_days"]:
                self.notify(f"Пароль истекает через {session.password_expires_in} дн.")
        self._note_activity()

    def lock_client(self):
        if not security.has_users():
            self._show_lock_screen("setup")
            return
        self._show_lock_screen("unlock")

    def log_out(self):
        if not security.has_users():
            self.notify("Пользователи не заведены: задайте пароль admin, чтобы включить вход")
            self._show_lock_screen("setup")
            return
        alarms.log_operation(self.session.username, "Выход из клиента")
        self.stop_all_streams()
        self._show_lock_screen("login")

    def _auto_lock_tick(self):
        if getattr(self, "_closing", False):
            return False
        minutes = int(security.load_policy().get("auto_lock_minutes") or 0)
        idle = time.monotonic() - getattr(self, "last_activity", 0)
        if minutes and security.has_users() and not self.lock_layer.get_visible() and idle > minutes * 60:
            self._show_lock_screen("unlock")
        return True

    def change_password_dialog(self):
        if not security.has_users():
            self._show_lock_screen("setup")
            return
        dialog = Gtk.Dialog(title="Сменить пароль", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Сменить", Gtk.ResponseType.ACCEPT)
        form = Gtk.Grid(column_spacing=10, row_spacing=10)
        W.margins(form, 16)
        old = Gtk.PasswordEntry(show_peek_icon=True)
        new = Gtk.PasswordEntry(show_peek_icon=True)
        again = Gtk.PasswordEntry(show_peek_icon=True)
        strength = W.label("", "muted")
        new.connect("changed", lambda e: strength.set_text(
            security.STRENGTH_TITLES[security.password_strength(e.get_text())]))
        self._field(form, "Текущий пароль", old, 0)
        self._field(form, "Новый пароль", new, 1)
        self._field(form, "Повторите", again, 2)
        self._field(form, "Надёжность", strength, 3)
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    if new.get_text() != again.get_text():
                        raise ValueError("Пароли не совпадают")
                    security.change_password(self.session.username, old.get_text(), new.get_text(),
                                             security.load_policy())
                except ValueError as exc:
                    self.notify(str(exc), error=True)
                    return
                alarms.log_operation(self.session.username, "Смена пароля")
                self.notify("Пароль изменён")
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    # ================================================================== двери
    def quick_lock_dialog(self):
        controllers = [d for d in self.devices if d.get("category") == "access"]
        if not controllers:
            self.notify("Нет контроллеров доступа. Добавьте устройство с типом «Контроль доступа».")
            return
        dialog = Gtk.Dialog(title="Быстрая блокировка/разблокировка", transient_for=self, modal=True)
        dialog.add_button("Закрыть", Gtk.ResponseType.CLOSE)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        W.margins(box, 16)
        total = sum(max(1, int(d.get("door_count") or 1)) for d in controllers)
        box.append(W.label(f"Контроллеров: {len(controllers)} · дверей: {total}", "muted"))

        def all_doors(command):
            for device in controllers:
                for door in range(1, max(1, int(device.get("door_count") or 1)) + 1):
                    self.door_command(device, door, command, quiet=True)
            alarms.log_operation(self.session.username, f"Все двери: {people.DOOR_TITLES[command]}")
            self.notify(f"Команда «{people.DOOR_TITLES[command]}» отправлена на {total} дверей")

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(W.button("Разблокировать все", lambda: self._confirm(
            "Открыть все двери (режим «Всегда открыта»)?", lambda: all_doors("always_open"), "Открыть"), "red-button"))
        row.append(W.button("Заблокировать все", lambda: self._confirm(
            "Закрыть все двери (режим «Всегда закрыта»)?", lambda: all_doors("always_close"), "Закрыть")))
        row.append(W.button("Обычный режим", lambda: all_doors("resume")))
        box.append(row)
        dialog.get_content_area().append(box)
        dialog.connect("response", lambda dlg, *_: dlg.close())
        dialog.present()
