import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import gtk_stub  # noqa: E402

if "app" not in sys.modules:
    gtk_stub.install()
import app as app_module  # noqa: E402
import sentinel_cloud_ui as cloud_ui  # noqa: E402
import sentinel_engine as engine_mod  # noqa: E402
import sentinel_hikconnect as hc  # noqa: E402
import sentinel_security as security  # noqa: E402
import sentinel_server as server_mod  # noqa: E402
import sentinel_storage as storage  # noqa: E402
from fake_hikconnect import FakeCloud, FakeSecrets  # noqa: E402


class CloudUiSmoke(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name) / "s"
        for name, value in (("DATA", data), ("DB_PATH", data / "d.sqlite3")):
            p = patch.object(storage, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.secrets = FakeSecrets()
        for target, attr, value in ((storage, "_secret_tool", self.secrets),
                                    (cloud_ui.GLib, "idle_add", lambda f, *a: f(*a)),
                                    (cloud_ui, "_start", lambda target, *args: target(*args))):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        cloud = self.cloud = FakeCloud()

        class Client(hc.HikConnectClient):
            def __init__(self, api_url=hc.DEFAULT_API, opener=None, timeout=15, feature_code=None):
                super().__init__(api_url, cloud, timeout, feature_code)

        p = patch.object(hc, "HikConnectClient", Client)
        p.start()
        self.addCleanup(p.stop)
        self.win = app_module.SentinelWindow(None)

    def _panel(self):
        self.win.open_hikconnect()
        system = self.win.system_window
        self.assertEqual(system.add_methods.get_visible_child_name(), "hikconnect")
        return system.hikconnect_panel

    def test_menu_opens_hikconnect_page(self):
        panel = self._panel()
        self.assertIn("Локальный режим", panel.mode_label.get_text())
        self.assertEqual(panel.accounts, [])

    def test_local_login_list_and_import(self):
        panel = self._panel()
        panel.login_entry.set_text("user@example.com")
        panel.password_entry.set_text("secret")
        with patch.object(panel.region, "get_selected", return_value=0):
            panel.login()
        self.assertEqual(len(panel.accounts), 1)
        self.assertEqual([d["serial"] for d in panel.devices], ["Q12345678", "K00000001"])
        self.assertIn("Устройств: 2, в сети: 1", panel.status.get_text())
        panel.device_user.set_text("admin")
        panel.device_password.set_text("devpass")
        with patch.object(panel.address, "get_selected", return_value=0):
            panel.import_devices(True)          # «все в сети» — только NVR
        catalog = storage.load_devices()
        self.assertEqual({d["cloud_serial"] for d in catalog}, {"Q12345678"})
        self.assertEqual(len(catalog), 2)
        self.assertTrue(all(self.win.session_credentials.get(d["id"]) for d in catalog))
        self.assertTrue(any(d.get("imported") for d in panel.devices))

    def test_server_link_and_dialog(self):
        security.ensure_admin("Adm1n-pass!")
        engine = engine_mod.Engine(receive_events=False, recording_enabled=False)
        server = server_mod.make_server("127.0.0.1", 0, engine)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        url = f"http://127.0.0.1:{server.server_address[1]}"
        hc.store_device({"name": "NVR", "host": "10.1.1.1", "sdk_port": 8000, "http_port": 80, "rtsp_port": 554,
                         "group_name": "Офис", "channel_names": {1: "Холл"}},
                        {"username": "admin", "password": "pw"}, [1, 2])
        link = cloud_ui.server_link(self.win)
        summary = link.connect(url, "admin", "Adm1n-pass!", listen=False)
        self.assertEqual(summary["devices"], 1)
        # каталог сервера и клиента в тесте — одна база; убираем локальную копию и подтягиваем с сервера
        server_rows = storage.load_devices()
        with storage.database() as db:
            db.execute("DELETE FROM devices")
        self.secrets.items.clear()
        with patch.object(link.client, "devices", return_value=[
                dict(engine_mod.public_device(d), area="Офис") for d in server_rows]), \
                patch.object(link.client, "stream_url",
                             return_value="rtsp://admin:p%40ss@10.1.1.1:554/Streaming/Channels/101"):
            added, errors = link.pull_devices()
        self.assertEqual((added, errors), (1, []))
        self.assertIn('"password": "p@ss"', self.secrets.items[("10.1.1.1", 80)])
        self.assertEqual({d["name"] for d in storage.load_devices()}, {"Холл", "NVR CH 2"})
        cloud_ui.open_server_dialog(self.win)
        panel = self._panel()
        self.assertIn("Через сервер", panel.mode_label.get_text())
        link.disconnect(forget=True)
        self.assertFalse(link.connected)

    def test_server_link_cluster_failover(self):
        import sentinel_cluster as cluster
        security.ensure_admin("Adm1n-pass!")
        nodes = {}
        for role in ("primary", "standby"):
            engines = []

            def new_engine(engines=engines):
                engines.append(engine_mod.Engine(receive_events=False, recording_enabled=False))
                return engines[-1]

            server = server_mod.make_server("127.0.0.1", 0, new_engine())
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

            def down(_method, _path):
                raise OSError("connection refused")

            url = f"http://127.0.0.1:{server.server_address[1]}"
            config = {"role": role, "peer_url": "http://127.0.0.1:1", "key": "k" * 64, "heartbeat": 5,
                      "failover": 30, "node": role}
            node = server_mod.attach_cluster(server, config, new_engine, transport=down, background=False)
            self.addCleanup(lambda s=server, e=engines: (s.shutdown(), s.server_close(), [x.stop() for x in e]))
            nodes[role] = (url, node)
        (primary_url, primary), (standby_url, standby) = nodes["primary"], nodes["standby"]
        self.assertTrue(primary.active and not standby.active)
        link = cloud_ui.server_link(self.win)
        # резервный указан первым — клиент сам выбирает активный узел
        summary = link.connect(standby_url, "admin", "Adm1n-pass!", listen=False, backup_url=primary_url)
        self.assertEqual(link.client.base_url, primary_url)
        self.assertIn("основной, активен", cloud_ui.cluster_text(summary["cluster"]))
        self.assertEqual(link.candidates(), [standby_url, primary_url])
        # основной узел пропал, резервный взял роль — клиент переключается
        primary._set_active(False, "тест")
        standby.promote()
        notices = []
        with patch.object(self.win, "notify", lambda text, *a: notices.append(text)):
            link._state(False, "HTTP 401")
        self.assertEqual(link.client.base_url, standby_url)
        self.assertEqual(link.client.devices(), [])
        self.assertTrue(any("Переключено на сервер" in n for n in notices))
        self.assertFalse(link._failing)
        link.disconnect(forget=True)
        self.assertIsNone(link._login)
        with self.assertRaises(cloud_ui.remote.RemoteError):
            cloud_ui.ServerLink.find_active([standby_url.replace(standby_url.rsplit(":", 1)[1], "1")])

    def _standalone_server(self):
        engines = []

        def new_engine():
            engines.append(engine_mod.Engine(receive_events=False, recording_enabled=False))
            return engines[-1]

        server = server_mod.make_server("127.0.0.1", 0, new_engine())
        server.new_engine = new_engine
        server.engine.start()
        server.cluster_transport = lambda _m, _p: (_ for _ in ()).throw(OSError("нет связи"))
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), [e.stop() for e in engines]))
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def test_cluster_page_and_pair_wizard(self):
        import sentinel_cluster_ui as cluster_ui
        security.ensure_admin("Adm1n-pass!")
        self.assertIn("без резерва", cluster_ui.status_lines({"role": "standalone"}))
        text = cluster_ui.status_lines({"role": "standby", "node": "b", "active": True, "promoted": True,
                                        "peer_url": "https://a:8765", "peer_error": "нет ответа", "failover": 30})
        self.assertIn("работает вместо основного", text)
        self.assertIn("нет ответа", text)
        primary_server, primary_url = self._standalone_server()
        standby_server, standby_url = self._standalone_server()
        # страница без подключения подсказывает, как подключиться
        self.win.open_system("config", "cluster")
        page = self.win.system_window.cluster_page
        page["refresh"]()
        link = cloud_ui.server_link(self.win)
        link.connect(primary_url, "admin", "Adm1n-pass!", listen=False)
        page["refresh"]()
        self.assertIn("один, без резерва", page["status"].get_text())
        # мастер пары: оба сервера настроены, резервный адрес сохранён в клиенте
        self.assertIsNotNone(cluster_ui.open_pair_wizard(self.win))
        result = cluster_ui.configure_pair(link, standby_url, "admin", "Adm1n-pass!", primary_url, standby_url)
        self.assertEqual(result, {"primary": primary_url, "standby": standby_url})
        self.assertEqual(primary_server.cluster.role, "primary")
        self.assertTrue(primary_server.cluster.active)
        self.assertEqual(standby_server.cluster.role, "standby")
        self.assertFalse(standby_server.cluster.active)
        self.assertEqual(primary_server.cluster.config["key"], standby_server.cluster.config["key"])
        self.assertEqual(link.settings()["backup_url"], standby_url)
        # страница показывает роль и сохраняет изменения через API
        page["refresh"]()
        self.assertIn("основной, активен", page["status"].get_text())
        with patch.object(page["role"], "get_selected", return_value=1):
            config = page["collect"]()
        self.assertEqual((config["role"], config["peer_url"]), ("primary", standby_url))
        notices = []
        heartbeat, failover, sync = page["spins"]
        with patch.object(page["role"], "get_selected", return_value=0), \
                patch.object(heartbeat, "get_value_as_int", return_value=5), \
                patch.object(failover, "get_value_as_int", return_value=30), \
                patch.object(sync, "get_value_as_int", return_value=60), \
                patch.object(self.win, "notify", lambda text, *a, **k: notices.append(text)):
            page["save"]()
        self.assertIn("Одиночный сервер", " ".join(notices))
        self.assertIsNone(primary_server.cluster)


if __name__ == "__main__":
    unittest.main()
