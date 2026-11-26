"""Страница «Кластер серверов» в конфигурации системы: состояние пары, роль узла, переключения и мастер пары.

Всё выполняется через REST API сервера, к которому подключён клиент (право «Система»)."""
from __future__ import annotations

from gi.repository import Gtk

import sentinel_alarms as alarms
import sentinel_cloud_ui as cloud_ui
import sentinel_cluster as cluster
import sentinel_remote as remote
import sentinel_widgets as W

ROLES = (cluster.STANDALONE, cluster.PRIMARY, cluster.STANDBY)


def _grid():
    grid = Gtk.Grid(column_spacing=12, row_spacing=9)
    W.margins(grid, 4)
    return grid


def _field(grid, row, title, widget, hint=None):
    grid.attach(W.label(title, "muted"), 0, row, 1, 1)
    widget.set_hexpand(True)
    grid.attach(widget, 1, row, 1, 1)
    if hint:
        grid.attach(W.label(hint, "muted", wrap=True), 2, row, 1, 1)
    return widget


def _spin(low, high, value):
    spin = Gtk.SpinButton.new_with_range(low, high, 1)
    spin.set_value(value)
    return spin


def _row(*items):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    for item in items:
        box.append(item)
    return box


def status_lines(status):
    """Текст состояния узла для страницы (без GTK, проверяется тестами)."""
    if not status:
        return "Нет данных"
    role = status.get("role", cluster.STANDALONE)
    if role == cluster.STANDALONE:
        return "Сервер работает один, без резерва."
    lines = [f"Узел «{status.get('node', '')}»: {cluster.ROLE_TITLES.get(role, role).lower()}, "
             + ("активен" if status.get("active") else "в ожидании")
             + (" (работает вместо основного)" if status.get("promoted") else "")
             + (" (ждёт возврата роли)" if status.get("yielded") else "")]
    seen = status.get("peer_seen")
    peer = status.get("peer_url") or "—"
    if status.get("peer_error"):
        lines.append(f"Второй узел {peer}: {status['peer_error']}")
    elif seen is not None:
        lines.append(f"Второй узел {peer}: на связи (ответ {seen} с назад)")
    if status.get("synced_at"):
        lines.append(f"Последняя копия базы: {status['synced_at']}")
    if status.get("witness"):
        lines.append(f"Контрольный узел: {status['witness']}")
    lines.append(f"Переключение через {status.get('failover', 30)} с без ответа основного")
    return "\n".join(lines)


def build_page(window):
    """window — SystemWindow (owner — главное окно клиента)."""
    owner = window.owner
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    W.margins(box, 16)
    box.append(W.label("Кластер серверов: горячий резерв", "section-title"))
    box.append(W.label("Два сервера Sentinel работают парой. Резервный хранит копию базы и берёт работу на себя, "
                       "если основной перестал отвечать. Настройки применяются к серверу, к которому подключён "
                       "клиент, сразу и без перезапуска службы.", "muted", wrap=True))
    connection = W.label("", "muted", wrap=True)
    box.append(connection)
    status = W.label("", None, wrap=True)
    status.set_selectable(True)
    box.append(status)

    # ------------------------------------------------------------ настройки узла
    form = _grid()
    role = _field(form, 0, "Роль этого сервера", Gtk.DropDown.new_from_strings(
        [cluster.ROLE_TITLES[r] for r in ROLES]))
    node = _field(form, 1, "Имя узла", Gtk.Entry())
    peer = _field(form, 2, "Адрес второго узла", Gtk.Entry(placeholder_text="https://10.0.0.2:8765"),
                  "как его видит этот сервер")
    key = _field(form, 3, "Общий ключ", Gtk.PasswordEntry(show_peek_icon=True),
                 "одинаковый на обоих узлах")
    witness = _field(form, 4, "Контрольный узел", Gtk.Entry(placeholder_text="необязательно: 10.0.0.254:80"),
                     "резервный берёт роль, только если видит его")
    heartbeat = _field(form, 5, "Опрос второго узла, с", _spin(1, 60, 5))
    failover = _field(form, 6, "Переключение без ответа, с", _spin(5, 3600, 30))
    sync = _field(form, 7, "Копия базы не реже, с", _spin(5, 3600, 60))
    ca_file = _field(form, 8, "Сертификат CA второго узла", Gtk.Entry(placeholder_text="/путь/к/peer.pem"))
    verify = Gtk.CheckButton(label="Проверять сертификат TLS второго узла")
    verify.set_active(True)
    form.attach(verify, 1, 9, 1, 1)
    box.append(form)

    save_button = W.button("Сохранить и применить", None, "red-button")
    key_button = W.button("Новый ключ", None, tooltip="Создать новый общий ключ; его нужно задать и на втором узле")
    copy_button = W.button("Копировать ключ", None)
    box.append(_row(save_button, key_button, copy_button))

    # ------------------------------------------------------------ действия
    box.append(W.label("Переключения", "section-title"))
    promote_button = W.button("Взять роль на этот узел", None, tooltip="Только на резервном: ручное переключение")
    failback_button = W.button("Вернуть роль основному", None,
                               tooltip="Только на основном: забрать базу у резервного и снова стать активным")
    pair_button = W.button("Мастер пары серверов…", None, "red-button",
                           tooltip="Настроить сразу оба сервера и резервный адрес в клиенте")
    refresh_button = W.button("Обновить", None)
    box.append(_row(promote_button, failback_button, refresh_button))
    box.append(_row(pair_button))
    box.append(W.label("Журнал кластера", "section-title"))
    journal = W.label("", "muted", wrap=True)
    journal.set_selectable(True)
    box.append(journal)

    controls = (role, node, peer, key, witness, heartbeat, failover, sync, ca_file, verify, save_button, key_button,
                copy_button, promote_button, failback_button, pair_button, refresh_button)
    state = {"status": None}

    def link():
        current = cloud_ui.server_link(owner)
        return current if current.connected else None

    def role_changed(*_):
        clustered = ROLES[role.get_selected()] != cluster.STANDALONE
        for widget in (peer, key, witness, heartbeat, failover, sync, ca_file, verify, key_button, copy_button):
            widget.set_sensitive(clustered and link() is not None)

    def fill(config, current):
        role.set_selected(ROLES.index(config.get("role", cluster.STANDALONE)))
        node.set_text(config.get("node") or config.get("hostname") or "")
        peer.set_text(config.get("peer_url") or "")
        key.set_text(config.get("key") or "")
        witness.set_text(config.get("witness") or "")
        heartbeat.set_value(config.get("heartbeat", 5))
        failover.set_value(config.get("failover", 30))
        sync.set_value(config.get("sync", 60))
        ca_file.set_text(config.get("ca_file") or "")
        verify.set_active(bool(config.get("verify_tls", True)))
        show_status(current)

    def show_status(current):
        state["status"] = current
        status.set_text(status_lines(current))
        items = (current or {}).get("journal") or []
        journal.set_text("\n".join(f"{i['time']}  {i['text']}" for i in items[:15]) or "Записей нет")
        promote_button.set_sensitive(bool(current) and current.get("role") == cluster.STANDBY
                                     and not current.get("active"))
        failback_button.set_sensitive(bool(current) and current.get("role") == cluster.PRIMARY
                                      and not current.get("active"))
        role_changed()

    def refresh():
        current = link()
        for widget in controls:
            widget.set_sensitive(current is not None)
        if current is None:
            connection.set_text("Клиент не подключён к серверу Sentinel. Подключитесь: меню пользователя › "
                                "«Сервер Sentinel…».")
            status.set_text("")
            journal.set_text("")
            return
        connection.set_text(f"Сервер: {current.client.base_url}")

        def work():
            return (current.client.request("GET", "/cluster/config"),
                    current.client.request("GET", "/cluster/status"))

        cloud_ui._run(owner, work, lambda result: result and fill(*result), "Кластер")

    def collect():
        return {"role": ROLES[role.get_selected()], "node": node.get_text().strip(),
                "peer_url": peer.get_text().strip(), "key": key.get_text().strip(),
                "witness": witness.get_text().strip(), "heartbeat": heartbeat.get_value_as_int(),
                "failover": failover.get_value_as_int(), "sync": sync.get_value_as_int(),
                "ca_file": ca_file.get_text().strip(), "verify_tls": verify.get_active()}

    def save():
        current = link()
        if current is None:
            return
        config = collect()
        try:
            if config["role"] != cluster.STANDALONE:
                config["peer_url"] = remote.normalize_server_url(config["peer_url"])
            cluster.validate(dict(config, key=config["key"] or cluster.new_key()))
        except (ValueError, cluster.ClusterError) as exc:
            owner.notify(str(exc), error=True)
            return
        save_button.set_sensitive(False)

        def done(result):
            save_button.set_sensitive(True)
            if result is None:
                return
            alarms.log_operation(owner.session.username, "Кластер: роль сервера", config["role"])
            owner.notify(f"Роль сервера: {cluster.ROLE_TITLES[config['role']]}. Настройки применены")
            fill(result["config"], result["status"])

        cloud_ui._run(owner, lambda: current.client.request("PUT", "/cluster/config", config), done, "Кластер")

    def new_key():
        key.set_text(cluster.new_key())
        owner.notify("Создан новый ключ. Сохраните и задайте тот же ключ на втором узле")

    def copy_key():
        window.get_clipboard().set(key.get_text())
        owner.notify("Ключ скопирован в буфер обмена")

    def action(path, text):
        current = link()
        if current is None:
            return

        def done(result):
            if result is not None:
                alarms.log_operation(owner.session.username, text, current.client.base_url)
                owner.notify(text)
                refresh()

        cloud_ui._run(owner, lambda: current.client.request("POST", path, {}), done, "Кластер")

    role.connect("notify::selected", role_changed)
    save_button.connect("clicked", lambda *_: save())
    key_button.connect("clicked", lambda *_: new_key())
    copy_button.connect("clicked", lambda *_: copy_key())
    promote_button.connect("clicked", lambda *_: action("/cluster/promote", "Резервный узел принял работу"))
    failback_button.connect("clicked", lambda *_: action("/cluster/failback", "Роль возвращена основному узлу"))
    refresh_button.connect("clicked", lambda *_: refresh())
    pair_button.connect("clicked", lambda *_: open_pair_wizard(owner, refresh))
    box.connect("map", lambda *_: refresh())
    window.cluster_page = {"refresh": refresh, "save": save, "fill": fill, "collect": collect, "role": role,
                           "peer": peer, "key": key, "status": status, "journal": journal,
                           "promote": promote_button, "failback": failback_button, "spins": (heartbeat, failover, sync)}
    refresh()
    return box


# ================================================================ мастер пары
def configure_pair(link, standby_url, username, password, primary_peer, standby_peer, witness="",
                   failover=30, verify_tls=True, ca_file=""):
    """Настроить оба узла (вызывать в рабочем потоке). link подключён к будущему основному узлу.

    primary_peer — адрес основного, как его видит резервный; standby_peer — адрес резервного для основного."""
    primary = link.client
    standby = remote.ServerClient(standby_url, verify_tls=verify_tls, ca_file=ca_file or None)
    standby.login(username, password)
    key = cluster.new_key()
    common = {"key": key, "witness": witness, "failover": int(failover), "verify_tls": bool(verify_tls),
              "ca_file": ca_file}
    # основной первым: он остаётся активным, резервный сразу начнёт копировать его базу
    primary.request("PUT", "/cluster/config", dict(common, role=cluster.PRIMARY,
                                                   peer_url=remote.normalize_server_url(standby_peer)))
    standby.request("PUT", "/cluster/config", dict(common, role=cluster.STANDBY,
                                                   peer_url=remote.normalize_server_url(primary_peer)))
    standby.logout()
    settings = link.settings()
    settings["backup_url"] = standby.base_url
    cloud_ui.storage.set_json_setting(cloud_ui.SERVER_SETTING, settings)
    return {"primary": primary.base_url, "standby": standby.base_url}


def open_pair_wizard(owner, on_done=None):
    link = cloud_ui.server_link(owner)
    if not link.connected:
        owner.notify("Сначала подключитесь к серверу, который станет основным", error=True)
        return None
    settings = link.settings()
    dialog = Gtk.Dialog(title="Мастер пары серверов", transient_for=owner, modal=True)
    dialog.set_default_size(620, -1)
    dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
    dialog.add_button("Настроить пару", Gtk.ResponseType.ACCEPT).add_css_class("red-button")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    W.margins(box, 16)
    box.append(W.label(f"Основным станет сервер, к которому подключён клиент: {link.client.base_url}. "
                       "Резервный сервер получит копию его базы (пользователи, устройства, правила), "
                       "его прежние данные будут заменены.", "muted", wrap=True))
    form = _grid()
    standby_url = _field(form, 0, "Адрес резервного сервера", Gtk.Entry(
        text=settings.get("backup_url") or "", placeholder_text="https://10.0.0.2:8765"))
    username = _field(form, 1, "Администратор резервного", Gtk.Entry(text=link.client.user["username"]
                                                                  if link.client.user else "admin"))
    password = _field(form, 2, "Пароль", Gtk.PasswordEntry(show_peek_icon=True))
    primary_peer = _field(form, 3, "Адрес основного для резервного", Gtk.Entry(text=link.client.base_url))
    standby_peer = _field(form, 4, "Адрес резервного для основного", Gtk.Entry(
        placeholder_text="по умолчанию — адрес выше"))
    witness = _field(form, 5, "Контрольный узел", Gtk.Entry(placeholder_text="рекомендуется: шлюз, 10.0.0.254:80"))
    failover = _field(form, 6, "Переключение без ответа, с", _spin(5, 3600, 30))
    verify = Gtk.CheckButton(label="Проверять сертификаты TLS между узлами")
    verify.set_active(settings.get("verify_tls", True))
    form.attach(verify, 1, 7, 1, 1)
    box.append(form)
    box.append(W.label("Адреса для узлов указывайте такими, какими серверы видят друг друга (не 127.0.0.1, если "
                       "серверы на разных компьютерах).", "muted", wrap=True))
    dialog.get_content_area().append(box)

    def done(result):
        dialog.set_response_sensitive(Gtk.ResponseType.ACCEPT, True)
        if result is None:
            return
        alarms.log_operation(owner.session.username, "Кластер: настроена пара серверов",
                             f"{result['primary']} → {result['standby']}")
        owner.notify(f"Пара настроена: основной {result['primary']}, резервный {result['standby']}. "
                     "Резервный адрес сохранён в клиенте")
        dialog.close()
        if on_done:
            on_done()

    def response(_dialog, code):
        if code != Gtk.ResponseType.ACCEPT:
            dialog.close()
            return
        if not standby_url.get_text().strip() or not password.get_text():
            owner.notify("Укажите адрес резервного сервера и пароль администратора", error=True)
            return
        dialog.set_response_sensitive(Gtk.ResponseType.ACCEPT, False)
        args = (link, standby_url.get_text().strip(), username.get_text().strip(), password.get_text(),
                primary_peer.get_text().strip() or link.client.base_url,
                standby_peer.get_text().strip() or standby_url.get_text().strip(), witness.get_text().strip(),
                failover.get_value_as_int(), verify.get_active(), settings.get("ca_file", ""))
        cloud_ui._run(owner, lambda: configure_pair(*args), done, "Пара серверов")

    dialog.connect("response", response)
    dialog.present()
    owner.pair_wizard = {"dialog": dialog, "standby_url": standby_url, "password": password,
                         "response": response}
    return dialog
