import io, json, sys, tempfile, unittest, zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import gtk_stub

gtk_stub.install()
import sentinel_alarms as alarms
import sentinel_evidence as evidence
import sentinel_parking as parking
import sentinel_patrol as patrol
import sentinel_people as people
import sentinel_storage as storage
import sentinel_videowall as wall


class _Db(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        data = self.tmp / "s"
        for name, value in (("DATA", data), ("DB_PATH", data / "d.sqlite3")):
            p = patch.object(storage, name, value); p.start(); self.addCleanup(p.stop)
        with storage.database() as db:
            self.cams = [db.execute("INSERT INTO devices(name,host,channel) VALUES(?,?,?)",
                                    (f"cam{i}", "10.0.0.5", i)).lastrowid for i in (1, 2, 3)]
            self.ctrl = db.execute("INSERT INTO devices(name,host,channel,category,door_count) "
                                   "VALUES('ctrl','10.0.0.9',1,'access',4)").lastrowid


class Parking(_Db):
    def setUp(self):
        super().setUp()
        self.staff = people.add_vehicle_list("Сотрудники")
        self.black = people.add_vehicle_list("Чёрный список")
        people.save_vehicle({"plate": "А123ВС77", "list_id": self.staff})
        people.save_vehicle({"plate": "Х999ХХ99", "list_id": self.black})
        self.lot = parking.save_lot({"name": "Главная", "capacity": 2, "mode": parking.MODE_ALL,
                                     "entry_cameras": [self.cams[0]], "exit_cameras": [self.cams[1]],
                                     "allow_lists": [self.staff], "deny_lists": [self.black],
                                     "barrier": {"device_id": self.cams[0], "output": 1, "auto_close": 5}})

    def plate(self, cam, plate, when):
        return parking.handle_plate(cam, plate, people.vehicle_index(), when)

    def test_validation(self):
        with self.assertRaises(ValueError):
            parking.save_lot({"name": "x", "entry_cameras": [self.cams[0]], "mode": parking.MODE_ALL})  # занята
        with self.assertRaises(ValueError):
            parking.save_lot({"name": "y", "entry_cameras": [self.cams[2]], "mode": parking.MODE_LISTS})
        with self.assertRaises(ValueError):
            parking.save_lot({"name": "z", "entry_cameras": [self.cams[2]], "exit_cameras": [self.cams[2]]})

    def test_entry_exit_capacity_blacklist(self):
        t = datetime(2026, 11, 26, 9, 0)
        r = self.plate(self.cams[0], "a123bc77", t)          # латиница = кириллица
        self.assertEqual((r["decision"], r["reason"]), ("allow", "list"))
        self.assertEqual(r["barrier"]["output"], 1)
        self.assertEqual(self.plate(self.cams[0], "Х999ХХ99", t)["reason"], "blacklist")
        self.assertEqual(self.plate(self.cams[0], "В001ОР77", t)["decision"], "allow")
        r = self.plate(self.cams[0], "Е555КХ77", t)            # мест нет
        self.assertEqual((r["decision"], r["reason"], r["barrier"]), ("deny", "full", None))
        lot = parking.get_lot(self.lot)
        self.assertEqual(parking.occupancy(lot)["free"], 0)
        r = self.plate(self.cams[1], "B00IOP77", t + timedelta(hours=2))   # одна ошибка распознавания
        self.assertEqual((r["reason"], r["duration"]), ("exit", 120))
        self.assertEqual(parking.occupancy(lot)["used"], 1)
        self.assertEqual(self.plate(self.cams[1], "М777ММ77", t)["reason"], "exit_unknown")
        self.assertIsNone(self.plate(self.cams[2], "А123ВС77", t))
        stats = parking.day_stats(self.lot, "2026-11-26")
        self.assertEqual(stats, {"in": 2, "out": 2, "denied": 2})
        out = io.StringIO()
        parking.export_passes_csv(parking.load_passes(self.lot), parking.load_lots(), out)
        self.assertIn("в чёрном списке", out.getvalue())

    def test_manual_and_overstay(self):
        parking.save_lot(dict(parking.get_lot(self.lot), overstay_hours=3), self.lot)
        barrier = parking.manual_pass(self.lot, "к001кк77", "in", "op", datetime(2026, 11, 26, 6, 0))
        self.assertEqual(barrier["device_id"], self.cams[0])
        lot = parking.get_lot(self.lot)
        self.assertEqual(len(parking.overstays(lot, datetime(2026, 11, 26, 10, 0))), 1)
        session = parking.open_sessions(self.lot)[0]
        parking.close_session(session["id"], "op")
        self.assertEqual(parking.open_sessions(self.lot), [])


class Patrol(_Db):
    def setUp(self):
        super().setUp()
        self.guard = people.save_person({"name": "Охранник", "employee_no": "77"})
        self.route = patrol.save_route({"name": "Ночной", "guards": [self.guard], "starts": ["22:00"],
                                        "tolerance": 5, "points": [
                                            {"device_id": self.ctrl, "door": 1, "name": "Вход", "offset": 0},
                                            {"device_id": self.ctrl, "door": 2, "name": "Склад", "offset": 15},
                                            {"device_id": self.ctrl, "door": 3, "name": "Гараж", "offset": 30}]})
        self.employees = {self.guard: "77"}

    def mark(self, when, door):
        alarms.store_event({"time": when, "device_id": self.ctrl, "device_name": "ctrl", "channel": str(door),
                            "event_type": "access", "category": "event", "state": "active",
                            "details": {"employeeNoString": "77", "doorNo": str(door), "minor": "1"}})

    def test_validation(self):
        with self.assertRaises(ValueError):
            patrol.save_route({"name": "x", "guards": [1], "starts": ["10:00"],
                               "points": [{"device_id": 1, "offset": 10}, {"device_id": 1, "offset": 5}]})
        with self.assertRaises(ValueError):
            patrol.save_route({"name": "x", "guards": [1], "starts": ["25:00"],
                               "points": [{"device_id": 1, "offset": 0}, {"device_id": 1, "offset": 5}]})

    def test_run_evaluation_and_missed(self):
        self.mark("2026-11-24T22:01:00", 1)
        self.mark("2026-11-24T22:24:00", 2)     # поздно (план 22:15, допуск 5)
        routes = patrol.load_routes()
        runs = patrol.runs_for_period(routes, self.employees, date(2026, 11, 24), date(2026, 11, 24),
                                      datetime(2026, 11, 25, 8, 0))
        statuses = [p["status"] for p in runs[0]["points"]]
        self.assertEqual(statuses, [patrol.ON_TIME, patrol.LATE, patrol.MISSED])
        self.assertEqual(runs[0]["status"], patrol.VIOLATION)
        running = patrol.runs_for_period(routes, self.employees, date(2026, 11, 24), date(2026, 11, 24),
                                         datetime(2026, 11, 24, 22, 20))[0]
        self.assertEqual(running["status"], patrol.RUNNING)
        missed = patrol.new_missed_points(routes, self.employees, datetime(2026, 11, 25, 8, 0))
        self.assertEqual([p["name"] for _r, _run, p in missed], ["Гараж"])
        self.assertEqual(patrol.new_missed_points(routes, self.employees, datetime(2026, 11, 25, 8, 0)), [])
        self.assertEqual(patrol.missed_event(*missed[0])["event_type"], "patrol_missed")
        out = io.StringIO()
        patrol.export_runs_csv(runs, out, {"77": "Охранник"})
        self.assertIn("Охранник", out.getvalue())

    def test_other_person_does_not_count(self):
        alarms.store_event({"time": "2026-11-24T22:00:00", "device_id": self.ctrl, "event_type": "access",
                            "details": {"employeeNoString": "12", "doorNo": "1", "minor": "1"}})
        run = patrol.runs_for_period(patrol.load_routes(), self.employees, date(2026, 11, 24), date(2026, 11, 24),
                                     datetime(2026, 11, 25))[0]
        self.assertEqual(run["done"], 0)


class VideoWall(_Db):
    def test_wall_scenes_and_alarm_slot(self):
        wid = wall.save_wall("Дежурная", 1, 2, alarm_screen=1)
        w = wall.get_wall(wid)
        self.assertEqual(len(w["screens"]), 2)
        screens = wall.place_cameras(w["screens"], 0, self.cams)
        self.assertEqual(screens[0]["layout"], 4)
        screens = wall.set_layout(screens, 1, 1)
        screens = wall.place_camera(screens, 1, 0, self.cams[2])
        wall.save_screens(wid, screens)
        wall.save_scene(wid, "День", screens)
        wall.save_screens(wid, [wall.empty_screen(0), wall.empty_screen(1)])
        scene = wall.load_scenes(wid)[0]
        restored = wall.apply_scene(wid, scene["id"])
        self.assertEqual(restored[0]["cameras"][:3], self.cams)
        self.assertEqual(wall.alarm_slot(wall.get_wall(wid), restored, rotation=3), (1, 0))
        self.assertEqual(wall.missing_cameras(restored, self.cams[:2]), [self.cams[2]])
        with self.assertRaises(ValueError):
            wall.save_wall("Большая", 5, 1)
        with self.assertRaises(ValueError):
            wall.place_camera(restored, 1, 3, self.cams[0])

    def test_linkage_wall_action(self):
        plan = alarms.linkage_plan({"linkage": {"wall": 3, "popup": False}}, {"device_id": 9})
        self.assertEqual(plan, [("wall", {"wall_id": 3, "device_id": 9})])


class Evidence(_Db):
    def test_case_lifecycle(self):
        cid = evidence.create_case("Кража со склада", "ночь", "op")
        self.assertEqual(evidence.get_case(cid)["number"], f"{datetime.now().year}-0001")
        src = self.tmp / "clip.mp4"
        src.write_bytes(b"video")
        pkg = self.tmp / "pkg"
        pkg.mkdir()
        (pkg / "a.mp4").write_bytes(b"a")
        (pkg / "manifest.json").write_text("{}")
        evidence.add_file(cid, src, "op", "snapshot")
        evidence.add_file(cid, pkg, "op", "export", "Экспорт склада")
        evidence.add_record(cid, "op", "alarm", "Тревога: движение", {"time": "t"})
        items = evidence.load_items(cid)
        self.assertEqual([i["kind"] for i in items], ["snapshot", "export", "alarm"])
        self.assertTrue(all(s == "ok" for _i, s in evidence.verify(cid, "op")))
        Path(items[0]["path"]).write_bytes(b"edited")
        self.assertEqual([s for _i, s in evidence.verify(cid, "op")], ["changed", "ok"])
        bundle = evidence.export_case(cid, self.tmp / "out", "op")
        with zipfile.ZipFile(bundle) as z:
            names = z.namelist()
            manifest = json.loads(z.read("manifest.json"))
        self.assertIn("SHA256SUMS", names)
        self.assertTrue(any(n.endswith("/a.mp4") for n in names))
        self.assertEqual(len(manifest["items"]), 3)
        evidence.set_status(cid, evidence.CLOSED, "boss", "передано")
        with self.assertRaises(ValueError):
            evidence.add_record(cid, "op", "note", "ещё")
        with self.assertRaises(ValueError):
            evidence.remove_item(items[0]["id"], "op", "ошибка")
        actions = [r["action"] for r in evidence.load_log(cid)]
        self.assertEqual(actions[0], "create")
        self.assertIn("verify", actions)
        self.assertEqual(actions[-1], "close")

    def test_remove_needs_reason(self):
        cid = evidence.create_case("x")
        item = evidence.add_record(cid, "op", "note", "заметка")
        with self.assertRaises(ValueError):
            evidence.remove_item(item, "op", "")
        evidence.remove_item(item, "op", "дубликат")
        self.assertEqual(evidence.load_items(cid), [])
        with self.assertRaises(ValueError):
            evidence.create_case("y", number=evidence.get_case(cid)["number"])


if __name__ == "__main__":
    unittest.main()


class EngineParking(_Db):
    def test_server_parking_and_patrol_tick(self):
        import sentinel_engine as engine_module
        staff = people.add_vehicle_list("S")
        people.save_vehicle({"plate": "А123ВС77", "list_id": staff})
        parking.save_lot({"name": "P", "mode": parking.MODE_LISTS, "allow_lists": [staff],
                          "entry_cameras": [self.cams[0]], "barrier": {"device_id": self.cams[0], "output": 1}})
        engine = engine_module.Engine(receive_events=False)
        outputs = []
        engine.trigger_alarm_output = lambda d, o, a=0: outputs.append((d, o))
        base = {"time": "2026-11-26T08:00:00", "device_name": "cam", "channel": "1", "raw_type": "ANPR",
                "event_type": "anpr_any", "category": "vehicle", "state": "active", "details": {}}
        engine.handle_event(dict(base, device_id=self.cams[0], plate="А123ВС77"))
        self.assertEqual(outputs, [(self.cams[0], 1)])
        engine.handle_event(dict(base, device_id=self.cams[0], plate="О000ОО00"))
        with storage.database() as db:
            types = [r[0] for r in db.execute("SELECT event_type FROM events")]
        self.assertIn("parking_denied", types)
        engine.patrol_tick()   # без маршрутов — ничего
