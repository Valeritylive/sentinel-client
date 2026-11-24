import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sentinel_storage as storage


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_path = Path(self.temp_dir.name) / "sentinel"
        self.db_path = self.data_path / "devices.sqlite3"
        for target, value in (("DATA", self.data_path), ("DB_PATH", self.db_path)):
            patcher = patch.object(storage, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.temp_dir.cleanup)


class CredentialStorageTests(_TempDb):
    def setUp(self):
        super().setUp()
        self.device = {"host": "nvr.example.test", "http_port": 8080}
        with storage.database() as db:
            db.execute("INSERT INTO devices(name,host,http_port,channel) VALUES(?,?,?,?)",
                       ("Test NVR", self.device["host"], self.device["http_port"], 1))

    def add_legacy_credentials(self):
        with storage.database() as db:
            db.execute("""INSERT INTO device_credentials(host,http_port,username,password,rtsp_url)
                VALUES(?,?,?,?,?)""",
                       (self.device["host"], self.device["http_port"], "operator", "test-password", ""))

    def legacy_count(self):
        with storage.database() as db:
            return db.execute("SELECT COUNT(*) FROM device_credentials").fetchone()[0]

    def test_saving_credentials_stores_secret_outside_sqlite(self):
        with patch.object(storage, "_secret_tool") as secret_tool:
            storage.save_credentials(self.device,
                                     {"username": "operator", "password": "test-password", "rtsp_url": ""})
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
            db.execute("""INSERT INTO device_credentials(host,http_port,username,password,rtsp_url)
                VALUES(?,?,?,?,?)""", ("nvr2.example.test", 8081, "operator", "test-password-2", ""))
        with patch.object(storage, "_store_secret",
                          side_effect=[None, storage.CredentialStoreError("unavailable")]):
            with self.assertRaises(storage.CredentialStoreError):
                storage.migrate_legacy_credentials()
        self.assertEqual(self.legacy_count(), 2)

    def test_loading_credentials_decodes_secret_service_payload(self):
        payload = json.dumps({"username": "operator", "password": "test-password",
                              "rtsp_url": "rtsp://nvr.example.test/live"})
        with patch.object(storage, "_secret_tool", return_value=payload):
            credentials = storage.load_saved_credentials(self.device)
        self.assertEqual(credentials["username"], "operator")
        self.assertEqual(credentials["password"], "test-password")
        self.assertEqual(credentials["rtsp_url"], "rtsp://nvr.example.test/live")


class ConnectionLifecycleTests(_TempDb):
    def test_connection_is_closed_after_with_block(self):
        with storage.database() as db:
            db.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")

    def test_connection_is_closed_and_rolled_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with storage.database() as db:
                db.execute("INSERT INTO app_settings(key,value) VALUES('k','v')")
                raise RuntimeError("boom")
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")
        with storage.database() as check:
            self.assertEqual(check.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0], 0)

    def test_schema_is_created_once_per_database_file(self):
        with patch.object(storage, "_create_schema", wraps=storage._create_schema) as create:
            for _ in range(5):
                with storage.database():
                    pass
        self.assertEqual(create.call_count, 1)

    def test_schema_is_recreated_if_database_file_disappears(self):
        with storage.database():
            pass
        self.db_path.unlink()
        with storage.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM devices").fetchone()[0], 0)

    def test_database_file_and_directory_are_private(self):
        with storage.database():
            pass
        self.assertEqual(self.data_path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.db_path.stat().st_mode & 0o777, 0o600)

    def test_legacy_devices_unique_index_is_migrated(self):
        self.data_path.mkdir(parents=True)
        legacy = sqlite3.connect(self.db_path)
        legacy.execute("""CREATE TABLE devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, host TEXT NOT NULL,
            sdk_port INTEGER NOT NULL DEFAULT 8000, rtsp_port INTEGER NOT NULL DEFAULT 554,
            channel INTEGER NOT NULL DEFAULT 1, stream TEXT NOT NULL DEFAULT 'main',
            protocol TEXT NOT NULL DEFAULT 'hikvision',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(host, sdk_port))""")
        legacy.execute("INSERT INTO devices(name,host,channel) VALUES('cam','h',1)")
        legacy.commit()
        legacy.close()
        devices = storage.load_devices()
        self.assertEqual([d["name"] for d in devices], ["cam"])
        self.assertEqual(devices[0]["http_port"], 80)
        with storage.database() as db:
            db.execute("INSERT INTO devices(name,host,channel) VALUES('cam2','h',2)")


if __name__ == "__main__":
    unittest.main()
