import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sentinel_storage as storage


class CredentialStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_path = Path(self.temp_dir.name) / "sentinel"
        self.db_path = self.data_path / "devices.sqlite3"
        self.patch_data = patch.object(storage, "DATA", self.data_path)
        self.patch_db = patch.object(storage, "DB_PATH", self.db_path)
        self.patch_data.start()
        self.patch_db.start()
        self.addCleanup(self.patch_db.stop)
        self.addCleanup(self.patch_data.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.device = {"host": "nvr.example.test", "http_port": 8080}
        with storage.database() as db:
            db.execute(
                """INSERT INTO devices(name,host,http_port,channel)
                VALUES(?,?,?,?)""",
                ("Test NVR", self.device["host"], self.device["http_port"], 1),
            )

    def add_legacy_credentials(self):
        with storage.database() as db:
            db.execute(
                """INSERT INTO device_credentials(host,http_port,username,password,rtsp_url)
                VALUES(?,?,?,?,?)""",
                (self.device["host"], self.device["http_port"], "operator", "test-password", ""),
            )

    def legacy_count(self):
        with storage.database() as db:
            return db.execute("SELECT COUNT(*) FROM device_credentials").fetchone()[0]

    def test_saving_credentials_stores_secret_outside_sqlite(self):
        with patch.object(storage, "_secret_tool") as secret_tool:
            storage.save_credentials(
                self.device,
                {"username": "operator", "password": "test-password", "rtsp_url": ""},
            )

        action, _device, payload = secret_tool.call_args.args
        stored = json.loads(payload)
        self.assertEqual(action, "store")
        self.assertEqual(stored["username"], "operator")
        self.assertEqual(stored["password"], "test-password")
        self.assertEqual(self.legacy_count(), 0)

    def test_migration_removes_legacy_row_only_after_secure_store_succeeds(self):
        self.add_legacy_credentials()
        with patch.object(storage, "_store_secret") as store_secret:
            migrated = storage.migrate_legacy_credentials()

        self.assertEqual(migrated, 1)
        self.assertEqual(self.legacy_count(), 0)
        self.assertEqual(store_secret.call_count, 1)
        with storage.database() as db:
            self.assertEqual(db.execute("PRAGMA secure_delete").fetchone()[0], 1)

    def test_migration_preserves_legacy_row_when_secret_service_fails(self):
        self.add_legacy_credentials()
        with patch.object(storage, "_store_secret", side_effect=storage.CredentialStoreError("unavailable")):
            with self.assertRaises(storage.CredentialStoreError):
                storage.migrate_legacy_credentials()

        self.assertEqual(self.legacy_count(), 1)

    def test_partial_migration_keeps_all_sqlite_rows_for_retry(self):
        self.add_legacy_credentials()
        with storage.database() as db:
            db.execute(
                """INSERT INTO device_credentials(host,http_port,username,password,rtsp_url)
                VALUES(?,?,?,?,?)""",
                ("nvr2.example.test", 8081, "operator", "test-password-2", ""),
            )
        with patch.object(
            storage,
            "_store_secret",
            side_effect=[None, storage.CredentialStoreError("unavailable")],
        ):
            with self.assertRaises(storage.CredentialStoreError):
                storage.migrate_legacy_credentials()

        self.assertEqual(self.legacy_count(), 2)

    def test_loading_credentials_decodes_secret_service_payload(self):
        payload = json.dumps({
            "username": "operator",
            "password": "test-password",
            "rtsp_url": "rtsp://nvr.example.test/live",
        })
        with patch.object(storage, "_secret_tool", return_value=payload):
            credentials = storage.load_saved_credentials(self.device)

        self.assertEqual(credentials["username"], "operator")
        self.assertEqual(credentials["password"], "test-password")
        self.assertEqual(credentials["rtsp_url"], "rtsp://nvr.example.test/live")


if __name__ == "__main__":
    unittest.main()
