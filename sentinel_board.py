"""Панель мониторинга с виджетами и меню модулей по ролям (GTK-часть)."""
from __future__ import annotations

from datetime import datetime

from gi.repository import GLib, Gtk

import sentinel_alarms as alarms
import sentinel_dashboard as dash
import sentinel_recording as rec
import sentinel_storage as storage
import sentinel_widgets as W

MENU_MODULES = (
    # заголовок, ключ модуля
    ("Панель мониторинга", "board"),
    ("Главный просмотр", "main_view"),
    ("Статистика и сводка", "summary"),
    ("Центр событий", "events"),
    ("Поиск записей", "playback"),
    ("Электронная карта", "emap"),
    ("Контроль доступа", "access"),
    ("Локальные файлы", "local_files"),
)


class BoardMixin:
    # ------------------------------------------------------------ страница
    def _board_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        W.margins(page, 14)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.board_subtitle = W.label("", "muted")
        self.board_subtitle.set_hexpand(True)
        head.append(self.board_subtitle)
        head.append(W.button("⟳ Обновить", self._render_board))
        head.append(W.button("✎ Настроить…", self.customize_board_dialog))
        page.append(head)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        self.board_scroll = scroll
        self._new_board_flow()
        page.append(scroll)
        self.board_cpu = dash.CpuMeter()
        self.board_health = {}
        GLib.timeout_add_seconds(3, self._board_tick)
        return page

    def _new_board_flow(self):
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(False)
        flow.set_min_children_per_line(1)
        flow.set_max_children_per_line(3)
        flow.set_column_spacing(10)
        flow.set_row_spacing(10)
        flow.set_valign(Gtk.Align.START)
        self.board_flow = flow
        self.board_scroll.set_child(flow)
        return flow

    def _board_visible(self):
        return (hasattr(self, "inner_window") and self.inner_window.get_visible()
                and self.stack.get_visible_child_name() == "board")

    def _board_tick(self):
        if getattr(self, "_closing", False):
            return GLib.SOURCE_REMOVE
        if self._board_visible():
            self._update_board_health()
        return GLib.SOURCE_CONTINUE

    def _render_board(self):
        if not hasattr(self, "board_flow"):
            return
        self._new_board_flow()
        self.board_health = {}
        keys = dash.load_widgets(self.session)
        self.board_subtitle.set_text(
            f"{self.session.username} · {self.session.role} · {datetime.now():%d.%m.%Y %H:%M}")
        if not keys:
            self.board_flow.append(W.label("Виджетов нет. Нажмите «Настроить…», чтобы добавить.", "muted"))
        builders = {"monitoring": self._board_monitoring, "alarms": self._board_alarms,
                    "health": self._board_health_card, "devices": self._board_devices,
                    "records": self._board_records, "map": self._board_map, "events": self._board_events}
        for key in keys:
            body = builders[key]()
            self.board_flow.append(self._board_card(dash.BY_KEY[key].title, body))
        self._update_board_health()

    def _board_card(self, title, body):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("board-card")
        card.set_size_request(330, -1)
        card.append(W.label(title, "section-title"))
        card.append(body)
        return card

    @staticmethod
    def _board_metric(value, caption, css="stat-number"):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        number = W.label(str(value), css)
        box.append(number)
        box.append(W.label(caption, "muted"))
        return box, number

    def _board_row(self, *metrics):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        for value, caption in metrics:
            row.append(self._board_metric(value, caption)[0])
        return row

    def _board_link(self, text, callback, permission_keys=()):
        if permission_keys and not any(self.session.can(p) for p in permission_keys):
            return None
        return W.button(text, callback, flat=True)

    # ------------------------------------------------------------ виджеты
    def _board_monitoring(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        streams = list(getattr(self, "streams", {}).values())
        extra = sum(len(w.streams) for w in getattr(self, "monitor_windows", []))
        live = [s for s in streams if not s.get("playback")]
        box.append(self._board_row((len(live) + extra, "камер в просмотре"),
                                   (len(getattr(self, "devices", [])), "каналов в каталоге")))
        for state in live[:6]:
            box.append(W.label(f"● {state['device']['name']}", "muted"))
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        actions.append(W.button("Главный просмотр", lambda: self._show_module("main_view"), flat=True))
        actions.append(W.button("Все камеры", lambda: (self._show_module("main_view"), self.start_all_channels()),
                                flat=True))
        box.append(actions)
        return box

    def _board_alarms(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        records = alarms.load_alarm_records(5, status="unacknowledged")
        box.append(self._board_row((alarms.unacknowledged_count(), "не подтверждено")))
        if not records:
            box.append(W.label("Новых тревог нет", "muted"))
        for record in records:
            row = Gtk.Button()
            row.set_has_frame(False)
            row.add_css_class("alarm-row")
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            line.append(W.label("●", f"prio-{record['priority']}"))
            text = W.label(f"{record['time'][11:19]}  {record['rule_name'] or alarms.event_title(record['event_type'])}"
                           f" · {record['device_name'] or '—'}")
            text.set_ellipsize(3)
            text.set_hexpand(True)
            line.append(text)
            row.set_child(line)
            row.connect("clicked", lambda _b, r=record: self.show_alarm_details(r["id"]))
            box.append(row)
        link = self._board_link("Центр событий", lambda: self._show_module("events"), ("alarm_ack", "alarms"))
        if link:
            box.append(link)
        return box

    def _board_health_card(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for key, title in (("cpu", "Процессор"), ("ram", "Память"), ("disk", "Диск резервной записи")):
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            name = W.label(title)
            name.set_size_request(150, -1)
            line.append(name)
            bar = Gtk.LevelBar()
            bar.set_min_value(0)
            bar.set_max_value(100)
            bar.set_hexpand(True)
            bar.set_valign(Gtk.Align.CENTER)
            line.append(bar)
            value = W.label("—", "muted")
            value.set_size_request(90, -1)
            line.append(value)
            box.append(line)
            self.board_health[key] = (bar, value)
        self.board_health["load"] = W.label("", "muted")
        box.append(self.board_health["load"])
        return box

    def _update_board_health(self):
        health = getattr(self, "board_health", {})
        if "cpu" not in health:
            return
        cpu = self.board_cpu.sample()
        health["cpu"][0].set_value(cpu or 0)
        health["cpu"][1].set_text(dash.percent_text(cpu))
        memory = dash.memory_usage()
        if memory:
            used, total = memory
            health["ram"][0].set_value(used * 100 / total)
            health["ram"][1].set_text(f"{rec.human_bytes(used)} / {rec.human_bytes(total)}")
        disk = dash.disk_usage(rec.storage_root())
        if disk:
            used, total, free = disk
            health["disk"][0].set_value(used * 100 / total)
            health["disk"][1].set_text(f"своб. {rec.human_bytes(free)}")
        load = dash.load_average()
        streams = len(getattr(self, "streams", {})) + sum(len(w.streams) for w in getattr(self, "monitor_windows", []))
        health["load"].set_text(f"Средняя нагрузка: {load:.2f} · потоков в клиенте: {streams}"
                                if load is not None else f"Потоков в клиенте: {streams}")

    def _board_devices(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        counts = dash.device_counts()
        box.append(self._board_row((counts["total"], "устройств"), (counts["online"], "в сети"),
                                   (counts["offline"], "не в сети")))
        box.append(W.label(f"Каналов: {counts['channels']} · не проверено: {counts['unknown']}", "muted"))
        link = self._board_link("Управление устройствами", lambda: self.open_system("devices"), ("devices",))
        if link:
            box.append(link)
        return box

    def _board_records(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        try:
            result = dash.record_integrity()
        except Exception as exc:  # повреждённая папка хранилища не должна ломать панель
            box.append(W.label(f"Не удалось проверить: {exc}", "muted"))
            return box
        box.append(self._board_row((result["scheduled"], "пишут по расписанию"), (result["ok"], "в норме"),
                                   (len(result["gaps"]), "с перебоями")))
        names = {d["id"]: d["name"] for d in getattr(self, "devices", [])}
        for device_id in result["gaps"][:5]:
            box.append(W.label(f"⚠ {names.get(device_id, f'Камера {device_id}')}: нет свежих сегментов", "muted"))
        if not result["scheduled"]:
            box.append(W.label("Резервная запись на ПК сейчас не запланирована. Основной архив — на регистраторах.",
                               "muted", wrap=True))
        link = self._board_link("Хранилище", lambda: self._show_module("storage"), ("system",))
        if link:
            box.append(link)
        return box

    def _board_map(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        with storage.database() as db:
            first = db.execute("SELECT id, name, image_path FROM maps ORDER BY id LIMIT 1").fetchone()
            total = db.execute("SELECT COUNT(*) FROM maps").fetchone()[0]
            spots = db.execute("SELECT COUNT(*) FROM map_hotspots WHERE map_id=?",
                               (first["id"],)).fetchone()[0] if first else 0
        if not first:
            box.append(W.label("Карт нет. Добавьте план объекта в разделе «Карта».", "muted", wrap=True))
            return box
        picture = Gtk.Picture.new_for_filename(first["image_path"])
        picture.set_can_shrink(True)
        picture.set_size_request(300, 160)
        button = Gtk.Button()
        button.set_has_frame(False)
        button.set_child(picture)
        button.set_tooltip_text("Открыть карту")
        button.connect("clicked", lambda *_a, i=first["id"]: self.open_map(i))
        box.append(button)
        box.append(W.label(f"{first['name']} · точек: {spots} · всего карт: {total}", "muted"))
        return box

    def _board_events(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        import sentinel_reports as reports
        summary = reports.summary()
        box.append(self._board_row((summary["events_today"], "событий"), (summary["alarms_today"], "тревог")))
        for event in alarms.load_events(limit=5):
            text = W.label(f"{event['time'][11:19]}  {alarms.event_title(event['event_type'])}"
                           f" · {event['device_name'] or '—'}", "muted")
            text.set_ellipsize(3)
            box.append(text)
        return box

    # ------------------------------------------------------------ настройка
    def customize_board_dialog(self):
        current = dash.load_widgets(self.session)
        dialog = Gtk.Dialog(title="Панель мониторинга", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("OK", Gtk.ResponseType.ACCEPT)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        W.margins(box, 16)
        box.append(W.label(f"Виджеты для роли «{self.session.role}»", "muted"))
        checks = {}
        for widget in dash.allowed_widgets(self.session):
            check = Gtk.CheckButton(label=widget.title)
            check.set_active(widget.key in current)
            checks[widget.key] = check
            box.append(check)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        start = Gtk.CheckButton(label="Открывать панель мониторинга при запуске")
        start.set_active(dash.start_on_dashboard())
        box.append(start)
        box.append(W.button("Набор по умолчанию для роли", lambda: [
            c.set_active(k in dash.default_widgets(self.session)) for k, c in checks.items()], flat=True))
        dialog.get_content_area().append(box)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                keep = [k for k in current if checks.get(k) and checks[k].get_active()]
                keep += [k for k, c in checks.items() if c.get_active() and k not in keep]
                dash.save_widgets(self.session, keep)
                dash.set_start_on_dashboard(start.get_active())
                self._render_board()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    # ------------------------------------------------------------ меню по ролям
    def _modules_popover(self, menu_button):
        popover = Gtk.Popover()
        self.modules_popover = popover
        self._fill_modules_menu()
        return popover

    def _fill_modules_menu(self):
        popover = getattr(self, "modules_popover", None)
        if popover is None:
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        W.margins(box, 6)
        popover.set_child(box)
        self.modules_menu_box = box
        session = self.session
        for title, key in MENU_MODULES:
            if dash.module_allowed(session, key):
                box.append(W.button(title, lambda k=key: (popover.popdown(), self._show_module(k)), flat=True))
        tools = []
        if session.can("live"):
            tools.append(("Окно просмотра на другом экране…", self.open_view_window))
        if session.can("devices"):
            tools.append(("Настройка устройств…", lambda: self.open_system("devices")))
        if tools:
            box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            for title, callback in tools:
                box.append(W.button(title, lambda cb=callback: (popover.popdown(), cb()), flat=True))

    def apply_role(self):
        """Скрыть разделы, модули и кнопки, на которые у роли нет прав."""
        session = self.session
        self._fill_modules_menu()
        buttons = getattr(self, "header_buttons", {})
        if "search" in buttons:
            buttons["search"].set_visible(session.can("playback"))
        for key, permissions in (("maps", ("maps",)), ("stats", ("reports",)), ("ptz", ("ptz",)),
                                 ("files", ("live", "playback"))):
            section = getattr(self, "left_sections", {}).get(key)
            if section is not None:
                section.set_visible(any(session.can(p) for p in permissions))
        if hasattr(self, "right_panel"):
            self.right_panel.set_visible(self.capabilities().get("alarm", True)
                                         and (session.can("alarm_ack") or session.can("alarms")))
        if hasattr(self, "playback_mode_button"):
            self.playback_mode_button.set_sensitive(session.can("playback"))
        for index in range(len(getattr(self, "video_slots", []))):
            self._sync_tile_toolbar(index)
        if hasattr(self, "board_flow"):
            self._render_board()
        if (hasattr(self, "inner_window") and self.inner_window.get_visible()
                and not dash.module_allowed(session, self._current_module_key())):
            self._close_inner()

    def _current_module_key(self):
        page = self.stack.get_visible_child_name() if hasattr(self, "stack") else None
        return {"archive": "playback", "alarms": "events", "dashboard": "summary"}.get(page, page or "board")
