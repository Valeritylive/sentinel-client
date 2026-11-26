"""Виджет онлайн-карты (GTK 4): тайлы Web Mercator, перетаскивание, масштаб колесом и кнопками.

Отрисовка через snapshot и Gdk.Texture (без cairo). Пока тайл грузится, показывается растянутый
тайл более мелкого масштаба из памяти. Жесты подключает владелец (attach_gestures), потому что
поверх карты лежит слой точек.
"""
from __future__ import annotations

import math
from collections import OrderedDict

from gi.repository import Gdk, GLib, GObject, Graphene, Gtk

import sentinel_geomap as geo

MEMORY_TILES = 600
OVERZOOM = 2            # приближение дальше максимума источника (тайлы растягиваются)
_FETCHER = None


def fetcher():
    global _FETCHER
    if _FETCHER is None:
        _FETCHER = geo.TileFetcher()
    return _FETCHER


def _rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def _rgba(text):
    color = Gdk.RGBA()
    color.parse(text)
    return color


class TileMapView(Gtk.Widget):
    __gtype_name__ = "SentinelTileMapView"
    __gsignals__ = {"view-changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_focusable(True)
        self.settings = geo.load_settings()
        self.provider = geo.provider_by_id(None, self.settings)
        self.lon, self.lat = self.settings["default_lon"], self.settings["default_lat"]
        self.zoom = int(self.settings["default_zoom"])
        self._textures = OrderedDict()
        self._size = (0, 0)
        self._background = _rgba("#202125")
        self._grid = _rgba("#2b2c31")

    # ------------------------------------------------------------ состояние
    @property
    def max_zoom(self):
        return int(self.provider.get("max_zoom") or 19) + OVERZOOM

    def set_provider(self, provider_id, settings=None):
        if settings is not None:
            self.settings = settings
        provider = geo.provider_by_id(provider_id, self.settings)
        if provider != self.provider:
            self.provider = provider
            self._textures.clear()
        self.zoom = int(geo.clamp_zoom(self.zoom, self.max_zoom))
        self.queue_draw()

    def set_view(self, lon, lat, zoom=None, notify=True):
        self.lon = geo.wrap_lon(lon)
        self.lat = geo.clamp_lat(lat)
        if zoom is not None:
            self.zoom = int(round(geo.clamp_zoom(zoom, self.max_zoom)))
        self.queue_draw()
        if notify:
            self.emit("view-changed")

    def view(self):
        return self.lon, self.lat, self.zoom

    def project(self, lon, lat):
        """Географическая точка → координаты в виджете (с учётом перехода через 180°)."""
        cx, cy = geo.lonlat_to_world(self.lon, self.lat, self.zoom)
        x, y = geo.lonlat_to_world(lon, lat, self.zoom)
        world = geo.TILE_SIZE * (2 ** self.zoom)
        dx = x - cx
        if dx > world / 2:
            dx -= world
        elif dx < -world / 2:
            dx += world
        return self.get_width() / 2 + dx, self.get_height() / 2 + (y - cy)

    def unproject(self, x, y):
        cx, cy = geo.lonlat_to_world(self.lon, self.lat, self.zoom)
        lon, lat = geo.world_to_lonlat(cx + x - self.get_width() / 2, cy + y - self.get_height() / 2, self.zoom)
        return lon, lat

    def pan_by(self, dx, dy):
        cx, cy = geo.lonlat_to_world(self.lon, self.lat, self.zoom)
        world = geo.TILE_SIZE * (2 ** self.zoom)
        cy = max(0.0, min(world, cy - dy))
        lon, lat = geo.world_to_lonlat(cx - dx, cy, self.zoom)
        self.set_view(lon, lat)

    def zoom_at(self, step, x=None, y=None):
        new_zoom = int(geo.clamp_zoom(self.zoom + step, self.max_zoom))
        if new_zoom == self.zoom:
            return
        if x is None:
            x, y = self.get_width() / 2, self.get_height() / 2
        lon, lat = self.unproject(x, y)
        self.zoom = new_zoom
        # Точка под курсором остаётся на месте.
        px, py = geo.lonlat_to_world(lon, lat, self.zoom)
        cx, cy = px - (x - self.get_width() / 2), py - (y - self.get_height() / 2)
        lon, lat = geo.world_to_lonlat(cx, cy, self.zoom)
        self.set_view(lon, lat)

    def fit(self, points):
        result = geo.fit_bounds(points, self.get_width() or 800, self.get_height() or 500,
                                int(self.provider.get("max_zoom") or 18))
        if result:
            self.set_view(*result)
            return True
        return False

    # ------------------------------------------------------------ отрисовка
    def do_size_allocate(self, width, height, baseline):
        if (width, height) != self._size:
            self._size = (width, height)
            GLib.idle_add(lambda: (self.emit("view-changed"), False)[1])

    def do_measure(self, orientation, for_size):
        return 64, 400, -1, -1

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        snapshot.append_color(self._background, _rect(0, 0, width, height))
        if width <= 0 or height <= 0:
            return
        max_tile = int(self.provider.get("max_zoom") or 19)
        pid = self.provider["id"]
        for z, x, y, sx, sy, size in geo.visible_tiles(self.lon, self.lat, self.zoom, width, height, max_tile):
            x0, y0 = math.floor(sx), math.floor(sy)
            x1, y1 = math.ceil(sx + size), math.ceil(sy + size)
            texture = self._texture((pid, z, x, y))
            if texture is not None:
                snapshot.append_texture(texture, _rect(x0, y0, x1 - x0, y1 - y0))
                continue
            fetcher().request(self.provider, z, x, y, self._tile_loaded, self.settings)
            if not self._draw_parent(snapshot, pid, z, x, y, x0, y0, x1, y1):
                snapshot.append_color(self._grid, _rect(x0, y0, x1 - x0, 1))
                snapshot.append_color(self._grid, _rect(x0, y0, 1, y1 - y0))

    def _draw_parent(self, snapshot, pid, z, x, y, x0, y0, x1, y1):
        for depth in range(1, min(z, 6) + 1):
            parent = self._textures.get((pid, z - depth, x >> depth, y >> depth))
            if parent is None:
                continue
            factor = 1 << depth
            size_x, size_y = (x1 - x0) * factor, (y1 - y0) * factor
            off_x, off_y = (x - ((x >> depth) << depth)) * (x1 - x0), (y - ((y >> depth) << depth)) * (y1 - y0)
            snapshot.push_clip(_rect(x0, y0, x1 - x0, y1 - y0))
            snapshot.append_texture(parent, _rect(x0 - off_x, y0 - off_y, size_x, size_y))
            snapshot.pop()
            return True
        return False

    def _texture(self, key):
        texture = self._textures.get(key)
        if texture is not None:
            self._textures.move_to_end(key)
        return texture

    def _tile_loaded(self, key, data):
        GLib.idle_add(self._store_tile, key, data)

    def _store_tile(self, key, data):
        if data and key[0] == self.provider["id"]:
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except GLib.Error:
                texture = None
            if texture is not None:
                self._textures[key] = texture
                while len(self._textures) > MEMORY_TILES:
                    self._textures.popitem(last=False)
                self.queue_draw()
        return GLib.SOURCE_REMOVE

    def reload(self):
        self.settings = geo.load_settings()
        self.provider = geo.provider_by_id(self.provider["id"], self.settings)
        self._textures.clear()
        self.queue_draw()


def attach_gestures(view, target, ignore=None, on_click=None):
    """Перетаскивание, колесо и двойной щелчок на виджете target (обычно Overlay поверх карты).

    ignore(x, y) → True, если под курсором точка карты (жест отдаётся ей).
    on_click(x, y) — одиночный щелчок по пустому месту карты без перетаскивания."""
    state = {"last": (0.0, 0.0), "moved": False}

    def active():
        return view.get_visible() and view.get_parent() is not None and view.get_mapped()

    def to_view(x, y):
        ok, point = target.compute_point(view, Graphene.Point().init(x, y))
        return (point.x, point.y) if ok else (x, y)

    drag = Gtk.GestureDrag()
    drag.set_button(1)

    def begin(gesture, x, y):
        if not active() or (ignore and ignore(x, y)):
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        state["last"], state["moved"] = (0.0, 0.0), False
        view.grab_focus()

    def update(_gesture, dx, dy):
        lx, ly = state["last"]
        if not state["moved"] and abs(dx) + abs(dy) < 4:
            return
        state["moved"] = True
        view.pan_by(dx - lx, dy - ly)
        state["last"] = (dx, dy)

    def end(gesture, dx, dy):
        if not state["moved"] and on_click is not None:
            ok, sx, sy = gesture.get_start_point()
            if ok:
                on_click(*to_view(sx, sy))

    drag.connect("drag-begin", begin)
    drag.connect("drag-update", update)
    drag.connect("drag-end", end)
    target.add_controller(drag)

    scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL
                                           | Gtk.EventControllerScrollFlags.DISCRETE)
    pointer = {"x": None, "y": None}
    motion = Gtk.EventControllerMotion()
    motion.connect("motion", lambda _c, x, y: pointer.update(x=x, y=y))
    target.add_controller(motion)

    def scrolled(_controller, _dx, dy):
        if not active():
            return False
        x, y = (None, None) if pointer["x"] is None else to_view(pointer["x"], pointer["y"])
        view.zoom_at(-1 if dy > 0 else 1, x, y)
        return True

    scroll.connect("scroll", scrolled)
    target.add_controller(scroll)

    double = Gtk.GestureClick()
    double.set_button(1)

    def pressed(_gesture, n_press, x, y):
        if n_press == 2 and active() and not (ignore and ignore(x, y)):
            view.zoom_at(1, *to_view(x, y))

    double.connect("pressed", pressed)
    target.add_controller(double)

    keys = Gtk.EventControllerKey()

    def key(_c, keyval, _code, _state):
        step = 80
        moves = {Gdk.KEY_Left: (step, 0), Gdk.KEY_Right: (-step, 0), Gdk.KEY_Up: (0, step), Gdk.KEY_Down: (0, -step)}
        if keyval in moves:
            view.pan_by(*moves[keyval])
            return True
        if keyval in (Gdk.KEY_plus, Gdk.KEY_KP_Add, Gdk.KEY_equal):
            view.zoom_at(1)
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            view.zoom_at(-1)
            return True
        return False

    keys.connect("key-pressed", key)
    view.add_controller(keys)
