import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sentinel_hikconnect as hc
import sentinel_storage as storage
from fake_hikconnect import FakeCloud, FakeSecrets, make_jwt


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.cloud = FakeCloud()
        self.client = hc.HikConnectClient(opener=self.cloud)

    def test_login_follows_region_redirect_and_hashes_password(self):
        self.client.login("user@example.com", "secret")
        self.assertEqual(self.client.api_url, "https://apiieu.hik-connect.com")
        self.assertTrue(self.client.logged_in)
        first = self.cloud.requests[0]
        self.assertEqual(first[1], hc.DEFAULT_API)
        self.assertNotEqual(first[4]["password"], "secret")
        self.assertEqual(first[5]["clienttype"], "55")

    def test_wrong_password(self):
        with self.assertRaises(hc.WrongCredentials):
            self.client.login("user@example.com", "nope")

    def test_devices_paginated_with_cameras_and_ports(self):
        self.client.login("user@example.com", "secret")
        devices = self.client.devices()
        self.assertEqual([d["serial"] for d in devices], ["Q12345678", "K00000001"])
        nvr, term = devices
        self.assertTrue(nvr["online"])
        self.assertFalse(term["online"])
        self.assertEqual((nvr["local_ip"], nvr["wan_ip"], nvr["wan_rtsp_port"], nvr["wan_http_port"]),
                         ("192.168.1.50", "203.0.113.7", 10554, 8080))
        self.assertEqual(nvr["category"], "video")
        self.assertEqual(term["category"], "access")
        self.assertEqual([c["channel"] for c in nvr["cameras"]], [1, 2, 3])
        self.assertFalse(nvr["cameras"][1]["online"])
        self.assertFalse(nvr["cameras"][2]["visible"])

    def test_refresh_and_expiry(self):
        self.client.login("user@example.com", "secret")
        self.assertFalse(self.client.needs_refresh())
        self.client.session_id = make_jwt(time.time() + 60)
        self.assertTrue(self.client.ensure_session())
        self.assertEqual(self.client.session_id, self.cloud.session)

    def test_state_roundtrip(self):
        self.client.login("user@example.com", "secret")
        clone = hc.HikConnectClient.from_state(self.client.state(), opener=self.cloud)
        self.assertEqual(len(clone.devices(with_cameras=False)), 2)

    def test_expired_session_raises(self):
        self.client.login("user@example.com", "secret")
        self.client.session_id = "stale"
        with self.assertRaises(hc.SessionExpired):
            self.client.devices()

    def test_api_url_validation(self):
        self.assertEqual(hc.normalize_api_url("api.hik-connectru.com/"), "https://api.hik-connectru.com")
        with self.assertRaises(ValueError):
            hc.normalize_api_url("http://insecure.example")


class AccountFlowTests(unittest.TestCase):
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
        self.cloud = FakeCloud()

        cloud = self.cloud

        class Client(hc.HikConnectClient):
            def __init__(self, api_url=hc.DEFAULT_API, opener=None, timeout=15, feature_code=None):
                super().__init__(api_url, cloud, timeout, feature_code)

        self.factory = Client

    def test_login_stores_session_not_password_by_default(self):
        account_id, devices = hc.login_account("user@example.com", "secret", client_factory=self.factory)
        self.assertEqual(len(devices), 2)
        session = hc.load_session("user@example.com")
        self.assertIn("session_id", session)
        self.assertNotIn("password", session)
        rows = hc.load_cloud_devices(account_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(hc.get_account(account_id)["api_url"], "https://apiieu.hik-connect.com")

    def test_sync_relogins_with_remembered_password_after_expiry(self):
        account_id, _ = hc.login_account("user@example.com", "secret", remember_password=True,
                                         client_factory=self.factory)
        self.cloud.session = make_jwt(time.time() + 7200)   # сервер «сбросил» сессию
        devices = hc.sync_account(account_id, client_factory=self.factory)
        self.assertEqual(len(devices), 2)

    def test_sync_without_password_reports_expiry(self):
        account_id, _ = hc.login_account("user@example.com", "secret", client_factory=self.factory)
        state = hc.load_session("user@example.com")
        state["session_id"] = "expired"
        state["refresh_id"] = "bad"
        storage.save_json_secret(hc.SECRET_NAMESPACE, "user@example.com", state)
        with self.assertRaises(hc.SessionExpired):
            hc.sync_account(account_id, client_factory=self.factory)
        self.assertIn("истекла", hc.get_account(account_id)["last_error"])

    def test_import_local_and_wan(self):
        account_id, _ = hc.login_account("user@example.com", "secret", client_factory=self.factory)
        added, errors = hc.import_devices(["Q12345678"], {"username": "admin", "password": "x"})
        self.assertEqual((added, errors), (1, []))
        devices = storage.load_devices()
        self.assertEqual(sorted(d["channel"] for d in devices), [1, 2])     # скрытый канал не импортируется
        self.assertEqual({d["host"] for d in devices}, {"192.168.1.50"})
        self.assertEqual({d["name"] for d in devices}, {"Ворота", "Двор"})
        self.assertTrue(all(d["cloud_serial"] == "Q12345678" for d in devices))
        self.assertTrue(hc.load_cloud_devices(account_id)[0]["imported"] or
                        hc.load_cloud_devices(account_id)[1]["imported"])
        self.assertIn(("192.168.1.50", 80), self.secrets.items)
        again, errors = hc.import_devices(["Q12345678"], None)
        self.assertEqual(again, 0)
        self.assertIn("уже в каталоге", errors[0])

    def test_import_template_wan(self):
        cloud = hc.normalize_device(self.cloud.devices[0], self.cloud.connections["Q12345678"],
                                    {"globalStatus": 1})
        template, channels = hc.import_template(cloud, "wan")
        self.assertEqual((template["host"], template["rtsp_port"], template["http_port"], template["sdk_port"]),
                         ("203.0.113.7", 10554, 8080, 18000))
        self.assertEqual(channels, [1])

    def test_apply_status_and_delete_account(self):
        account_id, _ = hc.login_account("user@example.com", "secret", client_factory=self.factory)
        hc.import_devices(["Q12345678"], None)
        with storage.database() as db:
            db.execute("UPDATE cloud_devices SET online=0")
        self.assertEqual(hc.apply_cloud_status(), 2)
        self.assertEqual({d["status"] for d in storage.load_devices()}, {"offline"})
        hc.delete_account(account_id)
        self.assertEqual(hc.load_accounts(), [])
        self.assertIsNone(hc.load_session("user@example.com"))


    def test_unreachable_cloud_device_keeps_cloud_online_status(self):
        import sentinel_engine as engine_mod
        hc.login_account("user@example.com", "secret", client_factory=self.factory)
        hc.import_devices(["Q12345678"], None)
        hc.apply_cloud_status()
        engine = engine_mod.Engine(receive_events=False, recording_enabled=False, hikconnect_factory=self.factory)
        self.addCleanup(engine.stop)
        with patch.object(engine, "probe_status", return_value=("offline", {})):
            engine.check_devices()
        self.assertEqual({d["status"] for d in storage.load_devices()}, {"online"})
        with storage.database() as db:
            db.execute("UPDATE cloud_devices SET online=0")
        with patch.object(engine, "probe_status", return_value=("offline", {})):
            engine.check_devices()
        self.assertEqual({d["status"] for d in storage.load_devices()}, {"offline"})

    def test_cloud_session_for_imported_device(self):
        hc.login_account("user@example.com", "secret", client_factory=self.factory)
        state = hc.cloud_session("Q12345678", client_factory=self.factory)
        self.assertEqual(state["session_id"], hc.load_session("user@example.com")["session_id"])
        self.assertTrue(state["feature_code"])
        with self.assertRaises(hc.HikConnectError):
            hc.cloud_session("NOPE", client_factory=self.factory)

    def test_validate_code_roundtrip(self):
        self.assertEqual(hc.load_validate_code("Q12345678"), "")
        hc.save_validate_code("Q12345678", " ABCDEF ")
        self.assertEqual(hc.load_validate_code("Q12345678"), "ABCDEF")

class StreamHintTests(unittest.TestCase):
    def test_private_address_hint(self):
        device = {"cloud_serial": "FE0562391", "host_name": "Arami64", "host": "192.168.6.127", "rtsp_port": 554}
        hint = hc.stream_failure_hint(device, "gst-resource-error-quark: Не удалось открыть ресурс для чтения "
                                              "и записи. (7)")
        self.assertIn("Arami64", hint)
        self.assertIn("VPN", hint)
        self.assertIn("192.168.6.127:554", hint)

    def test_auth_hint_mentions_device_password(self):
        device = {"cloud_serial": "X", "name": "NVR", "host": "203.0.113.7"}
        self.assertIn("не пароль аккаунта", hc.stream_failure_hint(device, "Unauthorized (401)"))

    def test_public_address_and_non_cloud(self):
        device = {"cloud_serial": "X", "name": "NVR", "host": "203.0.113.7", "rtsp_port": 10554}
        self.assertIn("проброшен", hc.stream_failure_hint(device, "Could not connect to server"))
        self.assertEqual(hc.stream_failure_hint({"host": "192.168.1.2"}, "Could not connect"), "")
        self.assertEqual(hc.stream_failure_hint(device, "some decoder error"), "")

    def test_private_host_detection(self):
        self.assertTrue(hc.is_private_host("172.16.50.230"))
        self.assertTrue(hc.is_private_host("169.254.42.234"))
        self.assertFalse(hc.is_private_host("87.241.159.150"))
        self.assertFalse(hc.is_private_host("cam.example.com"))


class FileSecretBackendTests(unittest.TestCase):
    def test_file_backend_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(storage, "DATA", Path(temp)), \
                patch.dict("os.environ", {storage.CREDENTIAL_STORE_ENV: "file"}):
            storage.save_json_secret("x", "y", {"a": 1})
            self.assertEqual(storage.load_json_secret("x", "y"), {"a": 1})
            self.assertEqual((Path(temp) / "secrets.json").stat().st_mode & 0o777, 0o600)
            storage.delete_json_secret("x", "y")
            self.assertIsNone(storage.load_json_secret("x", "y"))


if __name__ == "__main__":
    unittest.main()
