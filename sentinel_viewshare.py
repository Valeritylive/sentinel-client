"""Приватные и публичные виды на сервере Sentinel (GTK-часть клиента).

Без подключения к серверу виды хранятся только на этом компьютере, как раньше.
При подключении в окне «Сохранить вид» можно выбрать «Сервер · приватный»
(виден только этому пользователю с любого клиента) или «Сервер · публичный»
(виден всем клиентам сервера). Виды сервера показываются в разделе «Вид».
"""
from __future__ import annotations

import threading

from gi.repository import GLib, Gtk

import sentinel_shared_views as shared
import sentinel_widgets as W

TARGET_LOCAL, TARGET_PRIVATE, TARGET_PUBLIC = "local", "private", "public"
TARGET_TITLES = {TARGET_LOCAL: "Этот компьютер", TARGET_PRIVATE: "Сервер · приватный (только мне)",
                 TARGET_PUBLIC: "Сервер · публичный (всем клиентам)"}


class ViewShareMixin:
    def _server_link(self):
        link = self.__dict__.get("server_link")
        return link if link is not None and link.connected else None

    def _server_view_targets(self):
        if self._server_link() is None:
            return [TARGET_LOCAL]
        targets = [TARGET_LOCAL, TARGET_PRIVATE]
        if self.__dict__.get("server_can_publish", self.session.can(shared.PUBLIC_PERMISSION)):
            targets.append(TARGET_PUBLIC)
        return targets

    def _server_call(self, work, done, error_prefix="Сервер"):
        def worker():
            try:
                result = work()
            except Exception as exc:  # сеть, права, ошибки сервера
                message = f"{error_prefix}: {exc}"
                GLib.idle_add(lambda: (self.notify(message, error=True), False)[1])
                return
            GLib.idle_add(lambda: (done(result), False)[1])
        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ список
    def _refresh_server_views(self):
        link = self._server_link()
        if link is None:
            if self.__dict__.get("server_views"):
                self.server_views = []
                self._fill_custom_view_list()
            return False

        def done(payload):
            self.server_views = payload.get("items", [])
            self.server_can_publish = bool(payload.get("can_publish"))
            self._fill_custom_view_list()
        self._server_call(link.client.views, done, "Виды сервера")
        return False

    def _append_server_views(self, container):
        items = self.__dict__.get("server_views") or []
        if self._server_link() is None or not items:
            return
        for title, public in (("Сервер · публичные", True), ("Сервер · мои", False)):
            group = [v for v in items if v["public"] == public]
            if not group:
                continue
            expander = Gtk.Expander(label=f"☁ {title}  ({len(group)})")
            expander.set_expanded(True)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            inner.set_margin_start(12)
            expander.set_child(inner)
            container.append(expander)
            for view in group:
                row = W.button(("◉ " if public else "◎ ") + view["name"],
                               lambda v=view: self.open_server_view(v), "tree-row")
                row.set_tooltip_text(f"Владелец: {view['owner']} · изменён {view.get('updated_at', '')[:16]}")
                W.on_right_click(row, lambda w, x, y, v=view: self._server_view_menu(v, w, x, y))
                inner.append(row)

    def _server_view_menu(self, view, widget, x, y):
        items = [("Открыть", lambda: self.open_server_view(view))]
        if view.get("editable"):
            items.append(("Переименовать", lambda: self._ask_text("Название вида", view["name"],
                                                                  lambda n: self._update_server_view(view, name=n))))
            if view["public"]:
                items.append(("Сделать приватным", lambda: self._update_server_view(view, public=False)))
            elif TARGET_PUBLIC in self._server_view_targets():
                items.append(("Сделать публичным", lambda: self._update_server_view(view, public=True)))
            items += [None, ("Удалить вид с сервера", lambda: self._confirm(
                f"Удалить вид «{view['name']}» с сервера?", lambda: self.delete_server_view(view)))]
        W.popover_menu(widget, items, x, y)

    # ------------------------------------------------------------ открыть / сохранить
    def open_server_view(self, view):
        self._capture_current_tab()
        tab = next((t for t in self.view_tabs.tabs if t.server_view_id == view["id"]), None)
        if tab is None:
            values, missing = shared.view_values(view, self.devices)
            tab = self.view_tabs.new_tab(view["name"], **values)
            tab.server_view_id = view["id"]
            if missing:
                self.notify(f"Вид «{view['name']}»: камер нет в каталоге этого клиента: {missing}. "
                            "Загрузите устройства с сервера.")
        else:
            self.view_tabs.active = tab.key
        self._apply_tab(tab)

    def save_tab_to_server(self, tab, name, public, view_id=None):
        link = self._server_link()
        if link is None:
            self.notify("Нет подключения к серверу Sentinel", error=True)
            return
        layout = shared.tab_layout(tab, self.devices)

        def work():
            if view_id is None:
                return link.client.create_view(name, public, layout)
            link.client.update_view(view_id, name=name, layout=layout)
            return view_id

        def done(new_id):
            tab.server_view_id, tab.view_id, tab.name, tab.dirty = new_id, None, name, False
            self._render_view_tabs()
            self._refresh_server_views()
            self.notify(f"Вид «{name}» сохранён на сервере ({'публичный' if public else 'приватный'})")
        self._server_call(work, done, "Сохранение вида на сервере")

    def _update_server_view(self, view, **changes):
        link = self._server_link()
        if link is None:
            return
        self._server_call(lambda: link.client.update_view(view["id"], **changes),
                          lambda _r: self._refresh_server_views(), "Изменение вида")

    def delete_server_view(self, view):
        link = self._server_link()
        if link is None:
            return

        def done(_result):
            for tab in self.view_tabs.tabs:
                if tab.server_view_id == view["id"]:
                    tab.server_view_id, tab.dirty = None, True
            self._render_view_tabs()
            self._refresh_server_views()
        self._server_call(lambda: link.client.delete_view(view["id"]), done, "Удаление вида")
