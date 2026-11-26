"""Модули «Видеостена» и «Доказательства» (GTK-часть)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from gi.repository import Gdk, GLib, Gtk

import sentinel_alarms as alarms
import sentinel_evidence as evidence
import sentinel_storage as storage
import sentinel_videowall as wall
import sentinel_views as views
import sentinel_widgets as W
from sentinel_system import _dialog, _dropdown, _field, _form, _scroll, _spacer, _toolbar

WALL_GRID = {1: (1, 1), 4: (2, 2), 6: (3, 2), 9: (3, 3), 16: (4, 4)}


class WallEvidenceMixin:
    # ================================================================== видеостена
    def _wall_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        W.margins(page, 12)
        self.wall_choice = _dropdown(["—"])
        self.wall_choice.connect("notify::selected", lambda *_a: self._render_wall())
        self.wall_output_button = W.button("▶ Вывести на стену", self.toggle_wall_output, "red-button")
        page.append(_toolbar(self.wall_choice, W.button("＋ Стена", lambda: self.edit_wall(None)),
                             W.button("Настроить", lambda: self.edit_wall(self._current_wall())),
                             W.button("Удалить", self._delete_current_wall), _spacer(), self.wall_output_button))
        self.wall_scene_choice = _dropdown(["Сценарии"])
        page.append(_toolbar(W.label("Сценарий", "muted"), self.wall_scene_choice,
                             W.button("Вызвать", self._apply_selected_scene),
                             W.button("Сохранить как сценарий…", self._save_scene_dialog),
                             W.button("Удалить сценарий", self._delete_selected_scene), _spacer(),
                             W.label("Щелчок по окну экрана — выбрать камеру", "muted")))
        self.wall_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.wall_holder.set_vexpand(True)
        page.append(self.wall_holder)
        self.wall_info = W.label("", "muted", wrap=True)
        page.append(self.wall_info)
        return page

    def _walls(self):
        return self.__dict__.get("walls_cache") or []

    def _current_wall(self):
        walls = self._walls()
        index = self.wall_choice.get_selected()
        return walls[index] if walls and 0 <= index < len(walls) else None

    def _monitor_count(self):
        display = Gdk.Display.get_default()
        monitors = display.get_monitors() if display else None
        try:
            return max(1, monitors.get_n_items()) if monitors else 1
        except Exception:
            return 1

    def _render_wall(self):
        if not hasattr(self, "wall_holder"):
            return
        walls = wall.load_walls()
        names = [w["name"] for w in walls] or ["Нет стен"]
        if names != self.__dict__.get("_wall_names"):
            self._wall_names = names
            self.walls_cache = walls
            self.wall_choice.set_model(Gtk.StringList.new(names))
        self.walls_cache = walls
        self._clear_box(self.wall_holder)
        current = self._current_wall()
        if current is None:
            self.wall_holder.append(W.label("Видеостена выводит камеры на мониторы, подключённые к этому компьютеру: "
                                            "по окну на монитор, у каждого своя раскладка. Нажмите «＋ Стена».",
                                            "muted", wrap=True))
            self.wall_scene_choice.set_model(Gtk.StringList.new(["Нет сценариев"]))
            return
        scenes = wall.load_scenes(current["id"])
        self.wall_scenes_cache = scenes
        self.wall_scene_choice.set_model(Gtk.StringList.new([s["name"] for s in scenes] or ["Нет сценариев"]))
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True, row_homogeneous=True)
        grid.set_vexpand(True)
        grid.set_hexpand(True)
        names_by_id = {d["id"]: (d.get("name") or d["host"]) for d in self.devices}
        active = self.__dict__.get("wall_windows", {}).get(current["id"], {})
        for index, screen in enumerate(current["screens"]):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("wall-screen")
            if index == current["alarm_screen"]:
                card.add_css_class("wall-alarm-screen")
            layouts = [str(n) for n in wall.LAYOUTS]
            layout = _dropdown([f"{n} окон" if n > 1 else "1 окно" for n in wall.LAYOUTS],
                               layouts.index(str(screen["layout"])))
            layout.connect("notify::selected", lambda d, i=index: self._set_wall_layout(i, wall.LAYOUTS[d.get_selected()]))
            title = f"Экран {index + 1} · монитор {screen['monitor'] + 1}"
            if index == current["alarm_screen"]:
                title += " · тревоги"
            if index in active:
                title += " · ●"
            card.append(_toolbar(W.label(title, "section-title"), _spacer(), layout,
                                 W.button("Вид…", lambda i=index: self._wall_view_menu(i), flat=True,
                                          tooltip="Вывести сохранённый вид на экран"),
                                 W.button("✕", lambda i=index: self._clear_wall_screen(i), flat=True,
                                          tooltip="Очистить экран")))
            cells = Gtk.Grid(column_spacing=2, row_spacing=2, column_homogeneous=True, row_homogeneous=True)
            cells.set_vexpand(True)
            cols, _rows = WALL_GRID[screen["layout"]]
            for slot, device_id in enumerate(screen["cameras"]):
                text = names_by_id.get(device_id, f"#{device_id}") if device_id else "＋"
                cell = W.button(text, None, "wall-cell")
                cell.set_hexpand(True)
                cell.set_vexpand(True)
                cell.connect("clicked", lambda b, i=index, s=slot: self._wall_cell_menu(b, i, s))
                cells.attach(cell, slot % cols, slot // cols, 1, 1)
            card.append(cells)
            grid.attach(card, index % current["cols"], index // current["cols"], 1, 1)
        self.wall_holder.append(grid)
        live = bool(active)
        self.wall_output_button.set_label("■ Убрать со стены" if live else "▶ Вывести на стену")
        monitors = self._monitor_count()
        warn = "" if monitors >= len(current["screens"]) else (
            f" Подключено мониторов: {monitors} — лишние экраны откроются окнами на последнем мониторе.")
        missing = wall.missing_cameras(current["screens"], [d["id"] for d in self.devices])
        if missing:
            warn += f" Камер нет в каталоге: {len(missing)}."
        self.wall_info.set_text(f"Стена {current['rows']}×{current['cols']} · экран тревог: "
                                  f"{current['alarm_screen'] + 1 if current['alarm_screen'] >= 0 else 'нет'}"
                                  f" · {'выведена' if live else 'не выведена'}." + warn)

    def _update_wall_screens(self, screens, refresh_index=None):
        current = self._current_wall()
        if current is None:
            return
        wall.save_screens(current["id"], screens)
        self._render_wall()
        if self.__dict__.get("wall_windows", {}).get(current["id"]):
            self._output_wall(self._current_wall(), only=refresh_index)

    def _set_wall_layout(self, index, layout):
        current = self._current_wall()
        if current and current["screens"][index]["layout"] != layout:
            self._update_wall_screens(wall.set_layout(current["screens"], index, layout), index)

    def _clear_wall_screen(self, index):
        current = self._current_wall()
        if current:
            screens = wall.place_cameras(current["screens"], index, [])
            screens = wall.set_layout(screens, index, current["screens"][index]["layout"])
            self._update_wall_screens(screens, index)

    def _wall_cell_menu(self, widget, index, slot):
        current = self._current_wall()
        if current is None:
            return
        items = [("Пусто", lambda: self._update_wall_screens(wall.place_camera(current["screens"], index, slot, None),
                                                            index)), None]
        for device in [d for d in self.devices if d.get("category") != "access"][:60]:
            items.append((device.get("name") or device["host"], lambda d=device: self._update_wall_screens(
                wall.place_camera(current["screens"], index, slot, d["id"]), index)))
        W.popover_menu(widget, items)

    def _wall_view_menu(self, index):
        current = self._current_wall()
        saved = views.load_views()
        if current is None or not saved:
            self.notify("Нет сохранённых видов")
            return

        def put(view):
            mapping = views.saved_view_values(view)["mapping"]
            ids = [m["device_id"] for m in sorted(mapping, key=lambda m: m["slot"]) if m.get("device_id")]
            self._update_wall_screens(wall.place_cameras(current["screens"], index, ids), index)

        W.popover_menu(self.wall_holder, [(v["name"], lambda v=v: put(v)) for v in saved])

    def edit_wall(self, current):
        if not self.session.can("live"):
            self.notify("Нет прав", error=True)
            return
        current = current or {}
        dialog = _dialog(self, "Видеостена", "Сохранить")
        form = _form()
        name = _field(form, 0, "Название *", Gtk.Entry(text=current.get("name", "")))
        rows = _field(form, 1, "Рядов экранов", Gtk.SpinButton.new_with_range(1, 4, 1))
        rows.set_value(current.get("rows", 1))
        cols = _field(form, 2, "Экранов в ряду", Gtk.SpinButton.new_with_range(1, 6, 1))
        cols.set_value(current.get("cols", min(2, self._monitor_count())))
        alarm_screen = _field(form, 3, "Экран тревог (0 — нет)", Gtk.SpinButton.new_with_range(0, 24, 1))
        alarm_screen.set_value(current.get("alarm_screen", -1) + 1)
        seconds = _field(form, 4, "Показывать тревогу, с", Gtk.SpinButton.new_with_range(5, 3600, 5))
        seconds.set_value(current.get("alarm_seconds", 60))
        all_alarms = Gtk.CheckButton(label="Выводить все тревоги (иначе — только с действием «На видеостену»)")
        all_alarms.set_active(bool(current.get("all_alarms")))
        form.attach(all_alarms, 0, 5, 2, 1)
        form.attach(W.label(f"Подключено мониторов: {self._monitor_count()}. Экран N выводится на монитор N.",
                            "muted", wrap=True), 0, 6, 2, 1)
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    wall.save_wall(name.get_text(), rows.get_value_as_int(), cols.get_value_as_int(),
                                   current.get("screens"), alarm_screen.get_value_as_int() - 1,
                                   seconds.get_value_as_int(), current.get("id"), all_alarms.get_active())
                except (ValueError, sqlite3.Error) as exc:
                    self.notify(f"Не сохранено: {exc}", error=True)
                    return
                self._render_wall()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def _delete_current_wall(self):
        current = self._current_wall()
        if current:
            self._confirm(f"Удалить стену «{current['name']}» и её сценарии?", lambda: (
                self._stop_wall_output(current["id"]), wall.delete_wall(current["id"]), self._render_wall()))

    def _selected_scene(self):
        scenes = self.__dict__.get("wall_scenes_cache") or []
        index = self.wall_scene_choice.get_selected()
        return scenes[index] if scenes and 0 <= index < len(scenes) else None

    def _apply_selected_scene(self):
        current, scene = self._current_wall(), self._selected_scene()
        if current and scene:
            wall.apply_scene(current["id"], scene["id"])
            alarms.log_operation(self.session.username, f"Видеостена «{current['name']}»: сценарий «{scene['name']}»")
            self._render_wall()
            if self.__dict__.get("wall_windows", {}).get(current["id"]):
                self._output_wall(self._current_wall())

    def _save_scene_dialog(self):
        current = self._current_wall()
        if current:
            self._ask_text("Название сценария", "", lambda text: (wall.save_scene(current["id"], text,
                                                                                  current["screens"]),
                                                                  self._render_wall()))

    def _delete_selected_scene(self):
        scene = self._selected_scene()
        if scene:
            wall.delete_scene(scene["id"])
            self._render_wall()

    # ------------------------------------------------------------ вывод на мониторы
    def toggle_wall_output(self):
        current = self._current_wall()
        if current is None:
            return
        if self.__dict__.get("wall_windows", {}).get(current["id"]):
            self._stop_wall_output(current["id"])
        else:
            self._output_wall(current)
            alarms.log_operation(self.session.username, f"Видеостена «{current['name']}» выведена")
        self._render_wall()

    def _output_wall(self, current, only=None):
        from app import MonitorWindow  # отложенный импорт: app импортирует этот модуль
        windows = self.__dict__.setdefault("wall_windows", {}).setdefault(current["id"], {})
        devices = {d["id"]: d for d in self.devices}
        display = Gdk.Display.get_default()
        monitors = display.get_monitors() if display else None
        count = self._monitor_count()
        for index, screen in enumerate(current["screens"]):
            if only is not None and index != only:
                continue
            old = windows.pop(index, None)
            if old is not None:
                old.close()
            self.monitor_sequence = getattr(self, "monitor_sequence", 0) + 1
            monitor = min(screen["monitor"], count - 1)
            window = MonitorWindow(self, self.monitor_sequence, f"{current['name']} · экран {index + 1}", count,
                                   screen["layout"], monitor, None)
            window.wall_key = (current["id"], index)
            window.connect("close-request", lambda w: self._wall_window_closed(w))
            self.monitor_windows.append(window)
            windows[index] = window
            window.present()
            target = monitors.get_item(monitor) if monitors and count > 1 else None
            if target is not None and hasattr(window, "fullscreen_on_monitor"):
                window.fullscreen_on_monitor(target)
            window.start_assignments([(slot, devices[c]) for slot, c in enumerate(screen["cameras"])
                                      if c in devices], {}, screen["layout"] > 1)

    def _wall_window_closed(self, window):
        wall_id, index = getattr(window, "wall_key", (None, None))
        windows = self.__dict__.get("wall_windows", {}).get(wall_id, {})
        if windows.get(index) is window:
            windows.pop(index, None)
            GLib.idle_add(lambda: (self._render_wall(), False)[1])
        return False

    def _stop_wall_output(self, wall_id):
        for window in list(self.__dict__.get("wall_windows", {}).pop(wall_id, {}).values()):
            window.close()

    def wall_show_alarm(self, wall_id=None, device_id=None, any_alarm=False):
        """Вывести камеру тревоги на экран тревог стены (выведенной на мониторы)."""
        if not device_id:
            return
        device = next((d for d in self.devices if d["id"] == device_id), None)
        if device is None:
            return
        for current in wall.load_walls():
            if wall_id is not None and current["id"] != wall_id:
                continue
            if any_alarm and not current["all_alarms"]:
                continue
            windows = self.__dict__.get("wall_windows", {}).get(current["id"], {})
            rotation = self.__dict__.setdefault("_wall_rotation", {})
            target = wall.alarm_slot(current, current["screens"], rotation.get(current["id"], 0))
            if not target or target[0] not in windows:
                continue
            index, slot = target
            rotation[current["id"]] = rotation.get(current["id"], 0) + 1
            window = windows[index]
            try:
                window._start_preview(slot, dict(device, stream="sub"), window.session_credentials.get(device_id, {}))
            except Exception as exc:
                self.notify(f"Видеостена: {exc}", error=True)
                continue
            previous = current["screens"][index]["cameras"][slot]

            def restore(w=window, s=slot, prev=previous):
                if w.get_visible():
                    if prev and prev in {d["id"] for d in self.devices}:
                        dev = next(d for d in self.devices if d["id"] == prev)
                        w._start_preview(s, dict(dev, stream="sub"), w.session_credentials.get(prev, {}))
                    elif s in w.streams:
                        w._stop_stream(s)
                return False

            GLib.timeout_add_seconds(current["alarm_seconds"], restore)

    # ================================================================== доказательства
    def _evidence_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        W.margins(page, 12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_size_request(330, -1)
        self.evidence_filter = _dropdown(["Открытые", "Закрытые", "Все"])
        self.evidence_filter.connect("notify::selected", lambda *_a: self._render_evidence())
        self.evidence_search = Gtk.SearchEntry(placeholder_text="Номер, название, метки")
        self.evidence_search.connect("search-changed", lambda *_a: self._render_evidence())
        left.append(_toolbar(W.button("＋ Дело", self.create_case_dialog, "red-button"), self.evidence_filter))
        left.append(self.evidence_search)
        self.evidence_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        left.append(_scroll(self.evidence_list))
        page.append(left)
        self.evidence_detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.evidence_detail.set_hexpand(True)
        page.append(self.evidence_detail)
        return page

    def _render_evidence(self):
        if not hasattr(self, "evidence_list"):
            return
        status = {0: evidence.OPEN, 1: evidence.CLOSED}.get(self.evidence_filter.get_selected())
        cases = evidence.load_cases(status, self.evidence_search.get_text())
        self._clear_box(self.evidence_list)
        if not cases:
            self.evidence_list.append(W.label("Дел нет", "muted"))
        for case in cases:
            mark = "▣" if case["status"] == evidence.OPEN else "▤"
            text = f"{mark} № {case['number']} · {case['title']}\n    вложений: {case['items']} · {case['created_at'][:10]}"
            self.evidence_list.append(W.list_row(text, lambda c=case: self.open_case(c["id"])))
        current = self.__dict__.get("evidence_case_id")
        if current and evidence.get_case(current):
            self._render_case(current)
        else:
            self._clear_box(self.evidence_detail)
            self.evidence_detail.append(W.label("Дело объединяет записи, снимки, тревоги и заметки по одному "
                                                "происшествию. Файлы копируются в хранилище дела с контрольной "
                                                "суммой, все действия пишутся в журнал.", "muted", wrap=True))

    def open_case(self, case_id):
        self.evidence_case_id = case_id
        evidence.log(case_id, self.session.username, "view")
        self._render_case(case_id)

    def _render_case(self, case_id):
        case = evidence.get_case(case_id)
        self._clear_box(self.evidence_detail)
        closed = case["status"] == evidence.CLOSED
        self.evidence_detail.append(_toolbar(W.label(f"№ {case['number']} · {case['title']}", "section-title"),
                                             W.status_badge(evidence.STATUS_TITLES[case["status"]],
                                                            "muted" if closed else "ok"), _spacer()))
        if case["description"] or case["tags"]:
            self.evidence_detail.append(W.label(" · ".join(x for x in (case["description"], case["tags"]) if x),
                                                "muted", wrap=True))
        actions = [W.button("＋ Файл…", lambda: self._add_case_file(case_id)),
                   W.button("＋ Заметка", lambda: self._ask_text("Заметка", "", lambda t: self._add_case_note(
                       case_id, t))),
                   W.button("Изменить", lambda: self.create_case_dialog(case)),
                   W.button("Проверить целостность", lambda: self._verify_case(case_id)),
                   W.button("Выгрузить ZIP", lambda: self._export_case(case_id)), _spacer(),
                   W.button("Открыть повторно" if closed else "Закрыть дело", lambda: self._toggle_case(case))]
        for button in actions[:3]:
            button.set_sensitive(not closed)
        self.evidence_detail.append(_toolbar(*actions))
        items = evidence.load_items(case_id)
        table = W.Table(("Вложение", "Тип", "Размер", "SHA-256", "Добавил", "Когда"), selectable=False,
                        on_activate=lambda i: self._open_case_item(i), on_context=self._case_item_menu)
        table.set_rows([(i["title"], evidence.KIND_TITLES.get(i["kind"], i["kind"]),
                         f"{i['size'] / 1048576:.1f} МБ" if i["size"] > 1048576 else (f"{i['size']} Б" if i["size"]
                                                                                       else "—"),
                         i["sha256"][:16] + "…", i["added_by"], i["added_at"][:16].replace("T", " "))
                        for i in items], [i["id"] for i in items])
        self.evidence_items_cache = {i["id"]: i for i in items}
        self.evidence_detail.append(W.label("Вложения (двойной щелчок — открыть, правый — удалить)", "muted"))
        scroll = _scroll(table)
        self.evidence_detail.append(scroll)
        self.evidence_detail.append(W.label("Журнал дела", "section-title"))
        log = W.Table(("Время", "Пользователь", "Действие", "Подробности"), selectable=False)
        entries = evidence.load_log(case_id)[-200:][::-1]
        log.set_rows([(e["time"][:19].replace("T", " "), e["username"], evidence.ACTIONS.get(e["action"], e["action"]),
                       e["detail"]) for e in entries])
        log_scroll = _scroll(log)
        log_scroll.set_size_request(-1, 180)
        self.evidence_detail.append(log_scroll)

    def create_case_dialog(self, case=None):
        if not self.session.can("playback"):
            self.notify("Нет прав на работу с доказательствами", error=True)
            return
        dialog = _dialog(self, "Дело", "Сохранить" if case else "Создать")
        form = _form()
        number = _field(form, 0, "Номер", Gtk.Entry(text=case["number"] if case else "",
                                                    placeholder_text=evidence.next_number()))
        number.set_sensitive(case is None)
        title = _field(form, 1, "Название *", Gtk.Entry(text=case["title"] if case else ""))
        description = _field(form, 2, "Описание", Gtk.Entry(text=case["description"] if case else ""))
        tags = _field(form, 3, "Метки", Gtk.Entry(text=case["tags"] if case else "",
                                                  placeholder_text="склад, кража"))
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    if case:
                        evidence.update_case(case["id"], self.session.username, title.get_text(),
                                             description.get_text(), tags.get_text())
                        case_id = case["id"]
                    else:
                        case_id = evidence.create_case(title.get_text(), description.get_text(),
                                                       self.session.username, tags.get_text(), number.get_text())
                except ValueError as exc:
                    self.notify(str(exc), error=True)
                    return
                alarms.log_operation(self.session.username, f"Доказательства: дело «{title.get_text().strip()}»")
                self.evidence_case_id = case_id
                self._render_evidence()
            dlg.close()

        dialog.connect("response", response)
        dialog.present()

    def _add_case_file(self, case_id):
        chooser = Gtk.FileChooserNative.new("Файл для дела", self, Gtk.FileChooserAction.OPEN, "Добавить", "Отмена")

        def response(native, result):
            file = native.get_file()
            if result == Gtk.ResponseType.ACCEPT and file:
                self.attach_to_case(case_id, file.get_path())
            native.destroy()

        chooser.connect("response", response)
        chooser.show()

    def attach_to_case(self, case_id, path, kind="file", title="", meta=None):
        try:
            evidence.add_file(case_id, path, self.session.username, kind, title, meta)
        except (ValueError, OSError) as exc:
            self.notify(f"Не добавлено: {exc}", error=True)
            return False
        self.notify("Добавлено в дело")
        if hasattr(self, "evidence_list"):
            self._render_evidence()
        return True

    def _add_case_note(self, case_id, text):
        try:
            evidence.add_record(case_id, self.session.username, "note", text)
        except ValueError as exc:
            self.notify(str(exc), error=True)
        self._render_case(case_id)

    def _open_case_item(self, item_id):
        item = (self.__dict__.get("evidence_items_cache") or {}).get(item_id)
        if not item or not item["path"]:
            return
        evidence.log(item["case_id"], self.session.username, "view", item["title"])
        try:
            Gtk.show_uri(self, Path(item["path"]).as_uri(), 0)
        except Exception as exc:
            self.notify(str(exc), error=True)

    def _case_item_menu(self, item_id, widget, x, y):
        item = (self.__dict__.get("evidence_items_cache") or {}).get(item_id)
        if not item:
            return

        def remove(reason):
            try:
                evidence.remove_item(item_id, self.session.username, reason)
            except ValueError as exc:
                self.notify(str(exc), error=True)
            self._render_evidence()

        W.popover_menu(widget, [("Открыть", lambda: self._open_case_item(item_id)),
                                ("Удалить из дела…", lambda: self._ask_text("Причина удаления", "", remove))], x, y)

    def _verify_case(self, case_id):
        result = evidence.verify(case_id, self.session.username)
        bad = [f"{i['title']} ({'изменён' if s == 'changed' else 'нет файла'})" for i, s in result if s != "ok"]
        if bad:
            self.notify("Нарушена целостность: " + ", ".join(bad), error=True)
        else:
            self.notify(f"Все файлы целы ({len(result)})")
        self._render_case(case_id)

    def _export_case(self, case_id):
        target = getattr(self, "record_folder", Path.home() / "Videos" / "Sentinel") / "Evidence" / "Cases"
        try:
            bundle = evidence.export_case(case_id, target, self.session.username)
        except (ValueError, OSError) as exc:
            self.notify(f"Выгрузка не удалась: {exc}", error=True)
            return
        alarms.log_operation(self.session.username, "Доказательства: выгрузка дела", str(bundle))
        self.notify(f"Дело выгружено: {bundle}")
        self._render_case(case_id)

    def _toggle_case(self, case):
        if case["status"] == evidence.OPEN:
            self._ask_text("Причина закрытия", "", lambda reason: (
                evidence.set_status(case["id"], evidence.CLOSED, self.session.username, reason),
                self._render_evidence()))
        else:
            evidence.set_status(case["id"], evidence.OPEN, self.session.username)
            self._render_evidence()

    def add_to_case_menu(self, widget, attach):
        """Меню «В дело»: открытые дела + новое. attach(case_id) выполняет добавление."""
        cases = evidence.load_cases(evidence.OPEN)

        def new_case():
            self._ask_text("Название нового дела", "", lambda title: attach(
                evidence.create_case(title, username=self.session.username)))

        W.popover_menu(widget, [("＋ Новое дело…", new_case), None] +
                       [(f"№ {c['number']} · {c['title']}", lambda c=c: attach(c["id"])) for c in cases[:20]])

    def _alarm_to_case(self, case_id, record):
        try:
            title = f"Тревога «{record.get('rule_name') or alarms.event_title(record.get('event_type'))}» · " \
                    f"{record.get('device_name', '')} · {str(record.get('time', ''))[:19].replace('T', ' ')}"
            evidence.add_record(case_id, self.session.username, "alarm", title,
                                {k: record.get(k) for k in ("id", "time", "device_name", "channel", "event_type",
                                                            "priority", "status", "category", "remarks", "details")})
            picture = record.get("picture_path")
            if picture and Path(picture).is_file():
                evidence.add_file(case_id, picture, self.session.username, "snapshot", f"Снимок тревоги {record['id']}")
        except (ValueError, OSError) as exc:
            self.notify(f"Не добавлено: {exc}", error=True)
            return
        alarms.log_operation(self.session.username, f"Тревога {record.get('id')} добавлена в дело")
        self.notify("Тревога добавлена в дело")

    def offer_add_to_case(self, path, kind, title, meta=None):
        """После экспорта: предложить положить пакет в дело."""
        if not self.session.can("playback"):
            return
        cases = evidence.load_cases(evidence.OPEN)
        dialog = _dialog(self, "Добавить экспорт в дело?", "Добавить")
        form = _form()
        form.attach(W.label(f"{title}\n{path}", "muted", wrap=True), 0, 0, 2, 1)
        choice = _field(form, 1, "Дело", _dropdown(["＋ Новое дело"] + [f"№ {c['number']} · {c['title']}"
                                                                          for c in cases]))
        new_title = _field(form, 2, "Название нового дела", Gtk.Entry(text=title))
        choice.connect("notify::selected", lambda d, *_a: new_title.set_sensitive(d.get_selected() == 0))
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT:
                try:
                    index = choice.get_selected() - 1
                    case_id = cases[index]["id"] if index >= 0 else evidence.create_case(
                        new_title.get_text(), username=self.session.username)
                except ValueError as exc:
                    self.notify(str(exc), error=True)
                    return
                self.attach_to_case(case_id, path, kind, title, meta)
            dlg.close()

        dialog.connect("response", response)
        dialog.present()
