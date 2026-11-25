#!/usr/bin/env python3
"""Sentinel: native GTK 4 Linux client for video surveillance."""
from __future__ import annotations

import csv
import html
import http.client
import json
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import gi
except ImportError as exc:
    raise SystemExit(
        "Не найден PyGObject. Установите python3-gi и GTK 4 согласно README.md."
    ) from exc

require_version = getattr(gi, "require_version", None)
if not callable(require_version):
    raise SystemExit(
        "Загружен несовместимый пакет gi вместо PyGObject. "
        "Установите системный пакет python3-gi."
    )
require_version("Gtk", "4.0")
require_version("Gdk", "4.0")
require_version("Gst", "1.0")
try:
    from gi.repository import Gdk, GLib, GObject, Gst, Gtk
except ImportError as exc:
    raise SystemExit(
        "Не найдены GTK 4/GStreamer introspection bindings. "
        "Установите gir1.2-gtk-4.0 и gir1.2-gstreamer-1.0."
    ) from exc

import sentinel_devmgmt as devmgmt
import sentinel_widgets as W
from sentinel_mainwin import MainWindowMixin
from sentinel_services import ServicesMixin
import sentinel_layouts as layouts
import sentinel_shell as shell
from sentinel_protocols import (
    FfmpegJob,
    authenticated_opener,
    build_time_xml,
    iter_alert_payloads,
    parse_channel_ids,
    parse_channel_names,
    parse_device_info,
    parse_device_time,
    parse_streaming_channel_ids,
    set_allow_insecure_basic,
)
from sentinel_storage import (
    DATA,
    add_group,
    delete_group,
    load_custom_divisions,
    load_groups,
    rename_group,
    set_device_group,
    CredentialStoreError,
    database,
    delete_saved_credentials,
    load_devices,
    load_saved_credentials,
    migrate_legacy_credentials,
    save_credentials,
)

APP_ID = "io.sentinel.VideoClient"
APP_VERSION = Path(__file__).resolve().parent.joinpath("VERSION").read_text(encoding="utf-8").strip()
ROOT = Path(__file__).resolve().parent
# Hikvision sends a keep-alive roughly every 10 s; a short socket timeout kills a healthy alert stream.
ALARM_READ_TIMEOUT = 45
ALARM_RECONNECT_MIN = 2
ALARM_RECONNECT_MAX = 60
Gst.init(None)


def stylesheet():
    W.install_css()


def make_label(text: str, css_class: str | None = None):
    label = Gtk.Label(label=text)
    label.set_xalign(0)
    if css_class:
        label.add_css_class(css_class)
    return label


class SentinelWindow(MainWindowMixin, ServicesMixin, Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Sentinel — видеонаблюдение")
        self.set_resizable(True)
        self.set_default_size(*self._initial_window_size(1440, 900))
        self.devices = load_devices()
        self.session_credentials = {}
        self.last_view_credentials = None
        self.capture_folder = Path.home() / "Pictures" / "Sentinel"
        self.record_folder = Path.home() / "Videos" / "Sentinel"
        with database() as db:
            settings = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM app_settings")}
        self.capture_folder = Path(settings.get("capture_folder", str(self.capture_folder))).expanduser()
        self.record_folder = Path(settings.get("record_folder", str(self.record_folder))).expanduser()
        self.allow_insecure_basic = settings.get("allow_insecure_basic") == "1"
        set_allow_insecure_basic(self.allow_insecure_basic)
        self.streams = {}
        self.maximized_slot = None
        self.monitor_windows = []
        self.monitor_sequence = 0
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)
        self.stack.set_vhomogeneous(False)
        self.nav_buttons = {}
        self.toast_message = None
        self.alarm_stop = threading.Event()
        self.alarm_response = None
        self.alarm_thread = None
        self.alarm_events = []
        self._closing = False
        self.auto_switch_source = 0
        self.auto_switch_index = 0
        self.auto_switch_interval = 10
        self.camera_page = 0
        stylesheet()
        self._build_window()
        self._start_services()
        self.refresh()
        GLib.timeout_add(1600, self._update_stream_count)
        GLib.idle_add(self._migrate_credentials_before_restore)

    @staticmethod
    def _initial_window_size(max_width, max_height, monitor_index=0):
        display = Gdk.Display.get_default()
        monitors = display.get_monitors() if display else None
        monitor = None
        if monitors and monitors.get_n_items():
            index = monitor_index if monitor_index < monitors.get_n_items() else 0
            monitor = monitors.get_item(index)
        geometry = monitor.get_geometry() if monitor else None
        if not geometry:
            return min(max_width, 1280), min(max_height, 800)
        width = min(geometry.width, max(480, int(geometry.width * 0.9)), max_width)
        height = min(geometry.height, max(360, int(geometry.height * 0.9)), max_height)
        return width, height

    def _status_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        bar.add_css_class("statusbar")
        self.status_alarm = Gtk.Label(label="⚑ 0")
        self.status_devices = Gtk.Label(label="Устройств: 0")
        self.status_streams = Gtk.Label(label="Потоков: 0")
        self.status_clock = Gtk.Label(label="")
        for label in (self.status_alarm, self.status_devices, self.status_streams):
            bar.append(label)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)
        bar.append(self.status_clock)
        self._tick_clock()
        return bar

    def _tick_clock(self):
        if getattr(self, "_closing", False):
            return False
        self.status_clock.set_text(datetime.now().strftime("%d.%m.%Y  %H:%M:%S"))
        return True

    def _update_status_bar(self):
        if not hasattr(self, "status_devices"):
            return
        streams = len(self.streams) + sum(len(w.streams) for w in getattr(self, "monitor_windows", []))
        try:
            import sentinel_alarms
            events = sentinel_alarms.unacknowledged_count()
        except Exception:
            events = len(getattr(self, "alarm_events", []))
        self.status_devices.set_text(f"Устройств: {len(getattr(self, 'devices', []))}")
        self.status_streams.set_text(f"Потоков: {streams}")
        self.status_alarm.set_text(f"⚑ {events}")
        if events:
            self.status_alarm.add_css_class("status-alarm")
        else:
            self.status_alarm.remove_css_class("status-alarm")

    def _page(self, eyebrow, title, subtitle, action=None):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        outer.set_margin_top(18)
        outer.set_margin_bottom(16)
        outer.set_margin_start(18)
        outer.set_margin_end(18)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        titles.append(make_label(eyebrow, "eyebrow"))
        titles.append(make_label(title, "page-title"))
        titles.append(make_label(subtitle, "muted"))
        top.append(titles)
        if action:
            action.set_halign(Gtk.Align.END)
            action.set_valign(Gtk.Align.CENTER)
            action.set_hexpand(True)
            top.append(action)
        outer.append(top)
        return outer

    def _set_record_button_label(self, label):
        if hasattr(self, "record_button"):
            if getattr(self, "compact_record_button", False):
                self.record_button.set_label("■" if label.startswith("■") else "●")
            else:
                self.record_button.set_label(label)

    def _stat(self, label, number, caption):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        box.add_css_class("stat-card")
        box.append(make_label(label, "stat-label"))
        number.add_css_class("stat-number")
        box.append(number)
        box.append(make_label(caption, "muted"))
        return box

    def _button(self, text, callback, primary=False):
        button = Gtk.Button(label=text)
        if primary:
            button.add_css_class("suggested-action")
        button.connect("clicked", lambda *_: callback())
        return button

    def _audio_control_menu(self):
        button = Gtk.MenuButton(label="🔇 Звук")
        popover = Gtk.Popover()
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                           margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        controls.append(make_label("Громкость выбранной камеры", "muted"))
        volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
        volume.set_value(1.0)
        volume.set_draw_value(False)
        volume.set_size_request(180, -1)
        volume.connect("value-changed", self._set_selected_volume)
        controls.append(volume)
        mute = Gtk.Button(label="Включить / выключить звук")
        mute.connect("clicked", lambda *_: self.toggle_audio())
        controls.append(mute)
        popover.set_child(controls)
        button.set_popover(popover)
        return button

    def _set_selected_volume(self, scale):
        state = self.streams.get(getattr(self, "selected_slot", -1))
        if not state:
            return
        volume = scale.get_value()
        state["volume"] = volume
        state["pipeline"].set_property("volume", volume)
        if volume > 0 and not state.get("audio_enabled", False):
            state["audio_enabled"] = True
            state["pipeline"].set_property("mute", False)
            if hasattr(self, "audio_button"):
                self.audio_button.set_label("🔊" if getattr(self, "compact_audio_button", False) else "🔊 Звук")

    def export_devices_csv(self):
        rows = devmgmt.physical_devices(self.devices, getattr(self, "groups", []))
        if not rows:
            self.notify("Нет устройств для экспорта")
            return
        chooser = Gtk.FileChooserNative.new("Экспорт устройств", self, Gtk.FileChooserAction.SAVE,
                                            "Сохранить", "Отмена")
        chooser.set_current_name("devices.csv")
        self._device_export_chooser = chooser

        def done(dialog, response):
            file = dialog.get_file()
            if response == Gtk.ResponseType.ACCEPT and file and file.get_path():
                try:
                    with open(file.get_path(), "w", encoding="utf-8-sig", newline="") as output:
                        devmgmt.export_csv(rows, output)
                    self.notify(f"Сохранено: {file.get_path()}")
                except OSError as exc:
                    self.notify(f"Не удалось сохранить файл: {exc}")
            dialog.destroy()

        chooser.connect("response", done)
        chooser.show()

    def _group_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        for margin in ("top", "bottom", "start", "end"):
            getattr(page, f"set_margin_{margin}")(14)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.add_css_class("section-card")
        left.set_size_request(280, -1)
        left.append(make_label("Группы", "section-heading"))
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_margin_start(10)
        for title, callback in (("Добавить", self._add_group_dialog), ("Переименовать", self._rename_group_dialog),
                                ("Удалить", self._delete_group_dialog)):
            button = Gtk.Button(label=title)
            button.connect("clicked", lambda _b, cb=callback: cb())
            actions.append(button)
        left.append(actions)
        by_name = Gtk.Button(label="Создать группы по названиям устройств")
        by_name.set_margin_start(10)
        by_name.set_margin_end(10)
        by_name.connect("clicked", lambda *_: self._create_groups_by_device())
        left.append(by_name)
        self.group_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.group_list.set_margin_start(10)
        self.group_list.set_margin_end(10)
        left.append(self.group_list)
        page.append(left)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right.add_css_class("section-card")
        right.set_hexpand(True)
        self.group_title = make_label("Выберите группу", "section-heading")
        right.append(self.group_title)
        move = Gtk.Button(label="Добавить устройства в группу…")
        move.set_halign(Gtk.Align.START)
        move.set_margin_start(10)
        move.connect("clicked", lambda *_: self._assign_devices_dialog())
        right.append(move)
        self.group_members = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.group_members.set_margin_start(10)
        right.append(self.group_members)
        page.append(right)
        self.selected_group_id = None
        return page

    def _render_groups(self):
        if self.__dict__.get("group_list") is None:
            return
        self._clear_box(self.group_list)
        valid = {g["id"] for g in self.groups}
        if self.selected_group_id not in valid:
            self.selected_group_id = None
        rows = devmgmt.physical_devices(self.devices, self.groups)
        counts = {}
        for item in rows:
            counts[item["group_id"]] = counts.get(item["group_id"], 0) + 1
        if not self.groups:
            self.group_list.append(make_label("Групп пока нет", "muted"))
        for group in self.groups:
            button = Gtk.ToggleButton(label=f"{group['name']}  ({counts.get(group['id'], 0)})")
            button.set_active(group["id"] == self.selected_group_id)
            button.connect("clicked", lambda _b, gid=group["id"]: self._select_group(gid))
            self.group_list.append(button)
        self._clear_box(self.group_members)
        if self.selected_group_id is None:
            self.group_title.set_text("Выберите группу")
            return
        name = next(g["name"] for g in self.groups if g["id"] == self.selected_group_id)
        self.group_title.set_text(name)
        members = [r for r in rows if r["group_id"] == self.selected_group_id]
        if not members:
            self.group_members.append(make_label("В группе нет устройств", "muted"))
        for item in members:
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            line.append(make_label(f"{item['name']} · {item['host']} · каналов: {item['channels']}"))
            line.append(Gtk.Box(hexpand=True))
            remove = Gtk.Button(label="Убрать из группы")
            remove.connect("clicked", lambda _b, ids=item["ids"]: self._set_group_for(ids, None))
            line.append(remove)
            self.group_members.append(line)

    def _select_group(self, group_id):
        self.selected_group_id = group_id
        self._render_groups()

    def _set_group_for(self, ids, group_id):
        set_device_group(ids, group_id)
        self.refresh()

    def _ask_text(self, title, initial, callback):
        entry = Gtk.Entry(text=initial)
        for margin in ("top", "bottom", "start", "end"):
            getattr(entry, f"set_margin_{margin}")(14)
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("OK", Gtk.ResponseType.ACCEPT)
        dialog.get_content_area().append(entry)

        def response(dlg, result):
            text = entry.get_text().strip()
            dlg.close()
            if result == Gtk.ResponseType.ACCEPT and text:
                callback(text)

        dialog.connect("response", response)
        dialog.present()

    def _add_group_dialog(self):
        def create(name):
            self.selected_group_id = add_group(name)
            self.refresh()
        self._ask_text("Новая группа", "", create)

    def _rename_group_dialog(self):
        if self.selected_group_id is None:
            self.notify("Сначала выберите группу")
            return
        current = next(g["name"] for g in self.groups if g["id"] == self.selected_group_id)

        def rename(name):
            try:
                rename_group(self.selected_group_id, name)
            except sqlite3.IntegrityError:
                self.notify("Группа с таким названием уже есть")
                return
            self.refresh()
        self._ask_text("Переименовать группу", current, rename)

    def _delete_group_dialog(self):
        if self.selected_group_id is None:
            self.notify("Сначала выберите группу")
            return
        group_id = self.selected_group_id
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CANCEL, text="Удалить группу? Устройства останутся в каталоге.")
        dialog.add_button("Удалить", Gtk.ResponseType.ACCEPT)

        def response(dlg, result):
            dlg.close()
            if result == Gtk.ResponseType.ACCEPT:
                delete_group(group_id)
                self.selected_group_id = None
                self.refresh()

        dialog.connect("response", response)
        dialog.present()

    def _create_groups_by_device(self):
        rows = devmgmt.physical_devices(self.devices, self.groups)
        if not rows:
            self.notify("Нет устройств")
            return
        for item in rows:
            if item["group_id"] is None:
                set_device_group(item["ids"], add_group(item["name"]))
        self.refresh()

    def _assign_devices_dialog(self):
        if self.selected_group_id is None:
            self.notify("Сначала выберите группу")
            return
        rows = devmgmt.physical_devices(self.devices, self.groups)
        dialog = Gtk.Dialog(title="Устройства для группы", transient_for=self, modal=True)
        dialog.set_default_size(420, 420)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Добавить", Gtk.ResponseType.ACCEPT)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for margin in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{margin}")(14)
        checks = []
        for item in rows:
            if item["group_id"] == self.selected_group_id:
                continue
            check = Gtk.CheckButton(label=f"{item['name']} · {item['host']}"
                                          + (f" (в группе «{item['group_name']}»)" if item["group_name"] else ""))
            checks.append((check, item["ids"]))
            box.append(check)
        if not checks:
            box.append(make_label("Все устройства уже в этой группе", "muted"))
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_child(box)
        dialog.get_content_area().append(scroll)
        group_id = self.selected_group_id

        def response(dlg, result):
            ids = [i for check, device_ids in checks if check.get_active() for i in device_ids]
            dlg.close()
            if result == Gtk.ResponseType.ACCEPT and ids:
                self._set_group_for(ids, group_id)

        dialog.connect("response", response)
        dialog.present()

    def _archive_page(self):
        page = self._page("ВОСПРОИЗВЕДЕНИЕ", "Архив", "Удалённый поиск и воспроизведение записей")
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        body.set_vexpand(True)
        body.set_hexpand(True)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar.set_size_request(300, -1)
        sidebar.add_css_class("section-card")
        sidebar.append(make_label("Найти запись", "section-heading"))
        form = Gtk.Grid(column_spacing=8, row_spacing=8)
        form.set_margin_start(12)
        form.set_margin_end(12)
        form.set_margin_bottom(8)
        form.set_margin_top(4)
        self.archive_device = Gtk.DropDown.new_from_strings([])
        self.archive_choice_devices = []
        now = datetime.now().replace(second=0, microsecond=0)
        self.archive_start = Gtk.Entry(text=(now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M"))
        self.archive_end = Gtk.Entry(text=now.strftime("%Y-%m-%d %H:%M"))
        self._field(form, "Канал", self.archive_device, 0)
        self._field(form, "Начало (локальное время)", self.archive_start, 1)
        self._field(form, "Конец (локальное время)", self.archive_end, 2)
        sidebar.append(form)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_halign(Gtk.Align.END)
        actions.set_margin_start(8)
        actions.set_margin_end(8)
        actions.set_margin_bottom(8)
        actions.append(self._button("⌕ Найти", self.search_archive))
        actions.append(self._button("▶ Интервал", self.play_archive, primary=True))
        sidebar.append(actions)
        hint = make_label("Для поиска используются сохранённые учётные данные устройства.", "muted")
        hint.set_wrap(True)
        hint.set_margin_start(12)
        hint.set_margin_end(12)
        sidebar.append(hint)
        results_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        results_panel.append(make_label("Найденные записи", "section-heading"))
        self.archive_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.archive_results.set_margin_start(8)
        self.archive_results.set_margin_end(8)
        self.archive_results.set_margin_bottom(8)
        self.archive_results.append(make_label("Задайте интервал и нажмите «Найти».", "muted"))
        results_scroll = Gtk.ScrolledWindow()
        results_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        results_scroll.set_vexpand(True)
        results_scroll.set_child(self.archive_results)
        results_panel.append(results_scroll)
        results_panel.set_vexpand(True)
        sidebar.append(results_panel)
        body.append(sidebar)

        player = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        player.add_css_class("section-card")
        player.set_hexpand(True)
        player.set_vexpand(True)
        self.archive_video_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.archive_video_host.set_hexpand(True)
        self.archive_video_host.set_vexpand(True)
        self.archive_video_overlay = Gtk.Overlay()
        self.archive_video_overlay.set_hexpand(True)
        self.archive_video_overlay.set_vexpand(True)
        self.archive_video_overlay.set_child(make_label(
            "Видео воспроизводится в выбранном окне видеосетки (нижняя полоса «Воспроизведение»).", "muted"))
        self.archive_video_host.append(self.archive_video_overlay)
        player.append(self.archive_video_host)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_margin_start(8)
        controls.set_margin_end(8)
        controls.set_margin_bottom(8)
        self.archive_play_button = Gtk.Button(label="▶")
        self.archive_play_button.set_tooltip_text("Воспроизвести или приостановить")
        self.archive_play_button.connect("clicked", lambda *_: self._toggle_archive_playback())
        controls.append(self.archive_play_button)
        stop = Gtk.Button(label="■")
        stop.set_tooltip_text("Остановить воспроизведение")
        stop.connect("clicked", lambda *_: self._stop_archive_playback())
        controls.append(stop)
        self.archive_time_label = make_label("Выберите запись на временной шкале", "muted")
        self.archive_time_label.set_hexpand(True)
        controls.append(self.archive_time_label)
        self.archive_speed_choice = Gtk.DropDown.new_from_strings(["0.5×", "1×", "2×", "4×"])
        self.archive_speed_choice.set_selected(1)
        self.archive_speed_choice.set_tooltip_text("Скорость воспроизведения")
        self.archive_speed_choice.connect("notify::selected", self._set_archive_speed)
        controls.append(self.archive_speed_choice)
        playback_controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        playback_controls.add_css_class("archive-controls")
        playback_controls.set_margin_start(8)
        playback_controls.set_margin_end(8)
        playback_controls.set_margin_bottom(8)
        playback_controls.append(controls)
        self.archive_timeline = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1000, 1)
        self.archive_timeline.set_draw_value(False)
        self.archive_timeline.set_hexpand(True)
        self.archive_timeline.set_margin_start(4)
        self.archive_timeline.set_margin_end(4)
        self.archive_timeline.connect("value-changed", self._seek_archive_timeline)
        playback_controls.append(self.archive_timeline)
        timeline_labels = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        timeline_labels.set_margin_start(4)
        timeline_labels.set_margin_end(4)
        self.archive_range_start_label = make_label("", "muted")
        self.archive_range_middle_label = make_label("", "muted")
        self.archive_range_end_label = make_label("", "muted")
        self.archive_range_middle_label.set_xalign(0.5)
        self.archive_range_end_label.set_xalign(1)
        for label in (self.archive_range_start_label, self.archive_range_middle_label,
                      self.archive_range_end_label):
            label.set_hexpand(True)
            timeline_labels.append(label)
        playback_controls.append(timeline_labels)
        playback_controls.set_halign(Gtk.Align.FILL)
        playback_controls.set_valign(Gtk.Align.END)
        playback_controls.set_can_target(True)
        self.archive_video_overlay.add_overlay(playback_controls)
        body.append(player)
        page.append(body)
        self.archive_matches = []
        self.archive_current_match = None
        self.archive_playback_source = 0
        self.archive_seek_source = 0
        self.archive_query_token = None
        self.archive_timeline_updating = False
        self._update_archive_timeline_marks()
        return page

    def _emap_page(self):
        page = self._page("КАРТА ОБЪЕКТА", "E-map", "Камеры и их расположение на плане",
                          self._button("＋ Добавить карту", self.add_map, primary=True))
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.map_choice = Gtk.DropDown.new_from_strings([])
        self.map_choice.set_hexpand(True)
        self.map_choice.connect("notify::selected", lambda *_: self._render_map())
        controls.append(make_label("Карта"))
        controls.append(self.map_choice)
        controls.append(self._button("＋ Точка камеры", self.add_map_hotspot))
        controls.append(self._button("Удалить карту", self.delete_map))
        page.append(controls)
        self.map_status = make_label("Добавьте изображение плана или карты, затем разместите точки камер.", "muted")
        page.append(self.map_status)
        self.map_canvas = Gtk.Overlay()
        self.map_canvas.set_hexpand(True)
        self.map_canvas.set_vexpand(True)
        self.map_canvas.add_css_class("section-card")
        self.map_picture = Gtk.Picture()
        self.map_picture.set_can_shrink(True)
        self.map_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.map_canvas.set_child(self.map_picture)
        self.map_points = Gtk.Fixed()
        self.map_points.set_hexpand(True)
        self.map_points.set_vexpand(True)
        self.map_canvas.add_overlay(self.map_points)
        page.append(self.map_canvas)
        return page

    def _access_page(self):
        page = self._page("ДВЕРИ И СОБЫТИЯ", "Контроль доступа", "Управление дверью и журнал событий контроллера")
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        panel.add_css_class("section-card")
        panel.append(make_label("Подключение к контроллеру", "section-heading"))
        form = Gtk.Grid(column_spacing=12, row_spacing=9, margin_top=12,
                        margin_start=16, margin_end=16, margin_bottom=8)
        self.access_device = Gtk.DropDown.new_from_strings([])
        self.access_user = Gtk.Entry(placeholder_text="Имя пользователя")
        self.access_password = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="Пароль")
        self.access_door = Gtk.SpinButton.new_with_range(1, 256, 1)
        self.access_door.set_value(1)
        self._field(form, "Контроллер", self.access_device, 0)
        self._field(form, "Пользователь", self.access_user, 1)
        self._field(form, "Пароль", self.access_password, 2)
        self._field(form, "Номер двери", self.access_door, 3)
        panel.append(form)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_start=16, margin_bottom=10)
        actions.append(self._button("Открыть дверь…", self.access_open_door, primary=True))
        actions.append(self._button("Состояние контроллера", self.access_controller_status))
        panel.append(actions)
        page.append(panel)
        search = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        search.add_css_class("section-card")
        search.append(make_label("Журнал проходов", "section-heading"))
        filters = Gtk.Grid(column_spacing=10, row_spacing=8, margin_top=10,
                           margin_start=16, margin_end=16, margin_bottom=10)
        now = datetime.now().replace(second=0, microsecond=0)
        self.access_start = Gtk.Entry(text=(now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
        self.access_end = Gtk.Entry(text=now.strftime("%Y-%m-%d %H:%M:%S"))
        self._field(filters, "Начало", self.access_start, 0)
        self._field(filters, "Конец", self.access_end, 1)
        search.append(filters)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_start=16, margin_bottom=10)
        actions.append(self._button("Поиск событий", self.access_search_events))
        actions.append(self._button("Экспорт CSV", self.access_export_events))
        search.append(actions)
        self.access_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3,
                                      margin_start=16, margin_end=16, margin_bottom=14)
        search.append(self.access_results)
        page.append(search)
        self.access_event_rows = []
        return page

    def _refresh_maps(self):
        if not hasattr(self, "map_choice"):
            return
        with database() as db:
            self.maps = [dict(row) for row in db.execute("SELECT * FROM maps ORDER BY name")]
        self._update_dashboard(update_maps=True)
        self._fill_left_maps()
        self.map_choice.set_model(Gtk.StringList.new([item["name"] for item in self.maps]))
        if self.maps:
            self.map_choice.set_selected(min(self.map_choice.get_selected(), len(self.maps) - 1))
        self._render_map()

    def _render_map(self):
        if not hasattr(self, "map_picture"):
            return
        self._clear_box(self.map_points)
        selected = self.map_choice.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(getattr(self, "maps", [])):
            self.map_picture.set_paintable(None)
            self.map_status.set_text("Добавьте изображение плана или карты, затем разместите точки камер.")
            return
        current = self.maps[selected]
        image_path = Path(current["image_path"])
        if not image_path.is_file():
            self.map_picture.set_paintable(None)
            self.map_status.set_text(f"Файл карты не найден: {image_path}")
            return
        try:
            self.map_picture.set_filename(str(image_path))
        except GLib.Error as exc:
            self.map_status.set_text(f"Не удалось открыть карту: {exc.message}")
            return
        with database() as db:
            points = [dict(row) for row in db.execute("""SELECT h.*,d.name AS camera_name,d.host,d.channel
                FROM map_hotspots h JOIN devices d ON d.id=h.device_id WHERE h.map_id=? ORDER BY h.id""", (current["id"],))]
        self.map_status.set_text(f"{current['name']} · точек камер: {len(points)} · щёлкните точку для live view")
        GLib.idle_add(self._place_map_points, points)

    def _place_map_points(self, points):
        width = max(1, self.map_canvas.get_width())
        height = max(1, self.map_canvas.get_height())
        for point in points:
            marker = Gtk.Button(label="● " + point["label"])
            marker.set_tooltip_text(f"{point['camera_name']} · {point['host']} CH {point['channel']}\nПравый клик — удалить точку")
            marker.connect("clicked", lambda _b, p=point: self.open_live_dialog(next((d for d in self.devices if d["id"] == p["device_id"]), None)))
            context = Gtk.GestureClick()
            context.set_button(3)
            context.connect("released", lambda _g, _n, _x, _y, p=point: self.delete_map_hotspot(p["id"]))
            marker.add_controller(context)
            self.map_points.put(marker, point["x"] * width / 100.0, point["y"] * height / 100.0)
        return GLib.SOURCE_REMOVE

    def add_map(self, *_args):
        chooser = Gtk.FileChooserNative.new("Выберите изображение карты", self, Gtk.FileChooserAction.OPEN,
                                            "Добавить", "Отмена")
        chooser.connect("response", self._add_map_file)
        chooser.show()

    def _add_map_file(self, chooser, response):
        file = chooser.get_file()
        if response != Gtk.ResponseType.ACCEPT or not file:
            chooser.destroy()
            return
        path = file.get_path()
        name_entry = Gtk.Entry(text=Path(path).stem)
        dialog = Gtk.Dialog(title="Название карты", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Сохранить", Gtk.ResponseType.ACCEPT)
        name_entry.set_margin_top(14); name_entry.set_margin_bottom(14)
        name_entry.set_margin_start(14); name_entry.set_margin_end(14)
        dialog.get_content_area().append(name_entry)
        def response_cb(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    with database() as db:
                        db.execute("INSERT INTO maps(name,image_path) VALUES(?,?)", (name_entry.get_text().strip(), path))
                    self._refresh_maps()
                except sqlite3.IntegrityError:
                    self.notify("Карта с таким именем уже существует")
            dlg.close()
        dialog.connect("response", response_cb)
        dialog.present()
        chooser.destroy()

    def add_map_hotspot(self, *_args):
        selected = self.map_choice.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(getattr(self, "maps", [])):
            self.notify("Сначала выберите или добавьте карту")
            return
        if not self.devices:
            self.notify("Сначала добавьте камеры в раздел «Устройства»")
            return
        dialog = Gtk.Dialog(title="Точка камеры на карте", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Добавить", Gtk.ResponseType.ACCEPT)
        form = Gtk.Grid(column_spacing=10, row_spacing=9, margin_top=14, margin_bottom=14,
                        margin_start=14, margin_end=14)
        camera = Gtk.DropDown.new_from_strings([f"{d['name']} · {d['host']} CH {d['channel']}" for d in self.devices])
        label = Gtk.Entry(placeholder_text="Название точки")
        xpos = Gtk.SpinButton.new_with_range(0, 100, 1); xpos.set_value(50)
        ypos = Gtk.SpinButton.new_with_range(0, 100, 1); ypos.set_value(50)
        self._field(form, "Камера", camera, 0); self._field(form, "Подпись", label, 1)
        self._field(form, "Позиция X, %", xpos, 2); self._field(form, "Позиция Y, %", ypos, 3)
        dialog.get_content_area().append(form)
        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                if camera.get_selected() == Gtk.INVALID_LIST_POSITION:
                    self.notify("Выберите камеру")
                    return
                device = self.devices[camera.get_selected()]
                title = label.get_text().strip() or device["name"]
                with database() as db:
                    db.execute("INSERT INTO map_hotspots(map_id,device_id,label,x,y) VALUES(?,?,?,?,?)",
                               (self.maps[selected]["id"], device["id"], title, xpos.get_value() / 100, ypos.get_value() / 100))
                self._render_map()
            dlg.close()
        dialog.connect("response", response); dialog.present()

    def delete_map_hotspot(self, hotspot_id):
        with database() as db:
            db.execute("DELETE FROM map_hotspots WHERE id=?", (hotspot_id,))
        self._render_map()

    def delete_map(self, *_args):
        selected = self.map_choice.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(getattr(self, "maps", [])):
            return
        with database() as db:
            db.execute("DELETE FROM map_hotspots WHERE map_id=?", (self.maps[selected]["id"],))
            db.execute("DELETE FROM maps WHERE id=?", (self.maps[selected]["id"],))
        self._refresh_maps()

    def _access_credentials(self):
        selected = self.access_device.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self.devices):
            raise ValueError("Выберите контроллер")
        device = self.devices[selected]
        if device["protocol"] != "hikvision":
            raise ValueError("Контроль доступа по ISAPI доступен для устройств Hikvision")
        username = self.access_user.get_text().strip()
        if not username:
            known = self.session_credentials.get(device["id"])
            if known and known.get("username"):
                return device, {"username": known["username"], "password": known.get("password", "")}
            raise ValueError("Введите пользователя контроллера")
        return device, {"username": username, "password": self.access_password.get_text()}

    def _isapi_request(self, device, credentials, path, method="GET", body=None, content_type="application/xml"):
        url = f"http://{device['host']}:{device.get('http_port', 80)}{path}"
        opener = authenticated_opener(url, credentials["username"], credentials["password"])
        data = body.encode("utf-8") if isinstance(body, str) else body
        request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": content_type})
        with opener.open(request, timeout=12) as response:
            return response.read()

    def access_open_door(self, *_args):
        try:
            device, credentials = self._access_credentials()
        except ValueError as exc:
            self.notify(str(exc)); return
        door = self.access_door.get_value_as_int()
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CANCEL, text=f"Открыть дверь {door} на устройстве «{device['name']}»?")
        dialog.add_button("Открыть", Gtk.ResponseType.ACCEPT)
        def response(dlg, result):
            dlg.close()
            if result != Gtk.ResponseType.ACCEPT:
                return
            body = "<RemoteControlDoor><cmd>open</cmd></RemoteControlDoor>"
            threading.Thread(target=self._access_open_worker, args=(device, credentials, door, body), daemon=True).start()
        dialog.connect("response", response); dialog.present()

    def _access_open_worker(self, device, credentials, door, body):
        try:
            self._isapi_request(device, credentials, f"/ISAPI/AccessControl/RemoteControl/door/{door}", "PUT", body)
            GLib.idle_add(self.notify, f"Команда открытия двери {door} отправлена")
        except Exception as exc:
            GLib.idle_add(self.notify, f"Не удалось открыть дверь: {exc}")

    def access_controller_status(self, *_args):
        try:
            device, credentials = self._access_credentials()
        except ValueError as exc:
            self.notify(str(exc)); return
        threading.Thread(target=self._access_status_worker, args=(device, credentials), daemon=True).start()

    def _access_status_worker(self, device, credentials):
        try:
            body = self._isapi_request(device, credentials, "/ISAPI/System/deviceInfo")
            root = ET.fromstring(body)
            values = {node.tag.rsplit("}", 1)[-1]: (node.text or "").strip() for node in root.iter()}
            message = f"{values.get('deviceName', device['name'])}\nМодель: {values.get('model','—')}\nСерийный номер: {values.get('serialNumber','—')}\nПрошивка: {values.get('firmwareVersion','—')}"
            GLib.idle_add(self.notify, message)
        except Exception as exc:
            GLib.idle_add(self.notify, f"Нет ответа от контроллера: {exc}")

    def access_search_events(self, *_args):
        try:
            device, credentials = self._access_credentials()
            start = datetime.fromisoformat(self.access_start.get_text().strip()).astimezone().isoformat(timespec="seconds")
            end = datetime.fromisoformat(self.access_end.get_text().strip()).astimezone().isoformat(timespec="seconds")
            if end <= start:
                raise ValueError("Конец интервала должен быть позже начала")
        except (ValueError, TypeError) as exc:
            self.notify(str(exc)); return
        self._clear_box(self.access_results)
        self.access_results.append(make_label("Идёт запрос журнала контроллера…", "muted"))
        threading.Thread(target=self._access_event_worker, args=(device, credentials, start, end), daemon=True).start()

    def _access_event_worker(self, device, credentials, start, end):
        try:
            query = {"AcsEventCond": {"searchID": str(uuid.uuid4()), "searchResultPosition": 0,
                "maxResults": 100, "major": 0, "minor": 0, "startTime": start, "endTime": end}}
            raw = self._isapi_request(device, credentials, "/ISAPI/AccessControl/AcsEvent?format=json",
                "POST", json.dumps(query), "application/json")
            payload = json.loads(raw.decode("utf-8", "replace"))
            result = payload.get("AcsEvent", payload)
            events = result.get("InfoList", []) or []
            GLib.idle_add(self._show_access_events, events)
        except Exception as exc:
            GLib.idle_add(self._show_access_error, str(exc))

    def _show_access_events(self, events):
        self._clear_box(self.access_results)
        self.access_event_rows = events
        if not events:
            self.access_results.append(make_label("Событий за выбранный период нет.", "muted"))
        for event in events:
            info = " · ".join(str(event.get(key, "")) for key in ("time", "employeeNoString", "cardNo", "doorNo", "minor"))
            self.access_results.append(make_label(info or json.dumps(event, ensure_ascii=False), "muted"))
        return GLib.SOURCE_REMOVE

    def _show_access_error(self, message):
        self._clear_box(self.access_results)
        self.access_results.append(make_label("Контроллер не вернул журнал событий.", "muted"))
        self.notify(f"Ошибка журнала доступа: {message}")
        return GLib.SOURCE_REMOVE

    def access_export_events(self, *_args):
        rows = getattr(self, "access_event_rows", [])
        if not rows:
            self.notify("Сначала выполните поиск событий")
            return
        path = DATA / "access_events.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=sorted({key for row in rows for key in row.keys()}))
            writer.writeheader(); writer.writerows(rows)
        self.notify(f"Журнал сохранён: {path}")

    def _field(self, grid, title, widget, row):
        label = make_label(title, "muted")
        grid.attach(label, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
        widget.set_hexpand(True)

    def _refresh_archive_choices(self):
        selected = self.archive_device.get_selected()
        selected_id = (
            self.archive_choice_devices[selected]["id"]
            if selected < len(self.archive_choice_devices) else None
        )
        self.devices = load_devices()
        strings = [f"{d['name']} · {d['host']} CH {d['channel']}" for d in self.devices]
        self.archive_device.set_model(Gtk.StringList.new(strings))
        self.archive_choice_devices = list(self.devices)
        next_selected = next((index for index, device in enumerate(self.devices)
                              if device["id"] == selected_id), 0)
        if self.devices:
            self.archive_device.set_selected(next_selected)
        self.alarm_device.set_model(Gtk.StringList.new(strings))
        if hasattr(self, "access_device"):
            self.access_device.set_model(Gtk.StringList.new(strings))

    def refresh(self, *_args):
        self.devices = load_devices()
        self.groups = load_groups()
        self._fill_resource_list()
        self._render_groups()
        self._refresh_archive_choices()
        self._fill_custom_view_list()
        self._fill_left_maps()
        self._fill_web_pages()
        self._update_dashboard(update_maps=True)
        self._update_stream_count()
        system = getattr(self, "system_window", None)
        if system is not None and system.get_visible():
            system.refresh_devices()

    def _confirm_delete_devices(self, _button, device_ids, description):
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CANCEL, text=f"Удалить {description}?")
        dialog.add_button("Удалить", Gtk.ResponseType.ACCEPT)
        dialog.connect("response", lambda dlg, response:
                       (dlg.close(), self.delete_devices(device_ids)
                        if response == Gtk.ResponseType.ACCEPT else None))
        dialog.present()

    def delete_devices(self, device_ids):
        existing_ids = {device["id"] for device in self.devices}
        device_ids = sorted(set(device_ids) & existing_ids)
        if not device_ids:
            return
        deleted_devices = [device for device in self.devices if device["id"] in device_ids]
        credentials_to_delete = set()
        for owner in [self, *self.monitor_windows]:
            for slot, state in list(owner.streams.items()):
                if state["device"]["id"] in device_ids:
                    owner._stop_stream(slot)
        with database() as db:
            db.execute(f"DELETE FROM map_hotspots WHERE device_id IN ({','.join('?' for _ in device_ids)})",
                       device_ids)
            for row in db.execute("SELECT id,mapping FROM saved_views").fetchall():
                try:
                    mapping = json.loads(row["mapping"])
                except (TypeError, json.JSONDecodeError):
                    continue
                filtered = [item for item in mapping
                            if not isinstance(item, dict) or item.get("device_id") not in device_ids]
                if filtered != mapping:
                    db.execute("UPDATE saved_views SET mapping=? WHERE id=?",
                               (json.dumps(filtered), row["id"]))
            for row in db.execute("SELECT id,mapping FROM saved_windows").fetchall():
                try:
                    mapping = json.loads(row["mapping"])
                except (TypeError, json.JSONDecodeError):
                    continue
                filtered = [device_id if device_id not in device_ids else None
                            for device_id in mapping]
                if filtered != mapping:
                    db.execute("UPDATE saved_windows SET mapping=? WHERE id=?",
                               (json.dumps(filtered), row["id"]))
            db.execute(f"DELETE FROM devices WHERE id IN ({','.join('?' for _ in device_ids)})",
                       device_ids)
            for host, http_port in {
                (device["host"], int(device.get("http_port", 80)))
                for device in deleted_devices
            }:
                remaining = db.execute(
                    "SELECT 1 FROM devices WHERE host=? AND http_port=? LIMIT 1",
                    (host, http_port),
                ).fetchone()
                if not remaining:
                    credentials_to_delete.add((host, http_port))
        for host, http_port in credentials_to_delete:
            try:
                delete_saved_credentials({"host": host, "http_port": http_port})
            except (CredentialStoreError, sqlite3.Error) as exc:
                self.notify(f"Устройства удалены, но не удалось удалить сохранённые учётные данные: {exc}")
        for device_id in device_ids:
            self.session_credentials.pop(device_id, None)
        self.refresh()
        self.notify(f"Удалено каналов: {len(device_ids)}")

    def _load_view_cameras(self, mapping, position):
        if position >= len(mapping):
            return
        item = mapping[position]
        device = next((d for d in self.devices if d["id"] == item["device_id"]), None)
        if not device:
            self._load_view_cameras(mapping, position + 1)
            return
        slot = int(item["slot"])
        try:
            saved = self._get_device_credentials(device)
        except (CredentialStoreError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.notify(f"Не удалось прочитать сохранённые учётные данные: {exc}")
            saved = None
        if saved:
            try:
                self._start_preview(slot, device, saved)
            except Exception as exc:
                self.notify(str(exc))
            self._load_view_cameras(mapping, position + 1)
            return
        def open_and_continue(cam, credentials):
            self.session_credentials[cam["id"]] = dict(credentials)
            self._start_preview(slot, cam, credentials)
            self._load_view_cameras(mapping, position + 1)
        self._credentials_dialog("Live view · " + device["name"], device, open_and_continue)

    @staticmethod
    def _lookup_division(key):
        division = layouts.DIVISIONS.get(key)
        if division:
            return division
        if isinstance(key, str) and key.startswith("c:"):
            for item in load_custom_divisions():
                if item["name"] == key[2:]:
                    return layouts.Division(key, item["name"], item["cols"], item["rows"], tuple(item["cells"]))
        return None

    def _current_cells(self):
        cells = getattr(self, "cell_layout", None)
        if cells and len(cells) == self.layout_count:
            return cells
        columns = max(1, self.grid_columns)
        return [(i % columns, i // columns, 1, 1) for i in range(self.layout_count)]

    def _set_division(self, key):
        division = self._lookup_division(key)
        if division is None:
            return False
        if key == getattr(self, "division", "") and division.count == self.layout_count:
            return True
        for slot in list(self.streams):
            if slot >= division.count:
                self._stop_stream(slot)
        self._restore_maximized_slot()
        self.division = key
        self.layout_count = division.count
        self.grid_columns, self.grid_rows = division.cols, division.rows
        self.cell_layout = list(division.cells)
        self._render_video_grid()
        self._refresh_thumbnail_streams()
        self._mark_view_dirty()
        return True

    def _desired_stream(self, index):
        mode = getattr(self, "stream_mode", layouts.STREAM_AUTO)
        available = (1, 2) if mode == layouts.STREAM_AUTO else (1, 2, 3)
        number = layouts.resolve_stream(mode, self.layout_count,
                                        index == getattr(self, "maximized_slot", None), available)
        return {1: "main", 2: "sub", 3: "third"}[number]

    def _set_stream_mode(self, mode):
        if mode not in layouts.STREAM_MODES:
            return
        self.stream_mode = mode
        self._refresh_thumbnail_streams()

    @staticmethod
    def _default_grid_shape(count):
        columns = max(1, int(count ** 0.5))
        if columns * columns < count:
            columns += 1
        rows = (count + columns - 1) // columns
        return columns, rows

    def _set_layout(self, _button, count, columns=None, rows=None):
        if columns is None or rows is None:
            columns, rows = self._default_grid_shape(count)
        if count == self.layout_count and columns == self.grid_columns and rows == self.grid_rows:
            return
        for slot in list(self.streams):
            if slot >= count:
                self._stop_stream(slot)
        self._restore_maximized_slot()
        self.layout_count = count
        self.grid_columns, self.grid_rows = columns, rows
        self.division = ""
        self.cell_layout = None
        self._render_video_grid()
        self._refresh_thumbnail_streams()
        self._mark_view_dirty()

    def toggle_fullscreen(self, *_args):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def customize_layout(self):
        dialog = Gtk.Dialog(title="Настроить раскладку", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Применить", Gtk.ResponseType.ACCEPT)
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, margin_top=16, margin_bottom=16,
                        margin_start=16, margin_end=16)
        columns = Gtk.SpinButton.new_with_range(1, 16, 1)
        rows = Gtk.SpinButton.new_with_range(1, 16, 1)
        columns.set_value(self.grid_columns)
        rows.set_value(self.grid_rows)
        self._field(grid, "Колонки", columns, 0)
        self._field(grid, "Строки", rows, 1)
        dialog.get_content_area().append(grid)
        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                grid_columns = columns.get_value_as_int()
                grid_rows = rows.get_value_as_int()
                self._set_layout(None, grid_columns * grid_rows, grid_columns, grid_rows)
            dlg.close()
        dialog.connect("response", response)
        dialog.present()

    def open_local_settings(self):
        dialog = Gtk.Dialog(title="Настройки локальных файлов", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Сохранить", Gtk.ResponseType.ACCEPT)
        form = Gtk.Grid(column_spacing=10, row_spacing=10, margin_top=16, margin_bottom=16,
                        margin_start=16, margin_end=16)
        captures = Gtk.Entry(text=str(self.capture_folder))
        recordings = Gtk.Entry(text=str(self.record_folder))
        self._field(form, "Папка снимков", captures, 0)
        self._field(form, "Папка видеозаписей", recordings, 1)
        insecure_basic = Gtk.CheckButton(
            label="Разрешить HTTP Basic по http:// (пароль уходит открытым текстом; нужен только "
                  "камерам без Digest)")
        insecure_basic.set_active(self.allow_insecure_basic)
        form.attach(insecure_basic, 0, 2, 2, 1)
        dialog.get_content_area().append(form)
        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                self.capture_folder = Path(captures.get_text()).expanduser()
                self.record_folder = Path(recordings.get_text()).expanduser()
                self._set_insecure_basic(insecure_basic.get_active())
                with database() as db:
                    db.executemany("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                   (("capture_folder", str(self.capture_folder)), ("record_folder", str(self.record_folder)),
                                    ("allow_insecure_basic", "1" if self.allow_insecure_basic else "0")))
                self.notify("Параметры сохранены")
            dlg.close()
        dialog.connect("response", response)
        dialog.present()

    def _set_auto_switch_interval(self, choice, _param):
        values = (5, 10, 20, 30, 60)
        selected = choice.get_selected()
        if selected < len(values):
            self.auto_switch_interval = values[selected]

    def _render_video_grid(self):
        paintables = {
            index: slot["picture"].get_paintable()
            for index, slot in enumerate(self.video_slots)
        }
        self._clear_box(self.camera_grid)
        self.video_slots = []
        cells = self._current_cells()
        self.camera_grid.set_row_homogeneous(any(c[2] > 1 or c[3] > 1 for c in cells))
        for index in range(self.layout_count):
            paintable = paintables.get(index)
            aspect_ratio = (
                paintable.get_width() / paintable.get_height()
                if paintable and paintable.get_height() > 0 else 16 / 9
            )
            aspect_frame = Gtk.AspectFrame.new(0.5, 0.5, aspect_ratio, False)
            aspect_frame.set_hexpand(True)
            aspect_frame.set_halign(Gtk.Align.FILL)
            pane = Gtk.Overlay()
            pane.add_css_class("camera-tile")
            pane.set_hexpand(True)
            pane.set_halign(Gtk.Align.FILL)
            pane.set_valign(Gtk.Align.FILL)
            pane.set_size_request(1, 1)
            picture = Gtk.Picture()
            picture.set_can_shrink(True)
            picture.set_hexpand(True)
            picture.set_vexpand(True)
            picture.set_halign(Gtk.Align.FILL)
            picture.set_valign(Gtk.Align.FILL)
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            pane.set_child(picture)
            placeholder = Gtk.Label(label=f"{index + 1}\n\nНет видеопотока")
            placeholder.add_css_class("muted")
            pane.add_overlay(placeholder)
            caption = Gtk.Label(label="")
            caption.set_halign(Gtk.Align.START)
            caption.set_valign(Gtk.Align.END)
            caption.set_margin_start(10)
            caption.set_margin_bottom(8)
            caption.add_css_class("tile-caption")
            pane.add_overlay(caption)
            gesture = Gtk.GestureClick()
            gesture.connect("released", lambda _gesture, presses, _x, _y, i=index:
                            self._on_video_tile_click(i, presses))
            pane.add_controller(gesture)
            context_gesture = Gtk.GestureClick()
            context_gesture.set_button(3)
            context_gesture.connect("released", lambda _g, _n, x, y, i=index, p=pane: self._show_slot_menu(i, p, x, y))
            pane.add_controller(context_gesture)
            source = Gtk.DragSource()
            source.set_actions(Gdk.DragAction.MOVE)
            source.connect("prepare", lambda _source, _x, _y, i=index:
                           Gdk.ContentProvider.new_for_value(f"slot:{id(self)}:{i}"))
            pane.add_controller(source)
            target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
            target.connect("drop", self._drop_camera, index)
            pane.add_controller(target)
            pane.set_tooltip_text(
                "Двойной клик — развернуть/вернуть видео; перетащите, чтобы переместить камеру")
            scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
            scroll.connect("scroll", lambda _c, _dx, dy, i=index: self._digital_zoom(i, dy))
            pane.add_controller(scroll)
            zoom_drag = Gtk.GestureDrag()
            zoom_drag.connect("drag-end", lambda _g, x, y, dx, dy, i=index, p=pane:
                self._digital_zoom_region(i, x, y, dx, dy, p.get_width(), p.get_height()))
            pane.add_controller(zoom_drag)
            aspect_frame.set_child(pane)
            col, row, colspan, rowspan = cells[index]
            self.camera_grid.attach(aspect_frame, col, row, colspan, rowspan)
            self.video_slots.append({"pane": pane, "aspect_frame": aspect_frame,
                                     "aspect_ratio": aspect_ratio, "picture": picture, "placeholder": placeholder,
                                     "caption": caption, "device": None})
        for index, state in self.streams.items():
            if index >= self.layout_count:
                continue
            slot = self.video_slots[index]
            slot["device"] = state["device"]
            slot["caption"].set_text(
                f"● LIVE   {state['device']['name']}  ·  {state['device']['host']} CH {state['device']['channel']}")
            slot["placeholder"].set_visible(False)
            slot["picture"].set_paintable(paintables.get(index))
        for slot, entry in list(getattr(self, "slot_resources", {}).items()):
            if slot < self.layout_count:
                self._place_resource(slot, entry)
            else:
                self.slot_resources.pop(slot, None)
        self._select_slot(min(getattr(self, "selected_slot", 0), self.layout_count - 1))

    def _on_video_tile_click(self, index, presses):
        self._select_slot(index)
        if presses == 2:
            self._toggle_slot_maximize(index)

    def _toggle_slot_maximize(self, index):
        if not hasattr(self, "camera_area"):
            return
        if self.maximized_slot == index:
            self._restore_maximized_slot(restart_stream=True)
            return
        if index not in self.streams:
            return
        if self.maximized_slot is not None:
            self._restore_maximized_slot()
        slot = self.video_slots[index]
        self.camera_grid.remove(slot["aspect_frame"])
        slot["aspect_frame"].set_hexpand(True)
        slot["aspect_frame"].set_vexpand(False)
        slot["aspect_frame"].set_halign(Gtk.Align.CENTER)
        slot["aspect_frame"].set_valign(Gtk.Align.CENTER)
        slot["pane"].set_hexpand(True)
        slot["pane"].set_vexpand(True)
        self.camera_scroll.set_visible(False)
        self.camera_area.add_overlay(slot["aspect_frame"])
        self.maximized_slot = index
        self._fit_maximized_slot()
        state = self.streams[index]
        if state["device"]["protocol"] == "hikvision" and not state["playback"]:
            desired = self._desired_stream(index)
            if desired != state["device"]["stream"]:
                self._start_preview(index, state["base_device"], state["credentials"],
                                    stream_override=desired)

    def _restore_maximized_slot(self, restart_stream=False):
        index = getattr(self, "maximized_slot", None)
        if index is None:
            return
        slot = self.video_slots[index]
        self.camera_area.remove_overlay(slot["aspect_frame"])
        slot["aspect_frame"].set_size_request(-1, -1)
        slot["aspect_frame"].set_hexpand(True)
        slot["aspect_frame"].set_vexpand(False)
        slot["aspect_frame"].set_halign(Gtk.Align.FILL)
        slot["aspect_frame"].set_valign(Gtk.Align.FILL)
        slot["pane"].set_vexpand(True)
        col, row, colspan, rowspan = self._current_cells()[index]
        self.camera_grid.attach(slot["aspect_frame"], col, row, colspan, rowspan)
        self.camera_scroll.set_visible(True)
        self.maximized_slot = None
        state = self.streams.get(index)
        if (restart_stream and state and state["device"]["protocol"] == "hikvision"
                and not state["playback"]):
            desired = self._desired_stream(index)
            if desired != state["device"]["stream"]:
                self._start_preview(index, state["base_device"], state["credentials"],
                                    stream_override=desired)

    def _fit_maximized_slot(self, *_args):
        index = getattr(self, "maximized_slot", None)
        if index is None or index >= len(self.video_slots):
            return
        width = self.camera_area.get_width()
        height = self.camera_area.get_height()
        if width <= 0 or height <= 0:
            return
        slot = self.video_slots[index]
        aspect_ratio = slot["aspect_ratio"]
        fitted_width = min(width, int(height * aspect_ratio))
        fitted_height = int(fitted_width / aspect_ratio)
        if fitted_width > 0 and fitted_height > 0:
            slot["aspect_frame"].set_size_request(fitted_width, fitted_height)

    def _refresh_thumbnail_streams(self):
        for index, state in list(self.streams.items()):
            if state["device"]["protocol"] != "hikvision" or state["playback"]:
                continue
            desired_stream = self._desired_stream(index)
            if state["device"]["stream"] != desired_stream:
                self._start_preview(index, state["base_device"], state["credentials"],
                                    stream_override=desired_stream)

    def _select_slot(self, index):
        if not self.video_slots or index >= len(self.video_slots):
            return
        for slot in self.video_slots:
            slot["pane"].remove_css_class("selected")
        changed = getattr(self, "selected_slot", None) != index
        self.selected_slot = index
        self.video_slots[index]["pane"].add_css_class("selected")
        self._update_stream_count()
        if changed and hasattr(self, "timeline"):
            self._load_timeline_segments()
        return True

    def _show_slot_menu(self, index, pane, x, y):
        self._select_slot(index)
        popover = Gtk.Popover()
        popover.set_parent(pane)
        popover.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        options = (("Снимок", self.capture_selected), ("Запись начать/остановить", self.toggle_recording),
                   ("Найти записи камеры", lambda: self._open_archive_for_device(
                       self.streams[index]["device"] if index in self.streams else None)),
                   ("Мгновенный повтор · 30 с", lambda: self.instant_playback(30)),
                   ("Мгновенный повтор · 5 мин", lambda: self.instant_playback(300)),
                   ("Цифровой зум +", lambda: self._digital_zoom(index, -1)),
                   ("Цифровой зум −", lambda: self._digital_zoom(index, 1)),
                   ("Сбросить цифровой зум", lambda: self._reset_digital_zoom(index)),
                   ("Поток · основной", lambda: self.set_selected_stream("main")),
                   ("Поток · дополнительный", lambda: self.set_selected_stream("sub")),
                   ("Поток · третий", lambda: self.set_selected_stream("third")),
                   ("Сведения о камере", self.show_selected_stream_info),
                   ("Остановить камеру", lambda: self._stop_stream(index)))
        for title, callback in options:
            button = Gtk.Button(label=title)
            button.set_has_frame(False)
            button.connect("clicked", lambda _b, cb=callback, menu=popover: (menu.popdown(), cb()))
            actions.append(button)
        popover.set_child(actions)
        popover.connect("closed", lambda menu: menu.unparent())
        popover.popup()

    def _show_camera_menu(self, device, widget, x, y):
        popover = Gtk.Popover()
        popover.set_parent(widget)
        popover.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        mode = getattr(self, "stream_mode", layouts.STREAM_AUTO)

        def mark(title, value):
            return ("✓ " if mode == value else "    ") + title

        options = (
            ("Живой просмотр", lambda: self.open_live_dialog(device)),
            ("Открыть в выбранном окне", lambda: self.open_live_dialog(device, self.selected_slot)),
            ("Изменить название камеры", lambda: self.rename_camera_dialog(device)),
            ("Состояние камеры", lambda: self.show_camera_status(device)),
            ("Удалённая настройка (веб-интерфейс)", lambda: self.open_remote_configuration(device)),
            ("Синхронизировать время", lambda: self.sync_device_time(device)),
            ("Найти записи камеры", lambda: self._open_archive_for_device(device)),
            ("Резервная запись на ПК…", lambda: self.open_system("devices", f"recording:{device['id']}")),
            (mark("Поток: основной", layouts.STREAM_MAIN), lambda: self._set_stream_mode(layouts.STREAM_MAIN)),
            (mark("Поток: дополнительный", layouts.STREAM_SUB), lambda: self._set_stream_mode(layouts.STREAM_SUB)),
            (mark("Поток: третий", layouts.STREAM_THIRD), lambda: self._set_stream_mode(layouts.STREAM_THIRD)),
            (mark("Поток: автоматически", layouts.STREAM_AUTO), lambda: self._set_stream_mode(layouts.STREAM_AUTO)),
        )
        for title, callback in options:
            button = Gtk.Button(label=title)
            button.set_has_frame(False)
            button.set_halign(Gtk.Align.START)
            button.connect("clicked", lambda _button, cb=callback, menu=popover:
                           (menu.popdown(), cb()))
            actions.append(button)
        popover.set_child(actions)
        popover.connect("closed", lambda menu: menu.unparent())
        popover.popup()

    def rename_camera_dialog(self, device):
        def rename(name):
            with database() as db:
                db.execute("UPDATE devices SET name=? WHERE id=?", (name[:120], device["id"]))
            self.refresh()
        self._ask_text("Название камеры", device["name"], rename)

    def show_camera_status(self, device):
        def check(camera, credentials):
            threading.Thread(target=self._camera_status_worker, args=(camera, credentials), daemon=True).start()
        if device["protocol"] != "hikvision":
            self.notify(f"{device['name']}\nПроверка состояния доступна только для Hikvision.")
            return
        self._credentials_dialog("Состояние · " + device["name"], device, check)

    def _camera_status_worker(self, device, credentials):
        started = time.monotonic()
        try:
            info = parse_device_info(self._isapi_request(device, credentials, "/ISAPI/System/deviceInfo"))
            elapsed = int((time.monotonic() - started) * 1000)
            lines = [f"{device['name']}: в сети ({elapsed} мс)"]
            for key, title in (("deviceName", "Имя"), ("model", "Модель"), ("serialNumber", "Серийный номер")):
                if info.get(key):
                    lines.append(f"{title}: {info[key]}")
            message = "\n".join(lines)
        except urllib.error.HTTPError as exc:
            message = f"{device['name']}: устройство ответило HTTP {exc.code}"
        except (urllib.error.URLError, OSError, ET.ParseError) as exc:
            message = f"{device['name']}: недоступно ({getattr(exc, 'reason', exc)})"
        GLib.idle_add(self.notify, message)

    def open_remote_configuration(self, device):
        scheme = "https" if int(device.get("http_port", 80)) == 443 else "http"
        uri = f"{scheme}://{device['host']}:{device.get('http_port', 80)}/"
        try:
            Gtk.show_uri(self, uri, Gdk.CURRENT_TIME)
        except Exception as exc:
            self.notify(f"Не удалось открыть {uri}: {exc}")

    def sync_device_time(self, device):
        if device["protocol"] != "hikvision":
            self.notify("Синхронизация времени доступна только для Hikvision")
            return
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CANCEL,
            text=f"Установить на устройстве «{device['host']}» время этого компьютера?")
        dialog.add_button("Синхронизировать", Gtk.ResponseType.ACCEPT)

        def response(dlg, result):
            dlg.close()
            if result != Gtk.ResponseType.ACCEPT:
                return

            def go(camera, credentials):
                threading.Thread(target=self._sync_time_worker, args=(camera, credentials), daemon=True).start()
            self._credentials_dialog("Синхронизация времени · " + device["name"], device, go)

        dialog.connect("response", response)
        dialog.present()

    def _sync_time_worker(self, device, credentials):
        try:
            self._isapi_request(device, credentials, "/ISAPI/System/time", method="PUT",
                                body=build_time_xml(datetime.now(timezone.utc)))
            message = f"Время на {device['host']} синхронизировано"
        except urllib.error.HTTPError as exc:
            message = f"Устройство отклонило синхронизацию времени (HTTP {exc.code})"
        except (urllib.error.URLError, OSError) as exc:
            message = f"Не удалось связаться с устройством: {getattr(exc, 'reason', exc)}"
        GLib.idle_add(self.notify, message)

    def _open_archive_for_device(self, device):
        if not device:
            self.notify("В этом окне нет активной камеры")
            return
        self._refresh_archive_choices()
        index = next((i for i, item in enumerate(self.devices) if item["id"] == device["id"]), None)
        if index is None:
            self.notify("Устройство больше не находится в каталоге")
            return
        self.archive_device.set_selected(index)
        self._show_module("archive")

    def set_selected_stream(self, stream_type):
        state = self.streams.get(self.selected_slot)
        if not state:
            self.notify("Выберите окно с активной камерой")
            return
        if state["device"].get("protocol") != "hikvision":
            self.notify("Переключение потока доступно для Hikvision RTSP")
            return
        self._start_preview(self.selected_slot, state["base_device"], state["credentials"],
                            stream_override=stream_type)

    def show_selected_stream_info(self):
        state = self.streams.get(self.selected_slot)
        if not state:
            self.notify("Выберите окно с активной камерой")
            return
        device = state["device"]
        self.notify(f"{device['name']}\n{device['host']} · канал {device['channel']}\n"
                    f"Поток: {'дополнительный' if device.get('stream') == 'sub' else 'основной'}\n"
                    f"Адрес RTSP: {device['host']}:{device['rtsp_port']}")

    def _digital_zoom(self, slot, direction):
        state = self.streams.get(slot)
        if not state:
            return True
        current = state.get("digital_zoom", 1.0)
        state["digital_zoom"] = min(8.0, current * 1.2) if direction < 0 else max(1.0, current / 1.2)
        return True

    def _digital_zoom_region(self, slot, x, y, dx, dy, width, height):
        state = self.streams.get(slot)
        if not state or width <= 0 or height <= 0 or abs(dx) < 8 or abs(dy) < 8:
            return
        left, right = sorted((x, x + dx)); top, bottom = sorted((y, y + dy))
        fraction = max(0.125, min(1.0, min((right - left) / width, (bottom - top) / height)))
        state["digital_zoom"] = min(8.0, 1.0 / fraction)
        state["digital_x"] = max(0.0, min(1.0, (left + right) / (2 * width)))
        state["digital_y"] = max(0.0, min(1.0, (top + bottom) / (2 * height)))

    def _reset_digital_zoom(self, slot):
        state = self.streams.get(slot)
        if state:
            state["digital_zoom"] = 1.0
            state["digital_x"] = state["digital_y"] = 0.5

    def _drop_camera(self, _target, value, _x, _y, destination):
        if not isinstance(value, str):
            return False
        parts = value.split(":")
        if len(parts) == 2 and parts[0] == "camera":
            try:
                device_id = int(parts[1])
            except ValueError:
                return False
            device = next((item for item in self.devices if item["id"] == device_id), None)
            if not device:
                self.notify("Камера больше не находится в каталоге")
                return False
            self._select_slot(destination)
            self.open_live_dialog(device, destination)
            return True
        if parts[0] == "door" and len(parts) == 3 and hasattr(self, "slot_resources"):
            self._place_resource(destination, {"kind": "door", "device_id": int(parts[1]), "door": int(parts[2])})
            self._mark_view_dirty()
            return True
        if parts[0] == "web" and len(parts) == 2 and hasattr(self, "slot_resources"):
            import sentinel_views
            page = next((p for p in sentinel_views.load_web_pages() if p["id"] == int(parts[1])), None)
            if page:
                self._place_resource(destination, {"kind": "web", "url": page["url"], "name": page["name"]})
                self._mark_view_dirty()
            return page is not None
        if len(parts) != 3 or parts[0] != "slot":
            return False
        try:
            source_window, source = int(parts[1]), int(parts[2])
        except ValueError:
            return False
        if source_window != id(self) or source == destination:
            return False
        self._move_camera_slot(source, destination)
        return True

    def _move_camera_slot(self, source, destination):
        if source < 0 or destination < 0 or source >= self.layout_count or destination >= self.layout_count:
            return
        source_state = self.streams.get(source)
        target_state = self.streams.get(destination)
        if not source_state:
            return
        source_camera = (source_state["base_device"], source_state["credentials"])
        target_camera = (target_state["base_device"], target_state["credentials"]) if target_state else None
        if hasattr(self, "camera_mapping"):
            self.camera_mapping[source], self.camera_mapping[destination] = self.camera_mapping[destination], self.camera_mapping[source]
            self._save_camera_mapping()
        self._stop_stream(source)
        if target_state:
            self._stop_stream(destination)
        try:
            if target_camera:
                self._start_preview(source, *target_camera)
            self._start_preview(destination, *source_camera)
        except Exception as exc:
            self.notify(f"Не удалось переместить камеру: {exc}")

    def _save_camera_mapping(self):
        saved_window_id = getattr(self, "saved_window_id", None)
        if saved_window_id:
            with database() as db:
                db.execute("UPDATE saved_windows SET mapping=? WHERE id=?",
                           (json.dumps(self.camera_mapping), saved_window_id))

    def _clear_box(self, box):
        child = box.get_first_child()
        while child:
            following = child.get_next_sibling()
            box.remove(child)
            child = following

    def add_device(self):
        dialog = Gtk.Dialog(title="Добавить устройство", transient_for=self, modal=True)
        dialog.set_resizable(True)
        dialog.set_default_size(*self._initial_window_size(520, 820))
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        add_button = dialog.add_button("Проверить и добавить", Gtk.ResponseType.ACCEPT)
        add_button.add_css_class("suggested-action")
        grid = Gtk.Grid(column_spacing=12, row_spacing=11)
        grid.set_margin_top(20)
        grid.set_margin_bottom(18)
        grid.set_margin_start(20)
        grid.set_margin_end(20)
        fields = {}
        for row, (key, title, value) in enumerate((
            ("name", "Название", ""), ("host", "IP-адрес или DNS", ""),
            ("protocol", "Протокол", "Hikvision RTSP"), ("sdk_port", "SDK-порт", "8000"),
            ("http_port", "HTTP-порт", "80"), ("rtsp_port", "RTSP-порт", "554"),
            ("stream", "Поток", "Основной"), ("username", "Имя пользователя", ""),
            ("password", "Пароль устройства", ""))):
            grid.attach(make_label(title, "muted"), 0, row, 1, 1)
            if key == "protocol":
                widget = Gtk.DropDown.new_from_strings(["Hikvision RTSP", "Пользовательский RTSP"])
            elif key == "stream":
                widget = Gtk.DropDown.new_from_strings(["Основной", "Дополнительный"])
            elif key == "password":
                widget = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="Пароль")
            elif key == "username":
                widget = Gtk.Entry(placeholder_text="Имя пользователя устройства")
            elif key in ("sdk_port", "http_port", "rtsp_port"):
                maximum = 65535
                widget = Gtk.SpinButton.new_with_range(1, maximum, 1)
                widget.set_value(int(value))
            else:
                placeholder = "Например, Камера у входа" if key == "name" else "192.168.1.64"
                widget = Gtk.Entry(text=value, placeholder_text=placeholder)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)
            fields[key] = widget

        extra_row = len(fields)
        grid.attach(make_label("Режим добавления", "muted"), 0, extra_row, 1, 1)
        fields["mode"] = Gtk.DropDown.new_from_strings(["Один адрес", "Диапазон IP"])
        fields["mode"].set_hexpand(True)
        grid.attach(fields["mode"], 1, extra_row, 1, 1)
        end_label = make_label("Конечный IP диапазона", "muted")
        fields["end_host"] = Gtk.Entry(placeholder_text="192.168.1.80")
        fields["end_host"].set_hexpand(True)
        grid.attach(end_label, 0, extra_row + 1, 1, 1)
        grid.attach(fields["end_host"], 1, extra_row + 1, 1, 1)
        grid.attach(make_label("Группа", "muted"), 0, extra_row + 2, 1, 1)
        fields["group"] = Gtk.Entry(placeholder_text="Необязательно; будет создана, если её нет")
        fields["group"].set_hexpand(True)
        grid.attach(fields["group"], 1, extra_row + 2, 1, 1)

        fields["manual"] = Gtk.CheckButton(
            label="Не определять каналы автоматически (указать вручную)")
        grid.attach(fields["manual"], 0, extra_row + 3, 2, 1)

        def update_mode(*_args):
            is_range = fields["mode"].get_selected() == 1
            end_label.set_visible(is_range)
            fields["end_host"].set_visible(is_range)

        fields["mode"].connect("notify::selected", update_mode)
        update_mode()

        manual_channels = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        manual_channels.append(make_label("Первый канал", "muted"))
        fields["channel"] = Gtk.SpinButton.new_with_range(1, 512, 1)
        fields["channel"].set_value(1)
        manual_channels.append(fields["channel"])
        manual_channels.append(make_label("Каналов", "muted"))
        fields["channel_count"] = Gtk.SpinButton.new_with_range(1, 256, 1)
        fields["channel_count"].set_value(1)
        manual_channels.append(fields["channel_count"])
        manual_channels.set_hexpand(True)
        manual_channels.set_halign(Gtk.Align.FILL)
        manual_row = len(fields)
        grid.attach(manual_channels, 0, manual_row, 2, 1)
        channel_status = make_label("", "muted")
        channel_status.set_wrap(True)
        channel_status.set_max_width_chars(48)
        channel_status.set_width_chars(10)
        channel_status.set_selectable(True)
        grid.attach(channel_status, 0, manual_row + 1, 2, 1)

        def update_channel_fields(*_args):
            is_hikvision = fields["protocol"].get_selected() == 0
            manual = fields["manual"].get_active()
            manual_channels.set_visible(not is_hikvision or manual)
            fields["manual"].set_visible(is_hikvision)
            channel_status.set_text(
                "Каналы будут определены автоматически; после подключения покажу их количество."
                if is_hikvision and not manual else "Укажите диапазон каналов.")

        fields["protocol"].connect("notify::selected", update_channel_fields)
        fields["manual"].connect("toggled", update_channel_fields)
        update_channel_fields()
        dialog.get_content_area().append(grid)

        def response(dlg, result):
            if result != Gtk.ResponseType.ACCEPT:
                dlg.close()
                return
            name = fields["name"].get_text().strip()
            host = fields["host"].get_text().strip()
            protocol = "hikvision" if fields["protocol"].get_selected() == 0 else "custom"
            username = fields["username"].get_text().strip()
            password = fields["password"].get_text()
            if not name or not host:
                self.notify("Название и адрес обязательны")
                return
            if protocol == "hikvision" and not username:
                self.notify("Введите имя пользователя устройства, чтобы определить каналы")
                return
            first_channel = fields["channel"].get_value_as_int()
            channel_count = fields["channel_count"].get_value_as_int()
            if protocol == "custom" and first_channel + channel_count - 1 > 512:
                self.notify("Последний канал должен быть не выше 512")
                return

            hosts = [host]
            if fields["mode"].get_selected() == 1:
                try:
                    hosts = devmgmt.expand_ip_segment(host, fields["end_host"].get_text())
                except ValueError as exc:
                    self.notify(str(exc))
                    return
                if protocol != "hikvision":
                    self.notify("Диапазон IP поддерживается только для Hikvision")
                    return
            group_name = fields["group"].get_text().strip()
            device = {
                "name": name,
                "group_name": group_name,
                "host": host,
                "protocol": protocol,
                "sdk_port": fields["sdk_port"].get_value_as_int(),
                "http_port": fields["http_port"].get_value_as_int(),
                "rtsp_port": fields["rtsp_port"].get_value_as_int(),
                "stream": "main" if fields["stream"].get_selected() == 0 else "sub",
                "manual": fields["manual"].get_active() and protocol == "hikvision",
                "first_channel": first_channel,
                "manual_channel_count": channel_count,
            }
            credentials = {"username": username, "password": password}
            add_button.set_sensitive(False)
            channel_status.set_text(
                "Подключение и определение каналов…"
                if protocol == "hikvision" else "Добавление RTSP-устройства…")
            if len(hosts) > 1:
                channel_status.set_text(f"Проверка адресов: 0 из {len(hosts)}…")
                threading.Thread(
                    target=self._probe_range_for_add,
                    args=(dlg, add_button, channel_status, device, credentials, hosts),
                    daemon=True,
                ).start()
                return
            threading.Thread(
                target=self._probe_device_for_add,
                args=(dlg, add_button, channel_status, device, credentials),
                daemon=True,
            ).start()

        dialog.connect("response", response)
        dialog.present()

    def _probe_device_for_add(self, dialog, add_button, status, device, credentials):
        try:
            if device["protocol"] == "hikvision" and not device.get("manual"):
                channels = self._discover_hikvision_channels(device, credentials)
                device.update(self._probe_device_extras(device, credentials))
            else:
                first = device["first_channel"]
                channels = list(range(first, first + device["manual_channel_count"]))
            GLib.idle_add(self._save_probed_device, dialog, add_button, status,
                          device, credentials, channels)
        except Exception as exc:
            GLib.idle_add(self._device_probe_failed, add_button, status, str(exc))

    def _probe_device_extras(self, device, credentials):
        """Имя, модель, серийный номер и названия каналов с устройства; ошибки не критичны."""
        extras = {"channel_names": {}}
        try:
            info = parse_device_info(self._isapi_request(device, credentials, "/ISAPI/System/deviceInfo"))
            extras["device_name"] = info.get("deviceName", "")
            extras["device_model"] = info.get("model", "")
            extras["serial_no"] = info.get("serialNumber", "")
        except Exception:
            pass
        for path, tag in (("/ISAPI/ContentMgmt/InputProxy/channels", "InputProxyChannel"),
                          ("/ISAPI/System/Video/inputs/channels", "VideoInputChannel")):
            try:
                names = parse_channel_names(self._isapi_request(device, credentials, path), tag)
            except Exception:
                continue
            if names:
                extras["channel_names"] = names
                break
        return extras

    def _probe_range_for_add(self, dialog, add_button, status, template, credentials, hosts):
        from concurrent.futures import ThreadPoolExecutor

        results = {"added": 0, "failed": 0, "done": 0}
        lock = threading.Lock()

        def one(host):
            device = dict(template, host=host, name=f"{template['name']} {host}")
            try:
                channels = self._discover_hikvision_channels(device, credentials)
                device.update(self._probe_device_extras(device, credentials))
                self._store_probed_device(device, credentials, channels)
                outcome = "added"
            except Exception:
                outcome = "failed"
            with lock:
                results[outcome] += 1
                results["done"] += 1
                done = results["done"]
            GLib.idle_add(status.set_text, f"Проверка адресов: {done} из {len(hosts)}…")

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(one, hosts))
        GLib.idle_add(self._range_probe_finished, dialog, add_button, status, results)

    def _range_probe_finished(self, dialog, add_button, status, results):
        self.refresh()
        if results["added"]:
            dialog.close()
            self.notify(f"Добавлено устройств: {results['added']}. Не отвечают или отклонили вход: {results['failed']}")
        else:
            self._device_probe_failed(
                add_button, status, "Ни одно устройство в диапазоне не подключилось. Проверьте адреса и учётные данные.")
        return GLib.SOURCE_REMOVE

    def _discover_hikvision_channels(self, device, credentials):
        endpoints: tuple[tuple[str, Callable[[bytes], list[int]]], ...] = (
            ("/ISAPI/ContentMgmt/InputProxy/channels",
             lambda payload: parse_channel_ids(payload, "InputProxyChannel")),
            ("/ISAPI/System/Video/inputs/channels",
             lambda payload: parse_channel_ids(payload, "VideoInputChannel")),
            ("/ISAPI/Streaming/channels", parse_streaming_channel_ids),
        )
        endpoint_errors = []
        discovered = set()
        forbidden_endpoints = []
        for path, parser in endpoints:
            try:
                payload = self._isapi_request(device, credentials, path)
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 404, 405, 501):
                    endpoint_errors.append(f"{path}: HTTP {exc.code}")
                    if exc.code == 403:
                        forbidden_endpoints.append(path)
                    continue
                if exc.code == 401:
                    raise RuntimeError(
                        "HTTP доступен, но устройство отклонило учётные данные (HTTP 401). "
                        "Проверьте логин, пароль и разрешённый тип аутентификации."
                    ) from exc
                raise RuntimeError(
                    f"ISAPI вернул HTTP {exc.code} при запросе списка каналов."
                ) from exc
            except urllib.error.URLError as exc:
                reason = str(exc.reason)
                hint = ""
                if "timed out" in reason:
                    hint = (f" Проверьте адрес и HTTP-порт ({device.get('http_port', 80)}): "
                            "для доступа из интернета на роутере должен быть проброшен порт веб-интерфейса.")
                elif "refused" in reason.lower():
                    hint = " Порт закрыт: проверьте HTTP-порт устройства."
                raise RuntimeError(f"Не удалось подключиться к устройству: {reason}.{hint}") from exc

            try:
                channel_ids = parser(payload)
            except ET.ParseError as exc:
                endpoint_errors.append(f"{path}: некорректный XML ({exc})")
                continue

            if channel_ids:
                discovered.update(channel_ids)
            else:
                endpoint_errors.append(f"{path}: устройство не вернуло каналы")

        if discovered:
            return sorted(discovered)
        details = "; ".join(endpoint_errors)
        if forbidden_endpoints:
            raise RuntimeError(
                "HTTP-порт доступен, но ISAPI запрещает чтение списка каналов (HTTP 403). "
                "Проверьте, включён ли ISAPI в настройках регистратора и есть ли у пользователя "
                "права просмотра каналов. Запрошенные пути: "
                + ", ".join(forbidden_endpoints)
                + f". Ответы остальных путей: {details}"
            )
        raise RuntimeError(
            "HTTP-порт отвечает, но ISAPI Hikvision там не найден (HTTP 404 на всех путях): похоже, на этом "
            "порту работает не регистратор (роутер или другой веб-сервер) либо устройство без ISAPI. "
            "Проверьте HTTP-порт или отметьте «Не определять каналы автоматически» и укажите каналы вручную. "
            f"Подробности: {details}")

    def _store_probed_device(self, device, credentials, channels):
        """Записать устройство и его каналы; безопасно вызывать из рабочего потока."""
        if not channels or len(channels) > 512:
            raise ValueError("Устройство вернуло недопустимое число каналов")
        group_id = add_group(device["group_name"]) if device.get("group_name") else None
        names = device.get("channel_names") or {}
        base = device["name"]
        device_ids = []
        with database() as db:
            for channel in channels:
                title = devmgmt.channel_title(base, channel, names, single=len(channels) == 1)
                cursor = db.execute(
                    """INSERT INTO devices
                    (name,host,sdk_port,http_port,rtsp_port,channel,stream,protocol,
                     group_id,serial_no,device_model,host_name,category,door_count,firmware,status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, device["host"], device["sdk_port"], device["http_port"],
                     device["rtsp_port"], channel, device["stream"], device["protocol"],
                     group_id, device.get("serial_no", ""), device.get("device_model", ""),
                     device.get("device_name") or base, device.get("category", "video"),
                     int(device.get("door_count") or 0), device.get("firmware", ""),
                     "online" if device["protocol"] == "hikvision" else ""))
                device_ids.append(cursor.lastrowid)
        if device["protocol"] == "hikvision":
            for device_id in device_ids:
                self.session_credentials[device_id] = dict(credentials)
            try:
                save_credentials(device, credentials)
            except (CredentialStoreError, sqlite3.Error) as exc:
                GLib.idle_add(self.notify, f"Устройство добавлено, но пароль не сохранён: {exc}")
        return device_ids

    def _save_probed_device(self, dialog, add_button, status, device, credentials, channels):
        try:
            self._store_probed_device(device, credentials, channels)
        except ValueError as exc:
            return self._device_probe_failed(add_button, status, str(exc))
        except sqlite3.IntegrityError:
            return self._device_probe_failed(add_button, status, "Один или несколько каналов уже добавлены")
        self.refresh()
        dialog.close()
        self.notify(f"Подключено. Найдено каналов: {len(channels)}")
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _device_probe_failed(add_button, status, message):
        status.set_text(message)
        add_button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def delete_device(self, device_id):
        self.delete_devices([device_id])

    def open_live_dialog(self, device, slot_index=None):
        def start_in_target_slot(camera, credentials):
            self.session_credentials[camera["id"]] = dict(credentials)
            if slot_index is None:
                self.start_live(camera, credentials)
            else:
                self._start_preview(slot_index, camera, credentials)

        try:
            saved = self._get_device_credentials(device)
        except (CredentialStoreError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.notify(f"Не удалось прочитать сохранённые учётные данные: {exc}")
            saved = None
        if saved:
            try:
                start_in_target_slot(device, saved)
            except Exception as exc:
                self.notify(str(exc))
            return
        self._credentials_dialog("Live view · " + device["name"], device, start_in_target_slot)

    def _get_device_credentials(self, device):
        credentials = self.session_credentials.get(device["id"])
        if not credentials:
            credentials = load_saved_credentials(device)
            if credentials:
                self.session_credentials[device["id"]] = dict(credentials)
        return credentials

    def start_all_channels(self, *_args):
        self.devices = load_devices()
        if not self.devices:
            self.notify("Сначала добавьте камеры в раздел «Устройства»")
            return
        targets = self.devices[:64]
        if len(self.devices) > 64:
            self.notify(f"Найдено {len(self.devices)} каналов; эта раскладка поддерживает до 64. "
                        "Будут запущены первые 64.")
        if len(targets) > self.layout_count:
            new_count = next((count for count in (1, 4, 9, 16, 25, 36, 49, 64)
                              if count >= len(targets)), 64)
            self._set_layout(None, new_count)

        progress = {"index": 0, "started": 0, "errors": []}
        credentials_cache = {}

        def credential_key(device):
            return device["host"], int(device.get("http_port", 80))

        def start_camera(slot_index, device, credentials):
            self.session_credentials[device["id"]] = dict(credentials)
            try:
                self._start_preview(slot_index, device, credentials)
                self.streams[slot_index]["frame_interval"] = 0.25
                progress["started"] += 1
            except Exception as exc:
                progress["errors"].append(f"{device['name']}: {exc}")

        def pump():
            if progress["index"] >= len(targets):
                summary = f"Запущено каналов: {progress['started']} из {len(targets)}"
                if progress["errors"]:
                    summary += f". Ошибок: {len(progress['errors'])}. {progress['errors'][0]}"
                self.notify(summary)
                return GLib.SOURCE_REMOVE
            slot_index = progress["index"]
            device = targets[slot_index]
            try:
                credentials = self.session_credentials.get(device["id"])
                key = credential_key(device)
                if not credentials:
                    credentials = credentials_cache.get(key)
                    if credentials is None:
                        credentials = load_saved_credentials(device)
                        if credentials:
                            credentials_cache[key] = credentials
                    if credentials:
                        self.session_credentials[device["id"]] = dict(credentials)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                credentials = None
            if credentials:
                start_camera(slot_index, device, credentials)
                progress["index"] += 1
                return GLib.SOURCE_CONTINUE

            def start_after_credentials(camera, entered_credentials):
                credentials_cache[credential_key(camera)] = dict(entered_credentials)
                start_camera(slot_index, camera, entered_credentials)
                progress["index"] += 1
                GLib.timeout_add(120, pump)

            self._credentials_dialog("Live view · " + device["name"], device, start_after_credentials)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(120, pump)

    def _credentials_dialog(self, title, device, callback, playback=None):
        try:
            saved = self._get_device_credentials(device)
        except (CredentialStoreError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.notify(str(exc))
            saved = None
        if saved:
            credentials = dict(saved)
            if playback:
                credentials.update({"start": playback[0], "end": playback[1]})
            try:
                callback(device, credentials)
            except Exception as exc:
                self.notify(str(exc))
            return
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.set_resizable(True)
        dialog.set_default_size(*self._initial_window_size(460, 300))
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Открыть поток", Gtk.ResponseType.ACCEPT)
        dialog.get_widget_for_response(Gtk.ResponseType.ACCEPT).add_css_class("suggested-action")
        grid = Gtk.Grid(column_spacing=12, row_spacing=11)
        grid.set_margin_top(20)
        grid.set_margin_bottom(20)
        grid.set_margin_start(20)
        grid.set_margin_end(20)
        username = Gtk.Entry(placeholder_text="Имя пользователя")
        password = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="Пароль")
        self._field(grid, "Устройство", make_label(f"{device['host']} · CH {device['channel']}"), 0)
        self._field(grid, "Имя пользователя", username, 1)
        self._field(grid, "Пароль", password, 2)
        custom = None
        next_row = 3
        if device["protocol"] == "custom":
            custom = Gtk.Entry(placeholder_text="rtsp://адрес/путь")
            self._field(grid, "RTSP URL", custom, next_row)
            next_row += 1
        if playback:
            start = Gtk.Entry(text=playback[0])
            end = Gtk.Entry(text=playback[1])
            self._field(grid, "Начало", start, next_row)
            self._field(grid, "Конец", end, next_row + 1)
            next_row += 2
        dialog.get_content_area().append(grid)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    extra = {"username": username.get_text(), "password": password.get_text()}
                    if custom:
                        extra["rtsp_url"] = custom.get_text()
                    if playback:
                        extra["start"] = start.get_text()
                        extra["end"] = end.get_text()
                    callback(device, extra)
                    try:
                        save_credentials(device, {key: value for key, value in extra.items()
                                                  if key in ("username", "password", "rtsp_url")})
                    except (CredentialStoreError, sqlite3.Error) as exc:
                        self.notify(f"Поток открыт, но пароль не сохранён: {exc}")
                    dlg.close()
                except Exception as exc:
                    self.notify(str(exc))
                    return
            else:
                dlg.close()
        dialog.connect("response", response)
        dialog.present()

    def _stream_url(self, device, credentials, playback=None):
        user = urllib.parse.quote(credentials.get("username", ""), safe="")
        password = urllib.parse.quote(credentials.get("password", ""), safe="")
        if not user:
            raise ValueError("Введите имя пользователя устройства")
        auth = user + ":" + password + "@"
        if device["protocol"] == "hikvision":
            stream_num = int(device["channel"]) * 100 + layouts.STREAM_SUFFIX.get(device["stream"], 1)
            if playback:
                start = self._utc_timestamp(playback[0])
                end = self._utc_timestamp(playback[1])
                if end <= start:
                    raise ValueError("Время окончания должно быть позже начала")
                path = f"Streaming/tracks/{stream_num}?" + urllib.parse.urlencode({"starttime": start, "endtime": end})
            else:
                path = f"Streaming/Channels/{stream_num}"
            return f"rtsp://{auth}{device['host']}:{device['rtsp_port']}/{path}"
        parsed = urllib.parse.urlparse(credentials.get("rtsp_url", ""))
        if parsed.scheme != "rtsp" or parsed.hostname != device["host"]:
            raise ValueError("RTSP URL должна использовать адрес добавленного устройства")
        if playback:
            start = self._utc_timestamp(playback[0])
            end = self._utc_timestamp(playback[1])
            if end <= start:
                raise ValueError("Время окончания должно быть позже начала")
            query = urllib.parse.parse_qs(parsed.query)
            query.update({"starttime": [start], "endtime": [end]})
            parsed = parsed._replace(query=urllib.parse.urlencode(query, doseq=True))
        return parsed._replace(netloc=auth + parsed.netloc).geturl()

    def _utc_timestamp(self, value):
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _ptz_press(self, direction):
        if direction == "stop":
            self._send_ptz(0, 0, 0)
            return
        ptz_speed = getattr(self, "ptz_speed", None)
        speed = int(ptz_speed.get_value() * 14) if ptz_speed is not None else 56
        diagonal = int(speed * 0.7)
        values = {"up": (0, speed, 0), "down": (0, -speed, 0), "left": (-speed, 0, 0),
                  "right": (speed, 0, 0), "up_left": (-diagonal, diagonal, 0),
                  "up_right": (diagonal, diagonal, 0), "down_left": (-diagonal, -diagonal, 0),
                  "down_right": (diagonal, -diagonal, 0), "zoom_in": (0, 0, speed), "zoom_out": (0, 0, -speed)}
        self._send_ptz(*values[direction])

    def _ptz_release(self, _direction):
        self._send_ptz(0, 0, 0)

    def _send_ptz(self, pan, tilt, zoom):
        state = self.streams.get(self.selected_slot)
        if not state:
            self.notify("Сначала выберите активный Hikvision PTZ-поток")
            return
        device = state["device"]
        creds = state["credentials"]
        url = f"http://{device['host']}:{device.get('http_port', 80)}/ISAPI/PTZCtrl/channels/{device['channel']}/continuous"
        xml = f"<PTZData><pan>{pan}</pan><tilt>{tilt}</tilt><zoom>{zoom}</zoom></PTZData>".encode()

        def request():
            try:
                opener = authenticated_opener(
                    url, creds.get("username", ""), creds.get("password", ""))
                req = urllib.request.Request(url, data=xml, method="PUT", headers={"Content-Type": "application/xml"})
                with opener.open(req, timeout=3) as response:
                    response.read(512)
            except Exception as exc:
                GLib.idle_add(self.notify, f"PTZ: {exc}")
        threading.Thread(target=request, daemon=True).start()

    def open_ptz_tools(self, *_args):
        state = self.streams.get(self.selected_slot)
        if not state or state["device"].get("protocol") != "hikvision":
            self.notify("Выберите активную Hikvision PTZ-камеру")
            return
        device, credentials = state["device"], state["credentials"]
        channel = device["channel"]
        dialog = Gtk.Dialog(title=f"PTZ · {device['name']}", transient_for=self, modal=True)
        dialog.add_button("Закрыть", Gtk.ResponseType.CLOSE)
        notebook = Gtk.Notebook()
        notebook.set_margin_top(10); notebook.set_margin_bottom(10)
        notebook.set_margin_start(10); notebook.set_margin_end(10)
        preset_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        preset_form = Gtk.Grid(column_spacing=10, row_spacing=8)
        preset_id = Gtk.SpinButton.new_with_range(1, 300, 1)
        preset_name = Gtk.Entry(placeholder_text="Имя новой/изменяемой позиции")
        preset_list = Gtk.DropDown.new_from_strings(["Обновите список пресетов"])
        self._field(preset_form, "Номер пресета", preset_id, 0)
        self._field(preset_form, "Название", preset_name, 1)
        self._field(preset_form, "Список устройства", preset_list, 2)
        preset_page.append(preset_form)
        preset_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for title, callback in (("Обновить", lambda: load_presets()),
                                ("Сохранить текущую позицию", lambda: save_preset()),
                                ("Перейти", lambda: goto_preset())):
            preset_actions.append(self._button(title, callback))
        preset_page.append(preset_actions)
        notebook.append_page(preset_page, make_label("Пресеты"))

        patrol_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        patrol_form = Gtk.Grid(column_spacing=10, row_spacing=8)
        patrol_id = Gtk.SpinButton.new_with_range(1, 16, 1)
        patrol_name = Gtk.Entry(text="Маршрут")
        patrol_presets = Gtk.Entry(placeholder_text="Например: 1,2,3")
        patrol_speed = Gtk.SpinButton.new_with_range(1, 40, 1); patrol_speed.set_value(20)
        patrol_delay = Gtk.SpinButton.new_with_range(1, 30, 1); patrol_delay.set_value(5)
        self._field(patrol_form, "Номер маршрута", patrol_id, 0)
        self._field(patrol_form, "Название", patrol_name, 1)
        self._field(patrol_form, "Пресеты по порядку", patrol_presets, 2)
        self._field(patrol_form, "Скорость", patrol_speed, 3)
        self._field(patrol_form, "Пауза, сек", patrol_delay, 4)
        patrol_page.append(patrol_form)
        patrol_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for title, callback in (("Сохранить маршрут", lambda: save_patrol()),
                                ("Запустить", lambda: ptz_command(f"/patrols/{patrol_id.get_value_as_int()}/start")),
                                ("Остановить", lambda: ptz_command(f"/patrols/{patrol_id.get_value_as_int()}/stop"))):
            patrol_actions.append(self._button(title, callback))
        patrol_page.append(patrol_actions)
        notebook.append_page(patrol_page, make_label("Маршруты"))

        pattern_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pattern_form = Gtk.Grid(column_spacing=10, row_spacing=8)
        pattern_id = Gtk.SpinButton.new_with_range(1, 4, 1)
        self._field(pattern_form, "Номер паттерна", pattern_id, 0)
        pattern_page.append(pattern_form)
        pattern_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for title, suffix in (("Начать запись", "recordstart"), ("Остановить запись", "recordstop"),
                              ("Выполнить", "run"), ("Остановить", "stop")):
            pattern_actions.append(self._button(title, lambda s=suffix: ptz_command(f"/patterns/{pattern_id.get_value_as_int()}/{s}")))
        pattern_page.append(pattern_actions)
        notebook.append_page(pattern_page, make_label("Паттерны"))
        dialog.get_content_area().append(notebook)

        prefix = f"/ISAPI/PTZCtrl/channels/{channel}"
        def ptz_command(suffix, method="PUT", body=None, completed=None):
            threading.Thread(target=self._ptz_http_worker,
                args=(device, credentials, prefix + suffix, method, body, completed), daemon=True).start()
        def load_presets():
            def update(payload):
                try:
                    root = ET.fromstring(payload)
                    items = []
                    for node in root.iter():
                        if node.tag.rsplit("}", 1)[-1] == "PTZPreset":
                            values = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in node.iter()}
                            items.append((values.get("id", ""), values.get("presetName", "")))
                    if items:
                        preset_list.set_model(Gtk.StringList.new([f"{number} · {name}" for number, name in items]))
                        preset_list.connect("notify::selected", lambda choice, _p: preset_id.set_value(int(items[choice.get_selected()][0]))
                            if choice.get_selected() < len(items) and items[choice.get_selected()][0].isdigit() else None)
                    else:
                        self.notify("Камера не вернула список пресетов")
                except Exception as exc:
                    self.notify(f"Не удалось разобрать список пресетов: {exc}")
            ptz_command("/presets", method="GET", completed=update)
        def save_preset():
            number = preset_id.get_value_as_int()
            name = html.escape(preset_name.get_text().strip() or f"Preset {number}")
            body = f"<PTZPreset><id>{number}</id><presetName>{name}</presetName></PTZPreset>"
            ptz_command(f"/presets/{number}", method="PUT", body=body,
                        completed=lambda _payload: self.notify(f"Позиция {number} сохранена"))
        def goto_preset():
            number = preset_id.get_value_as_int()
            ptz_command(f"/presets/{number}/goto", method="PUT",
                        completed=lambda _payload: self.notify(f"Камера перешла на позицию {number}"))
        def save_patrol():
            try:
                numbers = [int(value.strip()) for value in patrol_presets.get_text().split(",") if value.strip()]
                if len(numbers) < 2:
                    raise ValueError("В маршруте должны быть минимум два пресета")
            except ValueError as exc:
                self.notify(str(exc)); return
            route_id = patrol_id.get_value_as_int()
            sequences = "".join(f"<PatrolSequence><id>{position}</id><presetID>{preset}</presetID>"
                f"<seqSpeed>{patrol_speed.get_value_as_int()}</seqSpeed><delay>{patrol_delay.get_value_as_int()}</delay></PatrolSequence>"
                for position, preset in enumerate(numbers, 1))
            body = (f"<PTZPatrol><enabled>true</enabled><id>{route_id}</id><patrolName>{html.escape(patrol_name.get_text().strip())}</patrolName>"
                    f"<PatrolSequenceList>{sequences}</PatrolSequenceList></PTZPatrol>")
            ptz_command(f"/patrols/{route_id}", method="PUT", body=body,
                        completed=lambda _payload: self.notify(f"Маршрут {route_id} сохранён"))
        dialog.connect("response", lambda dlg, *_: dlg.close())
        dialog.present()

    def _ptz_http_worker(self, device, credentials, path, method, body, completed):
        try:
            payload = self._isapi_request(device, credentials, path, method, body)
            if completed:
                GLib.idle_add(completed, payload)
            elif method != "GET":
                GLib.idle_add(self.notify, "Команда PTZ отправлена")
        except Exception as exc:
            GLib.idle_add(self.notify, f"PTZ/ISAPI: {exc}")

    def _open_alarm_video(self, event):
        device = next((item for item in self.devices
                       if item["id"] == event.get("device_id")), None)
        if not device:
            self.notify("Устройство события больше не находится в каталоге")
            return
        if device["protocol"] != "hikvision":
            self.notify("Архив тревоги доступен для Hikvision через ISAPI/RTSP")
            return
        try:
            event_time = parse_device_time(event["time"])
        except (KeyError, TypeError, ValueError) as exc:
            self.notify(f"Не удалось определить время события: {exc}")
            return
        start = (event_time - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        end = (event_time + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        playback = (start, end)
        slot = self.selected_slot
        self._show_module("overview")

        def play(camera, credentials):
            self.session_credentials[camera["id"]] = dict(credentials)
            self._start_preview(slot, camera, credentials, playback)

        credentials = self.session_credentials.get(device["id"])
        if credentials:
            try:
                play(device, credentials)
            except Exception as exc:
                self.notify(f"Не удалось открыть видео события: {exc}")
            return
        self._credentials_dialog("Видео события · " + device["name"], device, play)

    def _on_close(self, *_args):
        self._closing = True
        self._stop_services()
        self.alarm_stop.set()
        if getattr(self, "archive_playback_source", 0):
            GLib.source_remove(self.archive_playback_source)
            self.archive_playback_source = 0
        if getattr(self, "archive_seek_source", 0):
            GLib.source_remove(self.archive_seek_source)
            self.archive_seek_source = 0
        if self.alarm_response:
            try:
                self.alarm_response.close()
            except Exception:
                pass
        self.stop_all_streams()
        for window in list(self.monitor_windows):
            window.close()
        self.session_credentials.clear()
        self.last_view_credentials = None
        return False

    def _migrate_credentials_before_restore(self):
        threading.Thread(target=self._migrate_credentials_worker, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _migrate_credentials_worker(self):
        try:
            migrated = migrate_legacy_credentials()
            error = None
        except (CredentialStoreError, sqlite3.Error) as exc:
            migrated = 0
            error = str(exc)
        found = self._prefetch_credentials() if error is None else {}
        GLib.idle_add(self._finish_credential_migration, migrated, error, found)

    @staticmethod
    def _prefetch_credentials():
        """Read saved passwords on a worker thread so later lookups hit the in-memory cache.

        ``secret-tool`` can block for seconds (a locked keyring shows an unlock dialog), which
        must never happen on the GTK main thread.
        """
        found, by_endpoint = {}, {}
        try:
            for device in load_devices():
                key = (device["host"], int(device.get("http_port", 80)))
                if key not in by_endpoint:
                    by_endpoint[key] = load_saved_credentials(device)
                if by_endpoint[key]:
                    found[device["id"]] = dict(by_endpoint[key])
        except (CredentialStoreError, OSError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired):
            pass  # keyring unavailable: fall back to on-demand lookup / the credentials dialog
        return found

    def _finish_credential_migration(self, migrated, error, found=None):
        if self._closing:
            return GLib.SOURCE_REMOVE
        for device_id, credentials in (found or {}).items():
            self.session_credentials.setdefault(device_id, credentials)
        if error:
            self.notify(
                "Не удалось перенести пароли в системное хранилище; старые записи SQLite "
                f"не удалены. Проверьте Secret Service: {error}"
            )
        elif migrated:
            self.notify(f"Безопасно перенесено паролей устройств: {migrated}")
        self.restore_saved_windows()
        self.sync_event_subscriptions()
        self._recorder_tick()
        self.check_all_devices(quiet=True)
        return GLib.SOURCE_REMOVE

    def open_view_window(self):
        self.devices = load_devices()
        if not self.devices:
            self.notify("Сначала добавьте камеры в раздел «Устройства»")
            return
        dialog = Gtk.Dialog(title="Новое окно просмотра", transient_for=self, modal=True)
        dialog.set_resizable(True)
        dialog.set_default_size(*self._initial_window_size(520, 340))
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Создать окно", Gtk.ResponseType.ACCEPT)
        dialog.get_widget_for_response(Gtk.ResponseType.ACCEPT).add_css_class("suggested-action")
        form = Gtk.Grid(column_spacing=12, row_spacing=10)
        form.set_margin_top(18)
        form.set_margin_bottom(18)
        form.set_margin_start(18)
        form.set_margin_end(18)
        saved_login = self.last_view_credentials or {}
        username = Gtk.Entry(text=saved_login.get("username", ""), placeholder_text="Имя пользователя камер")
        password = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="Пароль")
        if saved_login.get("password"):
            password.set_text(saved_login["password"])
        substream = Gtk.CheckButton(label="Использовать дополнительный поток (рекомендуется для больших сеток)")
        substream.set_active(True)
        display = Gdk.Display.get_default()
        monitors = display.get_monitors() if display else None
        monitor_count = monitors.get_n_items() if monitors else 1
        monitor_names = [f"Экран {i + 1}" for i in range(max(1, monitor_count))]
        monitor_choice = Gtk.DropDown.new_from_strings(monitor_names)
        occupied = {window.monitor_index for window in self.monitor_windows}
        monitor_choice.set_selected(next((i for i in range(max(1, monitor_count)) if i not in occupied), 0))
        camera_capacity = Gtk.SpinButton.new_with_range(1, 64, 1)
        camera_capacity.set_value(16)
        window_name = Gtk.Entry(text=f"Окно просмотра {self.monitor_sequence + 1}", placeholder_text="Название окна")
        start_camera = Gtk.DropDown.new_from_strings([
            f"{i + 1}. {d['name']} · {d['host']} CH {d['channel']}" for i, d in enumerate(self.devices)])
        start_camera.set_enable_search(True)
        open_fullscreen = Gtk.CheckButton(label="Открыть в полноэкранном режиме на выбранном экране")
        open_fullscreen.set_active(True)
        self._field(form, "Название окна", window_name, 0)
        self._field(form, "Камер в окне", camera_capacity, 1)
        self._field(form, "Начать с камеры", start_camera, 2)
        self._field(form, "Экран", monitor_choice, 3)
        self._field(form, "Имя пользователя", username, 4)
        self._field(form, "Пароль", password, 5)
        form.attach(substream, 0, 6, 2, 1)
        form.attach(open_fullscreen, 0, 7, 2, 1)
        note = make_label(f"Создаётся только одно окно. Доступно экранов: {monitor_count}. При оконном режиме окно можно перетащить вручную.", "muted")
        note.set_wrap(True)
        form.attach(note, 0, 8, 2, 1)
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result != Gtk.ResponseType.ACCEPT:
                dlg.close()
                return
            creds = {"username": username.get_text().strip(), "password": password.get_text()}
            name = window_name.get_text().strip()
            if not creds["username"]:
                self.notify("Введите имя пользователя камеры")
                return
            if not name:
                self.notify("Введите название окна")
                return
            self.last_view_credentials = dict(creds)
            capacity = camera_capacity.get_value_as_int()
            start_index = start_camera.get_selected()
            if start_index == Gtk.INVALID_LIST_POSITION:
                self.notify("Выберите начальную камеру")
                return
            batch = self.devices[start_index:start_index + capacity]
            if not batch:
                self.notify("В выбранном диапазоне нет камер")
                return
            screen_index = monitor_choice.get_selected()
            target_monitor = monitors.get_item(screen_index) if monitors and screen_index < monitor_count else None
            mapping = [device["id"] for device in batch] + [None] * (capacity - len(batch))
            try:
                with database() as db:
                    cursor = db.execute("""INSERT INTO saved_windows
                        (name,capacity,screen_index,mapping,use_substream,fullscreen)
                        VALUES (?,?,?,?,?,?)""",
                        (name, capacity, screen_index, json.dumps(mapping), int(substream.get_active()), int(open_fullscreen.get_active())))
                    saved_window_id = cursor.lastrowid
            except sqlite3.IntegrityError:
                self.notify("Окно с таким названием уже сохранено")
                return
            self.monitor_sequence += 1
            window = MonitorWindow(self, self.monitor_sequence, name, monitor_count, capacity, screen_index, saved_window_id)
            self.monitor_windows.append(window)
            window.present()
            if open_fullscreen.get_active() and target_monitor and hasattr(window, "fullscreen_on_monitor"):
                window.fullscreen_on_monitor(target_monitor)
            window.start_assignments(list(enumerate(batch)), creds, substream.get_active())
            dlg.close()
        dialog.connect("response", response)
        dialog.present()

    def restore_saved_windows(self):
        with database() as db:
            saved = [dict(row) for row in db.execute("SELECT * FROM saved_windows ORDER BY id")]
        if not saved:
            return GLib.SOURCE_REMOVE
        self.devices = load_devices()
        devices_by_id = {device["id"]: device for device in self.devices}
        missing_credentials = {}
        credentials_cache = {}
        for row in saved:
            try:
                mapping = json.loads(row["mapping"])
            except (TypeError, json.JSONDecodeError):
                mapping = []
            for device_id in mapping[:row["capacity"]]:
                device = devices_by_id.get(device_id)
                if not device or device_id in self.session_credentials:
                    continue
                try:
                    key = (device["host"], int(device.get("http_port", 80)))
                    credentials = credentials_cache.get(key)
                    if credentials is None:
                        credentials = load_saved_credentials(device)
                        if credentials:
                            credentials_cache[key] = credentials
                except (CredentialStoreError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    self.notify(str(exc))
                    credentials = None
                if credentials:
                    self.session_credentials[device_id] = dict(credentials)
                else:
                    missing_credentials[device_id] = device
        dialog = Gtk.Dialog(title="Восстановление окон просмотра", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Восстановить", Gtk.ResponseType.ACCEPT)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(18); content.set_margin_bottom(18)
        content.set_margin_start(18); content.set_margin_end(18)
        prompt = ("Введите общие учётные данные для ещё не настроенных камер."
                  if missing_credentials else "Учётные данные загружены из хранилища Linux.")
        prompt_label = make_label(f"Сохранено окон: {len(saved)}. {prompt}")
        prompt_label.set_wrap(True)
        content.append(prompt_label)
        username = Gtk.Entry(placeholder_text="Имя пользователя камер")
        password = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="Пароль")
        if missing_credentials:
            content.append(username); content.append(password)
        dialog.get_content_area().append(content)

        def response(dlg, result):
            dlg.close()
            if result != Gtk.ResponseType.ACCEPT:
                return
            credentials = {"username": username.get_text().strip(), "password": password.get_text()}
            if missing_credentials and not credentials["username"]:
                self.notify("Введите имя пользователя камеры для восстановления")
                return
            if credentials["username"]:
                self.last_view_credentials = dict(credentials)
                for device_id, device in missing_credentials.items():
                    self.session_credentials[device_id] = dict(credentials)
                    try:
                        save_credentials(device, credentials)
                    except (CredentialStoreError, sqlite3.Error) as exc:
                        self.notify(f"Окна будут восстановлены, но пароль не сохранён: {exc}")
            display = Gdk.Display.get_default()
            monitors = display.get_monitors() if display else None
            monitor_count = monitors.get_n_items() if monitors else 1
            for row in saved:
                try:
                    mapping = json.loads(row["mapping"])
                except (TypeError, json.JSONDecodeError):
                    mapping = []
                assignments = [(slot, devices_by_id[device_id]) for slot, device_id in enumerate(mapping[:row["capacity"]])
                               if device_id in devices_by_id]
                screen_index = int(row["screen_index"]) % max(1, monitor_count)
                self.monitor_sequence += 1
                window = MonitorWindow(self, self.monitor_sequence, row["name"], monitor_count,
                                       int(row["capacity"]), screen_index, int(row["id"]))
                self.monitor_windows.append(window)
                window.present()
                target_monitor = monitors.get_item(screen_index) if monitors else None
                if row["fullscreen"] and target_monitor and hasattr(window, "fullscreen_on_monitor"):
                    window.fullscreen_on_monitor(target_monitor)
                window.start_assignments(assignments, credentials, bool(row["use_substream"]))
        dialog.connect("response", response)
        dialog.present()
        return GLib.SOURCE_REMOVE

    def start_live(self, device, credentials):
        self.session_credentials[device["id"]] = dict(credentials)
        free = next((i for i in range(self.layout_count) if i not in self.streams), self.selected_slot)
        self._start_preview(free, device, credentials)

    def play_archive(self):
        selected = self.archive_device.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self.archive_choice_devices):
            if not self.archive_choice_devices:
                self.notify("Сначала добавьте устройство")
                return
            self.notify("Выберите устройство")
            return
        device = self.archive_choice_devices[selected]
        try:
            start, end = self._archive_form_interval()
        except ValueError as exc:
            self.notify(str(exc))
            return
        self._credentials_dialog("Архив · " + device["name"], device, self._launch_archive, (start, end))

    def search_archive(self):
        selected = self.archive_device.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(self.archive_choice_devices):
            if not self.archive_choice_devices:
                self.notify("Сначала добавьте устройство")
                return
            self.notify("Выберите устройство")
            return
        device = self.archive_choice_devices[selected]
        try:
            start, end = self._archive_form_interval()
        except ValueError as exc:
            self.notify(str(exc))
            return
        self._credentials_dialog("Поиск архива · " + device["name"], device, self._search_archive, (start, end))

    def _archive_form_interval(self):
        try:
            start = datetime.fromisoformat(self.archive_start.get_text().strip())
            end = datetime.fromisoformat(self.archive_end.get_text().strip())
        except ValueError as exc:
            raise ValueError("Введите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ") from exc
        start = start.astimezone()
        end = end.astimezone()
        if end <= start:
            raise ValueError("Конец интервала должен быть позже начала")
        return (start.strftime("%Y-%m-%d %H:%M:%S"),
                end.strftime("%Y-%m-%d %H:%M:%S"))

    def _search_archive(self, device, credentials):
        start_text = credentials.pop("start")
        end_text = credentials.pop("end")
        token = uuid.uuid4().hex
        self.archive_query_token = token
        self.archive_matches = []
        self._update_archive_timeline_marks()
        self._clear_box(self.archive_results)
        self.archive_results.append(make_label("Идёт поиск…", "muted"))
        threading.Thread(target=self._archive_search_worker,
            args=(device, credentials, start_text, end_text, token), daemon=True).start()

    def _archive_search_worker(self, device, credentials, start_text, end_text, token):
        try:
            start = datetime.fromisoformat(start_text).astimezone().isoformat(timespec="seconds")
            end = datetime.fromisoformat(end_text).astimezone().isoformat(timespec="seconds")
            if end <= start:
                raise ValueError("Конец интервала должен быть позже начала")
            if device["protocol"] != "hikvision":
                raise ValueError("Поиск файлов доступен для устройств Hikvision с ISAPI")
            track = int(device["channel"]) * 100 + 1
            query = (f"<CMSearchDescription><searchID>{uuid.uuid4()}</searchID><trackList><trackID>{track}</trackID></trackList>"
                f"<timeSpanList><timeSpan><startTime>{start}</startTime><endTime>{end}</endTime></timeSpan></timeSpanList>"
                "<maxResults>64</maxResults><searchResultPosition>0</searchResultPosition></CMSearchDescription>").encode()
            url = f"http://{device['host']}:{device.get('http_port', 80)}/ISAPI/ContentMgmt/search"
            opener = authenticated_opener(url, credentials["username"], credentials["password"])
            request = urllib.request.Request(url, data=query, method="POST", headers={"Content-Type": "application/xml"})
            with opener.open(request, timeout=12) as response:
                xml = ET.fromstring(response.read())
            matches = []
            for item in xml.iter():
                if item.tag.rsplit("}", 1)[-1] != "searchMatchItem":
                    continue
                values = {node.tag.rsplit("}", 1)[-1]: (node.text or "").strip() for node in item.iter()}
                uri = values.get("playbackURI")
                if uri:
                    clip_start = values.get("startTime", "")
                    clip_end = values.get("endTime", "")
                    try:
                        if datetime.fromisoformat(clip_end.replace("Z", "+00:00")).astimezone() <= datetime.fromisoformat(
                                clip_start.replace("Z", "+00:00")).astimezone():
                            continue
                    except ValueError:
                        continue
                    matches.append({"start": clip_start, "end": clip_end, "uri": uri,
                                    "device": device, "credentials": credentials})
            GLib.idle_add(self._show_archive_results, token, matches)
        except Exception as exc:
            GLib.idle_add(self._archive_search_error, token, str(exc))

    def _show_archive_results(self, token, matches):
        if token != self.archive_query_token:
            return GLib.SOURCE_REMOVE
        self.archive_matches = matches
        self._clear_box(self.archive_results)
        if not matches:
            self.archive_results.append(make_label("Записей на выбранном интервале не найдено.", "muted"))
            self._update_archive_timeline_marks()
            return GLib.SOURCE_REMOVE
        matches.sort(key=lambda item: self._archive_datetime(item["start"]))
        for match in matches:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.set_margin_top(5)
            row.set_margin_bottom(5)
            label = make_label(
                f"{self._archive_datetime(match['start']).strftime('%m-%d %H:%M:%S')}  –  "
                f"{self._archive_datetime(match['end']).strftime('%H:%M:%S')}", "muted")
            label.set_hexpand(True)
            row.append(label)
            play = Gtk.Button(label="▶ Просмотр")
            play.connect("clicked", lambda _button, m=match: self._play_search_match(m))
            row.append(play)
            export = Gtk.Button(label="Сохранить")
            export.connect("clicked", lambda _button, m=match: self._export_search_match(m))
            row.append(export)
            self.archive_results.append(row)
        self._update_archive_timeline_marks()
        return GLib.SOURCE_REMOVE

    def _archive_search_error(self, token, message):
        if token != self.archive_query_token:
            return GLib.SOURCE_REMOVE
        self._clear_box(self.archive_results)
        self.archive_results.append(make_label("Поиск завершился с ошибкой.", "muted"))
        self.notify(f"Ошибка поиска архива: {message}")
        return GLib.SOURCE_REMOVE

    def _play_search_match(self, match):
        try:
            self._start_archive_playback(match)
        except Exception as exc:
            self.notify(str(exc))

    def _update_archive_timeline_marks(self):
        scale = self.archive_timeline
        scale.clear_marks()
        try:
            range_start, range_end = self._archive_form_interval()
            range_start = datetime.fromisoformat(range_start).astimezone()
            range_end = datetime.fromisoformat(range_end).astimezone()
            span = (range_end - range_start).total_seconds()
        except (ValueError, AttributeError):
            return
        if span <= 0:
            return
        self.archive_range_start_label.set_text(range_start.strftime("%m-%d %H:%M"))
        self.archive_range_middle_label.set_text(
            (range_start + (range_end - range_start) / 2).strftime("%m-%d %H:%M"))
        self.archive_range_end_label.set_text(range_end.strftime("%m-%d %H:%M"))
        for match in self.archive_matches:
            try:
                clip_start = self._archive_datetime(match["start"])
                clip_end = self._archive_datetime(match["end"])
            except (KeyError, ValueError):
                continue
            if clip_end <= range_start or clip_start >= range_end:
                continue
            begin = max(range_start, clip_start)
            finish = min(range_end, clip_end)
            start_fraction = (begin - range_start).total_seconds() / span
            end_fraction = (finish - range_start).total_seconds() / span
            scale.add_mark(start_fraction * 1000, Gtk.PositionType.BOTTOM, "")
            scale.add_mark(end_fraction * 1000, Gtk.PositionType.BOTTOM, "")
        self._set_archive_timeline_position(range_start, span)

    def _set_archive_timeline_position(self, range_start=None, span=None):
        if self.archive_seek_source:
            return
        state = self.streams.get(getattr(self, "archive_player_slot", -1))
        if not state or not state.get("playback") or not state.get("archive_start"):
            self.archive_timeline_updating = True
            self.archive_timeline.set_value(0)
            self.archive_timeline_updating = False
            return
        if range_start is None or span is None:
            try:
                start_text, end_text = self._archive_form_interval()
                range_start = datetime.fromisoformat(start_text).astimezone()
                range_end = datetime.fromisoformat(end_text).astimezone()
                span = (range_end - range_start).total_seconds()
            except ValueError:
                return
        if span <= 0:
            return
        elapsed = state.get("archive_elapsed", 0.0)
        if not state.get("archive_paused"):
            elapsed += (
                time.monotonic() - state.get("archive_clock", time.monotonic())
            ) * state.get("archive_speed", 1.0)
        position = state["archive_start"] + timedelta(seconds=elapsed)
        value = (position - range_start).total_seconds() / span * 1000
        self.archive_timeline_updating = True
        self.archive_timeline.set_value(min(1000, max(0, value)))
        self.archive_timeline_updating = False

    def _seek_archive_timeline(self, scale):
        if self.archive_timeline_updating or not self.archive_matches:
            return
        if self.archive_seek_source:
            GLib.source_remove(self.archive_seek_source)
        fraction = scale.get_value() / 1000
        self.archive_seek_source = GLib.timeout_add(350, self._seek_archive_position, fraction)

    def _seek_archive_position(self, fraction):
        self.archive_seek_source = 0
        try:
            range_start, range_end = self._archive_form_interval()
            range_start = datetime.fromisoformat(range_start).astimezone()
            range_end = datetime.fromisoformat(range_end).astimezone()
            target = range_start + (range_end - range_start) * fraction
            match = next((item for item in self.archive_matches
                          if self._archive_datetime(item["start"]) <= target <
                          self._archive_datetime(item["end"])), None)
            if not match:
                self.notify("В этой позиции нет записи")
                return GLib.SOURCE_REMOVE
            self._start_archive_playback(match, seek_to=target)
        except (ValueError, KeyError) as exc:
            self.notify(f"Не удалось перейти к выбранному времени: {exc}")
        return GLib.SOURCE_REMOVE

    def _toggle_archive_playback(self):
        slot = getattr(self, "archive_player_slot", self.selected_slot)
        state = self.streams.get(slot)
        if not state or not state.get("playback"):
            if self.archive_current_match:
                try:
                    self._start_archive_playback(self.archive_current_match)
                except Exception as exc:
                    self.notify(f"Не удалось возобновить архив: {exc}")
            else:
                self.notify("Сначала найдите запись или задайте интервал")
            return
        if state.get("archive_paused"):
            if state["pipeline"].set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                self.notify("Не удалось продолжить воспроизведение архива")
                return
            state["archive_clock"] = time.monotonic()
            state["archive_paused"] = False
            self.archive_play_button.set_label("Ⅱ")
        else:
            state["archive_elapsed"] += (
                time.monotonic() - state["archive_clock"]) * state.get("archive_speed", 1.0)
            if state["pipeline"].set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
                self.notify("Не удалось приостановить воспроизведение архива")
                return
            state["archive_paused"] = True
            self.archive_play_button.set_label("▶")
        self._update_archive_timeline_marks()

    def _stop_archive_playback(self):
        if self.archive_seek_source:
            GLib.source_remove(self.archive_seek_source)
            self.archive_seek_source = 0
        if self.archive_playback_source:
            GLib.source_remove(self.archive_playback_source)
            self.archive_playback_source = 0
        slot = getattr(self, "archive_player_slot", self.selected_slot)
        if slot in self.streams and self.streams[slot].get("playback"):
            self._stop_stream(slot)
        self.archive_play_button.set_label("▶")
        self.archive_time_label.set_text("Воспроизведение остановлено")
        self._update_archive_timeline_marks()

    def _set_archive_speed(self, choice, _property):
        state = self.streams.get(getattr(self, "archive_player_slot", -1))
        if not state or not state.get("playback"):
            return
        rates = (0.5, 1.0, 2.0, 4.0)
        selected = choice.get_selected()
        if selected >= len(rates):
            return
        rate = rates[selected]
        old_rate = state.get("archive_speed", 1.0)
        if rate == old_rate:
            return
        if not state.get("archive_paused"):
            state["archive_elapsed"] += (
                time.monotonic() - state["archive_clock"]) * old_rate
        try:
            supported = state["pipeline"].seek(
                rate, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                Gst.SeekType.NONE, 0, Gst.SeekType.NONE, 0)
        except (GLib.Error, TypeError, ValueError, RuntimeError) as exc:
            supported = False
            error = str(exc)
        else:
            error = "устройство или RTSP-сервер отклонил изменение скорости"
        if not supported:
            self.archive_speed_choice.set_selected(rates.index(old_rate))
            self.notify(f"Изменение скорости недоступно: {error}")
            if not state.get("archive_paused"):
                state["archive_clock"] = time.monotonic()
            return
        state["archive_speed"] = rate
        if not state.get("archive_paused"):
            state["archive_clock"] = time.monotonic()

    def _update_archive_playback(self):
        slot = getattr(self, "archive_player_slot", -1)
        state = self.streams.get(slot)
        if not state or not state.get("playback"):
            self.archive_playback_source = 0
            self.archive_play_button.set_label("▶")
            return GLib.SOURCE_REMOVE
        elapsed = state.get("archive_elapsed", 0.0)
        if not state.get("archive_paused"):
            elapsed += (time.monotonic() - state["archive_clock"]) * state.get("archive_speed", 1.0)
        duration = (state["archive_end"] - state["archive_start"]).total_seconds()
        if elapsed >= duration:
            state["archive_elapsed"] = duration
            self._stop_stream(slot)
            self.archive_playback_source = 0
            self.archive_play_button.set_label("▶")
            self.archive_time_label.set_text(state["archive_end"].strftime("%Y-%m-%d %H:%M:%S"))
            self._update_archive_timeline_marks()
            return GLib.SOURCE_REMOVE
        position = state["archive_start"] + timedelta(seconds=elapsed)
        self.archive_time_label.set_text(position.strftime("%Y-%m-%d %H:%M:%S"))
        self._set_archive_timeline_position()
        return GLib.SOURCE_CONTINUE

    @staticmethod
    def _archive_datetime(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()

    def _start_archive_playback(self, match, seek_to=None):
        device = match["device"]
        credentials = match["credentials"]
        start = self._archive_datetime(match["start"])
        end = self._archive_datetime(match["end"])
        if seek_to is not None:
            start = seek_to.astimezone()
        if end <= start:
            raise ValueError("Выбранная позиция находится за концом записи")
        interval = (start.strftime("%Y-%m-%d %H:%M:%S"),
                    end.strftime("%Y-%m-%d %H:%M:%S"))
        direct_uri = None
        if seek_to is None and match.get("uri"):
            direct_uri = self._authenticate_device_uri(match["uri"], device, credentials)
        self._close_inner()
        if self.maximized_slot is not None:
            self._restore_maximized_slot()
        slot = min(self.selected_slot, self.layout_count - 1)
        previous_slot = getattr(self, "archive_player_slot", None)
        if (previous_slot is not None and previous_slot != slot
                and previous_slot in self.streams
                and self.streams[previous_slot].get("playback")):
            self._stop_stream(previous_slot)
        if self.archive_seek_source:
            GLib.source_remove(self.archive_seek_source)
            self.archive_seek_source = 0
        if self.archive_playback_source:
            GLib.source_remove(self.archive_playback_source)
            self.archive_playback_source = 0
        self.archive_player_slot = slot
        self.archive_current_match = match
        self._start_preview(slot, device, credentials, interval,
                            direct_uri=direct_uri, stream_override="main")
        state = self.streams[slot]
        state["archive_start"] = start
        state["archive_end"] = end
        state["archive_elapsed"] = 0.0
        state["archive_speed"] = 1.0
        state["archive_clock"] = time.monotonic()
        state["archive_paused"] = False
        self.archive_play_button.set_label("Ⅱ")
        self.archive_time_label.set_text(start.strftime("%Y-%m-%d %H:%M:%S"))
        self.archive_speed_choice.set_selected(1)
        self.archive_playback_source = GLib.timeout_add(250, self._update_archive_playback)
        self._set_archive_timeline_position()

    def _export_search_match(self, match):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.notify("Для экспорта записи установите FFmpeg")
            return
        uri = self._authenticate_device_uri(match["uri"], match["device"], match["credentials"])
        folder = Path.home() / "Videos" / "Sentinel"
        folder.mkdir(parents=True, exist_ok=True)
        filename = folder / datetime.now().strftime("export_%Y%m%d_%H%M%S.mkv")
        try:
            FfmpegJob(ffmpeg, uri, str(filename), on_error=self._ffmpeg_failed)
        except OSError as exc:
            self.notify(f"Не удалось запустить FFmpeg: {exc}")
            return
        self.notify("Экспорт записи начат")

    def _authenticate_device_uri(self, uri, device, credentials):
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme != "rtsp":
            raise ValueError("Устройство вернуло неподдерживаемую ссылку воспроизведения")
        user = urllib.parse.quote(credentials["username"], safe="")
        password = urllib.parse.quote(credentials["password"], safe="")
        host = parsed.hostname or device["host"]
        port = f":{parsed.port}" if parsed.port else ""
        return parsed._replace(netloc=f"{user}:{password}@{host}{port}").geturl()

    def _launch_archive(self, device, credentials):
        interval = (credentials.pop("start"), credentials.pop("end"))
        self.session_credentials[device["id"]] = dict(credentials)
        self._start_archive_playback({
            "device": device, "credentials": credentials,
            "start": interval[0], "end": interval[1],
        })

    def _start_preview(self, slot_index, device, credentials, playback=None, direct_uri=None,
                       stream_override=None):
        if slot_index >= self.layout_count:
            raise ValueError("Выберите видимое окно")
        if slot_index in self.streams:
            self._stop_stream(slot_index)
        if slot_index in getattr(self, "slot_resources", {}):
            self._clear_resource(slot_index)
        base_device = dict(device)
        preview_device = dict(device)
        if stream_override is not None:
            preview_device["stream"] = stream_override
        elif device["protocol"] == "hikvision" and not playback and not direct_uri:
            preview_device["stream"] = self._desired_stream(slot_index)
        uri = direct_uri or self._stream_url(preview_device, credentials, playback)
        sink = Gst.ElementFactory.make("appsink", f"sentinel-video-{slot_index}")
        pipeline = Gst.ElementFactory.make("playbin", f"sentinel-player-{slot_index}")
        if not sink or not pipeline:
            raise RuntimeError("GStreamer playbin/appsink недоступен. Установите GStreamer base/good plugins.")
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 1)
        sink.set_property("drop", True)
        sink.set_property("sync", True)
        sink.set_property("caps", Gst.Caps.from_string("video/x-raw,format=RGBA"))
        state = {"pipeline": pipeline, "sink": sink, "device": preview_device,
                 "base_device": base_device, "configured_stream": base_device.get("stream", "main"),
                 "playback": playback is not None or direct_uri is not None,
                 "playback_interval": playback,
                 "credentials": dict(credentials),
                 "uri": uri, "slot": slot_index, "recording": None, "last_frame_time": 0,
                 "digital_zoom": 1.0, "digital_x": 0.5, "digital_y": 0.5,
                 "frame_interval": 0.25 if (
                     getattr(self, "silent_stream_errors", False)
                     or (self.layout_count >= 9 and slot_index != self.maximized_slot)
                 ) else 0.04}
        sink.connect("new-sample", self._on_new_sample, state)
        pipeline.set_property("uri", uri)
        pipeline.set_property("video-sink", sink)
        pipeline.set_property("mute", True)
        pipeline.set_property("volume", 1.0)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_pipeline_message, state)
        self.streams[slot_index] = state
        self.video_slots[slot_index]["device"] = preview_device
        mode = "ARCHIVE" if state["playback"] else "LIVE"
        self.video_slots[slot_index]["caption"].set_text(
            f"● {mode}   {preview_device['name']}  ·  {preview_device['host']} CH {preview_device['channel']}")
        self.video_slots[slot_index]["placeholder"].set_visible(False)
        self._select_slot(slot_index)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._stop_stream(slot_index)
            raise RuntimeError("Не удалось запустить поток. Проверьте RTSP-порт, учётные данные и доступность камеры.")
        if not state["playback"]:
            self._mark_view_dirty()
        self._update_stream_count()
        if hasattr(self, "timeline"):
            self._load_timeline_segments()

    def _on_new_sample(self, sink, state):
        sample = sink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.ERROR
        now = time.monotonic()
        if now - state["last_frame_time"] < state["frame_interval"]:
            return Gst.FlowReturn.OK
        if state.get("paint_pending"):
            return Gst.FlowReturn.OK  # UI has not drawn the previous frame yet: drop this one
        state["last_frame_time"] = now
        structure = sample.get_caps().get_structure(0)
        width_ok, width = structure.get_int("width")
        height_ok, height = structure.get_int("height")
        if not width_ok or not height_ok:
            return Gst.FlowReturn.ERROR
        buffer = sample.get_buffer()
        success, mapped = buffer.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.ERROR
        try:
            rgba = bytes(mapped.data)
        finally:
            buffer.unmap(mapped)
        state["paint_pending"] = True
        GLib.idle_add(self._paint_sample, state, width, height, rgba)
        return Gst.FlowReturn.OK

    def _paint_sample(self, state, width, height, rgba):
        state["paint_pending"] = False
        slot_index = state["slot"]
        if self.streams.get(slot_index) is not state or slot_index >= len(self.video_slots):
            return GLib.SOURCE_REMOVE
        slot = self.video_slots[slot_index]
        aspect_ratio = width / height if height else 16 / 9
        if abs(slot["aspect_ratio"] - aspect_ratio) > 0.01:
            slot["aspect_ratio"] = aspect_ratio
            slot["aspect_frame"].set_ratio(aspect_ratio)
            if self.maximized_slot == slot_index:
                self._fit_maximized_slot()
        zoom = state.get("digital_zoom", 1.0)
        if zoom > 1.001:
            crop_width = max(1, int(width / zoom)); crop_height = max(1, int(height / zoom))
            center_x = state.get("digital_x", 0.5); center_y = state.get("digital_y", 0.5)
            left = max(0, min(width - crop_width, int(center_x * width - crop_width / 2)))
            top = max(0, min(height - crop_height, int(center_y * height - crop_height / 2)))
            rgba = b"".join(rgba[(top + row) * width * 4 + left * 4:
                                   (top + row) * width * 4 + (left + crop_width) * 4]
                            for row in range(crop_height))
            width, height = crop_width, crop_height
        texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8A8,
                                       GLib.Bytes.new(rgba), width * 4)
        self.video_slots[slot_index]["picture"].set_paintable(texture)
        return GLib.SOURCE_REMOVE

    def _on_pipeline_message(self, _bus, message, state):
        if message.type not in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            return
        # При обрыве GStreamer присылает несколько ERROR подряд (каждый элемент конвейера):
        # реагируем только на первый, иначе пользователь получает стопку одинаковых сообщений.
        if state.get("ended"):
            return
        state["ended"] = True
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            slot = state["slot"]
            if self.streams.get(slot) is state:
                GLib.idle_add(self._stream_failed, slot, str(error), state)
        else:
            GLib.idle_add(self._stop_stream_if_current, state)

    def _stop_stream_if_current(self, state):
        if self.streams.get(state["slot"]) is state:
            self._stop_stream(state["slot"])
        return GLib.SOURCE_REMOVE

    def _stream_failed(self, slot, message, state=None):
        if state is not None and self.streams.get(slot) is not state:
            return GLib.SOURCE_REMOVE  # окно уже занято другим потоком
        self._stop_stream(slot)
        if getattr(self, "silent_stream_errors", False):
            if slot < len(self.video_slots):
                self.video_slots[slot]["placeholder"].set_text(f"{slot + 1}\n\nПоток недоступен")
        else:
            self.notify(f"Поток недоступен: {message}")
        return GLib.SOURCE_REMOVE

    def _stop_stream(self, slot):
        state = self.streams.pop(slot, None)
        if not state:
            return GLib.SOURCE_REMOVE
        if not state.get("playback") and not getattr(self, "_closing", False):
            self._mark_view_dirty()
        if state.get("recording") and state["recording"].poll() is None:
            state["recording"].stop()
        if hasattr(self, "record_button") and slot == getattr(self, "selected_slot", slot):
            self._set_record_button_label("● Запись")
        state["pipeline"].set_state(Gst.State.NULL)
        state["pipeline"].get_bus().remove_signal_watch()
        if slot < len(self.video_slots):
            item = self.video_slots[slot]
            item["picture"].set_paintable(None)
            item["placeholder"].set_text(f"{slot + 1}\n\nНет видеопотока")
            item["placeholder"].set_visible(True)
            item["caption"].set_text("")
            item["device"] = None
        self._update_stream_count()
        return GLib.SOURCE_REMOVE

    def stop_all_streams(self):
        if getattr(self, "auto_switch_source", 0):
            GLib.source_remove(self.auto_switch_source)
            self.auto_switch_source = 0
            self.auto_switch_button.set_label("⟳")
        for slot in list(self.streams):
            self._stop_stream(slot)

    def capture_selected(self):
        if self.selected_slot not in self.streams:
            self.notify("Сначала выберите окно с активным потоком")
            return
        texture = self.video_slots[self.selected_slot]["picture"].get_paintable()
        if not isinstance(texture, Gdk.Texture):
            self.notify("Кадр потока ещё не получен")
            return
        folder = getattr(self, "capture_folder", getattr(getattr(self, "parent_window", None), "capture_folder", Path.home() / "Pictures" / "Sentinel"))
        folder.mkdir(parents=True, exist_ok=True)
        filename = folder / datetime.now().strftime("capture_%Y%m%d_%H%M%S.png")
        self.notify("Снимок сохранён" if texture.save_to_png(str(filename)) else "Не удалось сохранить снимок")

    def toggle_audio(self):
        state = self.streams.get(self.selected_slot)
        if not state:
            self.notify("Выберите активный поток")
            return
        enabled = not state.get("audio_enabled", False)
        state["pipeline"].set_property("mute", not enabled)
        state["audio_enabled"] = enabled
        audio_label = "🔊" if enabled else "🔇"
        if not getattr(self, "compact_audio_button", False):
            audio_label += " Звук"
        self.audio_button.set_label(audio_label)

    def toggle_recording(self):
        state = self.streams.get(self.selected_slot)
        if not state:
            self.notify("Сначала запустите поток в выбранном окне")
            return
        recording = state.get("recording")
        if recording and recording.poll() is None:
            recording.stop()
            state["recording"] = None
            self._set_record_button_label("● Запись")
            self.notify("Запись остановлена")
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.notify("Для записи установите FFmpeg")
            return
        folder = getattr(self, "record_folder", getattr(getattr(self, "parent_window", None), "record_folder", Path.home() / "Videos" / "Sentinel"))
        folder.mkdir(parents=True, exist_ok=True)
        filename = folder / datetime.now().strftime("record_%Y%m%d_%H%M%S.mkv")
        try:
            state["recording"] = FfmpegJob(ffmpeg, state["uri"], str(filename), on_error=self._ffmpeg_failed)
        except OSError as exc:
            self.notify(f"Не удалось запустить FFmpeg: {exc}")
            return
        self._set_record_button_label("■ Остановить запись")
        self.notify("Запись начата")

    def _ffmpeg_failed(self, code, details):
        message = f"FFmpeg завершился с ошибкой (код {code})" + (f": {details}" if details else "")
        GLib.idle_add(self.notify, message)

    def instant_playback(self, seconds):
        state = self.streams.get(self.selected_slot)
        if not state:
            self.notify("Сначала запустите поток в выбранном окне")
            return
        end = datetime.now().astimezone().replace(microsecond=0)
        start = datetime.fromtimestamp(end.timestamp() - seconds).strftime("%Y-%m-%d %H:%M:%S")
        self._start_preview(self.selected_slot, state["device"], state["credentials"], (start, end.strftime("%Y-%m-%d %H:%M:%S")))

    def toggle_auto_switch(self):
        if self.auto_switch_source:
            GLib.source_remove(self.auto_switch_source)
            self.auto_switch_source = 0
            self.auto_switch_button.set_label("⟳")
            return
        if not self.devices:
            self.notify("Сначала добавьте камеры")
            return
        self.auto_switch_button.set_label("Ⅱ")
        self.auto_switch_source = GLib.timeout_add_seconds(self.auto_switch_interval, self._auto_switch_tick)
        self._auto_switch_tick()

    def _auto_switch_tick(self):
        if not self.devices:
            return GLib.SOURCE_CONTINUE
        for _ in range(len(self.devices)):
            device = self.devices[self.auto_switch_index % len(self.devices)]
            self.auto_switch_index += 1
            credentials = self.session_credentials.get(device["id"])
            if credentials:
                self._start_preview(self.selected_slot, device, credentials)
                return GLib.SOURCE_CONTINUE
        self.notify("Откройте каждую камеру один раз, чтобы сохранить учётные данные до конца сеанса; затем доступно автопереключение")
        GLib.source_remove(self.auto_switch_source)
        self.auto_switch_source = 0
        self.auto_switch_button.set_label("⟳")
        return GLib.SOURCE_REMOVE

    def _update_stream_count(self):
        count = len(self.streams)
        if hasattr(self, "monitor_windows"):
            count += sum(len(window.streams) for window in self.monitor_windows)
        if hasattr(self, "stream_count_label"):
            self.stream_count_label.set_text(f"{count} потоков")
        if "active_markers" in self.__dict__:
            self._update_active_markers()
        self._update_dashboard()
        self._update_status_bar()
        active = self.streams.get(getattr(self, "selected_slot", -1))
        if hasattr(self, "ptz_buttons"):
            enabled = bool(active and active["device"]["protocol"] == "hikvision")
            for button in self.ptz_buttons:
                button.set_sensitive(enabled)
        if hasattr(self, "audio_button"):
            self.audio_button.set_sensitive(active is not None)
            audio_label = "🔊" if active and active.get("audio_enabled") else "🔇"
            if not getattr(self, "compact_audio_button", False):
                audio_label += " Звук"
            self.audio_button.set_label(audio_label)
        if hasattr(self, "record_button"):
            recording = active.get("recording") if active else None
            if not recording or recording.poll() is not None:
                self._set_record_button_label("● Запись")
        return GLib.SOURCE_CONTINUE

class MonitorWindow(SentinelWindow):
    def __init__(self, parent, window_number, name, physical_monitor_count, camera_capacity, monitor_index, saved_window_id):
        Gtk.ApplicationWindow.__init__(self, application=parent.get_application(),
            title=f"Sentinel — {name} · до {camera_capacity} камер")
        self.parent_window = parent
        self.window_number = window_number
        self.window_name = name
        self.saved_window_id = saved_window_id
        self.monitor_index = monitor_index
        self.physical_monitor_count = physical_monitor_count
        self.devices = parent.devices
        self.session_credentials = parent.session_credentials
        self.streams = {}
        self.layout_count = camera_capacity
        self.grid_columns, self.grid_rows = self._default_grid_shape(camera_capacity)
        self.camera_mapping = [None] * camera_capacity
        self.selected_slot = 0
        self.video_slots = []
        self.maximized_slot = None
        self.silent_stream_errors = True
        self.assignment_queue = []
        self.assignment_position = 0
        self.add_substream = True
        self.set_resizable(True)
        self.set_default_size(*self._initial_window_size(1680, 960, monitor_index))
        stylesheet()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        bar = Gtk.FlowBox()
        bar.set_selection_mode(Gtk.SelectionMode.NONE)
        bar.set_min_children_per_line(1)
        bar.set_max_children_per_line(4)
        bar.set_row_spacing(4)
        bar.set_column_spacing(4)
        bar.set_margin_start(9)
        bar.set_margin_end(9)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        bar.append(make_label(f"{name.upper()}  ·  {camera_capacity} КАМЕР", "eyebrow"))
        bar.append(make_label(f"Экран {monitor_index + 1} · обнаружено экранов: {physical_monitor_count}", "muted"))
        fullscreen = Gtk.Button(label="⛶ Полный экран")
        fullscreen.connect("clicked", lambda *_: self.toggle_fullscreen())
        bar.append(fullscreen)
        play_all = Gtk.Button(label="▶ Все камеры")
        play_all.connect("clicked", self.start_all_channels)
        bar.append(play_all)
        windowed = Gtk.Button(label="▣ Оконный режим")
        windowed.connect("clicked", lambda *_: self.unfullscreen())
        bar.append(windowed)
        stop = Gtk.Button(label="■ Остановить все")
        stop.connect("clicked", lambda *_: self.stop_all_streams())
        bar.append(stop)
        capture = Gtk.Button(label="▣ Снимок")
        capture.connect("clicked", lambda *_: self.capture_selected())
        bar.append(capture)
        self.record_button = Gtk.Button(label="● Запись")
        self.record_button.connect("clicked", lambda *_: self.toggle_recording())
        bar.append(self.record_button)
        self.audio_button = self._audio_control_menu()
        bar.append(self.audio_button)
        instant = Gtk.MenuButton(label="⏪ Повтор")
        instant_popover = Gtk.Popover()
        instant_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for seconds in (30, 60, 180, 300, 480, 600):
            item = Gtk.Button(label=f"{seconds} сек")
            item.connect("clicked", lambda _b, sec=seconds, menu=instant: (menu.popdown(), self.instant_playback(sec)))
            instant_box.append(item)
        instant_popover.set_child(instant_box)
        instant.set_popover(instant_popover)
        bar.append(instant)
        delete_saved = Gtk.Button(label="Удалить сохранение")
        delete_saved.connect("clicked", self.delete_saved_configuration)
        bar.append(delete_saved)
        root.append(bar)
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        root.append(separator)
        self.camera_grid = Gtk.Grid(column_spacing=2, row_spacing=2)
        self.camera_grid.set_margin_top(2)
        self.camera_grid.set_margin_bottom(2)
        self.camera_grid.set_margin_start(2)
        self.camera_grid.set_margin_end(2)
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
        root.append(self.camera_area)
        status = Gtk.Label(label="Загрузка каналов…", xalign=0)
        status.set_margin_start(10)
        status.set_margin_end(10)
        status.set_margin_top(4)
        status.set_margin_bottom(5)
        status.add_css_class("muted")
        self.wall_status = status
        root.append(status)
        self.set_child(root)
        self._render_video_grid()
        self.connect("close-request", self._close_monitor)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

    def start_assignments(self, assignments, shared_credentials, use_substream=True):
        self.add_substream = use_substream
        self.assignment_queue = [(slot, device) for slot, device in assignments if 0 <= slot < self.layout_count]
        for slot, device in self.assignment_queue:
            self.camera_mapping[slot] = device["id"]
        self._save_camera_mapping()
        self.assignment_position = 0
        self.shared_credentials = dict(shared_credentials)
        self.assignment_source = GLib.timeout_add(180, self._start_next_assignment)

    def delete_saved_configuration(self, *_args):
        dialog = Gtk.Dialog(title="Удалить сохранённое окно?", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Удалить", Gtk.ResponseType.ACCEPT)
        dialog.get_content_area().append(make_label(f"«{self.window_name}» больше не будет открываться при запуске."))
        def response(dlg, result):
            dlg.close()
            if result == Gtk.ResponseType.ACCEPT:
                with database() as db:
                    db.execute("DELETE FROM saved_windows WHERE id=?", (self.saved_window_id,))
                self.close()
        dialog.connect("response", response)
        dialog.present()

    def _start_next_assignment(self):
        if self.assignment_position >= len(self.assignment_queue):
            active = len(self.streams)
            self.wall_status.set_text(f"{active} потоков · {self.layout_count - len(self.assignment_queue)} пустых окон")
            self.assignment_source = 0
            return GLib.SOURCE_REMOVE
        slot, device = self.assignment_queue[self.assignment_position]
        self.assignment_position += 1
        credentials = self.session_credentials.get(device["id"], self.shared_credentials)
        self.session_credentials[device["id"]] = dict(credentials)
        stream_device = dict(device)
        if self.add_substream:
            stream_device["stream"] = "sub"
        try:
            self._start_preview(slot, stream_device, credentials)
        except Exception:
            self.video_slots[slot]["placeholder"].set_text(f"{slot + 1}\n\nОшибка подключения")
        self.wall_status.set_text(f"Подключено {len(self.streams)} / {len(self.assignment_queue)} каналов")
        return GLib.SOURCE_CONTINUE

    def _close_monitor(self, *_args):
        if getattr(self, "assignment_source", 0):
            GLib.source_remove(self.assignment_source)
            self.assignment_source = 0
        self.stop_all_streams()
        if self in self.parent_window.monitor_windows:
            self.parent_window.monitor_windows.remove(self)
        self.parent_window._update_stream_count()
        return False


class SentinelApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=0)

    def do_activate(self):
        window = self.props.active_window
        if not window:
            window = SentinelWindow(self)
        window.present()


if __name__ == "__main__":
    app = SentinelApplication()
    raise SystemExit(app.run(None))
