"""Интерактивная карта — своё изображение или онлайн-карта (точки камер, дверей, тревожных входов, мигание при событии)
и постановка на охрану из интерфейса (GTK-часть)."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from gi.repository import Gdk, GLib, Graphene, Gtk, Pango

import sentinel_alarms as alarms
import sentinel_arming as arming
import sentinel_geomap as geo
import sentinel_storage as storage
import sentinel_tiletools as tt
import sentinel_widgets as W

HOTSPOT_KINDS = (("camera", "Камера", "◉"), ("door", "Дверь", "▣"), ("input", "Тревожный вход", "⚠"))
KIND_ICONS = {key: icon for key, _title, icon in HOTSPOT_KINDS}
FLASH_SECONDS_ALARM = 60
FLASH_SECONDS_EVENT = 8
MARKER_ANCHOR = (14, 12)    # значок метки (◉) стоит на самой точке


class AlarmMapMixin:
    # ------------------------------------------------------------ карты
    def _current_map(self):
        selected = self.map_choice.get_selected() if hasattr(self, "map_choice") else Gtk.INVALID_LIST_POSITION
        maps = getattr(self, "maps", [])
        if selected == Gtk.INVALID_LIST_POSITION or selected >= len(maps):
            return None
        return maps[selected]

    @staticmethod
    def _is_online(current):
        return bool(current) and current.get("kind") == geo.MAP_ONLINE

    def _map_hotspots(self, map_id):
        with storage.database() as db:
            return [dict(r) for r in db.execute("""SELECT h.*, d.name AS camera_name, d.host, d.channel, d.group_id
                FROM map_hotspots h JOIN devices d ON d.id=h.device_id WHERE h.map_id=? ORDER BY h.id""",
                                                 (map_id,))]

    def _geo_view(self):
        view = self.__dict__.get("geo_view")
        if view is None:
            import sentinel_mapview as mapview
            view = mapview.TileMapView()
            view.connect("view-changed", self._geo_view_changed)
            self.geo_view = view
        return view

    def _map_geo_bar(self):
        """Панель онлайн-карты: поиск адреса, источник тайлов, масштаб."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.map_search = Gtk.SearchEntry(placeholder_text="Адрес или координаты (55.7558, 37.6173)")
        self.map_search.set_hexpand(True)
        self.map_search.connect("activate", lambda *_: self.search_map_address())
        bar.append(self.map_search)
        bar.append(W.button("Найти", lambda: self.search_map_address()))
        self.map_provider_choice = Gtk.DropDown.new_from_strings([])
        self.map_provider_choice.set_tooltip_text("Подложка карты")
        self._map_provider_handler = self.map_provider_choice.connect(
            "notify::selected", lambda *_: self._map_provider_changed())
        bar.append(self.map_provider_choice)
        bar.append(W.button("−", lambda: self._geo_view().zoom_at(-1), tooltip="Отдалить"))
        bar.append(W.button("+", lambda: self._geo_view().zoom_at(1), tooltip="Приблизить"))
        bar.append(W.button("⤢ Все точки", lambda: self.fit_map_points(), tooltip="Показать все точки карты"))
        bar.append(W.button("⚙", lambda: self.open_system("config", "maps"), tooltip="Настройки онлайн-карт"))
        bar.set_visible(False)
        self.map_geo_bar = bar
        return bar

    def _fill_map_providers(self, selected_id):
        settings = geo.load_settings()
        self.map_providers = geo.providers(settings)
        chosen = geo.provider_by_id(selected_id or None, settings)["id"]
        self.map_provider_choice.handler_block(self._map_provider_handler)
        self.map_provider_choice.set_model(Gtk.StringList.new([p["name"] for p in self.map_providers]))
        index = next((i for i, p in enumerate(self.map_providers) if p["id"] == chosen), 0)
        self.map_provider_choice.set_selected(index)
        self.map_provider_choice.handler_unblock(self._map_provider_handler)
        return settings

    def _map_provider_changed(self):
        current = self._current_map()
        index = self.map_provider_choice.get_selected()
        if not self._is_online(current) or index >= len(getattr(self, "map_providers", [])):
            return
        provider = self.map_providers[index]
        current["provider"] = provider["id"]
        with storage.database() as db:
            db.execute("UPDATE maps SET provider=? WHERE id=?", (provider["id"], current["id"]))
        self._geo_view().set_provider(provider["id"])
        self.map_attribution.set_text(provider.get("attribution") or "")
        self.map_attribution.set_visible(bool(provider.get("attribution")))

    def reload_online_maps(self):
        """После изменения настроек онлайн-карт."""
        view = self.__dict__.get("geo_view")
        if view is not None:
            view.reload()
        if hasattr(self, "map_choice"):
            self.__dict__["geo_map_id"] = None
            self._render_map()

    def _render_map(self):
        if not hasattr(self, "map_picture"):
            return
        child = self.map_points.get_first_child()
        while child:        # меню (Popover) на слое точек закрывается само
            following = child.get_next_sibling()
            if not isinstance(child, Gtk.Popover):
                self.map_points.remove(child)
            child = following
        self.map_markers = {}
        self.map_marker_points = []
        self.map_render_token = self.__dict__.get("map_render_token", 0) + 1
        current = self._current_map()
        online = self._is_online(current)
        self.map_geo_bar.set_visible(online)
        self.map_attribution.set_visible(False)
        if current is None:
            self._show_map_child(self.map_picture)
            self.map_picture.set_paintable(None)
            self.map_status.set_text("Добавьте план объекта или онлайн-карту, затем разместите точки.")
            return
        points = self._map_hotspots(current["id"])
        if online:
            view = self._geo_view()
            self._show_map_child(view)
            settings = self._fill_map_providers(current.get("provider"))
            view.set_provider(current.get("provider") or None, settings)
            provider = view.provider
            self.map_attribution.set_text(provider.get("attribution") or "")
            self.map_attribution.set_visible(bool(provider.get("attribution")))
            if self.__dict__.get("geo_map_id") != current["id"]:
                self.geo_map_id = current["id"]
                if current.get("center_lat") is not None and current.get("center_lon") is not None:
                    view.set_view(current["center_lon"], current["center_lat"], current.get("zoom") or 15,
                                  notify=False)
                else:
                    geo_points = [(p["lon"], p["lat"]) for p in points if p.get("lat") is not None]
                    if not (geo_points and view.fit(geo_points)):
                        view.set_view(settings["default_lon"], settings["default_lat"], settings["default_zoom"],
                                      notify=False)
        else:
            self._show_map_child(self.map_picture)
            try:
                if not Path(current["image_path"]).is_file():
                    self.map_picture.set_paintable(None)
                    self.map_status.set_text(f"Файл карты не найден: {current['image_path']}")
                    return
                self.map_picture.set_filename(current["image_path"])
            except GLib.Error as exc:
                self.map_status.set_text(f"Не удалось открыть карту: {exc.message}")
                return
        devices = [p for p in points if p["kind"] == "camera"]
        states = arming.load_states()
        armed = sum(1 for p in points if arming.effective({"id": p["device_id"], "group_id": p["group_id"]}, states))
        hint = ("перетаскивание и колесо — перемещение и масштаб, правый щелчок по карте — добавить точку"
                if online else "правый щелчок по плану — добавить точку")
        self.map_status.set_text(f"{current['name']} · точек: {len(points)} ({len(devices)} камер) · "
                                 f"под охраной: {armed} из {len(points)} · щелчок по точке — открыть, "
                                 f"правый щелчок — действия · {hint}")
        GLib.idle_add(self._place_map_points, points, self.map_render_token)

    def _show_map_child(self, child):
        if self.map_canvas.get_child() is not child:
            self.map_canvas.set_child(child)

    def _marker_title(self, point):
        kind = point.get("kind") or "camera"
        title = point["label"]
        if kind == "door":
            title += f" · дверь {point['number'] or 1}"
        elif kind == "input":
            title += f" · вход {point['number'] or 1}"
        return f"{KIND_ICONS.get(kind, '●')} {title}"

    def _marker_position(self, point):
        """Координаты левого верхнего угла метки в слое точек или None (точка вне карты)."""
        if self._is_online(self._current_map()):
            if point.get("lat") is None or point.get("lon") is None:
                return None
            view = self._geo_view()
            x, y = view.project(point["lon"], point["lat"])
            if x < -400 or y < -60 or x > view.get_width() + 20 or y > view.get_height() + 20:
                return None
            return x - MARKER_ANCHOR[0], y - MARKER_ANCHOR[1]
        width = max(1, self.map_canvas.get_width())
        height = max(1, self.map_canvas.get_height())
        paintable = self.map_picture.get_paintable()
        image_w = paintable.get_intrinsic_width() if paintable is not None else 0
        image_h = paintable.get_intrinsic_height() if paintable is not None else 0
        # Точки привязаны к изображению плана, а не к окну: учитываем поля вписывания.
        left, top, width, height = tt.video_rect(width, height, image_w, image_h)
        return left + point["x"] * width - MARKER_ANCHOR[0], top + point["y"] * height - MARKER_ANCHOR[1]

    def _place_map_points(self, points, token=None):
        if token is not None and token != self.__dict__.get("map_render_token"):
            return GLib.SOURCE_REMOVE      # карту успели перерисовать — эти точки устарели
        states = arming.load_states()
        self.map_markers = {}
        self.map_marker_points = []
        for point in points:
            kind = point.get("kind") or "camera"
            marker = Gtk.Button(label=self._marker_title(point))
            marker.add_css_class("map-point")
            marker.add_css_class(f"map-{kind}")
            device = {"id": point["device_id"], "group_id": point["group_id"]}
            if not arming.effective(device, states):
                marker.add_css_class("map-disarmed")
            where = (f"\n{point['lat']:.6f}, {point['lon']:.6f}" if point.get("lat") is not None
                     and self._is_online(self._current_map()) else "")
            marker.set_tooltip_text(
                f"{point['camera_name']} · {point['host']} CH {point['channel']}{where}\n"
                f"{'Под охраной' if arming.effective(device, states) else 'Снято с охраны'}\n"
                "Щелчок — открыть · правый щелчок — действия")
            marker.connect("clicked", lambda _b, p=point: self.activate_map_point(p))
            context = Gtk.GestureClick()
            context.set_button(3)
            context.connect("released", lambda _g, _n, x, y, p=point, m=marker: self._map_point_menu(p, m, x, y))
            marker.add_controller(context)
            position = self._marker_position(point)
            marker.set_visible(position is not None)
            self.map_points.put(marker, *(position or (0, 0)))
            self.map_markers.setdefault(point["device_id"], []).append(marker)
            self.map_marker_points.append((marker, point))
        for device_id in list(self.__dict__.get("map_flashing", {})):
            self._apply_flash(device_id)
        return GLib.SOURCE_REMOVE

    def _reposition_map_markers(self):
        for marker, point in self.__dict__.get("map_marker_points", []):
            position = self._marker_position(point)
            marker.set_visible(position is not None)
            if position is not None:
                self.map_points.move(marker, *position)

    def _geo_view_changed(self, view):
        current = self._current_map()
        if not self._is_online(current) or self.__dict__.get("geo_map_id") != current["id"]:
            return
        self._reposition_map_markers()
        current["center_lon"], current["center_lat"], current["zoom"] = view.view()
        # Вид карты запоминается (с задержкой, чтобы не писать в базу на каждом шаге перетаскивания).
        if self.__dict__.get("_geo_save_source"):
            GLib.source_remove(self._geo_save_source)
        self._geo_save_source = GLib.timeout_add(1200, self._save_geo_view, dict(current))

    def _save_geo_view(self, current):
        self._geo_save_source = 0
        with storage.database() as db:
            db.execute("UPDATE maps SET center_lat=?, center_lon=?, zoom=? WHERE id=?",
                       (current["center_lat"], current["center_lon"], current["zoom"], current["id"]))
        return GLib.SOURCE_REMOVE

    def fit_map_points(self):
        current = self._current_map()
        if not self._is_online(current):
            return
        points = [(p["lon"], p["lat"]) for p in self._map_hotspots(current["id"]) if p.get("lat") is not None]
        if not self._geo_view().fit(points):
            self.notify("На карте ещё нет точек")

    # ------------------------------------------------------------ жесты на карте
    def _install_map_gestures(self):
        import sentinel_mapview as mapview
        mapview.attach_gestures(self._geo_view(), self.map_canvas, ignore=self._map_point_under,
                                on_click=self._map_clicked_geo)
        context = Gtk.GestureClick()
        context.set_button(3)
        context.connect("released", lambda _g, _n, x, y: self._map_canvas_menu(x, y))
        self.map_canvas.add_controller(context)
        click = Gtk.GestureClick()
        click.set_button(1)
        click.connect("released", lambda _g, n, x, y: n == 1 and self._map_clicked_image(x, y))
        self.map_canvas.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, keyval, *_a: keyval == Gdk.KEY_Escape and self._cancel_map_move())
        self.map_canvas.add_controller(keys)

    def _map_point_under(self, x, y):
        widget = self.map_canvas.pick(x, y, Gtk.PickFlags.DEFAULT)
        while widget is not None and widget is not self.map_canvas:
            if widget.has_css_class("map-point"):
                return True
            widget = widget.get_parent()
        return False

    def _map_location(self, x, y):
        """Щелчок по слою карты (координаты map_canvas) → ('geo', lat, lon) / ('image', fx, fy) / None."""
        current = self._current_map()
        if current is None:
            return None
        if self._is_online(current):
            view = self._geo_view()
            ok, point = self.map_canvas.compute_point(view, Graphene.Point().init(x, y))
            lon, lat = view.unproject(point.x, point.y) if ok else view.unproject(x, y)
            return ("geo", lat, lon)
        paintable = self.map_picture.get_paintable()
        if paintable is None:
            return None
        ok, point = self.map_canvas.compute_point(self.map_picture, Graphene.Point().init(x, y))
        px, py = (point.x, point.y) if ok else (x, y)
        left, top, width, height = tt.video_rect(self.map_picture.get_width(), self.map_picture.get_height(),
                                                 paintable.get_intrinsic_width(), paintable.get_intrinsic_height())
        if width <= 0 or height <= 0:
            return None
        fx, fy = (px - left) / width, (py - top) / height
        if not (0 <= fx <= 1 and 0 <= fy <= 1):
            return None
        return ("image", fx, fy)

    def _map_canvas_menu(self, x, y):
        if self._map_point_under(x, y):
            return
        where = self._map_location(x, y)
        if where is None:
            return
        items = []
        if self.session.can("maps"):
            items.append(("＋ Добавить точку здесь", lambda: self.add_map_hotspot(at=where)))
        if where[0] == "geo":
            lat, lon = where[1], where[2]
            view = self._geo_view()
            items += [("Центрировать здесь", lambda: view.set_view(lon, lat)),
                      ("⤢ Показать все точки", self.fit_map_points),
                      ("Копировать координаты", lambda: self._copy_text(f"{lat:.6f}, {lon:.6f}"))]
        if self.__dict__.get("map_moving"):
            items.insert(0, ("Переместить точку сюда", lambda: self._finish_map_move(where)))
        if items:
            ok, point = self.map_canvas.compute_point(self.map_points, Graphene.Point().init(x, y))
            W.popover_menu(self.map_points, items, *((point.x, point.y) if ok else (x, y)))

    def _copy_text(self, text):
        self.get_display().get_clipboard().set(text)
        self.notify(f"Скопировано: {text}")

    def _map_clicked_geo(self, x, y):
        if self.__dict__.get("map_moving"):
            view = self._geo_view()
            lon, lat = view.unproject(x, y)
            self._finish_map_move(("geo", lat, lon))

    def _map_clicked_image(self, x, y):
        if not self.__dict__.get("map_moving") or self._is_online(self._current_map()) or self._map_point_under(x, y):
            return
        where = self._map_location(x, y)
        if where is not None:
            self._finish_map_move(where)

    def start_map_move(self, point):
        self.map_moving = point
        self.map_status.set_text(f"Щёлкните на карте новое место для «{point['label']}» · Esc — отмена")
        self.map_canvas.set_cursor_from_name("crosshair")
        self.map_canvas.grab_focus()

    def _cancel_map_move(self):
        if not self.__dict__.get("map_moving"):
            return False
        self.map_moving = None
        self.map_canvas.set_cursor(None)
        self._render_map()
        return True

    def _finish_map_move(self, where):
        point = self.__dict__.get("map_moving")
        self.map_moving = None
        self.map_canvas.set_cursor(None)
        if not point or where is None:
            return
        with storage.database() as db:
            if where[0] == "geo":
                db.execute("UPDATE map_hotspots SET lat=?, lon=? WHERE id=?", (where[1], where[2], point["id"]))
            else:
                db.execute("UPDATE map_hotspots SET x=?, y=? WHERE id=?", (where[1], where[2], point["id"]))
        self._render_map()

    # ------------------------------------------------------------ точки
    def _device_by_id(self, device_id):
        return next((d for d in self.devices if d["id"] == device_id), None)

    def activate_map_point(self, point):
        if self.__dict__.get("map_moving"):
            return
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
            items += [None, ("Переместить точку", lambda: self.start_map_move(point)),
                      ("Удалить точку", lambda: self.delete_map_hotspot(point["id"]))]
        W.popover_menu(widget, items, x, y)

    def add_map_hotspot(self, *_args, at=None):
        current = self._current_map()
        if current is None:
            self.notify("Сначала выберите или добавьте карту")
            return
        if not self.devices:
            self.notify("Сначала добавьте устройства в разделе «Устройства»")
            return
        online = self._is_online(current)
        dialog = Gtk.Dialog(title="Точка на карте", transient_for=self, modal=True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Добавить", Gtk.ResponseType.ACCEPT)
        form = Gtk.Grid(column_spacing=10, row_spacing=9, margin_top=14, margin_bottom=14,
                        margin_start=14, margin_end=14)
        kind = Gtk.DropDown.new_from_strings([title for _k, title, _i in HOTSPOT_KINDS])
        device = Gtk.DropDown.new_from_strings([f"{d['name']} · {d['host']} CH {d['channel']}" for d in self.devices])
        number = Gtk.SpinButton.new_with_range(1, 256, 1)
        label = Gtk.Entry(placeholder_text="Название точки")
        rows = [("Тип", kind), ("Устройство", device), ("Номер двери / входа", number), ("Подпись", label)]
        if online:
            lon0, lat0, _zoom = self._geo_view().view()
            if at and at[0] == "geo":
                lat0, lon0 = at[1], at[2]
            first = Gtk.SpinButton.new_with_range(-90, 90, 0.0001)
            second = Gtk.SpinButton.new_with_range(-180, 180, 0.0001)
            for spin, value in ((first, lat0), (second, lon0)):
                spin.set_digits(6)
                spin.set_value(value)
            rows += [("Широта", first), ("Долгота", second)]
        else:
            fx, fy = (at[1], at[2]) if at and at[0] == "image" else (0.5, 0.5)
            first = Gtk.SpinButton.new_with_range(0, 100, 1)
            second = Gtk.SpinButton.new_with_range(0, 100, 1)
            first.set_digits(1)
            second.set_digits(1)
            first.set_value(fx * 100)
            second.set_value(fy * 100)
            rows += [("Позиция X, %", first), ("Позиция Y, %", second)]
        for row, (title, widget) in enumerate(rows):
            self._field(form, title, widget, row)
        form.attach(W.label("Точку можно перенести позже: правый щелчок по ней → «Переместить точку».",
                            "muted", wrap=True), 0, len(rows), 2, 1)
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result == Gtk.ResponseType.ACCEPT and device.get_selected() != Gtk.INVALID_LIST_POSITION:
                chosen = self.devices[device.get_selected()]
                key = HOTSPOT_KINDS[min(kind.get_selected(), len(HOTSPOT_KINDS) - 1)][0]
                if online:
                    x = y = 0.0
                    lat, lon = first.get_value(), second.get_value()
                else:
                    x, y = first.get_value() / 100, second.get_value() / 100
                    lat = lon = None
                with storage.database() as db:
                    db.execute("INSERT INTO map_hotspots(map_id,device_id,label,x,y,kind,number,lat,lon) "
                               "VALUES(?,?,?,?,?,?,?,?,?)",
                               (current["id"], chosen["id"], label.get_text().strip() or chosen["name"], x, y, key,
                                number.get_value_as_int() if key != "camera" else 0, lat, lon))
                self._render_map()
            dlg.close()
        dialog.connect("response", response)
        dialog.present()

    def delete_map_hotspot(self, hotspot_id):
        with storage.database() as db:
            db.execute("DELETE FROM map_hotspots WHERE id=?", (hotspot_id,))
        self._render_map()

    # ------------------------------------------------------------ добавление и свойства карты
    def add_map(self, *_args):
        self._map_dialog(None)

    def edit_map(self, *_args):
        current = self._current_map()
        if current is None:
            self.notify("Сначала выберите или добавьте карту")
            return
        self._map_dialog(current)

    def _map_dialog(self, current):
        settings = geo.load_settings()
        provider_list = geo.providers(settings)
        dialog = Gtk.Dialog(title="Свойства карты" if current else "Новая карта", transient_for=self, modal=True)
        dialog.set_default_size(560, -1)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Сохранить" if current else "Добавить", Gtk.ResponseType.ACCEPT)
        form = Gtk.Grid(column_spacing=10, row_spacing=9, margin_top=14, margin_bottom=14,
                        margin_start=14, margin_end=14)
        name = Gtk.Entry(text=current["name"] if current else "", placeholder_text="Например: Склад, 1 этаж")
        kinds = ((geo.MAP_IMAGE, "Своё изображение (план, схема, снимок)"), (geo.MAP_ONLINE, "Онлайн-карта"))
        kind = Gtk.DropDown.new_from_strings([title for _k, title in kinds])
        kind.set_selected(1 if self._is_online(current) else 0)
        kind.set_sensitive(current is None)     # у точек разные координаты — тип не меняется
        image = {"path": current["image_path"] if current and not self._is_online(current) else ""}
        file_label = W.label(image["path"] or "Файл не выбран", "muted")
        file_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        file_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        file_box.append(file_label)
        file_label.set_hexpand(True)
        provider = Gtk.DropDown.new_from_strings([p["name"] for p in provider_list])
        chosen = geo.provider_by_id(current.get("provider") if current else None, settings)["id"]
        provider.set_selected(next((i for i, p in enumerate(provider_list) if p["id"] == chosen), 0))
        center = Gtk.Entry(placeholder_text="Адрес или координаты центра (необязательно)")

        def choose_file():
            chooser = Gtk.FileChooserNative.new("Выберите изображение карты", dialog, Gtk.FileChooserAction.OPEN,
                                                "Выбрать", "Отмена")
            images = Gtk.FileFilter()
            images.set_name("Изображения")
            images.add_pixbuf_formats()
            chooser.add_filter(images)

            def done(native, response):
                file = native.get_file()
                if response == Gtk.ResponseType.ACCEPT and file:
                    image["path"] = file.get_path()
                    file_label.set_text(image["path"])
                    if not name.get_text().strip():
                        name.set_text(Path(image["path"]).stem)
                native.destroy()
            chooser.connect("response", done)
            chooser.show()

        file_box.append(W.button("Выбрать файл…", choose_file))
        rows = [("Название", name), ("Тип карты", kind), ("Изображение", file_box), ("Подложка", provider),
                ("Центр", center)]
        labels = {}
        for row, (title, widget) in enumerate(rows):
            labels[title] = W.label(title, "muted")
            form.attach(labels[title], 0, row, 1, 1)
            widget.set_hexpand(True)
            form.attach(widget, 1, row, 1, 1)
        hint = W.label("", "muted", wrap=True)
        form.attach(hint, 0, len(rows), 2, 1)

        def kind_changed(*_):
            online = kinds[kind.get_selected()][0] == geo.MAP_ONLINE
            for title, widget in (("Изображение", file_box),):
                labels[title].set_visible(not online)
                widget.set_visible(not online)
            for title, widget in (("Подложка", provider), ("Центр", center)):
                labels[title].set_visible(online)
                widget.set_visible(online)
            hint.set_text("Точки ставятся по географическим координатам: правый щелчок по карте → «Добавить "
                          "точку здесь». Нужен доступ в интернет или свой источник тайлов (Конфигурация "
                          "системы › Онлайн-карты)." if online else
                          "Подойдёт любой PNG/JPEG/SVG: поэтажный план, схема территории, снимок. Точки "
                          "привязываются к изображению.")

        kind.connect("notify::selected", kind_changed)
        kind_changed()
        dialog.get_content_area().append(form)

        def response(dlg, result):
            if result != Gtk.ResponseType.ACCEPT:
                dlg.close()
                return
            title = name.get_text().strip()
            online = kinds[kind.get_selected()][0] == geo.MAP_ONLINE
            if not title:
                self.notify("Введите название карты", error=True)
                return
            if not online and not image["path"]:
                self.notify("Выберите файл изображения", error=True)
                return
            provider_id = provider_list[provider.get_selected()]["id"] if online else ""
            try:
                with storage.database() as db:
                    if current:
                        db.execute("UPDATE maps SET name=?, image_path=?, provider=? WHERE id=?",
                                   (title, "" if online else image["path"], provider_id, current["id"]))
                        map_id = current["id"]
                    else:
                        map_id = db.execute("INSERT INTO maps(name,image_path,kind,provider) VALUES(?,?,?,?)",
                                            (title, "" if online else image["path"],
                                             geo.MAP_ONLINE if online else geo.MAP_IMAGE, provider_id)).lastrowid
            except sqlite3.IntegrityError:
                self.notify("Карта с таким именем уже существует", error=True)
                return
            dlg.close()
            alarms.log_operation(self.session.username, "Карта изменена" if current else "Карта добавлена", title)
            self.__dict__["geo_map_id"] = None
            self._refresh_maps()
            self.open_map(map_id)
            query = center.get_text().strip()
            if online and query:
                self._geocode_async(query, lambda results, mid=map_id: self._apply_center(mid, results))

        dialog.connect("response", response)
        dialog.present()

    def _apply_center(self, map_id, results):
        if not results:
            self.notify("Адрес центра не найден — найдите его в строке поиска над картой", error=True)
            return
        _title, lat, lon = results[0]
        with storage.database() as db:
            db.execute("UPDATE maps SET center_lat=?, center_lon=?, zoom=? WHERE id=?", (lat, lon, 16, map_id))
        current = self._current_map()
        if current and current["id"] == map_id:
            self._geo_view().set_view(lon, lat, 16)

    def delete_map(self, *_args):
        current = self._current_map()
        if current is None:
            return

        def go():
            with storage.database() as db:
                db.execute("DELETE FROM map_hotspots WHERE map_id=?", (current["id"],))
                db.execute("DELETE FROM maps WHERE id=?", (current["id"],))
            alarms.log_operation(self.session.username, "Карта удалена", current["name"])
            self._refresh_maps()

        self._confirm(f"Удалить карту «{current['name']}» вместе с её точками?", go)

    # ------------------------------------------------------------ поиск адреса
    def _geocode_async(self, query, done):
        def worker():
            try:
                results = geo.geocode(query)
            except Exception:
                results = None
            GLib.idle_add(lambda: (done(results), False)[1])
        threading.Thread(target=worker, daemon=True).start()

    def search_map_address(self):
        query = self.map_search.get_text().strip()
        if not query or not self._is_online(self._current_map()):
            return
        self.map_status.set_text(f"Поиск: {query}…")

        def show(results):
            if results is None:
                self.notify("Поиск адреса недоступен: нет связи с сервисом поиска", error=True)
                self._render_map()
                return
            if not results:
                self.notify(f"Ничего не найдено: {query}")
                self._render_map()
                return
            view = self._geo_view()
            if len(results) == 1:
                view.set_view(results[0][2], results[0][1], max(view.zoom, 17))
                self._render_map()
                return
            items = [(title if len(title) < 90 else title[:87] + "…",
                      lambda la=lat, lo=lon: view.set_view(lo, la, max(view.zoom, 17)))
                     for title, lat, lon in results]
            self._render_map()
            W.popover_menu(self.map_search, items)

        self._geocode_async(query, show)

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
