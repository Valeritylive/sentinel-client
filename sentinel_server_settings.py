#!/usr/bin/env python3
"""Окно настроек Sentinel Server (GTK 4): состояние службы, логин и пароль администратора, сеть и TLS.

Запуск: ``sentinel-server-settings`` (или из меню «Настройки Sentinel Server»).
``--setup`` сразу открывает окно «Задайте пароль администратора» (так делает значок в трее,
когда сервер запущен впервые и администратора ещё нет).

Совместимо с GTK 4.6 (Ubuntu 22.04): без Gtk.AlertDialog/FileDialog.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

import sentinel_server_ctl as ctl  # noqa: E402
import sentinel_widgets as W  # noqa: E402

APP_ID = "io.sentinel.ServerSettings"
POLL_SECONDS = 3
ACCESS = (("127.0.0.1", "Только этот компьютер (127.0.0.1)"),
          ("0.0.0.0", "Все сетевые интерфейсы (0.0.0.0)"),
          ("", "Другой адрес…"))
STATE_BADGE = {"running": ("ok", "● Работает"), "warning": ("unknown", "● Нужна настройка"),
               "starting": ("unknown", "● Запуск"), "stopped": ("unknown", "● Остановлен"),
               "error": ("bad", "● Ошибка"), "missing": ("bad", "● Не установлен")}
EXTRA_CSS = b"""
.server-state { font-size: 16px; font-weight: 700; }
.state-running { color: #3fbf6f; }
.state-warning, .state-starting { color: #f0a020; }
.state-error, .state-missing { color: #e8232c; }
.state-stopped { color: #8b8d93; }
.form-label { color: #9a9ca3; }
.msg-ok { color: #6fd896; }
.msg-error { color: #ff6b70; }
.mono { font-family: monospace; }
.setup-banner { background: alpha(#f0a020, 0.14); border: 1px solid alpha(#f0a020, 0.5); border-radius: 4px;
                padding: 10px 12px; }
"""


def run_async(func, done):
    """func() в фоне (pkexec ждёт пароль), done(result, error) — в главном цикле GTK."""
    def worker():
        try:
            result, error = func(), None
        except Exception as exc:   # noqa: BLE001 — показываем пользователю любую ошибку
            result, error = None, exc
        GLib.idle_add(lambda: done(result, error) and False)
    threading.Thread(target=worker, daemon=True).start()


def card(title, subtitle=None):
    frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    frame.add_css_class("section-card")
    frame.append(W.label(title, "section-heading"))
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    for side in ("start", "end", "top", "bottom"):
        getattr(body, f"set_margin_{side}")(12)
    if subtitle:
        body.append(W.label(subtitle, "muted", wrap=True))
    frame.append(body)
    return frame, body


def form_grid():
    grid = Gtk.Grid(column_spacing=12, row_spacing=8)
    return grid


def form_row(grid, row, text, widget):
    grid.attach(W.label(text, "form-label"), 0, row, 1, 1)
    widget.set_hexpand(True)
    grid.attach(widget, 1, row, 1, 1)


def message_label():
    label = W.label("", wrap=True)
    label.set_selectable(True)
    label.set_visible(False)
    return label


def show_message(label, text, ok=True):
    label.remove_css_class("msg-ok")
    label.remove_css_class("msg-error")
    label.add_css_class("msg-ok" if ok else "msg-error")
    label.set_text(text)
    label.set_visible(bool(text))


def password_entry(placeholder=""):
    entry = Gtk.PasswordEntry()
    entry.set_show_peek_icon(True)
    entry.set_property("placeholder-text", placeholder)
    return entry


def check_new_password(username, password, repeat):
    username = (username or "").strip()
    if not username:
        return "Введите логин"
    if not all(c.isalnum() or c in "._-" for c in username) or len(username) > 32:
        return "Логин: латиница, цифры и ._- (до 32 символов)"
    if len(password) < 8:
        return "Пароль должен быть не короче 8 символов"
    if password != repeat:
        return "Пароли не совпадают"
    return ""


# ==================================================================== окно первого запуска
class SetupDialog(Gtk.Window):
    """«Задайте логин и пароль администратора» — сервер запущен, а пользователей ещё нет."""

    def __init__(self, parent, on_done=None):
        super().__init__(title="Первый запуск Sentinel Server", transient_for=parent, modal=True)
        self.on_done = on_done
        self.set_default_size(460, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{side}")(18)
        box.append(W.label("ПЕРВЫЙ ЗАПУСК", "eyebrow"))
        box.append(W.label("Задайте логин и пароль администратора", "page-title", wrap=True))
        box.append(W.label("С этим логином и паролем вы будете входить на сервер из клиента Sentinel "
                           "(«Подключение к серверу»). Пароль — не короче 8 символов; надёжнее, если в нём "
                           "есть буквы разного регистра, цифры и знаки.", "muted", wrap=True))
        grid = form_grid()
        self.username = Gtk.Entry(text="admin")
        self.password = password_entry("не короче 8 символов")
        self.repeat = password_entry("ещё раз")
        form_row(grid, 0, "Логин", self.username)
        form_row(grid, 1, "Пароль", self.password)
        form_row(grid, 2, "Повторите", self.repeat)
        box.append(grid)
        self.message = message_label()
        box.append(self.message)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        buttons.append(W.button("Позже", self.close))
        self.save = W.button("Сохранить", self._save, "red-button")
        buttons.append(self.save)
        box.append(buttons)
        self.repeat.connect("activate", lambda *_: self._save())
        self.set_child(box)

    def _save(self):
        username, password = self.username.get_text().strip(), self.password.get_text()
        error = check_new_password(username, password, self.repeat.get_text())
        if error:
            show_message(self.message, error, False)
            return
        self.save.set_sensitive(False)
        show_message(self.message, "Сохраняю… Система может спросить пароль администратора компьютера.")

        def done(_result, error):
            self.save.set_sensitive(True)
            if error:
                show_message(self.message, str(error), False)
                return
            if self.on_done:
                self.on_done(username)
            self.close()
        run_async(lambda: ctl.set_password(username, password), done)


# ==================================================================== главное окно
class SettingsWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Настройки Sentinel Server")
        self.set_default_size(720, 760)
        self.set_icon_name("io.sentinel.Server")
        self.status = None
        self.setup_shown = False
        self.want_setup = False
        self._polling = False

        header = Gtk.HeaderBar()
        header.pack_start(W.label("SENTINEL", "hcl-logo"))
        header.set_title_widget(W.label("Сервер — настройки", "section-title", xalign=0.5))
        self.badge = W.label("● …", "status-badge unknown")
        header.pack_end(self.badge)
        self.set_titlebar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("start", "end", "top", "bottom"):
            getattr(content, f"set_margin_{side}")(14)
        content.append(self._build_status())
        content.append(self._build_admin())
        content.append(self._build_network())
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(content)
        self.set_child(scroller)

        self.load_settings()
        self.refresh()
        GLib.timeout_add_seconds(POLL_SECONDS, self._tick)

    # ------------------------------------------------------------ карточки
    def _build_status(self):
        frame, body = card("Состояние сервера")
        self.state_label = W.label("Проверяю…", "server-state")
        body.append(self.state_label)
        self.detail_label = W.label("", "muted", wrap=True)
        self.detail_label.set_selectable(True)
        body.append(self.detail_label)
        self.setup_banner = Gtk.Box(spacing=10)
        self.setup_banner.add_css_class("setup-banner")
        self.setup_banner.append(W.label("Администратор ещё не создан — войти на сервер пока нельзя.", wrap=True))
        banner_button = W.button("Задать пароль", self.show_setup, "red-button")
        banner_button.set_hexpand(True)
        banner_button.set_halign(Gtk.Align.END)
        self.setup_banner.append(banner_button)
        self.setup_banner.set_visible(False)
        body.append(self.setup_banner)

        urls = Gtk.Box(spacing=8)
        urls.append(W.label("Адрес для клиента:", "form-label"))
        self.urls_label = W.label("", "mono section-title")
        self.urls_label.set_selectable(True)
        urls.append(self.urls_label)
        copy = W.button("Копировать", self.copy_url, tooltip="Скопировать адрес — вставьте его в клиенте "
                                                            "(меню пользователя › «Сервер Sentinel…»)")
        copy.set_hexpand(True)
        copy.set_halign(Gtk.Align.END)
        copy.set_valign(Gtk.Align.START)
        urls.append(copy)
        body.append(urls)

        actions = Gtk.Box(spacing=8)
        self.start_button = W.button("▶ Запустить", lambda: self.service("start"))
        self.stop_button = W.button("■ Остановить", lambda: self.service("stop"))
        self.restart_button = W.button("⟳ Перезапустить", lambda: self.service("restart"))
        for b in (self.start_button, self.stop_button, self.restart_button):
            actions.append(b)
        journal = W.button("Журнал…", self.show_journal)
        journal.set_hexpand(True)
        journal.set_halign(Gtk.Align.END)
        actions.append(journal)
        body.append(actions)

        autostart = Gtk.Box(spacing=8)
        self.autostart = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._autostart_handler = self.autostart.connect("state-set", self._on_autostart)
        autostart.append(self.autostart)
        autostart.append(W.label("Запускать сервер при включении компьютера"))
        body.append(autostart)
        self.service_message = message_label()
        body.append(self.service_message)
        return frame

    def _build_admin(self):
        frame, body = card("Администратор сервера",
                           "Логин и пароль для входа на сервер из клиента Sentinel. Если пользователя с таким "
                           "логином нет, он будет создан с ролью «Администратор»; если есть — пароль будет "
                           "заменён, а блокировка после неверных попыток снята.")
        grid = form_grid()
        self.username = Gtk.Entry(text="admin")
        self.password = password_entry("не короче 8 символов")
        self.repeat = password_entry("ещё раз")
        form_row(grid, 0, "Логин", self.username)
        form_row(grid, 1, "Новый пароль", self.password)
        form_row(grid, 2, "Повторите", self.repeat)
        body.append(grid)
        row = Gtk.Box(spacing=8)
        self.password_button = W.button("Сохранить пароль", self.save_password, "red-button")
        row.append(self.password_button)
        users = W.button("Показать пользователей", self.show_users, flat=True)
        row.append(users)
        body.append(row)
        self.admin_message = message_label()
        body.append(self.admin_message)
        self.repeat.connect("activate", lambda *_: self.save_password())
        return frame

    def _build_network(self):
        frame, body = card("Сеть и шифрование",
                           "«Только этот компьютер» — клиент на этом же ПК, шифрование не требуется. Для "
                           "подключения с других компьютеров выберите все интерфейсы и включите TLS.")
        grid = form_grid()
        self.access = Gtk.DropDown.new_from_strings([title for _host, title in ACCESS])
        self.access.connect("notify::selected", lambda *_: self._sync_access())
        self.custom_host = Gtk.Entry(placeholder_text="например 192.168.1.10")
        self.port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port.set_value(ctl.DEFAULT_PORT)
        self.cert = Gtk.Entry(placeholder_text="/etc/sentinel-server/cert.pem")
        self.key = Gtk.Entry(placeholder_text="/etc/sentinel-server/key.pem")
        form_row(grid, 0, "Доступ", self.access)
        form_row(grid, 1, "Адрес", self.custom_host)
        form_row(grid, 2, "Порт", self.port)
        form_row(grid, 3, "Сертификат TLS", self._with_browse(self.cert, "Сертификат TLS (PEM)"))
        form_row(grid, 4, "Ключ TLS", self._with_browse(self.key, "Ключ TLS (PEM)"))
        body.append(grid)
        self.custom_host_row = (grid.get_child_at(0, 1), self.custom_host)
        cert_row = Gtk.Box(spacing=8)
        cert_row.append(W.button("Создать самоподписанный сертификат", self.generate_cert))
        cert_row.append(W.button("Без TLS", lambda: (self.cert.set_text(""), self.key.set_text("")), flat=True))
        body.append(cert_row)
        self.fingerprint = W.label("", "muted mono", wrap=True)
        self.fingerprint.set_selectable(True)
        body.append(self.fingerprint)
        self.plain = Gtk.CheckButton(label="Разрешить HTTP без шифрования в сети (только доверенная сеть)")
        body.append(self.plain)
        save = Gtk.Box(spacing=8)
        self.save_button = W.button("Сохранить и перезапустить сервер", self.save_network, "red-button")
        save.append(self.save_button)
        save.append(W.button("Отменить изменения", self.load_settings, flat=True))
        body.append(save)
        self.network_message = message_label()
        body.append(self.network_message)
        return frame

    def _with_browse(self, entry, title):
        box = Gtk.Box(spacing=6)
        entry.set_hexpand(True)
        box.append(entry)
        box.append(W.button("Обзор…", lambda: self._browse(entry, title)))
        return box

    def _browse(self, entry, title):
        chooser = Gtk.FileChooserNative.new(title, self, Gtk.FileChooserAction.OPEN, "Выбрать", "Отмена")

        def response(dialog, code):
            if code == Gtk.ResponseType.ACCEPT and dialog.get_file():
                entry.set_text(dialog.get_file().get_path() or "")
            dialog.destroy()
        chooser.connect("response", response)
        chooser.show()
        self._chooser = chooser   # держим ссылку, пока диалог открыт

    def _sync_access(self):
        custom = ACCESS[self.access.get_selected()][0] == ""
        for widget in self.custom_host_row:
            widget.set_visible(custom)

    # ------------------------------------------------------------ данные
    def load_settings(self):
        settings = ctl.read_settings()
        hosts = [h for h, _t in ACCESS]
        if settings["host"] in hosts[:-1]:
            self.access.set_selected(hosts.index(settings["host"]))
            self.custom_host.set_text("")
        else:
            self.access.set_selected(len(ACCESS) - 1)
            self.custom_host.set_text(settings["host"])
        self._sync_access()
        self.port.set_value(settings["port"])
        self.cert.set_text(settings["tls_cert"])
        self.key.set_text(settings["tls_key"])
        self.plain.set_active(settings["allow_plain_http"])
        self.urls_label.set_text("\n".join(ctl.client_urls(settings)))
        fingerprint = ctl.certificate_fingerprint(settings["tls_cert"]) if settings["tls_cert"] else ""
        self.fingerprint.set_text(f"Отпечаток SHA-256: {fingerprint}" if fingerprint else "")
        show_message(self.network_message, "")

    def form_settings(self):
        host = ACCESS[self.access.get_selected()][0] or self.custom_host.get_text().strip()
        return {"host": host, "port": int(self.port.get_value()), "tls_cert": self.cert.get_text().strip(),
                "tls_key": self.key.get_text().strip(), "allow_plain_http": self.plain.get_active()}

    # ------------------------------------------------------------ состояние
    def _tick(self):
        self.refresh()
        return True

    def refresh(self):
        if self._polling:
            return
        self._polling = True

        def done(status, error):
            self._polling = False
            if status is not None:
                self.apply_status(status)
        run_async(ctl.get_status, done)

    def apply_status(self, status):
        self.status = status
        for state in STATE_BADGE:
            self.state_label.remove_css_class(f"state-{state}")
        self.state_label.add_css_class(f"state-{status.state}")
        self.state_label.set_text(status.text)
        self.detail_label.set_text(status.detail)
        self.detail_label.set_visible(bool(status.detail))
        css, text = STATE_BADGE.get(status.state, ("unknown", status.text))
        for name in ("ok", "bad", "unknown"):
            self.badge.remove_css_class(name)
        self.badge.add_css_class(css)
        self.badge.set_text(text)
        self.urls_label.set_text("\n".join(status.urls))
        installed = status.state != "missing"
        self.start_button.set_sensitive(installed and not status.active)
        self.stop_button.set_sensitive(installed and status.state != "stopped")
        self.restart_button.set_sensitive(installed)
        self.autostart.handler_block(self._autostart_handler)
        self.autostart.set_active(status.enabled)
        self.autostart.set_sensitive(installed)
        self.autostart.handler_unblock(self._autostart_handler)
        self.setup_banner.set_visible(bool(status.setup_required))
        if status.setup_required and (self.want_setup or not self.setup_shown):
            self.show_setup()

    # ------------------------------------------------------------ действия
    def copy_url(self):
        urls = self.status.urls if self.status and self.status.urls else ctl.client_urls(ctl.read_settings())
        display = Gdk.Display.get_default()
        if display and urls:
            display.get_clipboard().set(urls[0])
            show_message(self.service_message, f"Скопировано: {urls[0]} — вставьте в клиенте целиком, "
                                               "вместе с http:// или https://")

    def service(self, action):
        titles = {"start": "Запускаю сервер…", "stop": "Останавливаю сервер…", "restart": "Перезапускаю сервер…"}
        show_message(self.service_message, titles[action])

        def done(_result, error):
            show_message(self.service_message, str(error) if error else "", error is None)
            self.refresh()
        run_async(lambda: ctl.service(action), done)

    def _on_autostart(self, _switch, state):
        show_message(self.service_message, "Меняю автозапуск… Система может спросить пароль.")

        def done(_result, error):
            show_message(self.service_message, str(error) if error else
                         ("Сервер будет запускаться вместе с компьютером" if state else "Автозапуск выключен"),
                         error is None)
            self.refresh()
        run_async(lambda: ctl.set_autostart(state), done)
        return False

    def save_password(self):
        username, password = self.username.get_text().strip(), self.password.get_text()
        error = check_new_password(username, password, self.repeat.get_text())
        if error:
            show_message(self.admin_message, error, False)
            return
        self.password_button.set_sensitive(False)
        show_message(self.admin_message, "Сохраняю… Система может спросить пароль администратора компьютера.")

        def done(result, error):
            self.password_button.set_sensitive(True)
            if error:
                show_message(self.admin_message, str(error), False)
                return
            self.password.set_text("")
            self.repeat.set_text("")
            self.password_saved(username, result)
        run_async(lambda: ctl.set_password(username, password), done)

    def password_saved(self, username, result=""):
        url = (self.status.urls[0] if self.status and self.status.urls else "")
        show_message(self.admin_message, f"{result or 'Пароль сохранён'}. В клиенте: «Подключение к серверу», "
                                         f"адрес {url}, логин {username} и этот пароль.")
        self.refresh()

    def show_setup(self):
        self.setup_shown = True
        self.want_setup = False
        if getattr(self, "_setup", None) is not None and self._setup.get_visible():
            self._setup.present()
            return
        self._setup = SetupDialog(self, on_done=self.password_saved)
        self._setup.present()

    def show_users(self):
        show_message(self.admin_message, "Читаю пользователей… Система может спросить пароль.")

        def done(users, error):
            if error:
                show_message(self.admin_message, str(error), False)
                return
            if not users:
                show_message(self.admin_message, "Пользователей нет — задайте логин и пароль выше.", False)
                return
            lines = [f"{u['username']} — {u['role'] or 'без роли'}" + ("" if u["enabled"] else " (отключён)")
                     + (f", заблокирован до {u['locked_until'][11:16]}" if u.get("locked_until") else "")
                     for u in users]
            show_message(self.admin_message, "Пользователи сервера:\n" + "\n".join(lines))
        run_async(ctl.list_users, done)

    def save_network(self):
        settings = self.form_settings()
        try:
            ctl.validate_settings(settings)
        except ValueError as exc:
            show_message(self.network_message, str(exc), False)
            return
        self.save_button.set_sensitive(False)
        show_message(self.network_message, "Сохраняю и перезапускаю… Система может спросить пароль.")

        def done(_result, error):
            self.save_button.set_sensitive(True)
            if error:
                show_message(self.network_message, str(error), False)
                return
            self.load_settings()
            show_message(self.network_message, "Сохранено, сервер перезапущен. Адрес для клиента: "
                         + ", ".join(ctl.client_urls(ctl.read_settings())))
            self.refresh()
        run_async(lambda: ctl.save_settings(settings, restart=True), done)

    def generate_cert(self):
        show_message(self.network_message, "Создаю сертификат… Система может спросить пароль.")

        def done(result, error):
            if error:
                show_message(self.network_message, str(error), False)
                return
            self.cert.set_text(result.get("cert", ""))
            self.key.set_text(result.get("key", ""))
            if ACCESS[self.access.get_selected()][0] == "127.0.0.1":
                self.access.set_selected(1)
            self.fingerprint.set_text(f"Отпечаток SHA-256: {result.get('fingerprint', '')}")
            show_message(self.network_message,
                         "Сертификат создан. Нажмите «Сохранить и перезапустить сервер». В клиенте при первом "
                         "подключении укажите файл CA /etc/sentinel-server/cert.pem (скопируйте его на другой "
                         "компьютер) или снимите галочку проверки сертификата.")
        run_async(ctl.generate_certificate, done)

    def show_journal(self):
        window = Gtk.Window(title="Журнал Sentinel Server", transient_for=self)
        window.set_default_size(900, 520)
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(view)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroller.set_vexpand(True)
        box.append(scroller)

        def load():
            def done(text, error):
                view.get_buffer().set_text(str(error) if error else text)
                GLib.idle_add(lambda: scroller.get_vadjustment().set_value(
                    scroller.get_vadjustment().get_upper()) and False)
            run_async(ctl.journal, done)
        bar = Gtk.Box(spacing=8)
        for side in ("start", "end", "top", "bottom"):
            getattr(bar, f"set_margin_{side}")(6)
        bar.append(W.button("Обновить", load))
        box.append(bar)
        window.set_child(box)
        load()
        window.present()


class SettingsApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None
        self.connect("command-line", self._command_line)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", True)
        W.install_css()
        provider = Gtk.CssProvider()
        provider.load_from_data(EXTRA_CSS)
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

    def _command_line(self, _app, command_line):
        args = command_line.get_arguments()[1:]
        if self.window is None:
            self.window = SettingsWindow(self)
        if "--setup" in args:
            self.window.want_setup = True
            if self.window.status and self.window.status.setup_required:
                self.window.show_setup()
        self.window.present()
        return 0


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(__doc__)
        return 0
    return SettingsApp().run(argv)


if __name__ == "__main__":
    sys.exit(main())
