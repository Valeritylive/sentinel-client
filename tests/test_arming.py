import sys, tempfile, unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import gtk_stub

gtk_stub.install()
import sentinel_alarms as alarms
import sentinel_arming as arming
import sentinel_storage as storage
import app as app_module


class _Db(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name) / "s"
        for name, value in (("DATA", data), ("DB_PATH", data / "d.sqlite3")):
            p = patch.object(storage, name, value); p.start(); self.addCleanup(p.stop)
        with storage.database() as db:
            self.g1 = db.execute("INSERT INTO groups(name) VALUES('Склад')").lastrowid
            self.g2 = db.execute("INSERT INTO groups(name) VALUES('Офис')").lastrowid
            self.ids = [db.execute("INSERT INTO devices(name,host,channel,group_id) VALUES(?,?,?,?)",
                                   (f"c{i}", "10.0.0.5", i, g)).lastrowid
                        for i, g in ((1, self.g1), (2, self.g1), (3, self.g2), (4, None))]
        self.devices = storage.load_devices()


class ArmingLogic(_Db):
    def test_default_armed_and_hierarchy(self):
        self.assertTrue(all(arming.device_armed(i) for i in self.ids))
        arming.set_armed(arming.AREA, self.g1, False, "op")
        self.assertEqual([arming.device_armed(i) for i in self.ids], [False, False, True, True])
        arming.set_armed(arming.DEVICE, self.ids[0], True, "op")   # канал важнее области
        self.assertEqual(arming.area_state(self.g1, self.devices), "partial")
        arming.set_armed(arming.AREA, self.g1, False, "op")        # область сбрасывает уточнения
        self.assertEqual(arming.area_state(self.g1, self.devices), "disarmed")
        arming.set_armed(arming.SYSTEM, 0, False, "op")
        self.assertEqual(arming.summary(self.devices), (0, 4))
        arming.set_armed(arming.SYSTEM, 0, True, "admin")
        self.assertEqual(arming.summary(self.devices), (4, 4))
        self.assertEqual(arming.load_states()[(arming.SYSTEM, 0)]["changed_by"], "admin")

    def test_user_event_always_raises(self):
        arming.set_armed(arming.SYSTEM, 0, False)
        self.assertFalse(arming.event_raises_alarms({"device_id": self.ids[0], "event_type": "VMD"}))
        self.assertTrue(arming.event_raises_alarms({"device_id": self.ids[0], "event_type": "user"}))
        with self.assertRaises(ValueError):
            arming.set_armed("room", 1, True)


class ArmingInClient(_Db):
    def setUp(self):
        super().setUp()
        self.win = app_module.SentinelWindow(None)
        self.win.refresh()
        alarms.save_rule({"name": "Движение", "event_type": "motion", "priority": "high",
                          "linkage": {"popup": False}})

    def _event(self, device_id):
        return {"device_id": device_id, "device_name": "c", "channel": "1", "event_type": "motion",
                "state": "active", "time": datetime.now().astimezone().isoformat(timespec="seconds")}

    def test_disarmed_device_logs_event_without_alarm(self):
        self.win.arm_devices([self.ids[0]], False, "c1")
        flashed = []
        with patch.object(self.win, "_flash_map_device", lambda d, alarm=False: flashed.append((d, alarm))):
            self.win.handle_event(self._event(self.ids[0]))
            self.win.handle_event(self._event(self.ids[2]))
        records = alarms.load_alarm_records(10)
        self.assertEqual([r["device_id"] for r in records], [self.ids[2]])
        self.assertEqual(len(alarms.load_events(10)), 2)
        self.assertEqual(flashed, [(self.ids[0], False), (self.ids[2], True)])

    def test_operator_without_rights_cannot_disarm(self):
        import sentinel_security as security
        self.win.session = security.Session(5, "hr", "Кадры", frozenset({"persons"}), None)
        self.win.arm_system(False)
        self.assertTrue(arming.device_armed(self.ids[0]))
        self.assertEqual(self.win.arming_menu_items(self.ids, "x"), [])

    def test_menu_items_follow_state(self):
        titles = [t for t, _cb in self.win.arming_menu_items(self.ids[:2], "x")]
        self.assertEqual(titles, ["○ Снять с охраны"])
        self.win.arm_area({"id": self.g1, "name": "Склад"}, False)
        titles = [t for t, _cb in self.win.arming_menu_items(self.ids[:3], "x")]
        self.assertEqual(titles, ["🛡 Поставить на охрану", "○ Снять с охраны"])

    def test_map_flash_and_stop(self):
        marker = gtk_stub.Widget()
        self.win.map_markers = {self.ids[0]: [marker]}
        self.win._flash_map_device(self.ids[0], alarm=True)
        self.assertTrue(marker.has_css_class("map-alarm"))
        self.win.stop_map_flash(self.ids[0])
        self.win._map_flash_tick()
        self.assertFalse(marker.has_css_class("map-alarm"))


if __name__ == "__main__":
    unittest.main()
