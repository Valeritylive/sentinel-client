"""Модуль «Контроль доступа» (GTK-часть): мониторинг дверей и проходов,
расширенные правила, посетители, учёт рабочего времени и журнал контроллера.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from gi.repository import GLib, Gtk

import sentinel_access_rules as ar
import sentinel_alarms as alarms
import sentinel_attendance as att
import sentinel_people as people
import sentinel_storage as storage
import sentinel_visitors as visitors
import sentinel_widgets as W
from sentinel_system import _dialog, _dropdown, _field, _form, _scroll, _spacer, _toolbar

FEED_LIMIT = 200
VISITOR_FILTERS = (("Сегодня", None, "today"), ("Ожидают согласования", visitors.PENDING, None),
                   ("На объекте", visitors.INSIDE, None), ("Все", None, None))
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _time_entry(value):
    entry = Gtk.Entry(text=value)
    entry.set_max_length(5)
    entry.set_width_chars(6)
    return entry


class AccessCenterMixin:
    # ================================================================== каркас
    def _access_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        W.margins(page, 10)
        self.access_apply_label = W.label("", "muted")
        self.access_apply_button = W.button("Применить изменения", self.apply_access_changes, "red-button")
        page.append(_toolbar(_spacer(), self.access_apply_label,
                             self.access_apply_button))
        self.access_notebook = Gtk.Notebook()
        self.access_notebook.set_vexpand(True)
        for builder, title in ((self._access_monitor_tab, "Мониторинг"), (self._access_rules_tab, "Правила"),
                               (self._visitors_tab, "Посетители"), (self._attendance_tab, "Учёт времени"),
                               (self._access_log_page, "Журнал контроллера")):
            self.access_notebook.append_page(builder(), W.label(title))
        self.access_notebook.connect("switch-page", lambda *_a: GLib.idle_add(self.refresh_access_center))
        page.append(self.access_notebook)
        return page

    def _access_controllers(self):
        return [d for d in self.devices if d.get("category") == "access"]

    def _controller_name(self, device_id):
        device = next((d for d in self.devices if d["id"] == device_id), None)
        return (device.get("host_name") or device.get("name") or device["host"]) if device else f"#{device_id}"

    def _access_credentials_for(self, device):
        """Пароль контроллера или None (без исключений хранилища паролей)."""
        try:
            return self._get_device_credentials(device)
        except Exception:
            return None

    def refresh_access_center(self):
        if not hasattr(self, "access_notebook"):
            return False
        self._update_access_apply_indicator()
        page = self.access_notebook.get_current_page()
        refresh = {0: self._render_access_monitor, 1: self._render_access_rules, 2: self._render_visitors,
                   3: lambda: None}.get(page)
        if refresh:
            refresh()
        if page == 4:
            self._refresh_archive_choices()
        return False

    # ================================================================== «Не применено» / «Применить»
    def _update_access_apply_indicator(self):
        if not hasattr(self, "access_apply_label"):
            return
        persons = len(people.pending_persons())
        rules_count = len(ar.pending_rules())
        if persons or rules_count:
            parts = [f"персон: {persons}"] if persons else []
            parts += [f"правил: {rules_count}"] if rules_count else []
            self.access_apply_label.set_text("⚠ Не применено на контроллерах — " + ", ".join(parts))
            self.access_apply_label.add_css_class("warning-text")
        else:
            self.access_apply_label.set_text("✓ Все изменения применены")
            self.access_apply_label.remove_css_class("warning-text")
        self.access_apply_button.set_sensitive(bool(persons or rules_count) and self.session.can("access"))

    def apply_access_changes(self):
        if not self.session.can("access"):
            self.notify("Нет прав на изменение доступа", error=True)
            return
        pending = people.pending_persons()
        if pending:
            self.apply_access_to_devices(pending, self.refresh_access_center)
        if ar.pending_devices():
            self.apply_access_rules(self.refresh_access_center)
        if not pending and not ar.pending_devices():
            self.notify("Нечего применять")

    def apply_access_rules(self, on_done=None):
        devices = {d["id"]: d for d in self.devices}
        jobs = []
        for device_id in ar.pending_devices():
            device = devices.get(device_id)
            credentials = self._access_credentials_for(device) if device else None
            jobs.append((device_id, device, credentials, [r["id"] for r in ar.pending_rules()
                                                          if r["device_id"] == device_id]))

        def worker():
            ok = failed = 0
            for device_id, device, credentials, rule_ids in jobs:
                error = ""
                if not device or not credentials:
                    error = "нет устройства или пароля"
                else:
                    try:
                        for path, body in ar.device_payloads(device_id, device.get("door_count")):
                            self._isapi_request(device, credentials, path, "PUT", json.dumps(body), "application/json")
                    except Exception as exc:
                        error = str(exc)[:200]
                for rule_id in rule_ids:
                    ar.set_status(rule_id, ar.FAILED if error else ar.APPLIED, error)
                failed, ok = failed + bool(error), ok + (not error)
            alarms.log_operation(self.session.username, "Применение правил доступа", f"{ok} ok / {failed} ошибок")
            GLib.idle_add(self.notify, f"Правила доступа: контроллеров {ok}, ошибок {failed}", bool(failed))
            if on_done:
                GLib.idle_add(on_done)

        threading.Thread(target=worker, daemon=True).start()

    def remove_person_from_devices(self, employee_no, device_ids):
        """Стереть персону (например, ушедшего посетителя) с контроллеров."""
        devices = [d for d in self.devices if d["id"] in set(device_ids)]
        jobs = [(d, self._access_credentials_for(d)) for d in devices]

        def worker():
            errors = []
            for device, credentials in jobs:
                try:
                    if not credentials:
                        raise RuntimeError("нет пароля")
                    self._isapi_request(device, credentials, "/ISAPI/AccessControl/UserInfo/Delete?format=json",
                                        "PUT", json.dumps(people.user_delete_payload([employee_no])),
                                        "application/json")
                except Exception as exc:
                    errors.append(f"{device.get('host_name') or device['host']}: {exc}")
            if errors:
                GLib.idle_add(self.notify, f"Пропуск {employee_no} не удалён: " + "; ".join(errors), True)

        if jobs:
            threading.Thread(target=worker, daemon=True).start()

    # ================================================================== мониторинг
    def _access_monitor_tab(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(page, 10)
        self.access_area = _dropdown(["Все области"])
        self.access_area.connect("notify::selected", lambda *_a: self._render_access_doors())
        page.append(_toolbar(W.label("Область", "muted"), self.access_area,
                             W.button("Открыть все двери области", lambda: self._area_doors_command("always_open"),
                                      "red-button"),
                             W.button("Закрыть все", lambda: self._area_doors_command("always_close")),
                             W.button("Обычный режим", lambda: self._area_doors_command("resume")),
                             _spacer(), W.button("⟳ Состояние", lambda: self.refresh_door_states(
                                 self._access_controllers()), flat=True)))
        self.access_doors_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                                            column_spacing=8, row_spacing=8, homogeneous=True)
        self.access_doors_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.access_doors_holder.append(self.access_doors_box)
        page.append(self.access_doors_holder)
        self.access_feed_denied = Gtk.CheckButton(label="Только отказы и нарушения")
        self.access_feed_denied.connect("toggled", lambda *_a: self._render_access_feed())
        page.append(_toolbar(W.label("Проходы в реальном времени", "section-title"), _spacer(),
                             self.access_feed_denied))
        self.access_feed_table = W.Table(("Время", "Персона", "Контроллер · дверь", "Результат", "Примечание"),
                                         selectable=False)
        page.append(_scroll(self.access_feed_table))
        return page

    def _access_areas(self):
        controllers = self._access_controllers()
        groups = {g["id"]: g["name"] for g in getattr(self, "groups", [])}
        ids = sorted({d.get("group_id") for d in controllers if d.get("group_id") in groups},
                     key=lambda i: groups[i])
        return [(None, "Все области")] + [(i, groups[i]) for i in ids]

    def _selected_area_doors(self):
        areas = self.__dict__.get("access_area_items") or [(None, "Все области")]
        index = self.access_area.get_selected()
        area = areas[index][0] if 0 <= index < len(areas) else None
        doors = []
        for device in self._access_controllers():
            if area is not None and device.get("group_id") != area:
                continue
            for door in range(1, max(1, int(device.get("door_count") or 1)) + 1):
                doors.append((device, door))
        return area, doors

    def _render_access_monitor(self):
        areas = self._access_areas()
        if areas != self.__dict__.get("access_area_items"):
            self.access_area_items = areas
            self.access_area.set_model(Gtk.StringList.new([name for _i, name in areas]))
        self._render_access_doors()
        self._render_access_feed()

    def _render_access_doors(self):
        if not hasattr(self, "access_doors_holder"):
            return
        self._clear_box(self.access_doors_holder)
        self.access_doors_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                                            column_spacing=8, row_spacing=8, homogeneous=True)
        self.access_doors_holder.append(self.access_doors_box)
        _area, doors = self._selected_area_doors()
        if not doors:
            self.access_doors_box.append(W.label("Нет контроллеров доступа. Добавьте устройство с типом "
                                                 "«Контроль доступа» в разделе Система → Устройства.", "muted",
                                                 wrap=True))
            return
        can = self.session.can("access")
        for device, door in doors:
            state = self.door_states.get((device["id"], door), "Неизвестно")
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("door-card")
            card.add_css_class({"Открыта": "door-open", "Закрыта": "door-closed"}.get(state, "door-unknown"))
            card.append(W.label(f"▣ Дверь {door}", "section-title"))
            card.append(W.label(device.get("host_name") or device["host"], "muted"))
            card.append(W.label(state, "door-state"))
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            for command, text in (("open", "Открыть"), ("always_open", "⇥"), ("always_close", "⊘"),
                                  ("resume", "↺")):
                button = W.button(text, lambda d=device, n=door, c=command: self.door_command(d, n, c),
                                  tooltip=people.DOOR_TITLES[command])
                button.set_sensitive(can)
                row.append(button)
            card.append(row)
            self.access_doors_box.append(card)

    def _area_doors_command(self, command):
        area, doors = self._selected_area_doors()
        if not doors:
            self.notify("В области нет дверей")
            return
        title = next((name for i, name in self.__dict__.get("access_area_items", []) if i == area), "Все области")

        def run():
            for device, door in doors:
                self.door_command(device, door, command, quiet=True)
            alarms.log_operation(self.session.username, f"{title}: все двери — {people.DOOR_TITLES[command]}")
            self.notify(f"«{people.DOOR_TITLES[command]}» отправлено на {len(doors)} дверей ({title})")

        if command == "resume":
            run()
        else:
            self._confirm(f"{people.DOOR_TITLES[command]}: {len(doors)} дверей ({title})?", run,
                          people.DOOR_TITLES[command])

    # ------------------------------------------------------------ лента проходов
    def _access_feed(self):
        feed = self.__dict__.get("access_feed")
        if feed is None:
            feed = []
            with storage.database() as db:
                rows = [dict(r) for r in db.execute(
                    "SELECT * FROM events WHERE event_type='access' ORDER BY time DESC LIMIT ?", (FEED_LIMIT,))]
            for row in rows:
                feed.append(self._feed_item(row, ""))
            self.access_feed = feed
        return feed

    def _feed_item(self, event, note):
        details = ar._details(event)
        employee = ar.event_person(event)
        names = self.__dict__.setdefault("_employee_names", {})
        if employee and employee not in names:
            names.update({str(p["employee_no"]): p["name"] for p in people.load_persons()})
        who = details.get("name") or names.get(employee) or employee or details.get("cardNo") or "—"
        outcome = {"granted": "Проход", "denied": "Отказ"}.get(ar.event_outcome(event), "Событие")
        return {"time": str(event.get("time", ""))[:19].replace("T", " "), "who": who,
                "where": f"{event.get('device_name') or self._controller_name(event.get('device_id'))} · "
                         f"{ar.event_door(event) or '—'}",
                "outcome": outcome, "note": note}

    def _on_access_event(self, event):
        """Вызывается из handle_event для событий контроля доступа."""
        rules = ar.load_rules()
        notes = []
        monitor = self.__dict__.setdefault("_passback_monitor", ar.PassbackMonitor())
        if monitor.check(event, rules):
            notes.append("⚠ анти-passback: повторный проход в том же направлении")
        tracker = self.__dict__.setdefault("_first_person", ar.FirstPersonTracker())
        # При подключении к серверу Sentinel правило выполняет сервер (он работает и без клиента).
        if self._server_link() is None and any(r["kind"] == ar.FIRST_PERSON for r in rules):
            employees = {p["id"]: str(p["employee_no"]) for p in people.load_persons()}
            for device_id, door, command, rule in tracker.on_event(event, rules, employees):
                self._auto_door(device_id, door, command, rule)
                notes.append(f"первый вошедший: дверь {door} открыта до {rule['config']['until']}")
            self._ensure_first_person_timer()
        feed = self._access_feed()
        feed.insert(0, self._feed_item(event, "; ".join(notes)))
        del feed[FEED_LIMIT:]
        if hasattr(self, "access_feed_table") and self.access_notebook.get_current_page() == 0:
            self._render_access_feed()

    def _auto_door(self, device_id, door, command, rule):
        device = next((d for d in self.devices if d["id"] == device_id), None)
        if device is None or not self._access_credentials_for(device):
            return
        alarms.log_operation("sentinel", f"Правило «{rule['name']}»: дверь {door} — {people.DOOR_TITLES[command]}",
                             device["host"])
        self.door_command(device, door, command, quiet=True)

    def _ensure_first_person_timer(self):
        if self.__dict__.get("_first_person_timer"):
            return

        def tick():
            tracker = self.__dict__.get("_first_person")
            if tracker is None or not tracker.opened:
                self._first_person_timer = 0
                return False
            for device_id, door, command, rule in tracker.due_resume(ar.load_rules(ar.FIRST_PERSON)):
                self._auto_door(device_id, door, command, rule)
            return True

        self._first_person_timer = GLib.timeout_add_seconds(60, tick)

    def _render_access_feed(self):
        if not hasattr(self, "access_feed_table"):
            return
        items = self._access_feed()
        if self.access_feed_denied.get_active():
            items = [i for i in items if i["outcome"] == "Отказ" or i["note"].startswith("⚠")]
        self.access_feed_table.set_rows([(i["time"], i["who"], i["where"],
                                          W.status_badge(i["outcome"], "ok" if i["outcome"] == "Проход" else "bad"),
                                          i["note"]) for i in items[:FEED_LIMIT]])

    # ================================================================== правила
    def _access_rules_tab(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(page, 10)
        self.access_rule_add = W.button("＋ Правило", self._add_rule_menu, "red-button")
        page.append(_toolbar(self.access_rule_add, W.button("Изменить", self._edit_selected_rule),
                             W.button("Вкл/выкл", self._toggle_selected_rules),
                             W.button("Удалить", self._delete_selected_rules), _spacer(),
                             W.button("Применить", self.apply_access_changes)))
        page.append(W.label("Анти-passback и шлюз записываются в контроллер кнопкой «Применить» и работают без "
                            "Sentinel. «Первый вошедший» выполняет сервер Sentinel, а без сервера — этот клиент, "
                            "пока он запущен.", "muted", wrap=True))
        self.access_rule_table = W.Table(("Название", "Тип", "Контроллер", "Двери", "Состояние", "Статус"),
                                         on_activate=lambda rid: self.edit_access_rule(ar.get_rule(rid)))
        page.append(_scroll(self.access_rule_table))
        return page

    def _rule_doors_text(self, rule):
        config = rule["config"]
        if rule["kind"] == ar.INTERLOCK:
            return " | ".join("+".join(str(d) for d in g) for g in config["groups"])
        if rule["kind"] == ar.ANTIPASSBACK:
            arrow = " → " if config["mode"] == "route" else ", "
            return arrow.join(str(d) for d in config["doors"]) + (" (маршрут)" if config["mode"] == "route" else "")
        return f"{config['door']} · до {config['until']} · людей: {len(config['person_ids'])}"

    def _render_access_rules(self):
        if not hasattr(self, "access_rule_table"):
            return
        rows, keys = [], []
        for rule in ar.load_rules():
            status = "Выполняет Sentinel" if rule["kind"] in ar.SOFTWARE_KINDS else ar.STATUS_TITLES[rule["status"]]
            badge = W.status_badge(status, {"applied": "ok", "failed": "bad"}.get(rule["status"], "warn"))
            if rule["message"]:
                badge.set_tooltip_text(rule["message"])
            rows.append((rule["name"], ar.KIND_TITLES[rule["kind"]], self._controller_name(rule["device_id"]),
                         self._rule_doors_text(rule), "включено" if rule["enabled"] else "выключено", badge))
            keys.append(rule["id"])
        self.access_rule_table.set_rows(rows, keys)

    def _add_rule_menu(self):
        if not self._access_controllers():
            self.notify("Нет контроллеров доступа")
            return
        W.popover_menu(self.access_rule_add, [(ar.KIND_TITLES[k], lambda k=k: self.edit_access_rule(None, k))
                                              for k in (ar.ANTIPASSBACK, ar.INTERLOCK, ar.FIRST_PERSON)])

    def _edit_selected_rule(self):
        selected = self.access_rule_table.selected()
        if len(selected) != 1:
            self.notify("Отметьте одно правило")
            return
        self.edit_access_rule(ar.get_rule(selected[0]))

    def _toggle_selected_rules(self):
        for rule_id in self.access_rule_table.selected():
            rule = ar.get_rule(rule_id)
            if rule:
                ar.set_enabled(rule_id, not rule["enabled"])
        self.refresh_access_center()

    def _delete_selected_rules(self):
        selected = self.access_rule_table.selected()
        if not selected:
            return
        self._confirm(f"Удалить правил: {len(selected)}? Аппаратные правила снимутся с контроллера после "
                      "«Применить».", lambda: ([ar.delete_rule(i) for i in selected], self.refresh_access_center()))

    def edit_access_rule(self, rule, kind=None):
        if rule is None and kind is None:
            return
        if not self.session.can("access"):
            self.notify("Нет прав на изменение доступа", error=True)
            return
        kind = rule["kind"] if rule else kind
        controllers = self._access_controllers()
        if not controllers:
            self.notify("Нет контроллеров доступа")
            return
        config = rule["config"] if rule else {}
        dialog = _dialog(self, ar.KIND_TITLES[kind], "Сохранить", 560)
        form = _form()
        form.attach(W.label(ar.KIND_HINTS[kind], "muted", wrap=True), 0, 0, 2, 1)
        name = _field(form, 1, "Название", Gtk.Entry(text=rule["name"] if rule else ar.KIND_TITLES[kind]))
        current = next((i for i, d in enumerate(controllers) if rule and d["id"] == rule["device_id"]), 0)
        controller = _field(form, 2, "Контроллер", _dropdown(
            [d.get("host_name") or d["host"] for d in controllers], current))
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        form.attach(holder, 0, 3, 2, 1)
        state = {}

        def door_count():
            return max(1, int(controllers[controller.get_selected()].get("door_count") or 1))

        def build(*_args):
            self._clear_box(holder)
            count = door_count()
            if kind == ar.ANTIPASSBACK:
                mode = _dropdown(["Вход и выход (для каждой двери)", "Маршрут (двери по порядку)"],
                                 1 if config.get("mode") == "route" else 0)
                holder.append(_toolbar(W.label("Режим", "muted"), mode))
                holder.append(W.label("Двери (для маршрута — номера через запятую в порядке прохода)", "muted"))
                doors = Gtk.Entry(text=", ".join(str(d) for d in config.get("doors", [])) or
                                  ", ".join(str(d) for d in range(1, count + 1)))
                holder.append(doors)
                state.update(read=lambda: {"mode": "route" if mode.get_selected() else "in_out",
                                           "doors": _numbers(doors.get_text())})
            elif kind == ar.INTERLOCK:
                holder.append(W.label("Группы дверей: одна строка — одна группа, номера через запятую или «+»",
                                      "muted", wrap=True))
                text = Gtk.TextView()
                text.set_size_request(-1, 90)
                text.get_buffer().set_text("\n".join(", ".join(str(d) for d in g) for g in config.get("groups", []))
                                           or ", ".join(str(d) for d in range(1, min(count, 2) + 1)))
                holder.append(text)

                def read():
                    buffer = text.get_buffer()
                    raw = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
                    return {"groups": [_numbers(line) for line in raw.splitlines() if line.strip()]}
                state.update(read=read)
            else:
                door = Gtk.SpinButton.new_with_range(1, count, 1)
                door.set_value(min(count, config.get("door", 1)))
                until = _time_entry(config.get("until", "18:00"))
                holder.append(_toolbar(W.label("Дверь", "muted"), door, W.label("Держать открытой до", "muted"),
                                       until))
                holder.append(W.label("Кто открывает дверь на день", "section-title"))
                checks = []
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                chosen = set(config.get("person_ids", []))
                for person in people.load_persons():
                    check = Gtk.CheckButton(label=f"{person['name']} ({person['employee_no']})")
                    check.set_active(person["id"] in chosen)
                    checks.append((check, person["id"]))
                    box.append(check)
                if not checks:
                    box.append(W.label("Нет персон: добавьте их в Система → Персона", "muted"))
                scroll = _scroll(box)
                scroll.set_size_request(-1, 200)
                holder.append(scroll)
                state.update(read=lambda: {"door": door.get_value_as_int(), "until": until.get_text().strip(),
                                           "person_ids": [pid for c, pid in checks if c.get_active()]})

        controller.connect("notify::selected", build)
        build()
        enabled = Gtk.CheckButton(label="Правило включено")
        enabled.set_active(rule["enabled"] if rule else True)
        form.attach(enabled, 0, 4, 2, 1)
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    device = controllers[controller.get_selected()]
                    ar.save_rule(kind, name.get_text(), device["id"], state["read"](), rule["id"] if rule else None,
                                 enabled.get_active(), device.get("door_count") or 1)
                except (ValueError, sqlite3.Error) as exc:
                    self.notify(f"Не сохранено: {exc}", error=True)
                    return
                alarms.log_operation(self.session.username, f"Правило доступа «{name.get_text()}» сохранено")
                self.refresh_access_center()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    # ================================================================== посетители
    def _visitors_tab(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(page, 10)
        self.visitor_counters = W.label("", "section-title")
        self.visitor_approval = Gtk.CheckButton(label="Требовать согласование заявок")
        self.visitor_approval.set_active(visitors.approval_required())
        self.visitor_approval.connect("toggled", lambda b: visitors.set_approval_required(b.get_active()))
        page.append(_toolbar(self.visitor_counters, _spacer(), self.visitor_approval))
        self.visitor_filter = _dropdown([title for title, _s, _d in VISITOR_FILTERS])
        self.visitor_filter.connect("notify::selected", lambda *_a: self._render_visitors())
        self.visitor_search = Gtk.SearchEntry(placeholder_text="Имя, компания, телефон, пропуск")
        self.visitor_search.connect("search-changed", lambda *_a: self._render_visitors())
        page.append(_toolbar(W.button("＋ Заявка", lambda: self.edit_visitor(), "red-button"),
                             W.button("Согласовать", lambda: self._decide_visitors(True)),
                             W.button("Отклонить", lambda: self._decide_visitors(False)),
                             W.button("Вход", self._check_in_selected), W.button("Выход", self._check_out_selected),
                             W.button("Пропуск", self._print_selected_badges),
                             W.button("Стоп-лист…", self.manage_watchlist), _spacer(),
                             self.visitor_filter, self.visitor_search))
        self.visitor_table = W.Table(("Посетитель", "Компания", "К кому", "Визит", "Пропуск", "Статус"),
                                     on_context=self._visitor_menu)
        page.append(_scroll(self.visitor_table))
        return page

    def _render_visitors(self):
        if not hasattr(self, "visitor_table"):
            return
        c = visitors.counters()
        self.visitor_counters.set_text(f"Ожидают согласования: {c['pending']}  ·  Ожидаются сегодня: {c['expected']}"
                                       f"  ·  На объекте: {c['inside']}  ·  Просрочили выход: {c['overdue']}")
        _title, status, day = VISITOR_FILTERS[self.visitor_filter.get_selected()]
        rows = visitors.load(status, date.today().isoformat() if day else None, self.visitor_search.get_text())
        stamp = datetime.now().strftime(visitors.FMT)
        kinds = {visitors.PENDING: "warn", visitors.APPROVED: "ok", visitors.INSIDE: "ok", visitors.REJECTED: "bad"}
        table = []
        for v in rows:
            text = visitors.STATUS_TITLES[v["status"]]
            kind = kinds.get(v["status"], "muted")
            if v["status"] == visitors.INSIDE and v["expected_to"] < stamp:
                text, kind = "Просрочил выход", "bad"
            table.append((f"{v['name']}  {v['phone']}".strip(), v["company"], v["host_name"],
                          f"{v['expected_from'][5:]} – {v['expected_to'][11:]}", v["badge"],
                          W.status_badge(text, kind)))
        self.visitor_table.set_rows(table, [v["id"] for v in rows])

    def _visitor_menu(self, visitor_id, widget, x, y):
        v = visitors.get(visitor_id)
        if v is None:
            return
        items = []
        if v["status"] == visitors.PENDING:
            items += [("Согласовать", lambda: self._decide_visitors(True, [visitor_id])),
                      ("Отклонить", lambda: self._decide_visitors(False, [visitor_id]))]
        if v["status"] == visitors.APPROVED:
            items.append(("Зарегистрировать вход", lambda: self.check_in_visitor(visitor_id)))
        if v["status"] == visitors.INSIDE:
            items += [("Зарегистрировать выход", lambda: self.check_out_visitor(visitor_id)),
                      ("Печать пропуска", lambda: self.print_badge(visitor_id))]
        items += [None, ("В стоп-лист", lambda: self._watchlist_add_dialog(v))]
        W.popover_menu(widget, items, x, y)

    def edit_visitor(self):
        if not self.session.can("persons"):
            self.notify("Нет прав на работу с посетителями", error=True)
            return
        persons = people.load_persons()
        levels = people.load_access_levels()
        now = datetime.now().replace(second=0, microsecond=0)
        dialog = _dialog(self, "Заявка на посещение", "Создать", 520)
        form = _form()
        name = _field(form, 0, "Имя *", Gtk.Entry())
        phone = _field(form, 1, "Телефон", Gtk.Entry())
        company = _field(form, 2, "Компания", Gtk.Entry())
        document = _field(form, 3, "Документ", Gtk.Entry(placeholder_text="серия и номер"))
        purpose = _field(form, 4, "Цель визита", _dropdown(list(visitors.PURPOSES)))
        host = _field(form, 5, "К кому", _dropdown(["—"] + [p["name"] for p in persons]))
        start = _field(form, 6, "Начало", Gtk.Entry(text=now.strftime(visitors.FMT)))
        end = _field(form, 7, "Окончание", Gtk.Entry(text=(now + timedelta(hours=2)).strftime(visitors.FMT)))
        level = _field(form, 8, "Уровень доступа", _dropdown(["Без доступа (только пропуск)"] +
                                                             [lvl["name"] for lvl in levels]))
        remark = _field(form, 9, "Примечание", Gtk.Entry())
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                host_index = host.get_selected() - 1
                level_index = level.get_selected() - 1
                try:
                    vid = visitors.register({
                        "name": name.get_text(), "phone": phone.get_text(), "company": company.get_text(),
                        "document": document.get_text(), "purpose": visitors.PURPOSES[purpose.get_selected()],
                        "host_person_id": persons[host_index]["id"] if host_index >= 0 else None,
                        "expected_from": start.get_text().strip(), "expected_to": end.get_text().strip(),
                        "level_id": levels[level_index]["id"] if level_index >= 0 else None,
                        "remark": remark.get_text()}, self.session.username)
                except ValueError as exc:
                    self.notify(f"Заявка не создана: {exc}", error=True)
                    return
                entry = visitors.watchlist_match(visitors.get(vid))
                if entry:
                    self.notify(f"Внимание: посетитель совпадает со стоп-листом ({entry['name']})", error=True)
                alarms.log_operation(self.session.username, f"Заявка на посещение: {name.get_text().strip()}")
                self._render_visitors()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def _selected_visitors(self):
        selected = self.visitor_table.selected()
        if not selected:
            self.notify("Отметьте посетителей в таблице")
        return selected

    def _decide_visitors(self, approved, ids=None):
        if not self.session.can("persons"):
            self.notify("Нет прав на согласование", error=True)
            return
        errors = []
        for vid in ids or self._selected_visitors():
            try:
                visitors.decide(vid, approved, self.session.username)
            except ValueError as exc:
                errors.append(str(exc))
        if errors:
            self.notify("; ".join(sorted(set(errors))), error=True)
        self._render_visitors()

    def _check_in_selected(self):
        for vid in self._selected_visitors():
            self.check_in_visitor(vid)

    def _check_out_selected(self):
        for vid in self._selected_visitors():
            self.check_out_visitor(vid)

    def _print_selected_badges(self):
        for vid in self._selected_visitors():
            self.print_badge(vid)

    def check_in_visitor(self, visitor_id, force=False):
        if not self.session.can("persons"):
            self.notify("Нет прав на работу с посетителями", error=True)
            return
        try:
            person = visitors.check_in(visitor_id, force=force)
        except visitors.WatchlistHit as hit:
            self._confirm(f"{hit}. Всё равно пропустить?", lambda: self.check_in_visitor(visitor_id, True),
                          "Пропустить")
            return
        except ValueError as exc:
            self.notify(str(exc), error=True)
            return
        visitor = visitors.get(visitor_id)
        alarms.log_operation(self.session.username, f"Вход посетителя {visitor['name']}, пропуск {visitor['badge']}"
                             + (" (стоп-лист, решение оператора)" if force else ""))
        if people.person_doors(person):
            self.apply_access_to_devices([person], self.refresh_access_center)
        self.notify(f"{visitor['name']}: вход зарегистрирован, пропуск {visitor['badge']}")
        self._render_visitors()

    def check_out_visitor(self, visitor_id):
        try:
            employee_no, devices = visitors.check_out(visitor_id)
        except ValueError as exc:
            self.notify(str(exc), error=True)
            return
        visitor = visitors.get(visitor_id)
        alarms.log_operation(self.session.username, f"Выход посетителя {visitor['name']}")
        self.remove_person_from_devices(employee_no, devices)
        self.notify(f"{visitor['name']}: выход зарегистрирован, пропуск {visitor['badge']} аннулирован")
        self._render_visitors()

    def print_badge(self, visitor_id):
        visitor = visitors.get(visitor_id)
        if not visitor or not visitor["badge"]:
            self.notify("Пропуск выдаётся при регистрации входа")
            return
        folder = storage.DATA / "badges"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"badge_{visitor['badge']}.html"
        path.write_text(visitors.badge_html(visitor, storage.get_setting("site_name", "Sentinel")), encoding="utf-8")
        try:
            Gtk.show_uri(self, path.as_uri(), 0)
        except Exception:
            pass
        self.notify(f"Пропуск: {path}")

    def manage_watchlist(self):
        dialog = _dialog(self, "Стоп-лист посетителей", None, 560, 420)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(box, 12)
        table = W.Table(("Имя", "Телефон", "Документ", "Причина"))

        def fill():
            entries = visitors.load_watchlist()
            table.set_rows([(e["name"], e["phone"], e["document"], e["reason"]) for e in entries],
                           [e["id"] for e in entries])

        box.append(_toolbar(W.button("＋ Добавить", lambda: self._watchlist_add_dialog({}, fill)),
                            W.button("Удалить", lambda: ([visitors.remove_from_watchlist(i) for i in table.selected()],
                                                         fill()))))
        box.append(W.label("Совпадение по документу, телефону или полному имени останавливает регистрацию входа: "
                           "оператор решает, пропускать ли посетителя.", "muted", wrap=True))
        box.append(_scroll(table))
        fill()
        dialog.get_content_area().append(box)
        dialog.connect("response", lambda dlg, *_a: dlg.close())
        dialog.present()

    def _watchlist_add_dialog(self, visitor, on_done=None):
        dialog = _dialog(self, "Добавить в стоп-лист", "Добавить")
        form = _form()
        name = _field(form, 0, "Имя *", Gtk.Entry(text=visitor.get("name", "")))
        phone = _field(form, 1, "Телефон", Gtk.Entry(text=visitor.get("phone", "")))
        document = _field(form, 2, "Документ", Gtk.Entry(text=visitor.get("document", "")))
        reason = _field(form, 3, "Причина", Gtk.Entry())
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    visitors.add_to_watchlist(name.get_text(), phone.get_text(), document.get_text(),
                                              reason.get_text())
                except ValueError as exc:
                    self.notify(str(exc), error=True)
                    return
                alarms.log_operation(self.session.username, f"Стоп-лист: добавлен {name.get_text().strip()}")
                if on_done:
                    on_done()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    # ================================================================== учёт времени
    def _attendance_tab(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(page, 10)
        today = date.today()
        self.att_start = Gtk.Entry(text=today.replace(day=1).isoformat())
        self.att_end = Gtk.Entry(text=today.isoformat())
        for entry in (self.att_start, self.att_end):
            entry.set_width_chars(11)
        self.att_department = _dropdown(["Все отделы"])
        self.att_mode = _dropdown(["По дням", "Итоги за период"])
        self.att_only_issues = Gtk.CheckButton(label="Только отклонения")
        page.append(_toolbar(W.label("С", "muted"), self.att_start, W.label("по", "muted"), self.att_end,
                             self.att_department, self.att_mode, self.att_only_issues,
                             W.button("Рассчитать", self.render_attendance, "red-button"), _spacer(),
                             W.button("Экспорт CSV", self.export_attendance)))
        page.append(_toolbar(W.button("Корректировка…", self.attendance_correction_dialog),
                             W.button("Смены…", self.manage_shifts),
                             W.button("Точки учёта…", self.manage_attendance_points),
                             W.button("Догрузить проходы с контроллеров", self.import_attendance_events), _spacer()))
        self.att_summary = W.label("Выберите период и нажмите «Рассчитать».", "muted", wrap=True)
        page.append(self.att_summary)
        self.att_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.att_holder.set_vexpand(True)
        page.append(self.att_holder)
        return page

    def _attendance_period(self):
        start = date.fromisoformat(self.att_start.get_text().strip())
        end = date.fromisoformat(self.att_end.get_text().strip())
        return start, end

    def _attendance_persons(self):
        departments = people.load_departments()
        names = ["Все отделы"] + [d["name"] for d in departments]
        if self.__dict__.get("_att_departments") != names:
            self._att_departments = names
            self.att_department.set_model(Gtk.StringList.new(names))
        index = self.att_department.get_selected() - 1
        department_id = departments[index]["id"] if 0 <= index < len(departments) else None
        return [p for p in people.load_persons(department_id) if not visitors.is_visitor(p)]

    def render_attendance(self):
        if not self.session.can("reports") and not self.session.can("persons"):
            self.notify("Нет прав на отчёты", error=True)
            return
        try:
            start, end = self._attendance_period()
            records = att.compute(sorted(self._attendance_persons(), key=lambda p: p["name"]), start, end)
        except ValueError as exc:
            self.notify(f"Учёт времени: {exc}", error=True)
            return
        self.att_records = records
        self._clear_box(self.att_holder)
        summary = att.summarize(records)
        total_late = sum(s["late_count"] for s in summary)
        total_absent = sum(s["absent"] for s in summary)
        self.att_summary.set_text(f"Персон: {len(summary)} · дней: {(end - start).days + 1} · опозданий: {total_late}"
                                  f" · отсутствий: {total_absent} · отработано: "
                                  f"{att.hm(sum(s['worked'] for s in summary))}")
        if self.att_mode.get_selected() == 1:
            table = W.Table(att.SUMMARY_CSV[1:], selectable=False)
            table.set_rows([att.summary_row(s)[1:] for s in summary])
        else:
            shown = [r for r in records if r["status"] != "future"]
            if self.att_only_issues.get_active():
                shown = [r for r in shown if r["status"] in ("late", "early", "late_early", "no_out", "absent")]
            table = W.Table(("Дата", "Имя", "Смена", "Вход", "Выход", "Отработано", "Опозд.", "Уход", "Статус",
                             "Корр."), selectable=False, on_activate=self._attendance_row_activated)
            kinds = {"ok": "ok", "extra": "ok", "day_off": "muted", "leave": "muted", "absent": "bad",
                     "no_out": "warn"}
            rows = []
            for r in shown:
                row = att.daily_row(r)
                rows.append((row[0][5:], row[2], row[3], row[4], row[5], row[6], row[7], row[8],
                             W.status_badge(att.status_text(r), kinds.get(r["status"], "warn")),
                             "✎" if r["corrected"] else ""))
            table.set_rows(rows, [(r["person_id"], r["day"]) for r in shown])
        self.att_holder.append(_scroll(table))

    def _attendance_row_activated(self, key):
        person_id, day = key
        self.attendance_correction_dialog(person_id, day)

    def export_attendance(self):
        records = self.__dict__.get("att_records")
        if not records:
            self.notify("Сначала нажмите «Рассчитать»")
            return
        summary = self.att_mode.get_selected() == 1
        start, end = self._attendance_period()
        path = Path.home() / f"sentinel_attendance_{'summary' if summary else 'daily'}_{start}_{end}.csv"
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            att.export_csv(records, handle, summary)
        self.notify(f"Сохранено: {path}")

    def attendance_correction_dialog(self, person_id=None, day=None):
        if not self.session.can("persons"):
            self.notify("Нет прав на корректировку", error=True)
            return
        persons = sorted(people.load_persons(), key=lambda p: p["name"])
        if not persons:
            self.notify("Нет персон")
            return
        current = att.load_corrections(day, day).get((person_id, day), {}) if person_id and day else {}
        dialog = _dialog(self, "Корректировка учёта времени", "Сохранить")
        form = _form()
        who = _field(form, 0, "Персона", _dropdown([f"{p['name']} ({p['employee_no']})" for p in persons],
                                                   next((i for i, p in enumerate(persons) if p["id"] == person_id),
                                                        0)))
        when = _field(form, 1, "Дата", Gtk.Entry(text=day or date.today().isoformat()))
        check_in = _field(form, 2, "Вход (ЧЧ:ММ)", _time_entry(current.get("check_in", "")))
        check_out = _field(form, 3, "Выход (ЧЧ:ММ)", _time_entry(current.get("check_out", "")))
        leave_keys = [""] + list(att.LEAVE_KINDS)
        leave = _field(form, 4, "Отсутствие", _dropdown(["—"] + list(att.LEAVE_KINDS.values()),
                                                        leave_keys.index(current.get("leave", ""))))
        reason = _field(form, 5, "Причина *", Gtk.Entry(text=current.get("reason", "")))
        form.attach(W.label("Корректировка заменяет расчёт за день и попадает в журнал операций.", "muted",
                            wrap=True), 0, 6, 2, 1)
        dialog.get_content_area().append(form)
        if current:
            dialog.add_button("Удалить корректировку", Gtk.ResponseType.REJECT)

        def response(dlg, result):
            person = persons[who.get_selected()]
            if result == Gtk.ResponseType.REJECT:
                att.delete_correction(person["id"], when.get_text().strip())
            elif result == Gtk.ResponseType.ACCEPT:
                try:
                    att.save_correction(person["id"], when.get_text().strip(), check_in.get_text().strip(),
                                        check_out.get_text().strip(), leave_keys[leave.get_selected()],
                                        reason.get_text(), self.session.username)
                except ValueError as exc:
                    self.notify(f"Не сохранено: {exc}", error=True)
                    return
                alarms.log_operation(self.session.username, f"Учёт времени: корректировка {person['name']} "
                                     f"{when.get_text().strip()}", reason.get_text().strip())
            if result in (Gtk.ResponseType.ACCEPT, Gtk.ResponseType.REJECT) and self.__dict__.get("att_records"):
                self.render_attendance()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def manage_shifts(self):
        config = att.load_config()
        dialog = _dialog(self, "Смены и графики", "Сохранить", 620)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(box, 12)
        shifts = config["shifts"]
        choice = _dropdown(["＋ Новая смена"] + [s["name"] for s in shifts], 1)
        box.append(choice)
        form = _form()
        name = _field(form, 0, "Название", Gtk.Entry())
        start, end = _time_entry("09:00"), _time_entry("18:00")
        form.attach(W.label("Время", "muted"), 0, 1, 1, 1)
        form.attach(_toolbar(start, W.label("–"), end, W.label("ночная смена: конец раньше начала", "muted")),
                    1, 1, 1, 1)
        pause = _field(form, 2, "Перерыв, мин", Gtk.SpinButton.new_with_range(0, 240, 5))
        late = _field(form, 3, "Опоздание не считать, мин", Gtk.SpinButton.new_with_range(0, 120, 1))
        early = _field(form, 4, "Ранний уход не считать, мин", Gtk.SpinButton.new_with_range(0, 120, 1))
        days = [Gtk.CheckButton(label=d) for d in WEEKDAYS]
        form.attach(W.label("Рабочие дни", "muted"), 0, 5, 1, 1)
        form.attach(_toolbar(*days), 1, 5, 1, 1)
        box.append(form)
        departments = people.load_departments()
        box.append(W.label("Смена отдела действует на всех его сотрудников.", "muted", wrap=True))
        dep_grid = _form()
        names = ["По умолчанию"] + [s["name"] for s in shifts]
        dep_choices = []
        for row, department in enumerate(departments):
            current = config["department_shifts"].get(str(department["id"]))
            index = next((i + 1 for i, s in enumerate(shifts) if s["id"] == current), 0)
            dep_choices.append((department["id"], _field(dep_grid, row, department["name"], _dropdown(names, index))))
        default = _field(dep_grid, len(departments), "Смена по умолчанию", _dropdown(
            [s["name"] for s in shifts], next((i for i, s in enumerate(shifts)
                                               if s["id"] == config["default_shift"]), 0)))
        box.append(dep_grid)

        def load(*_args):
            index = choice.get_selected() - 1
            shift = shifts[index] if index >= 0 else {"name": "", "start": "09:00", "end": "18:00", "break": 60,
                                                      "late": 10, "early": 10, "days": [0, 1, 2, 3, 4]}
            name.set_text(shift["name"])
            start.set_text(shift["start"])
            end.set_text(shift["end"])
            pause.set_value(shift["break"])
            late.set_value(shift["late"])
            early.set_value(shift["early"])
            for i, check in enumerate(days):
                check.set_active(i in shift["days"])

        choice.connect("notify::selected", load)
        load()
        dialog.get_content_area().append(_scroll(box))
        dialog.set_default_size(620, 560)
        if len(shifts) > 1:
            dialog.add_button("Удалить смену", Gtk.ResponseType.REJECT)

        def response(dlg, result):
            index = choice.get_selected() - 1
            try:
                if result == Gtk.ResponseType.REJECT and index >= 0:
                    att.delete_shift(shifts[index]["id"])
                elif result == Gtk.ResponseType.ACCEPT:
                    saved = att.save_shift({"id": shifts[index]["id"] if index >= 0 else 0, "name": name.get_text(),
                                            "start": start.get_text().strip(), "end": end.get_text().strip(),
                                            "break": pause.get_value_as_int(), "late": late.get_value_as_int(),
                                            "early": early.get_value_as_int(),
                                            "days": [i for i, c in enumerate(days) if c.get_active()]})
                    fresh = att.load_config()
                    fresh["default_shift"] = shifts[default.get_selected()]["id"] if shifts else saved
                    fresh["department_shifts"] = {str(did): shifts[w.get_selected() - 1]["id"]
                                                  for did, w in dep_choices if w.get_selected() > 0}
                    att.save_config(fresh)
            except ValueError as exc:
                self.notify(f"Не сохранено: {exc}", error=True)
                return
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def manage_attendance_points(self):
        controllers = self._access_controllers()
        if not controllers:
            self.notify("Нет контроллеров доступа")
            return
        config = att.load_config()
        dialog = _dialog(self, "Точки учёта времени", "Сохранить", 520)
        form = _form()
        form.attach(W.label("Какие двери отмечают приход и уход. Если ни одна не выбрана, учитываются все двери: "
                            "первый проход за день — приход, последний — уход.", "muted", wrap=True), 0, 0, 2, 1)
        options = ["Не учитывать"] + list(att.DIRECTIONS.values())
        keys = [None] + list(att.DIRECTIONS)
        choices = []
        row = 1
        for device in controllers:
            for door in range(1, max(1, int(device.get("door_count") or 1)) + 1):
                key = att.point_key(device["id"], door)
                current = config["points"].get(key)
                widget = _field(form, row, f"{device.get('host_name') or device['host']} · Дверь {door}",
                                _dropdown(options, keys.index(current) if current in keys else 0))
                choices.append((key, widget))
                row += 1
        dialog.get_content_area().append(_scroll(form))
        dialog.set_default_size(520, 460)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                fresh = att.load_config()
                fresh["points"] = {k: keys[w.get_selected()] for k, w in choices if w.get_selected() > 0}
                att.save_config(fresh)
                self.notify("Точки учёта сохранены")
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def import_attendance_events(self):
        try:
            start, end = self._attendance_period()
        except ValueError as exc:
            self.notify(str(exc), error=True)
            return
        jobs = [(d, self._access_credentials_for(d)) for d in self._access_controllers()]
        jobs = [(d, c) for d, c in jobs if c]
        if not jobs:
            self.notify("Нет контроллеров с сохранённым паролем", error=True)
            return
        lo = datetime.combine(start, datetime.min.time())
        hi = datetime.combine(end, datetime.max.time()).replace(microsecond=0)

        def worker():
            added, errors = 0, []
            for device, credentials in jobs:
                position = 0
                try:
                    while position < 20000:
                        raw = self._isapi_request(device, credentials, "/ISAPI/AccessControl/AcsEvent?format=json",
                                                  "POST", json.dumps(att.acs_event_query(lo, hi, position)),
                                                  "application/json")
                        result = json.loads(raw.decode("utf-8", "replace")).get("AcsEvent", {})
                        records = result.get("InfoList") or []
                        added += att.import_controller_events(device, records)
                        position += len(records)
                        if result.get("responseStatusStrg") != "MORE" or not records:
                            break
                except Exception as exc:
                    errors.append(f"{device.get('host_name') or device['host']}: {exc}")
            message = f"Догружено проходов: {added}" + (f"; ошибки: {'; '.join(errors)}" if errors else "")
            GLib.idle_add(self.notify, message, bool(errors))
            GLib.idle_add(lambda: (self.render_attendance(), False)[1])

        self.notify("Загрузка журнала контроллеров…")
        threading.Thread(target=worker, daemon=True).start()


def _numbers(text):
    result = []
    for part in str(text).replace("+", ",").replace(";", ",").replace("→", ",").split(","):
        part = part.strip()
        if part:
            if not part.isdigit():
                raise ValueError(f"Номер двери «{part}» — не число")
            result.append(int(part))
    return result
