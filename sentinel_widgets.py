"""GTK-виджеты интерфейса в стиле HikCentral Lite: шкала времени, уведомления,
внутренние окна, секции левой панели, таблицы, редактор недельного расписания.

Совместимо с GTK 4.6 (Ubuntu 22.04): без Gtk.AlertDialog/FileDialog/ContentFit.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import gi
from gi.repository import Gdk, GLib, Gtk

import sentinel_timeline as tl

try:
    gi.require_foreign("cairo")
    HAS_CAIRO = True
except (ImportError, AttributeError, ValueError):
    HAS_CAIRO = False

ACCENT = (0.91, 0.14, 0.17)

CSS = b"""
window, .background { background: #1e1f22; color: #d7d8db; font-size: 12px; }
headerbar { background: #141517; border-bottom: 1px solid #000; min-height: 36px; padding: 0 4px;
            box-shadow: none; color: #d7d8db; }
headerbar button { color: #c9cacd; background: transparent; border: none; box-shadow: none;
                   min-height: 26px; padding: 2px 7px; }
headerbar button:hover { background: #2a2b2f; }
.hcl-logo { color: #e8232c; font-size: 18px; font-weight: 800; margin: 0 8px 0 4px; }
.view-tab { background: #232428; border-radius: 6px 6px 0 0; margin-top: 5px; padding: 0 2px; }
.view-tab.active { background: #2e2f34; box-shadow: inset 0 2px #e8232c; }
.view-tab button { padding: 2px 8px; }
.view-tab .tab-close { padding: 0 4px; min-width: 16px; opacity: 0.6; }
.side-panel { background: #202124; border-right: 1px solid #111; }
.side-panel.right { border-right: none; border-left: 1px solid #111; }
.panel-handle { background: #26272b; border: none; min-width: 10px; padding: 0; color: #8b8d93; border-radius: 0; }
.panel-handle-h { min-width: 0; min-height: 10px; font-size: 9px; padding: 0; }
.panel-handle:hover { background: #303136; color: #fff; }
.section-title { font-weight: 600; }
expander title { padding: 6px 4px; }
.tree-row { padding: 3px 4px; border-radius: 3px; background: transparent; border: none; box-shadow: none; }
.tree-row:hover { background: #2c2d32; }
.tree-row.selected { background: #34353b; }
.status-online { color: #3fbf6f; }
.cam-idle { color: #6c6e74; }
.cam-active { color: #3fbf6f; }
.status-offline { color: #8b8d93; }
.status-error { color: #e8232c; }
.shortcut { background: #2a2b30; border: none; border-radius: 4px; padding: 8px 10px; margin: 2px 6px; }
.shortcut:hover { background: #33343a; }
.workspace { background: #141517; }
.camera-tile { background: #26272b; border: 1px solid #141517; border-radius: 0; }
.camera-tile:hover { border-color: #55565c; }
.camera-tile.selected { border: 2px solid #e8232c; }
.rec-continuous { color: #2985e0; } .rec-motion { color: #edc733; } .rec-alarm { color: #e03333; }
.rec-smart { color: #9e66e6; } .rec-local { color: #40bf73; }
.map-point { background: alpha(#1b1c20, 0.85); color: #e8e8ea; border: 1px solid #3a3b40; border-radius: 12px; padding: 1px 8px; }
.map-point.map-door { border-color: #3b8fd9; } .map-point.map-input { border-color: #d9a43b; }
.map-point.map-disarmed { color: #8d8f96; background: alpha(#1b1c20, 0.55); border: 1px dashed #6a6c72; }
.map-point.map-alarm { border-color: #e8232c; }
.map-point.map-alarm-on { background: #e8232c; color: #fff; }
.board-card { background: #232428; border: 1px solid #2f3035; border-radius: 6px; padding: 12px; }
.board-card button, .board-card button label { color: #cfd3da; }
.tile-toolbar { background: alpha(#000, 0.6); border-radius: 4px; padding: 1px; }
.tile-tool { color: #e6e6e6; min-width: 24px; min-height: 22px; padding: 1px 4px; font-size: 11px; }
.tile-tool:hover { background: alpha(#fff, 0.14); }
.tile-tool-on { color: #fff; background: #e8232c; }
.tile-ptz-pad { background: alpha(#000, 0.5); border-radius: 6px; padding: 3px; }
.tile-ptz-label { color: #bbb; font-size: 10px; }
.tile-ptz-pad button { background: alpha(#000, 0.35); background-image: none; color: #eee; border: none; box-shadow: none; min-width: 24px; min-height: 22px; padding: 0; }
.tile-ptz-pad button:hover { background: alpha(#fff, 0.2); }
.tile-caption { color: #eee; font-size: 11px; background: alpha(#000, 0.45); padding: 1px 6px; border-radius: 2px; }
.playback-bar { background: #1b1c1f; border-top: 1px solid #000; padding: 4px 8px; }
.mode-button { background: #26272b; border: none; border-radius: 3px; padding: 2px 10px; }
.mode-button:checked { background: #34353b; }
.mode-dot-live { color: #3fbf6f; }
.mode-dot-playback { color: #4a9eff; }
.filter-button { background: transparent; border: none; padding: 2px 8px; }
.filter-button:checked { background: #34353b; }
.round-button { border-radius: 50%; min-width: 30px; min-height: 30px; padding: 0; background: #e6e6e6; color: #111; }
.inner-window { background: #232428; border: 1px solid #e8232c; border-radius: 4px; }
.inner-header { background: #1a1b1e; padding: 4px 8px; border-bottom: 1px solid #000; }
.toast { background: #2e2f34; border: 1px solid #444; border-radius: 6px; padding: 8px 14px; margin: 14px; }
.toast-error { border-color: #e8232c; }
.alarm-row { padding: 6px 8px; border-bottom: 1px solid #2a2b2f; background: transparent; border-radius: 0; }
.alarm-row:hover { background: #2a2b30; }
.prio-high { color: #e8232c; }
.prio-medium { color: #f0a020; }
.prio-low { color: #4a9eff; }
.empty-state { color: #6c6e74; padding: 30px; }
.red-button, button.suggested-action { background: #e8232c; color: #fff; border: none; font-weight: 600; }
.red-button:hover { background: #f03a42; }
.badge { background: #e8232c; color: #fff; border-radius: 8px; padding: 0 5px; font-size: 10px; }
.status-badge { border-radius: 3px; padding: 1px 6px; font-size: 11px; }
.status-badge.ok { background: alpha(#3fbf6f, 0.18); color: #6fd896; }
.status-badge.bad { background: alpha(#e8232c, 0.2); color: #ff6b70; }
.status-badge.unknown { background: #2e2f34; color: #9a9ca3; }
.table-head { color: #8b8d93; font-weight: 600; padding: 6px 4px; border-bottom: 1px solid #333; }
.table-cell { padding: 6px 4px; border-bottom: 1px solid #2a2b2f; }
.system-tabs button { background: transparent; border: none; border-radius: 0; padding: 8px 12px; }
.system-tabs button:checked { box-shadow: inset 0 -2px #e8232c; color: #fff; }
.system-nav button { background: transparent; border: none; border-radius: 0; padding: 8px 14px; }
.system-nav button:checked { background: #e8232c; color: #fff; }
.method-card { background: #2a2b30; border: 1px solid #333; border-radius: 4px; padding: 12px; }
.method-card:checked { background: #34353b; border-color: #e8232c; }
.schedule-cell { min-width: 14px; min-height: 18px; padding: 0; border-radius: 0; border: 1px solid #1e1f22;
                 background: #2e2f34; }
.schedule-cell:checked { background: #2f7fd8; }
.lock-screen { background: alpha(#0c0d0f, 0.97); }
.door-bar { background: #1b1c1f; padding: 4px 10px; border-bottom: 1px solid #000; }
.page-title { font-size: 20px; font-weight: 700; }
.muted { color: #8b8d93; }
.eyebrow { color: #e8232c; font-size: 10px; letter-spacing: 1.5px; font-weight: 700; }
.section-card { background: #25262a; border: 1px solid #2f3034; border-radius: 4px; }
.section-heading { font-weight: 700; padding: 10px 12px; border-bottom: 1px solid #2f3034; }
.stat-card { background: #25262a; border: 1px solid #2f3034; border-radius: 4px; padding: 14px; }
.stat-label { color: #8b8d93; font-size: 10px; letter-spacing: 1px; }
.stat-number { font-size: 26px; font-weight: 700; }
.statusbar { background: #141517; border-top: 1px solid #000; padding: 2px 10px; color: #8b8d93; font-size: 11px; }
.status-alarm { color: #e8232c; font-weight: 700; }
entry, spinbutton, dropdown button, textview, textview text { background: #18191b; color: #d7d8db; border-color: #34353b; }
.archive-controls { background: #1b1c1f; border: 1px solid #2f3034; border-radius: 4px; padding: 6px; }
"""


def install_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    display = Gdk.Display.get_default()
    if display:
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def label(text, css=None, xalign=0.0, wrap=False):
    widget = Gtk.Label(label=text)
    widget.set_xalign(xalign)
    if wrap:
        widget.set_wrap(True)
    if css:
        for name in css.split():
            widget.add_css_class(name)
    return widget


def button(text, callback=None, css=None, tooltip=None, flat=False):
    widget = Gtk.Button(label=text)
    if css:
        for name in css.split():
            widget.add_css_class(name)
    if tooltip:
        widget.set_tooltip_text(tooltip)
    if flat:
        widget.set_has_frame(False)
    if callback:
        widget.connect("clicked", lambda *_: callback())
    return widget


def margins(widget, value):
    for side in ("top", "bottom", "start", "end"):
        getattr(widget, f"set_margin_{side}")(value)
    return widget


def clear(box):
    child = box.get_first_child()
    while child:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


def fit_contain(picture):
    """Gtk.Picture: вписать с сохранением пропорций (set_content_fit есть только с GTK 4.8)."""
    picture.set_can_shrink(True)
    if hasattr(picture, "set_content_fit") and hasattr(Gtk, "ContentFit"):
        try:
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            return
        except (AttributeError, TypeError):
            pass
    if hasattr(picture, "set_keep_aspect_ratio"):
        picture.set_keep_aspect_ratio(True)


def popover_menu(parent, items, x=None, y=None):
    """Контекстное меню: items = [(title, callback) | None (разделитель)]."""
    popover = Gtk.Popover()
    popover.set_parent(parent)
    if x is not None:
        popover.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
    for item in items:
        if item is None:
            box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            continue
        title, callback = item
        entry = Gtk.Button(label=title)
        entry.set_has_frame(False)
        entry.get_child().set_xalign(0) if hasattr(entry.get_child(), "set_xalign") else None
        entry.connect("clicked", lambda _b, cb=callback: (popover.popdown(), cb()))
        box.append(entry)
    popover.set_child(box)
    popover.connect("closed", lambda menu: GLib.idle_add(menu.unparent))
    popover.popup()
    return popover


def on_right_click(widget, callback):
    gesture = Gtk.GestureClick()
    gesture.set_button(3)
    gesture.connect("released", lambda _g, _n, x, y: callback(widget, x, y))
    widget.add_controller(gesture)
    return gesture


# ---------------------------------------------------------------- уведомления
class Toast(Gtk.Revealer):
    """Неблокирующее уведомление внизу рабочей области (вместо модальных окон)."""

    def __init__(self):
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.END)
        self.set_can_target(True)
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.add_css_class("toast")
        self.text = label("", wrap=True)
        self.text.set_max_width_chars(80)
        self.box.append(self.text)
        close = button("✕", self.dismiss, flat=True)
        self.box.append(close)
        self.set_child(self.box)
        self._source = 0

    def show_message(self, message, error=False, seconds=5):
        self.text.set_text(message)
        if error:
            self.box.add_css_class("toast-error")
        else:
            self.box.remove_css_class("toast-error")
        self.set_reveal_child(True)
        if self._source:
            GLib.source_remove(self._source)
        self._source = GLib.timeout_add_seconds(max(2, seconds + len(message) // 60), self._timeout)

    def _timeout(self):
        self._source = 0
        self.set_reveal_child(False)
        return False

    def dismiss(self):
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0
        self.set_reveal_child(False)


# ---------------------------------------------------------------- внутреннее окно
class InnerWindow(Gtk.Box):
    """Окно модуля поверх видеосетки (карта, поиск, статистика…), как в HikCentral."""

    def __init__(self, on_close):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("inner-window")
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.FILL)
        margins(self, 18)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.add_css_class("inner-header")
        self.title = label("", "section-title")
        self.title.set_hexpand(True)
        header.append(self.title)
        self.maximize = Gtk.ToggleButton(label="▢")
        self.maximize.set_has_frame(False)
        self.maximize.set_tooltip_text("Развернуть")
        self.maximize.connect("toggled", lambda b: margins(self, 0 if b.get_active() else 18))
        header.append(self.maximize)
        header.append(button("✕", on_close, flat=True, tooltip="Закрыть"))
        self.append(header)
        self.content = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.content.set_vexpand(True)
        self.content.set_hexpand(True)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.content)
        scroll.set_vexpand(True)
        self.append(scroll)


# ---------------------------------------------------------------- секция левой панели
def section(title, icon, content, expanded=False, actions=()):
    """Сворачиваемая секция «Карта», «Статистика», … с кнопками действий в заголовке."""
    expander = Gtk.Expander()
    head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    head.append(label(icon, "muted"))
    title_label = label(title, "section-title")
    title_label.set_hexpand(True)
    head.append(title_label)
    for text, tooltip, callback in actions:
        action = button(text, callback, flat=True, tooltip=tooltip)
        head.append(action)
    head.set_hexpand(True)
    expander.set_label_widget(head)
    expander.set_child(content)
    expander.set_expanded(expanded)
    return expander


# ---------------------------------------------------------------- таблица
class Table(Gtk.Box):
    """Простая таблица на Gtk.Grid с выбором строк флажками и щелчком по строке."""

    def __init__(self, columns, selectable=True, on_activate=None, on_context=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.columns = columns
        self.selectable = selectable
        self.on_activate = on_activate
        self.on_context = on_context
        self.grid = Gtk.Grid(column_spacing=0, row_spacing=0)
        self.grid.set_hexpand(True)
        self.append(self.grid)
        self.rows = []
        self.checks = []
        self.select_all = None
        self.set_rows([])

    def set_rows(self, rows, keys=None):
        clear(self.grid)
        self.rows = list(rows)
        self.keys = list(keys) if keys is not None else list(range(len(self.rows)))
        self.checks = []
        offset = 0
        if self.selectable:
            self.select_all = Gtk.CheckButton()
            self.select_all.add_css_class("table-head")
            self.select_all.connect("toggled", self._toggle_all)
            self.grid.attach(self.select_all, 0, 0, 1, 1)
            offset = 1
        for index, title in enumerate(self.columns):
            head = label(title, "table-head")
            head.set_hexpand(index == 0)
            self.grid.attach(head, index + offset, 0, 1, 1)
        if not self.rows:
            self.grid.attach(label("Нет данных", "empty-state", 0.5), 0, 1, len(self.columns) + offset, 1)
            return
        for row_index, row in enumerate(self.rows, start=1):
            if self.selectable:
                check = Gtk.CheckButton()
                check.add_css_class("table-cell")
                self.checks.append(check)
                self.grid.attach(check, 0, row_index, 1, 1)
            for col, value in enumerate(row):
                if isinstance(value, Gtk.Widget):
                    cell = value
                    cell.add_css_class("table-cell")
                else:
                    cell = label(str(value), "table-cell")
                    cell.set_ellipsize(3)  # Pango.EllipsizeMode.END
                    cell.set_tooltip_text(str(value))
                    key = self.keys[row_index - 1]
                    if self.on_activate:
                        click = Gtk.GestureClick()
                        click.connect("released", lambda _g, n, _x, _y, k=key: self.on_activate(k) if n == 2 else None)
                        cell.add_controller(click)
                    if self.on_context:
                        on_right_click(cell, lambda w, x, y, k=key: self.on_context(k, w, x, y))
                cell.set_hexpand(col == 0)
                self.grid.attach(cell, col + offset, row_index, 1, 1)

    def _toggle_all(self, check):
        for item in self.checks:
            item.set_active(check.get_active())

    def selected(self):
        return [self.keys[i] for i, check in enumerate(self.checks) if check.get_active()]


def status_badge(text, kind):
    badge = label(text, f"status-badge {kind}")
    badge.set_halign(Gtk.Align.START)
    return badge


# ---------------------------------------------------------------- редактор расписания 7×24
class ScheduleEditor(Gtk.Box):
    """Сетка 7 дней × 24 часа: щелчок/протяжка включает часы (как в «Recording Schedule»)."""

    def __init__(self, cells=None, days=("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.cells = [row[:] for row in (cells or [[True] * 24 for _ in range(7)])]
        tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tools.append(button("Весь день", lambda: self.fill(True)))
        tools.append(button("Очистить", lambda: self.fill(False)))
        tools.append(button("Будни 8–18", self.workdays))
        self.append(tools)
        grid = Gtk.Grid(column_spacing=0, row_spacing=0)
        grid.attach(label(""), 0, 0, 1, 1)
        for hour in range(0, 24, 2):
            grid.attach(label(f"{hour:02d}", "muted"), 1 + hour, 0, 2, 1)
        self.buttons = []
        for day, title in enumerate(days):
            grid.attach(label(title, "muted"), 0, day + 1, 1, 1)
            row = []
            for hour in range(24):
                cell = Gtk.ToggleButton()
                cell.add_css_class("schedule-cell")
                cell.set_active(self.cells[day][hour])
                cell.set_tooltip_text(f"{title} {hour:02d}:00–{hour + 1:02d}:00")
                cell.connect("toggled", self._toggled, day, hour)
                grid.attach(cell, hour + 1, day + 1, 1, 1)
                row.append(cell)
            self.buttons.append(row)
        self.append(grid)

    def _toggled(self, cell, day, hour):
        self.cells[day][hour] = cell.get_active()

    def fill(self, value):
        for day in range(7):
            for hour in range(24):
                self.buttons[day][hour].set_active(value)
                self.cells[day][hour] = value

    def workdays(self):
        for day in range(7):
            for hour in range(24):
                value = day < 5 and 8 <= hour < 18
                self.buttons[day][hour].set_active(value)
                self.cells[day][hour] = value


# ---------------------------------------------------------------- шкала времени
class Timeline(Gtk.DrawingArea):
    """Шкала с отрезками записей, курсором, перетаскиванием и масштабом колесом."""

    HEIGHT = 46

    def __init__(self, on_seek, on_view_changed=None, label_text="", on_marker=None):
        super().__init__()
        self.model = tl.TimelineModel()
        self.on_seek = on_seek
        self.on_marker = on_marker
        self.on_view_changed = on_view_changed
        self.label_text = label_text
        self.set_hexpand(True)
        self.set_content_height(self.HEIGHT)
        if HAS_CAIRO:
            self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._scroll)
        self.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        self.add_controller(motion)
        self._drag_center = None
        self._drag_x = 0

    def set_label(self, text):
        self.label_text = text
        self.queue_draw()

    def _motion(self, _controller, x, _y):
        moment = self.model.time_at(x, self.get_width())
        segment = self.model.segment_at(moment)
        tip = tl.format_position(moment)
        marker = self.model.marker_near(x, self.get_width())
        if marker is not None:
            event = (marker.payload or {}).get("event") or {} if isinstance(marker.payload, dict) else {}
            tip = (f"{tl.KIND_TITLES.get(marker.kind, 'Событие')} · {marker.moment:%H:%M:%S}\n"
                   f"{event.get('title', '')}  {event.get('device_name', '')}\nЩелчок — перейти и открыть карточку")
        elif segment:
            tip += (f"\n{tl.KIND_TITLES.get(segment.kind, 'Запись')}: "
                    f"{segment.start:%H:%M:%S}–{segment.end:%H:%M:%S}")
        self.set_tooltip_text(tip)

    def _drag_begin(self, _gesture, x, _y):
        self._drag_center = self.model.center
        self._drag_x = x

    def _drag_update(self, _gesture, dx, _dy):
        if self._drag_center is None:
            return
        width = max(1, self.get_width())
        self.model.center = self._drag_center - timedelta(seconds=dx / width * self.model.span)
        self.queue_draw()

    def _drag_end(self, _gesture, dx, _dy):
        if self._drag_center is None:
            return
        self._drag_center = None
        if abs(dx) < 4:
            marker = self.model.marker_near(self._drag_x + dx, self.get_width())
            if marker is not None and self.on_marker:
                self.on_marker(marker)
                return
            moment = self.model.time_at(self._drag_x + dx, self.get_width())
            if self.on_seek:
                self.on_seek(moment)
        elif self.on_view_changed:
            self.on_view_changed()

    def _scroll(self, _controller, _dx, dy):
        self.model.zoom(+1 if dy < 0 else -1, self.model.cursor)
        self.queue_draw()
        if self.on_view_changed:
            self.on_view_changed()
        return True

    def _draw(self, _area, cr, width, height):
        model = self.model
        cr.set_source_rgb(0.105, 0.11, 0.122)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        cr.select_font_face("Sans")
        cr.set_font_size(10)
        for moment, major in model.ticks():
            x = int(model.x_for(moment, width)) + 0.5
            cr.set_source_rgb(0.4, 0.41, 0.44) if major else cr.set_source_rgb(0.27, 0.28, 0.3)
            cr.move_to(x, 0)
            cr.line_to(x, 9 if major else 5)
            cr.set_line_width(1)
            cr.stroke()
            if major:
                text = model.label(moment)
                extents = cr.text_extents(text)
                cr.set_source_rgb(0.7, 0.71, 0.74)
                cr.move_to(x - extents.width / 2, 20)
                cr.show_text(text)
        bar_top, bar_height = 25, 13
        cr.set_source_rgb(0.15, 0.155, 0.17)
        cr.rectangle(0, bar_top, width, bar_height)
        cr.fill()
        visible = model.visible_segments()
        # Непрерывная запись снизу, записи по движению/тревоге/событию поверх неё.
        for segment in sorted((s for s in visible if not s.is_marker),
                              key=lambda s: 0 if s.kind in ("continuous", "local") else 1):
            x1 = model.x_for(segment.start, width)
            x2 = max(x1 + 1.5, model.x_for(segment.end, width))
            cr.set_source_rgb(*tl.SEGMENT_COLORS.get(segment.kind, tl.SEGMENT_COLORS["continuous"]))
            if segment.kind == "local":
                cr.rectangle(x1, bar_top + bar_height - 3, x2 - x1, 3)
            else:
                cr.rectangle(x1, bar_top, x2 - x1, bar_height)
            cr.fill()
        for segment in model.segments:  # маркеры событий: флажок над полосой
            if not segment.is_marker or (model.filter != "all" and segment.kind != model.filter):
                continue
            moment = segment.moment
            if not model.start <= moment <= model.end:
                continue
            x = int(model.x_for(moment, width)) + 0.5
            cr.set_source_rgb(*tl.SEGMENT_COLORS.get(segment.kind, tl.SEGMENT_COLORS["event"]))
            cr.set_line_width(1)
            cr.move_to(x, bar_top - 1)
            cr.line_to(x, bar_top + bar_height)
            cr.stroke()
            cr.move_to(x - 4, bar_top - 8)
            cr.line_to(x + 4, bar_top - 8)
            cr.line_to(x, bar_top - 2)
            cr.close_path()
            cr.fill()
        if self.label_text:
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(4, bar_top + 10)
            cr.show_text(self.label_text[:40])
        now = datetime.now().astimezone()
        if model.start < now < model.end:
            x = model.x_for(now, width)
            cr.set_source_rgba(1, 1, 1, 0.06)
            cr.rectangle(x, bar_top, width - x, bar_height)
            cr.fill()
        x = int(model.x_for(model.cursor, width)) + 0.5
        cr.set_source_rgb(*ACCENT)
        cr.set_line_width(2)
        cr.move_to(x, 0)
        cr.line_to(x, height)
        cr.stroke()
        cr.rectangle(x - 4, 0, 8, 6)
        cr.fill()
