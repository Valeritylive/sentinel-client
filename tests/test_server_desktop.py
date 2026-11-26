"""Окно настроек и значок в трее Sentinel Server: логика без GTK и D-Bus."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sentinel_security as security  # noqa: E402
import sentinel_server as server_mod  # noqa: E402
import sentinel_server_ctl as ctl  # noqa: E402
import sentinel_server_helper as helper  # noqa: E402
import sentinel_storage as storage  # noqa: E402

DEFAULT = (ROOT / "packaging" / "sentinel-server.default").read_text(encoding="utf-8")


class ConfigTests(unittest.TestCase):
    def test_parse_default_file(self):
        values = ctl.parse_config(DEFAULT)
        self.assertEqual(values["SENTINEL_HOST"], "127.0.0.1")
        self.assertEqual(values["SENTINEL_PORT"], "8765")
        self.assertNotIn("SENTINEL_TLS_CERT", values)   # закомментирован

    def test_render_uncomments_and_keeps_comments(self):
        text = ctl.render_config(DEFAULT, {"SENTINEL_HOST": "0.0.0.0", "SENTINEL_PORT": "9000",
                                           "SENTINEL_TLS_CERT": "/etc/sentinel-server/cert.pem",
                                           "SENTINEL_TLS_KEY": "/etc/sentinel-server/key.pem",
                                           "SENTINEL_ALLOW_PLAIN_HTTP": None})
        values = ctl.parse_config(text)
        self.assertEqual(values["SENTINEL_HOST"], "0.0.0.0")
        self.assertEqual(values["SENTINEL_PORT"], "9000")
        self.assertEqual(values["SENTINEL_TLS_CERT"], "/etc/sentinel-server/cert.pem")
        self.assertNotIn("SENTINEL_ALLOW_PLAIN_HTTP", values)
        self.assertIn("# Адрес и порт", text)
        self.assertEqual(text.count("SENTINEL_TLS_CERT="), 1)

    def test_render_none_comments_out_and_appends_missing(self):
        text = ctl.render_config("SENTINEL_ALLOW_PLAIN_HTTP=1\nSENTINEL_HOST=a\nSENTINEL_HOST=b\n",
                                 {"SENTINEL_ALLOW_PLAIN_HTTP": None, "SENTINEL_HOST": "c", "SENTINEL_PORT": "1"})
        self.assertEqual(ctl.parse_config(text), {"SENTINEL_HOST": "c", "SENTINEL_PORT": "1"})

    def test_render_rejects_injection(self):
        with self.assertRaises(ValueError):
            ctl.render_config("", {"SENTINEL_HOST": "x\nSENTINEL_OPTS=--evil"})

    def test_validate(self):
        ok = ctl.validate_settings({"host": "127.0.0.1", "port": 8765})
        self.assertEqual(ok["SENTINEL_TLS_CERT"], None)
        with self.assertRaisesRegex(ValueError, "TLS"):
            ctl.validate_settings({"host": "0.0.0.0", "port": 8765})
        plain = ctl.validate_settings({"host": "0.0.0.0", "port": 8765, "allow_plain_http": True})
        self.assertEqual(plain["SENTINEL_ALLOW_PLAIN_HTTP"], "1")
        with self.assertRaises(ValueError):
            ctl.validate_settings({"host": "0.0.0.0", "port": 8765, "tls_cert": "/c.pem"})
        with self.assertRaises(ValueError):
            ctl.validate_settings({"host": "bad host!", "port": 8765})
        with self.assertRaises(ValueError):
            ctl.validate_settings({"host": "127.0.0.1", "port": 70000})

    def test_client_urls(self):
        self.assertEqual(ctl.client_urls({"host": "127.0.0.1", "port": 8765}), ["http://127.0.0.1:8765"])
        with patch.object(ctl, "local_addresses", return_value=["192.168.1.5"]):
            urls = ctl.client_urls({"host": "0.0.0.0", "port": 9000, "tls_cert": "/c"})
        self.assertEqual(urls, ["https://127.0.0.1:9000", "https://192.168.1.5:9000"])


class StatusTests(unittest.TestCase):
    settings = {"host": "127.0.0.1", "port": 8765, "tls_cert": ""}

    def status(self, props, health=None):
        with patch.object(ctl, "last_log_lines", return_value="boom"):
            return ctl.get_status(self.settings, props, health)

    def test_states(self):
        self.assertEqual(self.status({"LoadState": "not-found"}).state, "missing")
        self.assertEqual(self.status({"LoadState": "loaded", "ActiveState": "inactive"}).state, "stopped")
        failed = self.status({"LoadState": "loaded", "ActiveState": "failed", "Result": "exit-code"})
        self.assertEqual((failed.state, failed.detail), ("error", "boom"))
        crash = self.status({"LoadState": "loaded", "ActiveState": "activating", "SubState": "auto-restart"})
        self.assertEqual(crash.state, "error")
        props = {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled",
                 "ActiveEnterTimestampMonotonic": "1"}
        running = self.status(props, {"status": "ok", "version": "0.6.0", "setup_required": False})
        self.assertEqual((running.state, running.enabled, running.setup_required), ("running", True, False))
        setup = self.status(props, {"setup_required": True})
        self.assertEqual((setup.state, setup.setup_required), ("warning", True))
        self.assertTrue(setup.active)
        self.assertEqual(self.status(props, None).state, "error")   # давно запущен, но не отвечает

    def test_tray_menu_follows_state(self):
        tray = _tray_module()
        stopped = ctl.Status("stopped", "Сервер остановлен")
        items = dict(tray.menu_items(stopped))
        self.assertTrue(items[tray.M_START]["enabled"])
        self.assertFalse(items[tray.M_STOP]["enabled"])
        running = dict(tray.menu_items(ctl.Status("running", "Сервер работает")))
        self.assertFalse(running[tray.M_START]["enabled"])
        self.assertTrue(running[tray.M_STOP]["enabled"] and running[tray.M_RESTART]["enabled"])
        busy = dict(tray.menu_items(ctl.Status("running", "x"), busy="Перезапуск…"))
        self.assertEqual(busy[tray.M_STATUS]["label"], "Перезапуск…")
        self.assertFalse(busy[tray.M_EXIT]["enabled"])
        missing = dict(tray.menu_items(None))
        self.assertFalse(missing[tray.M_RESTART]["enabled"])


def _tray_module():
    try:
        import sentinel_server_tray as tray
    except ImportError as exc:   # нет PyGObject
        raise unittest.SkipTest(str(exc)) from exc
    return tray


class CommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data = Path(temp.name) / "s"
        for name, value in (("DATA", data), ("DB_PATH", data / "d.sqlite3")):
            patcher = patch.object(storage, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *argv, password=None):
        env = {"SENTINEL_ADMIN_PASSWORD": password} if password else {}
        out = io.StringIO()
        with patch.dict(os.environ, env), redirect_stdout(out):
            server_mod.main(list(argv))
        return out.getvalue()

    def test_reset_password_creates_then_resets_and_unlocks(self):
        self.assertIn("создан", self.run_cli("reset-password", password="First-pass1"))
        self.assertTrue(security.authenticate("admin", "First-pass1"))
        with storage.database() as db:
            db.execute("UPDATE users SET locked_until='2999-01-01T00:00:00+00:00', enabled=1")
        self.assertIn("изменён", self.run_cli("reset-password", password="Second-pass2"))
        self.assertTrue(security.authenticate("admin", "Second-pass2"))
        self.run_cli("reset-password", "--username", "valerii", password="Third-pass3")
        users = json.loads(self.run_cli("list-users", "--json"))["users"]
        self.assertEqual([(u["username"], u["role"]) for u in users],
                         [("admin", "Администратор"), ("valerii", "Администратор")])

    def test_reset_password_rejects_weak(self):
        with self.assertRaises(SystemExit):
            self.run_cli("reset-password", password="123")


class HelperTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.config = self.dir / "sentinel-server"
        self.config.write_text(DEFAULT, encoding="utf-8")
        for target, name, value in ((ctl, "CONFIG_PATH", self.config), (helper, "CERT_DIR", self.dir / "tls")):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_set_config_copies_tls_out_of_home(self):
        cert, key = self.dir / "c.pem", self.dir / "k.pem"
        cert.write_text("CERT")
        key.write_text("KEY")
        values = ctl.validate_settings({"host": "0.0.0.0", "port": 9443, "tls_cert": str(cert), "tls_key": str(key)})
        with patch.object(helper, "_group_id", return_value=-1), redirect_stdout(io.StringIO()):
            helper.cmd_set_config({"values": values, "restart": False})
        saved = ctl.read_settings(self.config)
        self.assertEqual((saved["host"], saved["port"]), ("0.0.0.0", 9443))
        self.assertEqual(saved["tls_cert"], str(self.dir / "tls" / "cert.pem"))
        self.assertEqual((self.dir / "tls" / "key.pem").read_text(), "KEY")
        self.assertEqual(oct((self.dir / "tls" / "key.pem").stat().st_mode & 0o777), "0o640")

    def test_set_config_revalidates(self):
        with self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
            helper.cmd_set_config({"values": {"SENTINEL_HOST": "0.0.0.0", "SENTINEL_PORT": "1"}})

    def test_set_password_passes_secret_via_env(self):
        calls = []

        class Done:
            returncode, stdout, stderr = 0, "Пароль пользователя admin изменён", ""

        def fake_run(args, **kwargs):
            calls.append((args, kwargs["env"].get("SENTINEL_ADMIN_PASSWORD")))
            return Done()
        with patch.object(helper.subprocess, "run", fake_run), redirect_stdout(io.StringIO()):
            helper.cmd_set_password({"username": "admin", "password": "S3cret-pass"})
        self.assertEqual(calls, [([helper.SERVER_BIN, "reset-password", "--username", "admin"], "S3cret-pass")])
        with self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
            helper.cmd_set_password({"username": "bad name;rm", "password": "x"})



class ClientAddressTests(unittest.TestCase):
    def test_scheme_defaults(self):
        import sentinel_remote as remote
        self.assertEqual(remote.normalize_server_url("127.0.0.1:8765"), "http://127.0.0.1:8765")
        self.assertEqual(remote.normalize_server_url("localhost"), "http://localhost:8765")
        self.assertEqual(remote.normalize_server_url("192.168.1.5:8765"), "https://192.168.1.5:8765")
        self.assertEqual(remote.normalize_server_url("https://127.0.0.1:9000"), "https://127.0.0.1:9000")

    def test_https_to_plain_server_gives_hint(self):
        import threading
        import sentinel_engine as engine_mod
        import sentinel_remote as remote
        engine = engine_mod.Engine(receive_events=False, recording_enabled=False)
        server = server_mod.make_server("127.0.0.1", 0, engine)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        with self.assertRaises(remote.RemoteError) as ctx:
            remote.ServerClient(f"https://127.0.0.1:{port}", verify_tls=False, timeout=5).health()
        self.assertTrue(ctx.exception.plain_http)
        self.assertIn("http://", str(ctx.exception))
        self.assertEqual(remote.ServerClient(f"127.0.0.1:{port}", timeout=5).health()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
