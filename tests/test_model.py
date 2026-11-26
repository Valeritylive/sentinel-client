import sqlite3
import unittest

import sentinel_storage as storage
from test_storage import _TempDb


class GroupTests(_TempDb):
    def test_groups_crud_and_device_assignment(self):
        gid = storage.add_group("  Офис ")
        self.assertEqual(storage.add_group("Офис"), gid)
        with self.assertRaises(ValueError):
            storage.add_group("   ")
        with storage.database() as db:
            db.execute("INSERT INTO devices(name,host,channel) VALUES('c1','h',1),('c2','h',2)")
            ids = [r[0] for r in db.execute("SELECT id FROM devices")]
        storage.set_device_group(ids, gid)
        self.assertTrue(all(d["group_id"] == gid for d in storage.load_devices()))
        storage.rename_group(gid, "Склад")
        self.assertEqual(storage.load_groups()[0]["name"], "Склад")
        storage.delete_group(gid)
        self.assertEqual(storage.load_groups(), [])
        self.assertTrue(all(d["group_id"] is None for d in storage.load_devices()))

    def test_custom_divisions(self):
        storage.save_custom_division("мой", 2, 2, [(0, 0, 2, 1), (0, 1, 1, 1), (1, 1, 1, 1)])
        storage.save_custom_division("мой", 2, 2, [(0, 0, 1, 1), (1, 0, 1, 1), (0, 1, 2, 1)])
        items = storage.load_custom_divisions()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["cells"][2], (0, 1, 2, 1))
        with self.assertRaises(ValueError):
            storage.save_custom_division("плохой", 2, 2, [(0, 0, 1, 1)])
        storage.delete_custom_division("мой")
        self.assertEqual(storage.load_custom_divisions(), [])


class MigrationTests(_TempDb):
    def test_old_database_gets_new_columns(self):
        self.data_path.mkdir(parents=True)
        db = sqlite3.connect(self.db_path)
        db.execute("""CREATE TABLE devices (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            host TEXT NOT NULL, sdk_port INTEGER NOT NULL DEFAULT 8000, http_port INTEGER NOT NULL DEFAULT 80,
            rtsp_port INTEGER NOT NULL DEFAULT 554, channel INTEGER NOT NULL DEFAULT 1,
            stream TEXT NOT NULL DEFAULT 'main', protocol TEXT NOT NULL DEFAULT 'hikvision',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        db.execute("INSERT INTO devices(name,host) VALUES('old','1.2.3.4')")
        db.commit(); db.close()
        devices = storage.load_devices()
        self.assertEqual(devices[0]["name"], "old")
        for key in ("group_id", "serial_no", "device_model", "use_tls", "host_name"):
            self.assertIn(key, devices[0])
        with storage.database() as db:
            cols = {r[1] for r in db.execute("PRAGMA table_info(saved_views)")}
        self.assertIn("division", cols)

    def test_brand_cloud_group_renamed(self):
        legacy = storage.add_group("Hik-Connect")
        with storage.database() as db:
            db.execute("INSERT INTO devices(name,host,group_id) VALUES('cam','10.0.0.1',?)", (legacy,))
            storage._rename_brand_groups(db)
            names = [r[0] for r in db.execute("SELECT name FROM groups")]
            group = db.execute("SELECT g.name FROM devices d JOIN groups g ON g.id=d.group_id").fetchone()[0]
        self.assertNotIn("Hik-Connect", names)
        self.assertEqual(group, storage.CLOUD_GROUP_NAME)

    def test_brand_cloud_group_merged_into_existing(self):
        target = storage.add_group(storage.CLOUD_GROUP_NAME)
        legacy = storage.add_group("Hik-Connect")
        with storage.database() as db:
            db.execute("INSERT INTO devices(name,host,group_id) VALUES('cam','10.0.0.1',?)", (legacy,))
            storage._rename_brand_groups(db)
            row = db.execute("SELECT group_id FROM devices").fetchone()[0]
            count = db.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        self.assertEqual(row, target)
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
