import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sentinel_engine as engine_mod
import sentinel_hikconnect as hc
import sentinel_remote as remote
import sentinel_security as security
import sentinel_server as server_mod
import sentinel_storage as storage
from fake_hikconnect import FakeCloud, FakeSecrets


class ServerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data = Path(temp.name) / "sentinel"
        for name, value in (("DATA", data), ("DB_PATH", data / "d.sqlite3")):
            patcher = patch.object(storage, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.secrets = FakeSecrets()
        patcher = patch.object(storage, "_secret_tool", self.secrets)
        patcher.start()
        self.addCleanup(patcher.stop)
        security.ensure_admin("Adm1n-pass!")
        roles = {r["name"]: r["id"] for r in security.load_roles()}
        security.create_user("viewer", "View3r-pass!", roles["Оператор"])

        cloud = self.cloud = FakeCloud()

        class Client(hc.HikConnectClient):
            def __init__(self, api_url=hc.DEFAULT_API, opener=None, timeout=15, feature_code=None):
                super().__init__(api_url, cloud, timeout, feature_code)

        self.engine = engine_mod.Engine(receive_events=False, recording_enabled=False, hikconnect_factory=Client)
        self.server = server_mod.make_server("127.0.0.1", 0, self.engine)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        self.admin = remote.ServerClient(self.url)
        self.admin.login("admin", "Adm1n-pass!")

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.engine.stop()

    def test_health_and_auth_required(self):
        anon = remote.ServerClient(self.url)
        self.assertEqual(anon.health()["status"], "ok")
        with self.assertRaises(remote.RemoteError) as ctx:
            anon.devices()
        self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(remote.RemoteError) as ctx:
            anon.login("admin", "wrong")
        self.assertEqual(ctx.exception.status, 401)

    def test_permissions_enforced(self):
        viewer = remote.ServerClient(self.url)
        viewer.login("viewer", "View3r-pass!")
        self.assertEqual(viewer.devices(), [])
        with self.assertRaises(remote.RemoteError) as ctx:
            viewer.hikconnect_accounts()
        self.assertEqual(ctx.exception.status, 403)

    def test_device_crud_and_stream_url_without_leaking_password(self):
        result = self.admin.request("POST", "/devices", {"name": "NVR", "host": "10.0.0.5", "channels": [1, 2],
                                                         "username": "admin", "password": "p@ss w",
                                                         "area": "Склад"})
        self.assertEqual(len(result["ids"]), 2)
        devices = self.admin.devices()
        self.assertEqual({d["area"] for d in devices}, {"Склад"})
        self.assertNotIn("p@ss", str(devices))
        url = self.admin.stream_url(result["ids"][1], "sub")
        self.assertEqual(url, "rtsp://admin:p%40ss%20w@10.0.0.5:554/Streaming/Channels/202")
        self.admin.request("PATCH", f"/devices/{result['ids'][0]}", {"name": "Вход"})
        self.assertIn("Вход", [d["name"] for d in self.admin.devices()])
        self.admin.request("DELETE", f"/devices/{result['ids'][0]}")
        self.admin.request("DELETE", f"/devices/{result['ids'][1]}")
        self.assertEqual(self.admin.devices(), [])
        self.assertNotIn(("10.0.0.5", 80), self.secrets.items)

    def test_hikconnect_login_list_import(self):
        result = self.admin.hikconnect_login("user@example.com", "secret")
        self.assertEqual(result["account"]["devices"], 2)
        self.assertEqual(result["account"]["api_url"], "https://apiieu.hik-connect.com")
        accounts = self.admin.hikconnect_accounts()
        self.assertEqual(len(accounts), 1)
        devices = self.admin.hikconnect_devices()
        self.assertEqual({d["serial"] for d in devices}, {"Q12345678", "K00000001"})
        imported = self.admin.hikconnect_import(["Q12345678"], "admin", "devpass")
        self.assertEqual(imported, {"added": 1, "errors": []})
        catalog = self.admin.devices()
        self.assertEqual({d["cloud_serial"] for d in catalog}, {"Q12345678"})
        self.assertEqual({d["area"] for d in catalog}, {"Hik-Connect"})
        sync = self.admin.hikconnect_sync(accounts[0]["id"])
        self.assertEqual(sync["accounts"], {str(accounts[0]["id"]): 2})
        with self.assertRaises(remote.RemoteError) as ctx:
            self.admin.hikconnect_login("user@example.com", "bad")
        self.assertEqual(ctx.exception.status, 401)
        self.admin.hikconnect_remove(accounts[0]["id"])
        self.assertEqual(self.admin.hikconnect_accounts(), [])

    def test_event_stream_and_alarm_ack(self):
        import sentinel_alarms as alarms
        alarms.save_rule({"name": "Любое", "event_type": "user", "sources": "all", "priority": "high",
                          "linkage": {"popup": True}})
        received, stop = [], threading.Event()
        listener = remote.ServerClient(self.url, token=self.admin.token)
        thread = threading.Thread(target=listener.listen, args=(received.append, stop), daemon=True)
        thread.start()
        time.sleep(0.3)
        created = self.admin.request("POST", "/events", {"text": "Проверка", "source": "Оператор"})
        self.assertEqual(len(created["alarms"]), 1)
        deadline = time.time() + 5
        while time.time() < deadline and not any(m["type"] == "alarm" for m in received):
            time.sleep(0.05)
        stop.set()
        self.assertIn("event", [m["type"] for m in received])
        self.assertIn("alarm", [m["type"] for m in received])
        listed = self.admin.alarms()
        self.assertEqual(listed["unacknowledged"], 1)
        self.admin.acknowledge(created["alarms"])
        self.assertEqual(self.admin.alarms()["unacknowledged"], 0)

    def test_tokens_revoked_on_logout_and_user_delete(self):
        viewer = remote.ServerClient(self.url)
        viewer.login("viewer", "View3r-pass!")
        users = self.admin.request("GET", "/users")["items"]
        self.assertTrue(all("password_hash" not in u for u in users))
        viewer_id = next(u["id"] for u in users if u["username"] == "viewer")
        self.admin.request("DELETE", f"/users/{viewer_id}")
        with self.assertRaises(remote.RemoteError) as ctx:
            viewer.devices()
        self.assertEqual(ctx.exception.status, 401)
        self.admin.logout()
        with self.assertRaises(remote.RemoteError):
            remote.ServerClient(self.url, token="nope").system()


class EngineUnitTests(unittest.TestCase):
    def test_bus_history_and_resume(self):
        bus = engine_mod.EventBus(history=3)
        for i in range(5):
            bus.publish("event", {"n": i})
        q = bus.subscribe(last_id=3)
        self.assertEqual([q.get_nowait()["data"]["n"] for _ in range(2)], [3, 4])

    def test_rtsp_url(self):
        device = {"protocol": "hikvision", "host": "h", "rtsp_port": 554, "channel": 3, "stream": "main"}
        self.assertTrue(engine_mod.rtsp_url(device, {"username": "u", "password": "p"}).endswith("/301"))
        with self.assertRaises(ValueError):
            engine_mod.rtsp_url(dict(device, protocol="rtsp"), {"username": "u", "rtsp_url": "rtsp://other/x"})

    def test_cli_default_command_is_run(self):
        args = server_mod.build_parser().parse_args(["run", "--port", "9000", "--data-dir", "/tmp/x"])
        self.assertEqual((args.command, args.port, args.data_dir), ("run", 9000, "/tmp/x"))

    def test_plain_http_refused_on_network_interface(self):
        args = server_mod.build_parser().parse_args(["run", "--host", "0.0.0.0"])
        with self.assertRaises(SystemExit):
            server_mod.cmd_run(args)


if __name__ == "__main__":
    unittest.main()
