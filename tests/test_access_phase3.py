import io, json, sys, tempfile, unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import gtk_stub

gtk_stub.install()
import sentinel_access_rules as ar
import sentinel_alarms as alarms
import sentinel_attendance as att
import sentinel_people as people
import sentinel_storage as storage
import sentinel_visitors as visitors


class _Db(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name) / "s"
        for name, value in (("DATA", data), ("DB_PATH", data / "d.sqlite3")):
            p = patch.object(storage, name, value); p.start(); self.addCleanup(p.stop)
        with storage.database() as db:
            self.ctrl = db.execute("INSERT INTO devices(name,host,channel,category,door_count) "
                                   "VALUES('ctrl','10.0.0.9',1,'access',4)").lastrowid


def access_event(when, employee, door=1, minor=75, reader=0, device_id=1):
    details = {"employeeNoString": employee, "doorNo": str(door), "major": "5", "minor": str(minor)}
    if reader:
        details["cardReaderNo"] = str(reader)
    return {"time": when, "device_id": device_id, "device_name": "ctrl", "channel": str(door),
            "event_type": "access", "category": "event", "state": "active", "details": details}


class AccessRules(_Db):
    def test_validate(self):
        with self.assertRaises(ValueError):
            ar.validate(ar.INTERLOCK, {"groups": [[1]]}, 4)
        with self.assertRaises(ValueError):
            ar.validate(ar.INTERLOCK, {"groups": [[1, 2], [2, 3]]}, 4)
        with self.assertRaises(ValueError):
            ar.validate(ar.ANTIPASSBACK, {"doors": [5]}, 4)
        with self.assertRaises(ValueError):
            ar.validate(ar.ANTIPASSBACK, {"doors": [1], "mode": "route"}, 4)
        with self.assertRaises(ValueError):
            ar.validate(ar.FIRST_PERSON, {"door": 1, "person_ids": []}, 4)
        self.assertEqual(ar.validate(ar.INTERLOCK, {"groups": [[1, 2, 2]]}, 4), {"groups": [[1, 2]]})

    def test_pending_apply_and_payloads(self):
        rid = ar.save_rule(ar.INTERLOCK, "Шлюз", self.ctrl, {"groups": [[1, 2]]}, door_count=4)
        ar.save_rule(ar.ANTIPASSBACK, "АПБ", self.ctrl, {"doors": [3], "mode": "in_out"}, door_count=4)
        self.assertEqual(ar.pending_devices(), [self.ctrl])
        payloads = dict(ar.device_payloads(self.ctrl, 4))
        lock = payloads["/ISAPI/AccessControl/MultiDoorInterLockCfg?format=json"]["MultiDoorInterLockCfg"]
        self.assertEqual(lock, {"enable": True, "MultiDoorGroup": [{"id": 1, "doorNoList": [1, 2]}]})
        self.assertTrue(payloads["/ISAPI/AccessControl/AntiSneakCfg?format=json"]["AntiSneakCfg"]["enable"])
        reader5 = payloads["/ISAPI/AccessControl/CardReaderAntiSneakCfg/5?format=json"]["CardReaderAntiSneakCfg"]
        self.assertEqual(reader5["followUpCardReader"], [6])
        with self.assertRaises(ValueError):   # дверь уже в другом шлюзе
            ar.save_rule(ar.INTERLOCK, "Ещё", self.ctrl, {"groups": [[2, 4]]}, door_count=4)
        for rule in ar.pending_rules():
            ar.set_status(rule["id"], ar.APPLIED)
        self.assertEqual(ar.pending_devices(), [])
        # удалённое применённое правило ждёт «Применить», затем исчезает, а шлюз выключается
        ar.delete_rule(rid)
        self.assertEqual(ar.pending_devices(), [self.ctrl])
        lock = dict(ar.device_payloads(self.ctrl, 4))["/ISAPI/AccessControl/MultiDoorInterLockCfg?format=json"]
        self.assertFalse(lock["MultiDoorInterLockCfg"]["enable"])
        ar.set_status(rid, ar.APPLIED)
        self.assertIsNone(ar.get_rule(rid))

    def test_route_antipassback(self):
        ar.save_rule(ar.ANTIPASSBACK, "Маршрут", self.ctrl, {"doors": [1, 2, 3], "mode": "route"}, door_count=4)
        payloads = dict(ar.device_payloads(self.ctrl, 4))
        follow = {r: payloads[f"/ISAPI/AccessControl/CardReaderAntiSneakCfg/{r}?format=json"]
                  ["CardReaderAntiSneakCfg"]["followUpCardReader"] for r in (1, 3, 5)}
        self.assertEqual(follow, {1: [3], 3: [5], 5: [1]})

    def test_first_person_is_software(self):
        rid = ar.save_rule(ar.FIRST_PERSON, "Утро", self.ctrl, {"door": 2, "person_ids": [7], "until": "18:00"},
                           door_count=4)
        self.assertEqual(ar.pending_rules(), [])
        tracker = ar.FirstPersonTracker()
        rules = ar.load_rules()
        morning = datetime(2026, 11, 26, 8, 30)
        self.assertEqual(tracker.on_event(access_event("t", "99", door=2, device_id=self.ctrl), rules, {7: "42"},
                                          morning), [])
        cmds = tracker.on_event(access_event("t", "42", door=2, device_id=self.ctrl), rules, {7: "42"}, morning)
        self.assertEqual([(c[1], c[2]) for c in cmds], [(2, "always_open")])
        self.assertEqual(tracker.on_event(access_event("t", "42", door=2, device_id=self.ctrl), rules, {7: "42"},
                                          morning), [])           # второй раз за день — ничего
        self.assertEqual(tracker.due_resume(rules, morning), [])
        resume = tracker.due_resume(rules, datetime(2026, 11, 26, 18, 0))
        self.assertEqual([(c[1], c[2], c[3]["id"]) for c in resume], [(2, "resume", rid)])

    def test_passback_monitor(self):
        ar.save_rule(ar.ANTIPASSBACK, "АПБ", self.ctrl, {"doors": [1], "mode": "in_out"}, door_count=4)
        rules, monitor = ar.load_rules(), ar.PassbackMonitor()
        flags = [monitor.check(access_event("t", "5", reader=r, device_id=self.ctrl), rules) for r in (1, 2, 1, 1)]
        self.assertEqual(flags, [False, False, False, True])

    def test_outcome(self):
        self.assertEqual(ar.event_outcome(access_event("t", "1", minor=75)), "granted")
        self.assertEqual(ar.event_outcome(access_event("t", "1", minor=76)), "denied")
        self.assertEqual(ar.event_outcome({"details": json.dumps({"minor": 1})}), "granted")


class ApplyIndicator(_Db):
    def test_level_change_marks_pending(self):
        pid = people.save_person({"name": "Иван", "employee_no": "10"})
        lid = people.save_access_level("Офис", [{"device_id": self.ctrl, "door": 1}])
        people.assign_levels(person_ids=[pid], level_ids=[lid])
        self.assertEqual(people.apply_summary()["pending"], 1)
        people.set_apply_status(pid, self.ctrl, "applied")
        self.assertEqual(people.pending_persons(), [])
        people.save_access_level("Офис", [{"device_id": self.ctrl, "door": 2}], level_id=lid)
        self.assertEqual([p["id"] for p in people.pending_persons()], [pid])


class Visitors(_Db):
    def visit(self, **extra):
        now = datetime.now().replace(second=0, microsecond=0)
        data = {"name": "Пётр Гость", "phone": "+7 900 111-22-33", "document": "45 01 123456",
                "expected_from": now.strftime("%Y-%m-%d %H:%M"),
                "expected_to": (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")}
        data.update(extra)
        return visitors.register(data, "op")

    def test_flow(self):
        lid = people.save_access_level("Гости", [{"device_id": self.ctrl, "door": 1}])
        vid = self.visit(level_id=lid)
        self.assertEqual(visitors.get(vid)["status"], visitors.PENDING)
        with self.assertRaises(ValueError):
            visitors.check_in(vid)
        visitors.decide(vid, True, "boss")
        person = visitors.check_in(vid)
        self.assertEqual(person["employee_no"], f"V{vid}")
        self.assertEqual(len(person["cards"]), 1)
        self.assertEqual(people.person_doors(person), {self.ctrl: {1}})
        self.assertEqual(visitors.counters()["inside"], 1)
        employee, devices = visitors.check_out(vid)
        self.assertEqual((employee, devices), (f"V{vid}", {self.ctrl}))
        self.assertEqual(people.load_persons(), [])
        self.assertEqual(visitors.get(vid)["status"], visitors.LEFT)

    def test_no_approval_and_watchlist(self):
        visitors.set_approval_required(False)
        visitors.add_to_watchlist("Другое Имя", document="4501-123456", reason="нарушение")
        vid = self.visit()
        self.assertEqual(visitors.get(vid)["status"], visitors.APPROVED)
        with self.assertRaises(visitors.WatchlistHit):
            visitors.check_in(vid)
        visitors.check_in(vid, badge="777", force=True)
        self.assertEqual(visitors.get(vid)["badge"], "777")

    def test_validation_and_badge(self):
        with self.assertRaises(ValueError):
            self.visit(name=" ")
        with self.assertRaises(ValueError):
            self.visit(expected_to="2000-01-01 10:00")
        vid = self.visit(company="<b>ООО</b>")
        page = visitors.badge_html(visitors.get(vid))
        self.assertIn("&lt;b&gt;ООО", page)
        self.assertNotIn("<b>ООО", page)

    def test_overdue(self):
        visitors.set_approval_required(False)
        vid = self.visit()
        visitors.check_in(vid)
        later = datetime.now() + timedelta(hours=5)
        self.assertEqual([v["id"] for v in visitors.overdue(later)], [vid])


class Attendance(_Db):
    def setUp(self):
        super().setUp()
        self.pid = people.save_person({"name": "Анна", "employee_no": "21"})
        self.person = people.load_persons()[0]
        self.day = date(2026, 11, 24)   # вторник

    def passes(self, *times):
        for t in times:
            alarms.store_event(access_event(f"2026-11-24T{t}:00", "21", device_id=self.ctrl))

    def day_record(self, now=datetime(2026, 11, 30)):
        return att.compute([self.person], self.day, self.day, now)[0]

    def test_normal_and_late(self):
        self.passes("08:55", "12:00", "18:05")
        r = self.day_record()
        self.assertEqual((r["status"], r["worked"], r["late"]), ("ok", 490, 0))
        alarms.store_event(access_event("2026-11-25T09:25:00", "21", device_id=self.ctrl))
        alarms.store_event(access_event("2026-11-25T17:30:00", "21", device_id=self.ctrl))
        r = att.compute([self.person], date(2026, 11, 25), date(2026, 11, 25), datetime(2026, 11, 30))[0]
        self.assertEqual((r["status"], r["late"], r["early"]), ("late_early", 25, 30))

    def test_absent_dayoff_no_out_and_denied(self):
        alarms.store_event(access_event("2026-11-24T09:00:00", "21", minor=76, device_id=self.ctrl))  # отказ
        self.assertEqual(self.day_record()["status"], "absent")
        r = att.compute([self.person], date(2026, 11, 29), date(2026, 11, 29), datetime(2026, 11, 30))[0]
        self.assertEqual(r["status"], "day_off")
        self.passes("09:00")
        self.assertEqual(self.day_record()["status"], "no_out")

    def test_points_and_corrections(self):
        config = att.load_config()
        config["points"] = {att.point_key(self.ctrl, 2): "both"}
        att.save_config(config)
        self.passes("09:00", "18:00")              # дверь 1 не точка учёта
        self.assertEqual(self.day_record()["status"], "absent")
        att.save_correction(self.pid, self.day, check_in="09:00", check_out="18:00", reason="забыл карту",
                            username="hr")
        r = self.day_record()
        self.assertEqual((r["status"], r["worked"], r["corrected"]), ("ok", 480, True))
        att.save_correction(self.pid, self.day, leave="sick", reason="справка")
        self.assertEqual(att.status_text(self.day_record()), "Больничный")
        with self.assertRaises(ValueError):
            att.save_correction(self.pid, self.day, reason="")

    def test_night_shift(self):
        sid = att.save_shift({"name": "Ночь", "start": "22:00", "end": "06:00", "break": 30, "days": list(range(7))})
        config = att.load_config()
        config["person_shifts"] = {str(self.pid): sid}
        att.save_config(config)
        alarms.store_event(access_event("2026-11-24T21:58:00", "21", device_id=self.ctrl))
        alarms.store_event(access_event("2026-11-25T06:02:00", "21", device_id=self.ctrl))
        r = self.day_record()
        self.assertEqual((r["status"], r["worked"]), ("ok", 454))

    def test_shift_validation_and_csv(self):
        with self.assertRaises(ValueError):
            att.save_shift({"name": "x", "start": "09:00", "end": "09:00", "days": [0]})
        with self.assertRaises(ValueError):
            att.save_shift({"name": "x", "start": "9", "end": "18:00", "days": [0]})
        self.passes("09:20", "18:00")
        records = self.day_record() and att.compute([self.person], self.day, self.day, datetime(2026, 11, 30))
        out = io.StringIO()
        att.export_csv(records, out, summary=True)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("ID;Имя"))
        self.assertIn("Анна;1;1;0;0;1;20", lines[1])

    def test_import_controller_events(self):
        device = {"id": self.ctrl, "host_name": "ctrl"}
        records = [{"time": "2026-11-24T09:00:00+03:00", "employeeNoString": "21", "doorNo": 1, "minor": 75},
                   {"time": "2026-11-24T18:00:00+03:00", "employeeNoString": "21", "doorNo": 1, "minor": 75}]
        self.assertEqual(att.import_controller_events(device, records), 2)
        self.assertEqual(att.import_controller_events(device, records), 0)


if __name__ == "__main__":
    unittest.main()


class EngineFirstPerson(_Db):
    def test_server_opens_and_resumes_door(self):
        import sentinel_engine as engine_module
        pid = people.save_person({"name": "Старший", "employee_no": "42"})
        ar.save_rule(ar.FIRST_PERSON, "Утро", self.ctrl, {"door": 2, "person_ids": [pid], "until": "23:59"},
                     door_count=4)
        engine = engine_module.Engine(receive_events=False, credentials_loader=lambda d: {"username": "a",
                                                                                         "password": "b"})
        calls = []
        with patch.object(engine_module, "isapi_request", lambda d, c, path, method="GET", body=None, **k:
                          calls.append((path, body))):
            event = access_event(datetime.now().replace(hour=0, minute=1).isoformat(timespec="seconds"), "42",
                                 door=2, device_id=self.ctrl)
            engine.handle_event(event)
            self.assertEqual(len(calls), 1)
            self.assertIn("/RemoteControl/door/2", calls[0][0])
            self.assertIn("alwaysOpen", calls[0][1])
            ar.set_enabled(ar.load_rules()[0]["id"], False)
            engine.access_rules_tick()
            self.assertIn("resume", calls[-1][1])
