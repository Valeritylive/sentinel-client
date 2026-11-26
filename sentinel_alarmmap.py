"""Интерактивная карта (точки камер, дверей, тревожных входов, мигание при событии)
и постановка на охрану из интерфейса (GTK-часть)."""
from __future__ import annotations

import time

from gi.repository import GLib, Gtk

import sentinel_alarms as alarms
import sentinel_arming as arming
import sentinel_storage as storage
import sentinel_tiletools as tt
import sentinel_widgets as W

HOTSPOT_KINDS = (("camera", "Камера", "◉"), ("door", "Дверь", "▣"), ("input", "Тревожный вход", "⚠"))
KIND_ICONS = {key: icon for key, _title, icon in HOTSPOT_KINDS}
FLASH_SECONDS_ALARM = 60
FLASH_SECONDS_EVENT = 8


class AlarmMapMixin:
    # ------------------------------------------------------------ точки карты
    def _map_hotspots(self, map_id):
        with storage.database() as db:
            return [dict(r) for r in db.execute("""SELECT h.*, d.name AS camera_name, d.host, d.channel, d.group_id
                FROM map_hotspots h JOIN devices d ON d.id=h.device_id WHERE h.map_id=? ORDER BY h.id""",
                                                 (map_id,))]

    def _render_map(self):
        if not hasattr(self, "map_picture"):
            return
        self._clear_box(self.map_points)
        self.map_markers = {}
        selected = self.map_choice.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(getattr(self, "maps", [])):
            self.map_picture.set_paintable(None)
            self.map_status.set_text("Добавьте изображение плана или карты, затем разместите точки.")
            return
        current = self.maps[selected]
        try:
            from pathlib import Path
            if not Path(current["image_path"]).is_file():
                self.map_picture.set_paintable(None)
                self.map_status.set_text(f"Файл карты не найден: {current['image_path']}")
                return
            self.map_picture.set_filename(current["image_path"])
        except GLib.Error as exc:
            self.map_status.set_text(f"Не удалось открыть карту: {exc.message}")
            return
        points = self._map_hotspots(current["id"])
        devices = [p for p in points if p["kind"] == "camera"]
        states = arming.load_states()
        armed = sum(1 for p in points if arming.effective({"id": p["device_id"], "group_id": p["group_id"]}, states))
        self.map_status.set_text(f"{current['name']} · точек: {len(points)} ({len(devices)} камер) · "
                                 f"под охраной: {armed} из {len(points)} · щелчок — открыть, правый щелчок — действия")
        GLib.idle_add(self._place_map_points, points)

    def _place_map_points(self, points):
        width = max(1, self.map_canvas.get_width())
        height = max(1, self.map_canvas.get_height())
        paintable = self.map_picture.get_paintable()
        image_w = paintable.get_intrinsic_width() if paintable is not None else 0
        image_h = paintable.get_intrinsic_height() if paintable is not None else 0
        # Точки привязаны к изображению плана, а не к окну: учитываем поля вписывания.
        left, top, width, height = tt.video_rect(width, height, image_w, image_h)
        states = arming.load_states()
        self.map_markers = {}
        for point in points:
            kind = point.get("kind") or "camera"
            title = point["label"]
            if kind == "door":
                title += f" · дверь {point['number'] or 1}"
            elif kind == "input":
                title += f" · вход {point['number'] or 1}"
            marker = Gtk.Button(label=f"{KIND_ICONS.get(kind, '●')} {title}")
            marker.add_css_class("map-point")
            marker.add_css_class(f"map-{kind}")
            device = {"id": point["device_id"], "group_id": point["group_id"]}
            if not arming.effective(device, states):
                marker.add_css_class("map-disarmed")
            marker.set_tooltip_text(
                f"{point['camera_name']} · {point['host']} CH {point['channel']}\n"
                f"{'Под охраной' if arming.effective(device, states) else 'Снято с охраны'}\n"
                "Щелчок — открыть · правый щелчок — действия")
            marker.connect("clicked", lambda _b, p=point: self.activate_map_point(p))
            context = Gtk.GestureClick()
            context.set_button(3)
            context.connect("released", lambda _g, _n, x, y, p=point, m=marker: self._map_point_menu(p, m, x, y))
            marker.add_controller(context)
            self.map_points.put(marker, left + point["x"] * width, top + point["y"] * height)
            self.map_markers.setdefault(point["device_id"], []).append(marker)
        for device_id in list(self.__dict__.get("map_flashing", {})):
            self._apply_flash(device_id)
        return GLib.SOURCE_REMOVE

    def _device_by_id(self, device_id):
        return next((d for d in self.devices if d["id"] == device_id), None)

    def activate_map_point(self, point):
        self.stop_map_flash(point["device_id"])
        device = self._device_by_id(point["device_id"])
        if device is None:
            return
        kind = point.get("kind") or "camera"
        if kind == "door":
            self._close_inner()
            self._place_door(device, point["number"] or 1)
            return
        if kind == "input":
            records = [r for r in alarms.load_alarm_records(50, status="unacknowledged")
                       if r["device_id"] == device["id"]]
            if records:
                self.show_alarm_details(records[0]["id"])
            else:
                self.notify(f"{point['label']}: новых тревог нет")
            return
        records = [r for r in alarms.load_alarm_records(20, status="unacknowledged") if r["device_id"] == device["id"]]
        if records:
            self.show_alarm_details(records[0]["id"])
        self.open_live_dialog(device)

    def _map_point_menu(self, point, widget, x, y):
        device = self._device_by_id(point["device_id"])
        if device is None:
            return
        armed = arming.effective(device)
        items = [("Открыть видео", lambda: self.open_live_dialog(device)),
                 ("Найти записи", lambda: self._open_archive_for_device(device))]
        if point.get("kind") == "door":
            items.insert(0, ("Показать дверь в окне", lambda: self.activate_map_point(point)))
        if self.session.can("alarm_ack") or self.session.can("alarms"):
            items.append(("Снять с охраны" if armed else "Поставить на охрану",
                          lambda: self.arm_devices([device["id"]], not armed, point["label"])))
        if self.session.can("maps"):
            items += [None, ("Удалить точку", lambda: self.delete_map_hotspot(point["id"]))]
        W.popover_menu(widget, items, x, y)

    def add_map_hotspot(self, *_args):
        selected = self.map_choice.get_selected()
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(getattr(self, "maps", [])):
            self.notify("Сначала выберите или добавьте карту")
            return
        if not self.devices:
            self.notify("Сначала добавьте устройства в разделе «Устройства»")
            return
        dialog = Gtk.Dialog(title="Точка на карте", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Добавить", Gtk.ResponseType.ACCEPT)
        form = Gtk.Grid(column_spacing=10, row_spacing=9, margin_top=14, margin_bottom=14,
                        margin_start=14, margin_end=14)
        kind = Gtk.DropDown.new_from_strings([title for _k, title, _i in HOTSPOT_KINDS])
        device = Gtk.DropDown.new_from_strings([f"{d['name']} · {d['host']} CH {d['channel']}" for d in self.devices])
        number = Gtk.SpinButton.new_with_range(1, 256, 1)
        label = Gtk.Entry(placeholder_text="Название точки")
        xpos = Gtk.SpinButton.new_with_range(0, 100, 1)
        xpos.set_value(50)
        ypos = Gtk.SpinButton.new_with_range(0, 100, 1)
        ypos.set_value(50)
        for row, (title, widget) in enumerate((("Тип", kind), ("Устройство", device),
                                               ("Номер двери / входа", number), ("Подпись", label),
                                               ("Позиция X, %", xpos), ("Позиция Y, %", ypos))):
            self._field(form, title, widget, row)
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT and device.get_selected() != Gtk.INVALID_LIST_POSITION:
                chosen = self.devices[device.get_selected()]
                key = HOTSPOT_KINDS[min(kind.get_selected(), len(HOTSPOT_KINDS) - 1)][0]
                with storage.database() as db:
                    db.execute("INSERT INTO map_hotspots(map_id,device_id,label,x,y,kind,number) VALUES(?,?,?,?,?,?,?)",
                               (self.maps[selected]["id"], chosen["id"], label.get_text().strip() or chosen["name"],
                                xpos.get_value() / 100, ypos.get_value() / 100, key,
                                number.get_value_as_int() if key != "camera" else 0))
                self._render_map()
            dlg.close()
        dialog.connect("response", response)
        dialog.present()

    def delete_map_hotspot(self, hotspot_id):
        with storage.database() as db:
            db.execute("DELETE FROM map_hotspots WHERE id=?", (hotspot_id,))
        self._render_map()

    # ------------------------------------------------------------ мигание
    def _flash_map_device(self, device_id, alarm=False):
        """Событие от устройства: точки на карте мигают (тревога — дольше, до подтверждения/щелчка)."""
        if not device_id:
            return
        flashing = self.__dict__.setdefault("map_flashing", {})
        until = time.monotonic() + (FLASH_SECONDS_ALARM if alarm else FLASH_SECONDS_EVENT)
        flashing[device_id] = max(flashing.get(device_id, 0), until)
        self._apply_flash(device_id)
        if not self.__dict__.get("_map_flash_source"):
            self._map_flash_source = GLib.timeout_add(500, self._map_flash_tick)

    def _apply_flash(self, device_id):
        for marker in self.__dict__.get("map_markers", {}).get(device_id, []):
            marker.add_css_class("map-alarm")

    def _map_flash_tick(self):
        flashing = self.__dict__.get("map_flashing", {})
        now = time.monotonic()
        phase = int(now * 2) % 2 == 0
        for device_id, until in list(flashing.items()):
            markers = self.__dict__.get("map_markers", {}).get(device_id, [])
            if now >= until:
                flashing.pop(device_id, None)
                for marker in markers:
                    marker.remove_css_class("map-alarm")
                    marker.remove_css_class("map-alarm-on")
                continue
            for marker in markers:
                (marker.add_css_class if phase else marker.remove_css_class)("map-alarm-on")
        if not flashing:
            self._map_flash_source = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def stop_map_flash(self, device_id):
        flashing = self.__dict__.get("map_flashing", {})
        if device_id in flashing:
            flashing[device_id] = 0

    def show_device_on_map(self, device_id):
        """Открыть карту, где есть точка устройства, и подсветить её."""
        with storage.database() as db:
            row = db.execute("SELECT map_id FROM map_hotspots WHERE device_id=? ORDER BY id LIMIT 1",
                             (device_id,)).fetchone()
        if not row:
            self.notify("Устройства нет ни на одной карте")
            return
        self.open_map(row["map_id"])
        self._flash_map_device(device_id, alarm=False)

    # ------------------------------------------------------------ охрана
    def _can_arm(self):
        return self.session.can("alarm_ack") or self.session.can("alarms")

    def arm_devices(self, device_ids, armed, title=""):
        if not self._can_arm():
            self.notify("Нет прав на постановку на охрану", error=True)
            return
        arming.set_devices_armed(device_ids, armed, self.session.username)
        self._after_arming(f"{'Поставлено на охрану' if armed else 'Снято с охраны'}: {title or len(device_ids)}")

    def arm_area(self, group, armed):
        if not self._can_arm():
            self.notify("Нет прав на постановку на охрану", error=True)
            return
        if group is None:  # «Без области» — все каналы без группы
            ids = [d["id"] for d in self.devices if not d.get("group_id")]
            arming.set_devices_armed(ids, armed, self.session.username)
        else:
            arming.set_armed(arming.AREA, group["id"], armed, self.session.username)
        name = group["name"] if group else "Без области"
        self._after_arming(f"Область «{name}» {'поставлена на охрану' if armed else 'снята с охраны'}")

    def arm_system(self, armed):
        if not self._can_arm():
            self.notify("Нет прав на постановку на охрану", error=True)
            return
        arming.set_armed(arming.SYSTEM, 0, armed, self.session.username)
        self._after_arming("Вся система поставлена на охрану" if armed else "Вся система снята с охраны")

    def _after_arming(self, message):
        alarms.log_operation(self.session.username, message)
        self.notify(message)
        self._fill_resource_list()
        if hasattr(self, "map_choice"):
            self._render_map()
        self._update_arming_summary()

    def _update_arming_summary(self):
        label = self.__dict__.get("arming_summary")
        if label is None:
            return
        cameras = [d for d in self.devices if (d.get("category") or "video") == "video"]
        armed, total = arming.summary(cameras)
        label.set_text(f"🛡 Под охраной: {armed} из {total}")

    def arming_menu_items(self, device_ids, title):
        if not self._can_arm() or not device_ids:
            return []
        states = arming.load_states()
        by_id = {d["id"]: d for d in self.devices}
        values = {arming.effective(by_id.get(i, {"id": i}), states) for i in device_ids}
        items = []
        if False in values:
            items.append(("🛡 Поставить на охрану", lambda: self.arm_devices(device_ids, True, title)))
        if True in values:
            items.append(("○ Снять с охраны", lambda: self.arm_devices(device_ids, False, title)))
        return items
