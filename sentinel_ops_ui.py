"""Модули «Парковка» и «Патрулирование» (GTK-часть)."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from gi.repository import GLib, Gtk

import sentinel_alarms as alarms
import sentinel_parking as parking
import sentinel_patrol as patrol
import sentinel_people as people
import sentinel_widgets as W
from sentinel_system import _dialog, _dropdown, _field, _form, _scroll, _spacer, _toolbar

WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
PATROL_KINDS = {patrol.DONE: "ok", patrol.VIOLATION: "bad", patrol.RUNNING: "warn", patrol.PLANNED: "muted",
                patrol.ON_TIME: "ok", patrol.EARLY: "warn", patrol.LATE: "warn", patrol.MISSED: "bad",
                patrol.WAITING: "muted"}


def _checks(container, items, chosen):
    """Список флажков [(флажок, id)]."""
    result = []
    for key, title in items:
        check = Gtk.CheckButton(label=title)
        check.set_active(key in chosen)
        container.append(check)
        result.append((check, key))
    return result


def _checked(checks):
    return [key for check, key in checks if check.get_active()]


def _scrolled_box(height=140):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    scroll = _scroll(box)
    scroll.set_size_request(-1, height)
    return box, scroll


class OpsMixin:
    def _ops_credentials(self, device):
        try:
            return self._get_device_credentials(device)
        except Exception:
            return None

    def _camera_title(self, device_id):
        device = next((d for d in self.devices if d["id"] == device_id), None)
        if device is None:
            return f"#{device_id}"
        return device.get("name") or device.get("host_name") or device["host"]

    # ================================================================== парковка
    def _parking_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(page, 12)
        self.parking_lot_choice = _dropdown(["Все стоянки"])
        self.parking_lot_choice.connect("notify::selected", lambda *_a: self._render_parking())
        page.append(_toolbar(self.parking_lot_choice,
                             W.button("＋ Стоянка", lambda: self.edit_parking_lot(None), "red-button"),
                             W.button("Изменить", self._edit_current_lot), W.button("Удалить", self._delete_current_lot),
                             _spacer(), W.button("Открыть шлагбаум", lambda: self._manual_parking("in")),
                             W.button("Ручной выезд", lambda: self._manual_parking("out"))))
        self.parking_cards = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        page.append(self.parking_cards)
        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)
        inside = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        W.margins(inside, 8)
        inside.append(_toolbar(W.label("Правый щелчок по строке — закрыть сессию вручную (машина уехала, выезд не "
                                       "распознан)", "muted"), _spacer()))
        self.parking_inside = W.Table(("Номер", "Стоянка", "Въезд", "На стоянке", "Списки"), selectable=False,
                                      on_context=self._parking_session_menu)
        inside.append(_scroll(self.parking_inside))
        notebook.append_page(inside, W.label("На стоянке"))
        history = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        W.margins(history, 8)
        today = date.today().isoformat()
        self.parking_from = Gtk.Entry(text=today)
        self.parking_to = Gtk.Entry(text=today)
        for entry in (self.parking_from, self.parking_to):
            entry.set_width_chars(11)
        self.parking_search = Gtk.SearchEntry(placeholder_text="Номер")
        self.parking_search.connect("search-changed", lambda *_a: self._render_parking_history())
        history.append(_toolbar(W.label("С", "muted"), self.parking_from, W.label("по", "muted"), self.parking_to,
                                self.parking_search, W.button("Найти", self._render_parking_history), _spacer(),
                                W.button("Экспорт CSV", self.export_parking)))
        self.parking_history = W.Table(("Время", "Стоянка", "Номер", "Направление", "Решение", "Причина",
                                        "Оператор"), selectable=False)
        history.append(_scroll(self.parking_history))
        notebook.append_page(history, W.label("Проезды"))
        page.append(notebook)
        return page

    def _current_lot(self):
        lots = self.__dict__.get("parking_lots_cache") or []
        index = self.parking_lot_choice.get_selected() - 1
        return lots[index] if 0 <= index < len(lots) else None

    def _render_parking(self):
        if not hasattr(self, "parking_cards"):
            return
        lots = parking.load_lots()
        names = ["Все стоянки"] + [lot["name"] for lot in lots]
        if names != self.__dict__.get("_parking_names"):
            self._parking_names = names
            self.parking_lots_cache = lots
            self.parking_lot_choice.set_model(Gtk.StringList.new(names))
        self.parking_lots_cache = lots
        current = self._current_lot()
        shown = [current] if current else lots
        self._clear_box(self.parking_cards)
        if not lots:
            self.parking_cards.append(W.label("Стоянок нет. Нажмите «＋ Стоянка»: выберите камеры распознавания "
                                              "номеров на въезде и выезде и тревожный выход шлагбаума.", "muted",
                                              wrap=True))
        for lot in shown:
            occ = parking.occupancy(lot)
            stats = parking.day_stats(lot["id"])
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            card.add_css_class("board-card")
            card.append(W.label(lot["name"], "section-title"))
            free = "без лимита" if occ["free"] is None else f"свободно {occ['free']} из {occ['capacity']}"
            card.append(W.label(f"Занято: {occ['used']} · {free}", "board-value" if occ["free"] != 0 else
                                "warning-text"))
            card.append(W.label(f"Сегодня: въезд {stats['in']} · выезд {stats['out']} · отказ {stats['denied']}",
                                "muted"))
            over = parking.overstays(lot)
            if over:
                card.append(W.label(f"⚠ Дольше {lot['overstay_hours']} ч: {len(over)}", "warning-text"))
            card.append(W.label(parking.MODE_TITLES[lot["mode"]], "muted"))
            self.parking_cards.append(card)
        sessions = parking.open_sessions(current["id"] if current else None)
        lot_names = {lot["id"]: lot["name"] for lot in lots}
        now = datetime.now()
        rows = []
        for s in sessions:
            minutes = parking.duration_minutes(s["entered_at"], now.strftime(parking.FMT))
            rows.append((s["plate"], lot_names.get(s["lot_id"], ""), s["entered_at"][5:16],
                         f"{minutes // 60} ч {minutes % 60:02d} мин", s["lists"] or "—"))
        self.parking_inside.set_rows(rows, [s["id"] for s in sessions])
        self._render_parking_history()

    def _parking_period(self):
        return self.parking_from.get_text().strip(), self.parking_to.get_text().strip() + " 23:59:59"

    def _render_parking_history(self):
        if not hasattr(self, "parking_history"):
            return
        current = self._current_lot()
        start, end = self._parking_period()
        passes = parking.load_passes(current["id"] if current else None, start, end, self.parking_search.get_text())
        names = {lot["id"]: lot["name"] for lot in self.__dict__.get("parking_lots_cache") or []}
        self.parking_passes = passes
        self.parking_history.set_rows([
            (p["time"][5:], names.get(p["lot_id"], ""), p["plate"], "→ въезд" if p["direction"] == "in" else "← выезд",
             W.status_badge("Пропущен" if p["decision"] == parking.ALLOW else "Отказ",
                            "ok" if p["decision"] == parking.ALLOW else "bad"),
             parking.REASONS.get(p["reason"], p["reason"]), p["username"]) for p in passes])

    def export_parking(self):
        passes = self.__dict__.get("parking_passes") or []
        if not passes:
            self.notify("Нет проездов за период")
            return
        start, _end = self._parking_period()
        path = Path.home() / f"sentinel_parking_{start}.csv"
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            parking.export_passes_csv(passes, parking.load_lots(), handle)
        self.notify(f"Сохранено: {path}")

    def _parking_session_menu(self, session_id, widget, x, y):
        W.popover_menu(widget, [("Закрыть сессию (машина уехала)", lambda: self._confirm(
            "Отметить выезд вручную?", lambda: (parking.close_session(session_id, self.session.username),
                                               alarms.log_operation(self.session.username, "Парковка: ручной выезд"),
                                               self._render_parking()), "Отметить"))], x, y)

    def _edit_current_lot(self):
        lot = self._current_lot()
        if lot is None:
            self.notify("Выберите стоянку в списке")
            return
        self.edit_parking_lot(lot)

    def _delete_current_lot(self):
        lot = self._current_lot()
        if lot is None:
            self.notify("Выберите стоянку в списке")
            return
        self._confirm(f"Удалить стоянку «{lot['name']}» и её историю?",
                      lambda: (parking.delete_lot(lot["id"]), self._render_parking()))

    def edit_parking_lot(self, lot):
        if not self.session.can("persons"):
            self.notify("Нет прав на настройку парковки", error=True)
            return
        cameras = [d for d in self.devices if d.get("category") != "access"]
        lists = people.load_vehicle_lists()
        outputs = [d for d in self.devices if d.get("category") != "access" or int(d.get("alarm_outputs") or 0)]
        lot = lot or {}
        dialog = _dialog(self, "Стоянка", "Сохранить", 640)
        form = _form()
        name = _field(form, 0, "Название *", Gtk.Entry(text=lot.get("name", "")))
        capacity = _field(form, 1, "Мест (0 — без лимита)", Gtk.SpinButton.new_with_range(0, 10000, 1))
        capacity.set_value(lot.get("capacity", 0))
        modes = list(parking.MODE_TITLES)
        mode = _field(form, 2, "Въезд", _dropdown(list(parking.MODE_TITLES.values()),
                                                  modes.index(lot.get("mode", parking.MODE_LISTS))))
        overstay = _field(form, 3, "Предупреждать после, ч (0 — нет)", Gtk.SpinButton.new_with_range(0, 720, 1))
        overstay.set_value(lot.get("overstay_hours", 0))
        camera_items = [(d["id"], self._camera_title(d["id"])) for d in cameras]
        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        groups = {}
        for key, title, chosen in (("entry", "Камеры въезда", lot.get("entry_cameras", [])),
                                   ("exit", "Камеры выезда", lot.get("exit_cameras", [])),
                                   ("allow", "Разрешённые списки", lot.get("allow_lists", [])),
                                   ("deny", "Чёрные списки", lot.get("deny_lists", []))):
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            column.set_hexpand(True)
            column.append(W.label(title, "section-title"))
            box, scroll = _scrolled_box()
            items = camera_items if key in ("entry", "exit") else [(v["id"], v["name"]) for v in lists]
            groups[key] = _checks(box, items, set(chosen))
            if not items:
                box.append(W.label("Нет камер" if key in ("entry", "exit") else "Нет списков ТС", "muted"))
            column.append(scroll)
            columns.append(column)
        form.attach(columns, 0, 4, 2, 1)
        barrier_choices = ["Нет"] + [self._camera_title(d["id"]) for d in outputs]

        def barrier_row(row, title, current):
            index = next((i + 1 for i, d in enumerate(outputs) if current and d["id"] == current.get("device_id")), 0)
            device = _dropdown(barrier_choices, index)
            output = Gtk.SpinButton.new_with_range(1, 16, 1)
            output.set_value((current or {}).get("output", 1))
            close = Gtk.SpinButton.new_with_range(0, 120, 1)
            close.set_value((current or {}).get("auto_close", 5))
            form.attach(W.label(title, "muted"), 0, row, 1, 1)
            form.attach(_toolbar(device, W.label("выход", "muted"), output, W.label("закрыть через, с", "muted"),
                                 close), 1, row, 1, 1)
            return lambda: ({"device_id": outputs[device.get_selected() - 1]["id"], "output": output.get_value_as_int(),
                             "auto_close": close.get_value_as_int()} if device.get_selected() > 0 else {})

        read_barrier = barrier_row(5, "Шлагбаум въезда", lot.get("barrier"))
        read_exit = barrier_row(6, "Шлагбаум выезда", lot.get("exit_barrier"))
        form.attach(W.label("Шлагбаум — тревожный выход камеры или контроллера. Если шлагбаум выезда не задан, "
                            "используется шлагбаум въезда.", "muted", wrap=True), 0, 7, 2, 1)
        outer = _scroll(form)
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        dialog.get_content_area().append(outer)
        dialog.set_default_size(720, 620)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    parking.save_lot({"name": name.get_text(), "capacity": capacity.get_value_as_int(),
                                      "mode": modes[mode.get_selected()], "overstay_hours": overstay.get_value_as_int(),
                                      "entry_cameras": _checked(groups["entry"]),
                                      "exit_cameras": _checked(groups["exit"]),
                                      "allow_lists": _checked(groups["allow"]), "deny_lists": _checked(groups["deny"]),
                                      "barrier": read_barrier(), "exit_barrier": read_exit()}, lot.get("id"))
                except (ValueError, sqlite3.Error) as exc:
                    self.notify(f"Не сохранено: {exc}", error=True)
                    return
                alarms.log_operation(self.session.username, f"Парковка: стоянка «{name.get_text().strip()}» сохранена")
                self._render_parking()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def _manual_parking(self, direction):
        lots = parking.load_lots()
        if not lots:
            self.notify("Сначала создайте стоянку")
            return
        current = self._current_lot()
        dialog = _dialog(self, "Открыть шлагбаум" if direction == "in" else "Ручной выезд", "Открыть")
        form = _form()
        lot_choice = _field(form, 0, "Стоянка", _dropdown([lot["name"] for lot in lots],
                                                          lots.index(current) if current in lots else 0))
        plate = _field(form, 1, "Номер *", Gtk.Entry())
        form.attach(W.label("Проезд записывается в историю с именем оператора.", "muted"), 0, 2, 2, 1)
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                lot = lots[lot_choice.get_selected()]
                try:
                    barrier = parking.manual_pass(lot["id"], plate.get_text(), direction, self.session.username)
                except ValueError as exc:
                    self.notify(str(exc), error=True)
                    return
                alarms.log_operation(self.session.username,
                                     f"Парковка «{lot['name']}»: ручной {'въезд' if direction == 'in' else 'выезд'} "
                                     f"{plate.get_text().strip().upper()}")
                self._open_barrier(barrier)
                self._render_parking()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def _open_barrier(self, barrier):
        if not barrier:
            return
        try:
            self.trigger_alarm_output(barrier["device_id"], barrier["output"], barrier.get("auto_close", 5),
                                      quiet=True)
        except Exception as exc:
            self.notify(f"Шлагбаум не открыт: {exc}", error=True)

    def _on_parking_plate(self, event):
        """ANPR-событие: решение стоянки и шлагбаум (если нет сервера Sentinel — он делает это сам)."""
        if self._server_link() is not None or not event.get("plate"):
            return
        names = {v["id"]: v["name"] for v in people.load_vehicle_lists()}
        result = parking.handle_plate(event.get("device_id"), event["plate"], people.vehicle_index(),
                                      list_names=names)
        if result is None:
            return
        if result["decision"] == parking.ALLOW:
            self._open_barrier(result["barrier"])
        else:
            alarms.log_operation("sentinel", f"Парковка «{result['lot']['name']}»: отказ {result['plate']} — "
                                 f"{parking.REASONS[result['reason']]}")
            GLib.idle_add(self.handle_event, {
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "device_id": event.get("device_id"), "device_name": result["lot"]["name"], "channel": "",
                "raw_type": "parking", "event_type": "parking_denied", "category": "vehicle", "state": "active",
                "plate": result["plate"], "details": {"eventDescription": parking.REASONS[result["reason"]]}})
        if hasattr(self, "parking_cards") and self.stack.get_visible_child_name() == "parking":
            self._render_parking()

    # ================================================================== патрулирование
    def _patrol_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(page, 12)
        today = date.today()
        self.patrol_from = Gtk.Entry(text=(today - timedelta(days=1)).isoformat())
        self.patrol_to = Gtk.Entry(text=today.isoformat())
        for entry in (self.patrol_from, self.patrol_to):
            entry.set_width_chars(11)
        self.patrol_issues = Gtk.CheckButton(label="Только нарушения")
        self.patrol_issues.connect("toggled", lambda *_a: self._render_patrol())
        page.append(_toolbar(W.button("＋ Маршрут", lambda: self.edit_patrol_route(None), "red-button"),
                             W.label("С", "muted"), self.patrol_from, W.label("по", "muted"), self.patrol_to,
                             W.button("Показать", self._render_patrol), self.patrol_issues, _spacer(),
                             W.button("Экспорт CSV", self.export_patrol)))
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        body.set_vexpand(True)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left.set_size_request(250, -1)
        left.append(W.label("Маршруты", "section-title"))
        self.patrol_routes_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        left.append(_scroll(self.patrol_routes_box))
        body.append(left)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right.set_hexpand(True)
        self.patrol_summary = W.label("", "muted")
        right.append(self.patrol_summary)
        self.patrol_runs = W.Table(("Начало", "Маршрут", "Отмечено", "Статус", "Точки"), selectable=False,
                                   on_activate=self._show_patrol_run)
        right.append(_scroll(self.patrol_runs))
        body.append(right)
        page.append(body)
        page.append(W.label("Контрольная точка — считыватель двери: охранник прикладывает карту. Пропущенная точка "
                            "создаёт событие «Патруль: пропущена точка» (для него можно настроить тревогу).",
                            "muted", wrap=True))
        return page

    def _patrol_employees(self):
        return {p["id"]: str(p["employee_no"]) for p in people.load_persons()}

    def _render_patrol(self):
        if not hasattr(self, "patrol_runs"):
            return
        routes = patrol.load_routes()
        self._clear_box(self.patrol_routes_box)
        if not routes:
            self.patrol_routes_box.append(W.label("Маршрутов нет — нажмите «＋ Маршрут»", "muted", wrap=True))
        for route in routes:
            text = (f"{'⛯' if route['enabled'] else '○'} {route['name']}\n    точек: {len(route['points'])} · "
                    f"{', '.join(route['starts'])}")
            button = W.list_row(text, lambda r=route: self.edit_patrol_route(r))
            W.on_right_click(button, lambda w, x, y, r=route: W.popover_menu(w, [
                ("Изменить", lambda: self.edit_patrol_route(r)),
                ("Удалить", lambda: self._confirm(f"Удалить маршрут «{r['name']}»?", lambda: (
                    patrol.delete_route(r["id"]), self._render_patrol())))], x, y))
            self.patrol_routes_box.append(button)
        try:
            start = date.fromisoformat(self.patrol_from.get_text().strip())
            end = date.fromisoformat(self.patrol_to.get_text().strip())
            runs = patrol.runs_for_period(routes, self._patrol_employees(), start, end)
        except ValueError as exc:
            self.notify(f"Патруль: {exc}", error=True)
            return
        runs = [r for r in runs if r["status"] != patrol.PLANNED]
        counts = {k: sum(1 for r in runs if r["status"] == k) for k in patrol.RUN_TITLES}
        self.patrol_summary.set_text(f"Обходов: {len(runs)} · выполнено: {counts[patrol.DONE]} · с нарушениями: "
                                     f"{counts[patrol.VIOLATION]} · идёт: {counts[patrol.RUNNING]}")
        if self.patrol_issues.get_active():
            runs = [r for r in runs if r["status"] == patrol.VIOLATION]
        self.patrol_runs_cache = runs
        marks = {patrol.ON_TIME: "●", patrol.EARLY: "◐", patrol.LATE: "◑", patrol.MISSED: "✕", patrol.WAITING: "○"}
        self.patrol_runs.set_rows([
            (r["start"].strftime("%d.%m %H:%M"), r["route"], f"{r['done']} / {r['total']}",
             W.status_badge(patrol.RUN_TITLES[r["status"]], PATROL_KINDS[r["status"]]),
             " ".join(marks[p["status"]] for p in r["points"])) for r in runs], list(range(len(runs))))

    def _show_patrol_run(self, index):
        runs = self.__dict__.get("patrol_runs_cache") or []
        if not 0 <= index < len(runs):
            return
        run = runs[index]
        names = {str(p["employee_no"]): p["name"] for p in people.load_persons()}
        dialog = _dialog(self, f"Обход «{run['route']}» · {run['start']:%d.%m.%Y %H:%M}", None, 560)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        W.margins(box, 12)
        table = W.Table(("Точка", "План", "Факт", "Охранник", "Отметка"), selectable=False)
        table.set_rows([(p["name"], p["due"].strftime("%H:%M"), p["time"].strftime("%H:%M:%S") if p["time"] else "—",
                         names.get(p["guard"], p["guard"]) or "—",
                         W.status_badge(patrol.POINT_TITLES[p["status"]], PATROL_KINDS[p["status"]]))
                        for p in run["points"]])
        box.append(table)
        dialog.get_content_area().append(box)
        dialog.connect("response", lambda dlg, *_a: dlg.close())
        dialog.present()

    def export_patrol(self):
        runs = self.__dict__.get("patrol_runs_cache") or []
        if not runs:
            self.notify("Нет обходов за период")
            return
        path = Path.home() / f"sentinel_patrol_{self.patrol_from.get_text().strip()}.csv"
        names = {str(p["employee_no"]): p["name"] for p in people.load_persons()}
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            patrol.export_runs_csv(runs, handle, names)
        self.notify(f"Сохранено: {path}")

    def edit_patrol_route(self, route):
        if not self.session.can("alarms"):
            self.notify("Нет прав на настройку патрулирования", error=True)
            return
        controllers = [d for d in self.devices if d.get("category") == "access"]
        doors = [(d, n) for d in controllers for n in range(1, max(1, int(d.get("door_count") or 1)) + 1)]
        if not doors:
            self.notify("Нет контроллеров доступа: контрольные точки — это считыватели дверей")
            return
        route = route or {}
        dialog = _dialog(self, "Маршрут патрулирования", "Сохранить", 680)
        form = _form()
        name = _field(form, 0, "Название *", Gtk.Entry(text=route.get("name", "")))
        starts = _field(form, 1, "Начало обходов", Gtk.Entry(text=", ".join(route.get("starts", ["22:00"])),
                                                              placeholder_text="22:00, 02:00"))
        days = [Gtk.CheckButton(label=d) for d in WEEKDAYS]
        for i, check in enumerate(days):
            check.set_active(i in route.get("days", range(7)))
        form.attach(W.label("Дни", "muted"), 0, 2, 1, 1)
        form.attach(_toolbar(*days), 1, 2, 1, 1)
        tolerance = _field(form, 3, "Допуск, мин", Gtk.SpinButton.new_with_range(1, 60, 1))
        tolerance.set_value(route.get("tolerance", 5))
        enabled = Gtk.CheckButton(label="Маршрут включён")
        enabled.set_active(route.get("enabled", True))
        form.attach(enabled, 1, 4, 1, 1)
        form.attach(W.label("Контрольные точки по порядку (минуты от начала обхода)", "section-title"), 0, 5, 2, 1)
        points_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        form.attach(points_box, 0, 6, 2, 1)
        rows = []
        door_titles = [f"{d.get('host_name') or d['host']} · Дверь {n}" for d, n in doors]

        def add_point(point=None):
            point = point or {}
            index = next((i for i, (d, n) in enumerate(doors) if d["id"] == point.get("device_id")
                          and n == point.get("door")), 0)
            title = Gtk.Entry(text=point.get("name", f"Точка {len(rows) + 1}"))
            door = _dropdown(door_titles, index)
            offset = Gtk.SpinButton.new_with_range(0, 600, 1)
            offset.set_value(point.get("offset", 10 * len(rows)))
            line = _toolbar(title, door, W.label("мин", "muted"), offset)
            entry = (line, title, door, offset)
            line.append(W.button("✕", lambda: (rows.remove(entry), points_box.remove(line)), flat=True))
            rows.append(entry)
            points_box.append(line)

        for point in route.get("points", []) or [None, None]:
            add_point(point)
        form.attach(W.button("＋ Точка", lambda: add_point()), 0, 7, 1, 1)
        form.attach(W.label("Охранники", "section-title"), 0, 8, 2, 1)
        guard_box, guard_scroll = _scrolled_box(120)
        guards = _checks(guard_box, [(p["id"], f"{p['name']} ({p['employee_no']})") for p in people.load_persons()],
                         set(route.get("guards", [])))
        form.attach(guard_scroll, 0, 9, 2, 1)
        dialog.get_content_area().append(_scroll(form))
        dialog.set_default_size(700, 640)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    patrol.save_route({
                        "name": name.get_text(), "enabled": enabled.get_active(),
                        "starts": [s.strip() for s in starts.get_text().replace(";", ",").split(",") if s.strip()],
                        "days": [i for i, c in enumerate(days) if c.get_active()],
                        "tolerance": tolerance.get_value_as_int(), "guards": _checked(guards),
                        "points": [{"name": t.get_text(), "device_id": doors[d.get_selected()][0]["id"],
                                    "door": doors[d.get_selected()][1], "offset": o.get_value_as_int()}
                                   for _l, t, d, o in rows]}, route.get("id"))
                except (ValueError, sqlite3.Error) as exc:
                    self.notify(f"Не сохранено: {exc}", error=True)
                    return
                alarms.log_operation(self.session.username, f"Патруль: маршрут «{name.get_text().strip()}» сохранён")
                self._render_patrol()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def _patrol_tick(self):
        """Раз в минуту: пропущенные точки → событие (если нет сервера Sentinel — он проверяет сам)."""
        if self._server_link() is not None:
            return True
        routes = [r for r in patrol.load_routes() if r["enabled"]]
        if routes:
            for route, run, point in patrol.new_missed_points(routes, self._patrol_employees()):
                self.handle_event(patrol.missed_event(route, run, point))
        return True

    def _start_patrol_timer(self):
        if not self.__dict__.get("_patrol_timer"):
            self._patrol_timer = GLib.timeout_add_seconds(60, self._patrol_tick)
