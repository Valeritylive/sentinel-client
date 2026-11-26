"""GTK-часть облака и сервера: вход в Hik-Connect и подключение клиента к Sentinel Server.

* ``HikConnectPanel`` — способ добавления «Hik-Connect» в «Система › Управление устройствами ›
  Добавить устройство»: вход в аккаунт, список привязанных устройств с состоянием и адресами,
  импорт выбранных в каталог. Если клиент подключён к серверу, все операции идут через сервер.
* ``ServerLink`` / ``open_server_dialog`` — вход в Sentinel Server, загрузка его каталога в
  клиент и приём тревог сервера.

Сеть — в рабочих потоках, GTK — только через GLib.idle_add.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import urllib.parse

from gi.repository import GLib, Gtk

import sentinel_alarms as alarms
import sentinel_hikconnect as hikconnect
import sentinel_remote as remote
import sentinel_storage as storage
import sentinel_widgets as W

SERVER_SETTING = "remote_server"
SERVER_SECRET = "sentinel-server"
ADDRESS_MODES = (("auto", "Авто: локальный адрес, иначе внешний"), ("local", "Локальный IP (та же сеть)"),
                 ("wan", "Внешний адрес / HiDDNS (проброс портов)"))


def _form():
    grid = Gtk.Grid(column_spacing=12, row_spacing=8)
    W.margins(grid, 4)
    return grid


def _field(grid, row, title, widget):
    grid.attach(W.label(title, "muted"), 0, row, 1, 1)
    widget.set_hexpand(True)
    grid.attach(widget, 1, row, 1, 1)
    return widget


def _row(*items):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    for item in items:
        box.append(item)
    return box


def _spacer():
    box = Gtk.Box()
    box.set_hexpand(True)
    return box


def _start(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()


def _run(owner, work, done, error_prefix):
    """work() в потоке; done(result) или уведомление об ошибке — в главном потоке."""
    def worker():
        try:
            result = work()
        except (hikconnect.HikConnectError, remote.RemoteError, storage.CredentialStoreError,
                ValueError, OSError) as exc:
            GLib.idle_add(_failed, str(exc))
            return
        GLib.idle_add(_done, result)

    def _done(result):
        done(result)
        return False

    def _failed(message):
        owner.notify(f"{error_prefix}: {message}", error=True)
        done(None)
        return False

    _start(worker)


# ==================================================================== Hik-Connect
class HikConnectPanel(Gtk.Box):
    def __init__(self, owner):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.owner = owner
        self.accounts = []
        self.devices = []
        self.account_index = 0
        self.busy = False

        self.mode_label = W.label("", "muted", wrap=True)
        self.append(self.mode_label)

        self.append(W.label("2. Аккаунт Hik-Connect", "section-title"))
        self.account_holder = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.account_picker = None
        self.append(_row(self.account_holder, _spacer(),
                         W.button("⟳ Синхронизировать", self.sync),
                         W.button("Выйти из аккаунта", self.logout)))

        form = _form()
        self.login_entry = _field(form, 0, "Логин (email или телефон)", Gtk.Entry())
        self.password_entry = _field(form, 1, "Пароль", Gtk.PasswordEntry(show_peek_icon=True))
        self.region = _field(form, 2, "Сервер", Gtk.DropDown.new_from_strings([t for _k, t, _u in hikconnect.REGIONS]))
        self.custom_url = _field(form, 3, "Адрес сервера", Gtk.Entry(placeholder_text="https://api.hik-connect.com"))
        self.custom_url.set_sensitive(False)
        self.region.connect("notify::selected", lambda *_: self.custom_url.set_sensitive(
            self.region.get_selected() == len(hikconnect.REGIONS) - 1))
        self.remember = Gtk.CheckButton(label="Запомнить пароль (для автоматического продления сессии)")
        form.attach(self.remember, 1, 4, 1, 1)
        self.password_entry.connect("activate", lambda *_: self.login())
        self.append(form)
        self.append(_row(W.button("Войти в Hik-Connect", self.login, "red-button"), _spacer()))
        self.append(W.label("Пароль аккаунта отправляется в облако только как хеш (так же, как в приложении "
                            "Hik-Connect). Сессия хранится в системном хранилище секретов.", "muted", wrap=True))

        self.status = W.label("Войдите в аккаунт, чтобы увидеть привязанные устройства.", "muted", wrap=True)
        self.append(_row(W.label("3. Устройства аккаунта", "section-title"), _spacer(), self.status))
        self.table = W.Table(("Имя", "Серийный номер", "Модель", "Состояние", "Локальный IP", "Внешний адрес",
                              "Каналы", "В каталоге"))
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_min_content_height(180)
        scroll.set_child(self.table)
        self.append(scroll)

        self.append(W.label("4. Добавление в каталог", "section-title"))
        self.append(W.label("Видео через P2P-облако Hikvision передаётся по закрытому протоколу, поэтому Sentinel "
                            "подключается к устройству напрямую по ISAPI/RTSP: укажите логин и пароль самого "
                            "устройства (не аккаунта) и способ доступа.", "muted", wrap=True))
        form = _form()
        self.device_user = _field(form, 0, "Пользователь устройства", Gtk.Entry(text="admin"))
        self.device_password = _field(form, 1, "Пароль устройства", Gtk.PasswordEntry(show_peek_icon=True))
        self.address = _field(form, 2, "Адрес подключения", Gtk.DropDown.new_from_strings([t for _k, t in ADDRESS_MODES]))
        self.area = _field(form, 3, "Область", Gtk.Entry(text="Hik-Connect"))
        self.append(form)
        self.append(_row(_spacer(), W.button("Добавить выбранные", lambda: self.import_devices(False), "red-button"),
                         W.button("Добавить все в сети", lambda: self.import_devices(True))))
        self.refresh()

    # ------------------------------------------------------------ режим и данные
    @property
    def link(self):
        link = self.owner.__dict__.get("server_link")
        return link if link is not None and link.connected else None

    def _account(self):
        if not self.accounts:
            return None
        return self.accounts[min(self.account_index, len(self.accounts) - 1)]

    def refresh(self):
        link = self.link
        self.mode_label.set_text(
            f"Через сервер {link.client.base_url}: аккаунты и импорт хранятся на сервере." if link else
            "Локальный режим: аккаунт и устройства хранятся в этом клиенте.")

        def work():
            if link:
                accounts = link.client.hikconnect_accounts()
                devices = link.client.hikconnect_devices()
            else:
                accounts = hikconnect.load_accounts()
                devices = hikconnect.load_cloud_devices()
            return accounts, devices

        _run(self.owner, work, self._loaded, "Hik-Connect")

    def _loaded(self, result):
        if result is None:
            return
        self.accounts, all_devices = result
        self.all_devices = all_devices
        focus = self.__dict__.pop("_focus_account", None)
        if focus:
            self.account_index = next((i for i, a in enumerate(self.accounts) if a["account"] == focus),
                                      self.account_index)
        W.clear(self.account_holder)
        if self.accounts:
            names = [f"{a['account']}  ·  {a.get('devices', '')} устр." if a.get("devices") is not None
                     else a["account"] for a in self.accounts]
            picker = Gtk.DropDown.new_from_strings(names)
            self.account_index = min(self.account_index, len(self.accounts) - 1)
            picker.set_selected(self.account_index)
            picker.connect("notify::selected", lambda p, *_: self._select_account(p.get_selected()))
            self.account_picker = picker
            self.account_holder.append(W.label("Аккаунт:", "muted"))
            self.account_holder.append(picker)
        else:
            self.account_holder.append(W.label("Нет сохранённых аккаунтов", "muted"))
        self._show_devices()

    def _select_account(self, index):
        self.account_index = int(index)
        self._show_devices()

    def _show_devices(self):
        account = self._account()
        if account is None:
            self.devices = []
        else:
            self.devices = [d for d in getattr(self, "all_devices", [])
                            if d.get("account_id") == account["id"] or d.get("account") == account["account"]]
        rows = []
        for d in self.devices:
            state = W.status_badge("В сети", "ok") if d["online"] else W.status_badge("Не в сети", "bad")
            remote_addr = d.get("domain") or d.get("wan_ip") or "—"
            if remote_addr != "—" and d.get("wan_http_port"):
                remote_addr += f":{d['wan_http_port']}"
            cameras = d.get("cameras") or []
            visible = sum(1 for c in cameras if c.get("visible", True))
            rows.append((d["name"], d["serial"], d["model"] or "—", state, d.get("local_ip") or "—", remote_addr,
                         visible or 1, "✓" if d.get("imported") else ""))
        self.table.set_rows(rows, [d["serial"] for d in self.devices])
        if account is not None:
            when = (account.get("last_sync") or "")[:16].replace("T", " ")
            error = account.get("last_error")
            online = sum(1 for d in self.devices if d["online"])
            self.status.set_text(f"Ошибка: {error}" if error else
                                 f"Устройств: {len(self.devices)}, в сети: {online}. Обновлено: {when or '—'}")

    # ------------------------------------------------------------ действия
    def _region(self):
        index = self.region.get_selected()
        key, _title, url = hikconnect.REGIONS[min(index, len(hikconnect.REGIONS) - 1)]
        if key == "custom":
            return key, hikconnect.normalize_api_url(self.custom_url.get_text())
        return key, url

    def login(self):
        if self.busy:
            return
        account = self.login_entry.get_text().strip()
        password = self.password_entry.get_text()
        if not account or not password:
            self.owner.notify("Введите логин и пароль Hik-Connect", error=True)
            return
        try:
            region, url = self._region()
        except ValueError as exc:
            self.owner.notify(str(exc), error=True)
            return
        remember = self.remember.get_active()
        link = self.link
        self.busy = True
        self.status.set_text("Вход в Hik-Connect…")

        def work():
            if link:
                return link.client.hikconnect_login(account, password, region, url, remember)["account"]["id"]
            account_id, _devices = hikconnect.login_account(account, password, url, remember)
            hikconnect.apply_cloud_status()
            return account_id

        def done(account_id):
            self.busy = False
            if account_id is None:
                self.status.set_text("Вход не выполнен")
                return
            self.password_entry.set_text("")
            alarms.log_operation(self.owner.session.username, "Вход в Hik-Connect", account)
            self.owner.notify(f"Hik-Connect: вход выполнен ({account})")
            self._focus_account = account
            self.refresh()

        _run(self.owner, work, done, "Hik-Connect")

    def sync(self):
        account = self._account()
        if account is None or self.busy:
            self.owner.notify("Сначала войдите в аккаунт Hik-Connect")
            return
        password = self.password_entry.get_text() or None
        link = self.link
        self.busy = True
        self.status.set_text("Синхронизация…")

        def work():
            if link:
                return link.client.hikconnect_sync(account["id"], password)
            devices = hikconnect.sync_account(account["id"], password)
            hikconnect.apply_cloud_status()
            return devices

        def done(result):
            self.busy = False
            if result is not None:
                self.owner.notify(f"Hik-Connect: список устройств обновлён ({account['account']})")
                if not link:
                    self.owner.refresh()
            self.refresh()

        _run(self.owner, work, done, "Hik-Connect")

    def logout(self):
        account = self._account()
        if account is None:
            return
        link = self.link

        def confirmed():
            def work():
                if link:
                    link.client.hikconnect_remove(account["id"])
                else:
                    hikconnect.delete_account(account["id"])
                return True

            def done(result):
                if result:
                    self.owner.notify(f"Выход из Hik-Connect: {account['account']}. Добавленные в каталог "
                                      "устройства сохранены.")
                    self.account_index = 0
                self.refresh()

            _run(self.owner, work, done, "Hik-Connect")

        self.owner._confirm(f"Выйти из аккаунта {account['account']} и забыть его сессию?", confirmed, "Выйти")

    def import_devices(self, all_online):
        if self.busy:
            return
        if all_online:
            serials = [d["serial"] for d in self.devices if d["online"] and not d.get("imported")]
        else:
            serials = self.table.selected()
        if not serials:
            self.owner.notify("Отметьте устройства в списке" if not all_online else "Нет новых устройств в сети")
            return
        username = self.device_user.get_text().strip()
        password = self.device_password.get_text()
        credentials = {"username": username, "password": password, "rtsp_url": ""} if username else None
        address = ADDRESS_MODES[min(self.address.get_selected(), len(ADDRESS_MODES) - 1)][0]
        area = self.area.get_text().strip()
        account = self._account()
        link = self.link
        owner = self.owner
        self.busy = True
        self.status.set_text("Добавление…")

        def store(template, creds, channels):
            if creds:
                return owner._store_probed_device(template, creds, channels)
            return hikconnect.store_device(template, None, channels)

        def work():
            if link:
                result = link.client.hikconnect_import(serials, username, password, address, area,
                                                       account["id"] if account else None)
                pulled = link.pull_devices()
                return result["added"], result["errors"] + pulled[1]
            added, errors = hikconnect.import_devices(serials, credentials, address, area, store=store,
                                                      account_id=account["id"] if account else None)
            hikconnect.apply_cloud_status()
            return added, errors

        def done(result):
            self.busy = False
            if result is not None:
                added, errors = result
                owner.refresh()
                owner.sync_event_subscriptions()
                text = f"Добавлено из Hik-Connect: {added}"
                if errors:
                    text += ". " + "; ".join(errors[:3])
                owner.notify(text, error=bool(errors))
                alarms.log_operation(owner.session.username, "Импорт из Hik-Connect", ", ".join(serials),
                                     f"добавлено: {added}")
                if not credentials and added:
                    owner.notify("Пароль устройства не задан: он будет запрошен при первом открытии камеры")
            self.refresh()

        _run(owner, work, done, "Импорт Hik-Connect")


# ==================================================================== Sentinel Server
class ServerLink:
    """Подключение клиента к Sentinel Server."""

    def __init__(self, owner):
        self.owner = owner
        self.client = None
        self.stop = None
        self.online = False
        self.session = 0            # номер подключения: фоновое переключение прекращается при смене
        self._login = None          # (пользователь, пароль, TLS, CA) — только в памяти на время сеанса
        self._failing = False
        self._lock = threading.Lock()

    @property
    def connected(self):
        return self.client is not None and bool(self.client.token)

    def settings(self):
        return storage.get_json_setting(SERVER_SETTING, {"url": "", "backup_url": "", "username": "admin",
                                                         "verify_tls": True, "ca_file": "", "listen": True,
                                                         "auto": False})

    def candidates(self):
        """Адреса узлов кластера: основной, затем резервный (без повторов)."""
        settings = self.settings()
        urls = []
        for url in (settings.get("url"), settings.get("backup_url")):
            if url:
                url = remote.normalize_server_url(url)
                if url not in urls:
                    urls.append(url)
        return urls

    @staticmethod
    def find_active(urls, verify_tls=True, ca_file=""):
        """Первый узел, который отвечает и активен (резервный в ожидании отвечает active=false)."""
        errors = []
        for url in urls:
            probe = remote.ServerClient(url, verify_tls=verify_tls, ca_file=ca_file or None, timeout=6)
            try:
                health = probe.health()
            except remote.RemoteError as exc:
                errors.append(f"{probe.base_url}: {exc}")
                continue
            if health.get("active", True):
                return probe
            errors.append(f"{probe.base_url}: резервный узел в ожидании")
        raise remote.RemoteError("Нет активного сервера. " + "; ".join(errors))

    def connect(self, url, username, password, verify_tls=True, ca_file="", listen=True, remember=False,
                backup_url=None):
        """Вход (вызывать в рабочем потоке). Возвращает сводку сервера.

        С резервным адресом выбирается активный узел кластера, а при потере связи клиент сам
        переключается на другой узел."""
        primary = remote.normalize_server_url(url)
        if backup_url is None:
            backup_url = self.settings().get("backup_url") or ""
        backup = remote.normalize_server_url(backup_url) if backup_url.strip() else ""
        if backup and backup != primary:
            client = self.find_active([primary, backup], verify_tls, ca_file)
        else:
            client = remote.ServerClient(primary, verify_tls=verify_tls, ca_file=ca_file or None)
        client.login(username, password)
        summary = client.system()
        self.disconnect()
        with self._lock:
            self.session += 1
            self.client = client
            self._login = (username, password, bool(verify_tls), ca_file or "")
        storage.set_json_setting(SERVER_SETTING, {"url": primary, "backup_url": backup, "username": username,
                                                  "verify_tls": bool(verify_tls), "ca_file": ca_file or "",
                                                  "listen": bool(listen), "auto": bool(remember)})
        if remember:
            storage.save_json_secret(SERVER_SECRET, primary, {"username": username, "password": password})
        if listen:
            self.start_listening()
        GLib.idle_add(self._views_changed)
        return summary

    def _views_changed(self):
        refresh = getattr(self.owner, "_refresh_server_views", None)
        if refresh:
            refresh()
        return False

    def disconnect(self, forget=False):
        with self._lock:
            self.session += 1
            self._login = None
        if self.stop is not None:
            self.stop.set()
            self.stop = None
        if self.client is not None:
            client = self.client
            self.client = None
            _start(client.logout)
            if forget:
                settings = self.settings()
                for url in {settings.get("url") or client.base_url, client.base_url}:
                    try:
                        storage.delete_json_secret(SERVER_SECRET, remote.normalize_server_url(url))
                    except (storage.CredentialStoreError, ValueError):
                        pass
                settings["auto"] = False
                storage.set_json_setting(SERVER_SETTING, settings)
        self.online = False
        GLib.idle_add(self._views_changed)

    def start_listening(self):
        if self.stop is not None:
            self.stop.set()
        self.stop = threading.Event()
        _start(self.client.listen, self._message, self.stop, self._state)

    def _state(self, online, reason):
        self.online = online
        if online:
            return
        if reason:
            GLib.idle_add(self.owner.notify, f"Связь с сервером потеряна: {reason}", True)
        if self._login is not None and len(self.candidates()) > 1 and not self._failing:
            self._failing = True
            _start(self._failover_loop, self.session)

    def _failover_loop(self, session, attempts=None, delay=2.0):
        """Фоновый поиск активного узла, пока связь не вернётся или подключение не сменится."""
        try:
            tries = 0
            while session == self.session and not self.online:
                try:
                    if self.switch_to_active(session):
                        return
                except (remote.RemoteError, ValueError, OSError):
                    pass
                tries += 1
                if attempts is not None and tries >= attempts:
                    return
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        finally:
            self._failing = False

    def switch_to_active(self, session=None):
        """Найти активный узел и войти на него (вызывать в рабочем потоке). True — подключено."""
        login = self._login
        if login is None or (session is not None and session != self.session):
            return False
        username, password, verify_tls, ca_file = login
        probe = self.find_active(self.candidates(), verify_tls, ca_file)
        probe.login(username, password)
        with self._lock:
            if session is not None and session != self.session or self._login is None:
                _start(probe.logout)
                return False
            old, self.client = self.client, probe
        listening = self.stop is not None
        if listening:
            self.start_listening()
        if old is not None:
            _start(old.logout)
        if old is None or old.base_url != probe.base_url:
            alarms.log_operation(getattr(getattr(self.owner, "session", None), "username", ""),
                                 "Переключение на резервный сервер", probe.base_url)
            GLib.idle_add(self.owner.notify, f"Переключено на сервер {probe.base_url} (активный узел кластера)",
                          False)
        GLib.idle_add(self._views_changed)
        return True

    def _message(self, message):
        kind, data = message.get("type"), message.get("data") or {}
        if kind == "views":
            GLib.idle_add(self._views_changed)
            return
        text = None
        if kind == "alarm":
            if data.get("device_id") and hasattr(self.owner, "wall_show_alarm"):
                local = self._local_device_id(data)
                if local:
                    GLib.idle_add(lambda: (self.owner.wall_show_alarm(device_id=local, any_alarm=True), False)[1])
            text = (f"Сервер · тревога «{data.get('rule_name', '')}»: {data.get('device_name', '')} "
                    f"{alarms.event_title(data.get('event_type', ''))}")
        elif kind == "device_status" and data.get("status") == "offline":
            text = f"Сервер: устройство {data.get('host')} не в сети"
        elif kind == "notice":
            text = f"Сервер: {data.get('message', '')}"
        if text:
            GLib.idle_add(self.owner.notify, text, kind in ("alarm", "device_status") or bool(data.get("error")))

    def _local_device_id(self, data):
        """id камеры в каталоге клиента для устройства тревоги сервера (по адресу и каналу)."""
        host, channel = data.get("host"), str(data.get("channel") or "")
        for device in self.owner.devices:
            if host and device["host"] == host and str(device.get("channel")) == channel:
                return device["id"]
        return None

    def pull_devices(self):
        """Скопировать каталог сервера в клиент (вызывать в рабочем потоке). (добавлено, [ошибки])."""
        if not self.connected:
            raise remote.RemoteError("Нет подключения к серверу")
        items = self.client.devices()
        host = urllib.parse.urlparse(self.client.base_url).hostname
        groups = {}
        for item in items:
            groups.setdefault((item["host"], item["http_port"], item["sdk_port"]), []).append(item)
        known = {(d["host"], d["sdk_port"], d["channel"]) for d in storage.load_devices()}
        added, errors = 0, []
        for (address, _http, sdk), channels in groups.items():
            fresh = [c for c in channels if (address, sdk, c["channel"]) not in known]
            if not fresh:
                continue
            first = fresh[0]
            credentials = None
            try:
                url = urllib.parse.urlparse(self.client.stream_url(first["id"]))
                if url.username:
                    credentials = {"username": urllib.parse.unquote(url.username),
                                   "password": urllib.parse.unquote(url.password or ""), "rtsp_url": ""}
            except remote.RemoteError as exc:
                if exc.status not in (403, 409):
                    errors.append(f"{address}: {exc}")
            template = {"name": first.get("host_name") or address, "device_name": first.get("host_name") or "",
                        "host": address, "protocol": first.get("protocol") or "hikvision",
                        "sdk_port": sdk, "http_port": first["http_port"], "rtsp_port": first["rtsp_port"],
                        "stream": first.get("stream") or "main", "category": first.get("category") or "video",
                        "serial_no": first.get("serial_no") or "", "device_model": first.get("device_model") or "",
                        "firmware": first.get("firmware") or "", "status": first.get("status") or "",
                        "group_name": first.get("area") or f"Сервер {host}",
                        "channel_names": {c["channel"]: c["name"] for c in fresh},
                        "cloud_serial": first.get("cloud_serial") or ""}
            if template["protocol"] != "hikvision":
                credentials = None   # пользовательский RTSP-адрес сервер не раскрывает
            try:
                hikconnect.store_device(template, credentials, sorted(c["channel"] for c in fresh))
                added += 1
            except sqlite3.IntegrityError:
                errors.append(f"{address}: уже есть в каталоге")
            except storage.CredentialStoreError as exc:
                errors.append(f"{address}: пароль не сохранён ({exc})")
        return added, errors


def cluster_text(info):
    """Строка о кластере для статуса подключения ('' для одиночного сервера)."""
    if not info or info.get("role") in (None, "standalone"):
        return ""
    role = {"primary": "основной", "standby": "резервный"}.get(info.get("role"), info.get("role"))
    state = "активен" if info.get("active") else "в ожидании"
    text = f" · кластер: узел {info.get('node') or ''} ({role}, {state})"
    if info.get("promoted"):
        text += ", работает вместо основного"
    if info.get("peer_ok") is False:
        text += ", второй узел недоступен"
    return text


def server_link(owner):
    link = owner.__dict__.get("server_link")
    if link is None:
        link = ServerLink(owner)
        owner.server_link = link
    return link


def restore_server_connection(owner):
    """При запуске: автоподключение, если пользователь выбрал «Подключаться автоматически»."""
    link = server_link(owner)
    settings = link.settings()
    if not settings.get("auto") or not settings.get("url"):
        return

    def work():
        secret = storage.load_json_secret(SERVER_SECRET, remote.normalize_server_url(settings["url"]))
        if not secret:
            raise ValueError("сохранённый пароль не найден")
        return link.connect(settings["url"], secret["username"], secret["password"], settings.get("verify_tls", True),
                            settings.get("ca_file", ""), settings.get("listen", True), True)

    _run(owner, work, lambda summary: summary and owner.notify(
        f"Подключено к серверу Sentinel {settings['url']}"), "Сервер Sentinel")


def open_server_dialog(owner):
    link = server_link(owner)
    settings = link.settings()
    dialog = Gtk.Dialog(title="Сервер Sentinel", transient_for=owner, modal=True)
    dialog.set_default_size(560, -1)
    dialog.add_button("Закрыть", Gtk.ResponseType.CLOSE)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    W.margins(box, 16)
    box.append(W.label("Подключение к серверной части Sentinel (sentinel-server): общий каталог устройств, "
                       "пользователи, правила тревог и аккаунты Hik-Connect на сервере.", "muted", wrap=True))
    form = _form()
    url = _field(form, 0, "Адрес сервера", Gtk.Entry(text=settings.get("url") or "",
                                                       placeholder_text="https://server:8765"))
    backup = _field(form, 1, "Резервный сервер", Gtk.Entry(text=settings.get("backup_url") or "",
                                                            placeholder_text="необязательно: https://standby:8765"))
    user = _field(form, 2, "Пользователь", Gtk.Entry(text=settings.get("username") or "admin"))
    password = _field(form, 3, "Пароль", Gtk.PasswordEntry(show_peek_icon=True))
    ca_file = _field(form, 4, "Файл CA (необязательно)", Gtk.Entry(text=settings.get("ca_file") or "",
                                                                   placeholder_text="/путь/к/cert.pem"))
    verify = Gtk.CheckButton(label="Проверять сертификат TLS")
    verify.set_active(settings.get("verify_tls", True))
    listen = Gtk.CheckButton(label="Получать тревоги и события сервера")
    listen.set_active(settings.get("listen", True))
    auto = Gtk.CheckButton(label="Подключаться автоматически при запуске (пароль — в хранилище секретов)")
    auto.set_active(settings.get("auto", False))
    for row, widget in enumerate((verify, listen, auto), start=5):
        form.attach(widget, 1, row, 1, 1)
    box.append(form)
    status = W.label("Подключено: " + link.client.base_url if link.connected else "Не подключено", "muted", wrap=True)
    box.append(status)
    connect_button = W.button("Подключиться", None, "red-button")
    pull_button = W.button("Загрузить устройства сервера в клиент", None)
    disconnect_button = W.button("Отключиться", None)
    pull_button.set_sensitive(link.connected)
    disconnect_button.set_sensitive(link.connected)
    box.append(_row(connect_button, pull_button, _spacer(), disconnect_button))
    cluster_button = W.button("Кластер серверов…", lambda: (dialog.close(), owner.open_system("config", "cluster")),
                              tooltip="Горячий резерв: роль сервера, второй узел, переключения")
    box.append(_row(cluster_button))

    def connected(summary):
        connect_button.set_sensitive(True)
        if summary is None:
            status.set_text("Не подключено")
            return
        password.set_text("")
        status.set_text(f"Подключено: {link.client.base_url} · пользователь {link.client.user['username']} "
                        f"({link.client.user['role']}) · устройств {summary['devices']}, в сети {summary['online']}, "
                        f"неподтверждённых тревог {summary['unacknowledged_alarms']}"
                        + cluster_text(summary.get("cluster")))
        pull_button.set_sensitive(True)
        disconnect_button.set_sensitive(True)
        alarms.log_operation(owner.session.username, "Подключение к серверу", link.client.base_url)
        update_user_menu(owner)

    def do_connect():
        if not password.get_text():
            owner.notify("Введите пароль пользователя сервера", error=True)
            return
        connect_button.set_sensitive(False)
        status.set_text("Подключение…")
        args = (url.get_text(), user.get_text().strip(), password.get_text(), verify.get_active(),
                ca_file.get_text().strip(), listen.get_active(), auto.get_active(), backup.get_text().strip())
        _run(owner, lambda: link.connect(*args), connected, "Сервер Sentinel")

    def pulled(result):
        pull_button.set_sensitive(True)
        if result is None:
            return
        added, errors = result
        owner.refresh()
        owner.sync_event_subscriptions()
        owner.notify(f"С сервера добавлено устройств: {added}" + (f". {'; '.join(errors[:3])}" if errors else ""),
                     error=bool(errors))

    def do_pull():
        pull_button.set_sensitive(False)
        _run(owner, link.pull_devices, pulled, "Загрузка с сервера")

    def do_disconnect():
        link.disconnect(forget=True)
        status.set_text("Не подключено")
        pull_button.set_sensitive(False)
        disconnect_button.set_sensitive(False)
        update_user_menu(owner)

    connect_button.connect("clicked", lambda *_: do_connect())
    pull_button.connect("clicked", lambda *_: do_pull())
    disconnect_button.connect("clicked", lambda *_: do_disconnect())
    password.connect("activate", lambda *_: do_connect())
    dialog.get_content_area().append(box)
    dialog.connect("response", lambda d, _r: d.close())
    dialog.present()
    return dialog


def update_user_menu(owner):
    label = getattr(owner, "user_mode_label", None)
    if label is None:
        return
    link = owner.__dict__.get("server_link")
    label.set_text(f"Сервер: {link.client.base_url}" if link and link.connected else "Локальный клиент Sentinel")
