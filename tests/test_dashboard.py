import os, sys, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sentinel_dashboard as dash
import sentinel_recording as rec
import sentinel_security as security
import sentinel_storage as storage


def session(role, perms, name="u"):
    return security.Session(1, name, role, frozenset(perms), None)


class DashboardLogic(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        data = self.tmp / "s"
        for name, value in (("DATA", data), ("DB_PATH", data / "d.sqlite3")):
            p = patch.object(storage, name, value); p.start(); self.addCleanup(p.stop)

    def test_widgets_follow_role(self):
        admin = security.local_session()
        operator = session("Оператор", security.BUILTIN_ROLES["Оператор"])
        hr = session("Кадры", ("persons",))
        self.assertIn("health", dash.default_widgets(admin))
        self.assertNotIn("health", [w.key for w in dash.allowed_widgets(operator)])
        self.assertEqual(dash.default_widgets(operator), ["monitoring", "alarms", "map", "events"])
        self.assertEqual(dash.allowed_widgets(hr), [])

    def test_saved_widgets_per_user_and_filtered(self):
        operator = session("Оператор", security.BUILTIN_ROLES["Оператор"], "ivan")
        dash.save_widgets(operator, ["map", "health", "alarms"])
        self.assertEqual(dash.load_widgets(operator), ["map", "alarms"])
        other = session("Оператор", security.BUILTIN_ROLES["Оператор"], "petr")
        self.assertEqual(dash.load_widgets(other), dash.default_widgets(other))

    def test_module_permissions(self):
        operator = session("Оператор", security.BUILTIN_ROLES["Оператор"])
        self.assertTrue(dash.module_allowed(operator, "playback"))
        self.assertFalse(dash.module_allowed(operator, "devices"))
        self.assertFalse(dash.module_allowed(operator, "accounts"))
        self.assertTrue(dash.module_allowed(operator, "board"))
        self.assertTrue(dash.module_allowed(operator, "unknown_module"))

    def test_cpu_meter_and_meminfo(self):
        samples = iter([(100, 1000), (150, 1100)])
        meter = dash.CpuMeter(lambda: next(samples))
        self.assertAlmostEqual(meter.sample(), 50.0)
        stat = self.tmp / "stat"
        stat.write_text("cpu  10 0 10 70 10 0 0 0 0 0\ncpu0 1 2 3\n")
        self.assertEqual(dash.read_cpu_times(str(stat)), (20, 100))
        mem = self.tmp / "meminfo"
        mem.write_text("MemTotal:  1000 kB\nMemFree: 100 kB\nMemAvailable:  400 kB\n")
        self.assertEqual(dash.memory_usage(str(mem)), (600 * 1024, 1000 * 1024))
        self.assertIsNone(dash.memory_usage(str(self.tmp / "missing")))

    def test_device_counts(self):
        with storage.database() as db:
            for host, ch, status in (("a", 1, "online"), ("a", 2, "online"), ("b", 1, "offline"), ("c", 1, "")):
                db.execute("INSERT INTO devices(name,host,channel,status) VALUES(?,?,?,?)", (host, host, ch, status))
        self.assertEqual(dash.device_counts(),
                         {"total": 3, "online": 1, "offline": 1, "unknown": 1, "channels": 4})

    def test_record_integrity(self):
        now = datetime(2026, 11, 26, 12, 0).astimezone()
        always = [[(0, 1440)] for _ in range(7)]
        plans = [rec.Plan(1, True, "main", always, 30, True), rec.Plan(2, True, "main", always, 30, True),
                 rec.Plan(3, True, "main", [[] for _ in range(7)], 30, True)]
        fresh = {1: [(now - timedelta(minutes=5), now - timedelta(minutes=1), "x")], 2: []}
        result = dash.record_integrity(now, root=self.tmp, plans=plans,
                                       list_segments=lambda root, d, s, e: fresh.get(d, []))
        self.assertEqual(result, {"scheduled": 2, "ok": 1, "gaps": [2]})

    def test_disk_usage_walks_up_to_existing_folder(self):
        used, total, free = dash.disk_usage(self.tmp / "no" / "such" / "dir")
        self.assertEqual(used + free, total)


if __name__ == "__main__":
    unittest.main()
