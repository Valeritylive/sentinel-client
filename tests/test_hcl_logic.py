import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sentinel_alarms as alarms
import sentinel_discovery as discovery
import sentinel_people as people
import sentinel_recording as recording
import sentinel_reports as reports
import sentinel_security as security
import sentinel_storage as storage
import sentinel_timeline as tl
import sentinel_views as views
from test_storage import _TempDb


class ViewTabsTests(_TempDb):
    def test_new_tabs_close_and_dirty_title(self):
        tabs = views.ViewTabs()
        first = tabs.new_tab()
        second = tabs.new_tab()
        self.assertEqual((first.name, second.name), ("Новый вид 1", "Новый вид 2"))
        tabs.mark_dirty()
        self.assertEqual(tabs.current().title, "Новый вид 2*")
        self.assertIs(tabs.close(second.key), first)
        replacement = tabs.close(first.key)
        self.assertEqual(len(tabs.tabs), 1)
        self.assertEqual(replacement.name, "Новый вид 3")

    def test_save_and_reopen_view_with_mixed_cells(self):
        tabs = views.ViewTabs()
        tab = tabs.new_tab(division="s4", layout=4, grid_columns=2, grid_rows=2,
                           mapping=[{"slot": 0, "device_id": 5}, {"slot": 1, "kind": "web", "url": "https://x.org"},
                                    {"slot": 1, "device_id": 6}, {"slot": 2, "kind": "door", "device_id": 7},
                                    {"slot": "bad"}])
        group = views.add_view_group("Склад")
        view_id = views.save_view("DVS", tab, group)
        saved = views.load_views()[0]
        reopened, created = tabs.open_saved(saved)
        self.assertTrue(created)
        self.assertEqual([m["slot"] for m in reopened.mapping], [0, 1, 2])
        self.assertEqual(reopened.mapping[2]["door"], 1)
        self.assertEqual(tabs.open_saved(saved), (reopened, False))
        tree = views.view_tree(views.load_view_groups(), views.load_views())
        self.assertEqual(tree[0][1][0]["id"], view_id)
        views.delete_view_group(group)
        self.assertIsNone(views.load_views()[0]["group_id"])

    def test_shortcuts_roundtrip_and_url(self):
        self.assertEqual(views.load_shortcuts(), list(views.DEFAULT_SHORTCUTS))
        self.assertEqual(views.save_shortcuts(["add_user", "bogus", "add_user"]), ["add_user"])
        self.assertEqual(views.load_shortcuts(), ["add_user"])
        self.assertEqual(views.normalize_url("dvs.co.uk"), "https://dvs.co.uk")
        with self.assertRaises(ValueError):
            views.normalize_url("file:///etc/passwd")

    def test_adaptive(self):
        wide = views.adaptive_layout(1600)
        self.assertTrue(wide.left and wide.right)
        mid = views.adaptive_layout(1000)
        self.assertTrue(mid.left)
        self.assertFalse(mid.right)
        narrow = views.adaptive_layout(700)
        self.assertFalse(narrow.left or narrow.right)
        self.assertTrue(narrow.compact_bar)
        self.assertFalse(views.adaptive_layout(1600, user_left=False).left)


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.center = datetime(2026, 8, 26, 12, 0).astimezone()
        self.model = tl.TimelineModel(self.center)

    def test_mapping_roundtrip(self):
        x = self.model.x_for(self.center, 1000)
        self.assertAlmostEqual(x, 500)
        self.assertEqual(self.model.time_at(500, 1000), self.center)

    def test_zoom_keeps_anchor(self):
        anchor = self.center + timedelta(hours=3)
        before = self.model.x_for(anchor, 1000) / 1000
        self.model.zoom(+1, anchor)
        self.assertEqual(self.model.span, 12 * 3600)
        self.assertAlmostEqual(self.model.x_for(anchor, 1000) / 1000, before, places=3)
        for _ in range(20):
            self.model.zoom(+1)
        self.assertEqual(self.model.span, 600)

    def test_ticks_and_segments(self):
        ticks = self.model.ticks()
        majors = [m for m, major in ticks if major]
        self.assertTrue(all(m.hour % 2 == 0 and m.minute == 0 for m in majors))
        self.model.segments = tl.merge_segments([
            tl.Segment(self.center - timedelta(hours=30), self.center - timedelta(hours=1)),
            tl.Segment(self.center - timedelta(hours=1, seconds=-1), self.center),
            tl.Segment(self.center - timedelta(minutes=5), self.center - timedelta(minutes=4), "event")])
        self.assertEqual(len(self.model.segments), 2)
        visible = self.model.visible_segments()
        self.assertEqual(visible[0].start, self.model.start)
        self.assertEqual(self.model.segment_at(self.center - timedelta(minutes=4, seconds=30)).kind, "event")
        self.model.filter = "vehicle"
        self.assertEqual(self.model.visible_segments(), [])


class AlarmTests(_TempDb):
    ANPR = b"""<EventNotificationAlert><ipAddress>10.0.0.9</ipAddress><channelID>1</channelID>
      <dateTime>2026-08-26T07:49:15+01:00</dateTime><eventType>ANPR</eventType><eventState>active</eventState>
      <ANPR><licensePlate>wp 68 vdt</licensePlate><vehicleType>sedan</vehicleType></ANPR></EventNotificationAlert>"""

    def test_parse_and_heartbeat(self):
        device = {"id": 3, "name": "Gate", "host": "10.0.0.9"}
        event = alarms.parse_event_xml(self.ANPR, device)
        self.assertEqual((event["category"], event["plate"], event["channel"]), ("vehicle", "wp 68 vdt", "1"))
        heartbeat = b"<EventNotificationAlert><eventType>videoloss</eventType><eventState>inactive</eventState></EventNotificationAlert>"
        self.assertIsNone(alarms.parse_event_xml(heartbeat, device))
        motion = alarms.parse_event_xml(b'{"eventType":"VMD","eventState":"active","channelID":2}', device)
        self.assertEqual(motion["event_type"], "motion")

    def test_rules_matched_and_mismatched(self):
        list_id = people.load_vehicle_lists()[0]["id"]
        other = people.add_vehicle_list("Склад")
        people.save_vehicle({"plate": "WP68 VDT", "list_id": list_id})
        matched = alarms.save_rule({"name": "Свой", "event_type": "anpr_matched", "priority": "high",
                                    "vehicle_lists": [list_id],
                                    "linkage": {"alarm_outputs": [{"device_id": 3, "output": 1, "auto_close": 10}]}})
        alarms.save_rule({"name": "Чужой", "event_type": "anpr_mismatched", "vehicle_lists": "all"})
        alarms.save_rule({"name": "Только склад", "event_type": "anpr_matched", "vehicle_lists": [other]})
        rules = alarms.load_rules(True)
        event = alarms.parse_event_xml(self.ANPR, {"id": 3, "name": "Gate"})
        fired = alarms.match_rules(event, rules, people.vehicle_index())
        self.assertEqual([r["name"] for r in fired], ["Свой"])
        event["plate"] = "AB1234"
        self.assertEqual([r["name"] for r in alarms.match_rules(event, rules, people.vehicle_index())], ["Чужой"])
        plan = alarms.linkage_plan(next(r for r in rules if r["id"] == matched), event)
        self.assertEqual([p[0] for p in plan], ["alarm_output", "popup"])
        self.assertIn("<outputState>high</outputState>", alarms.alarm_output_xml(True))

    def test_schedule_filter_and_source_filter(self):
        weekday = people.save_schedule("Будни", people.weekdays_only(9 * 60, 10 * 60))
        rule = {"id": 1, "name": "x", "event_type": "motion", "enabled": 1, "sources": [7],
                "schedule_id": weekday}
        event = {"event_type": "motion", "category": "event", "device_id": 7, "state": "active"}
        monday_9 = datetime(2026, 8, 24, 9, 30).astimezone()
        sunday = datetime(2026, 8, 30, 9, 30).astimezone()
        self.assertTrue(alarms.match_rules(event, [rule], {}, people.get_schedule, monday_9))
        self.assertFalse(alarms.match_rules(event, [rule], {}, people.get_schedule, sunday))
        self.assertFalse(alarms.match_rules(dict(event, device_id=8), [rule], {}, people.get_schedule, monday_9))

    def test_records_acknowledge_and_purge(self):
        rule = {"id": 1, "name": "x", "event_type": "motion", "priority": "high"}
        rid = alarms.create_alarm_record(rule, {"time": "2026-01-01T00:00:00+00:00", "device_name": "c"})
        alarms.create_alarm_record(rule, {"time": datetime.now().astimezone().isoformat(), "device_name": "c"})
        self.assertEqual(alarms.unacknowledged_count(), 2)
        alarms.update_alarm_record(rid, category="Проверено", status="hack")
        alarms.acknowledge([rid], "admin", "ok")
        record = alarms.get_alarm_record(rid)
        self.assertEqual((record["status"], record["remarks"], record["category"]),
                         ("acknowledged", "ok", "Проверено"))
        self.assertEqual(alarms.purge_older_than(now=datetime(2026, 8, 1).astimezone())["alarm_records"], 1)
        self.assertEqual(alarms.load_operation_log()[0]["action"], "Подтверждение тревоги")

    def test_bad_rule(self):
        with self.assertRaises(ValueError):
            alarms.save_rule({"name": "", "event_type": "motion"})
        with self.assertRaises(ValueError):
            alarms.save_rule({"name": "a", "event_type": "nope"})


class PeopleTests(_TempDb):
    def test_schedule_cells_roundtrip(self):
        cells = [[False] * 24 for _ in range(7)]
        cells[0][8] = cells[0][9] = cells[6][23] = True
        week = people.week_from_cells(cells)
        self.assertEqual(week[0], [[480, 600]])
        self.assertEqual(people.cells_from_week(week), cells)
        self.assertEqual(people.normalize_week([[[0, 60], [30, 120]]] + [[]] * 6)[0], [[0, 120]])
        self.assertEqual(len(people.load_schedules()), 3)

    def test_persons_levels_and_payloads(self):
        root = people.load_departments()[0]["id"]
        visitors = people.add_department("Посетители")
        pid = people.save_person({"name": "DVS test", "department_id": visitors, "cards": ["123456", "123456"],
                                  "pin": "1234"})
        person = people.load_persons()[0]
        self.assertEqual(person["employee_no"], "1")
        self.assertEqual(person["cards"], ["123456"])
        level = people.save_access_level("Выходные", [{"device_id": 9, "door": 1}, {"device_id": 9, "door": 2}])
        people.assign_levels(department_ids=[visitors], level_ids=[level])
        self.assertEqual(people.person_doors(person), {9: {1, 2}})
        payload = people.user_info_payload(person, [1, 2])
        self.assertEqual(payload["UserInfo"]["doorRight"], "1,2")
        self.assertEqual(payload["UserInfo"]["password"], "1234")
        self.assertEqual(people.card_payload(person, "123456")["CardInfo"]["cardNo"], "123456")
        self.assertEqual(people.apply_summary()["authorized"], 1)
        people.delete_department(visitors)
        self.assertEqual(people.load_persons()[0]["department_id"], root)
        with self.assertRaises(ValueError):
            people.save_person({"name": "x", "pin": "12"})
        people.delete_persons([pid])
        self.assertEqual(people.load_persons(), [])

    def test_csv_roundtrip(self):
        people.save_person({"name": "=cmd", "phone": "+7"})
        out = io.StringIO()
        people.export_persons_csv(people.load_persons(), people.load_departments(), out)
        self.assertIn("'=cmd", out.getvalue())
        added, errors = people.import_persons_csv(io.StringIO(out.getvalue()))
        self.assertEqual((added, errors), (1, []))
        self.assertEqual(len(people.load_persons()), 1)

    def test_vehicles_move_and_index(self):
        main = people.load_vehicle_lists()[0]["id"]
        hq = people.add_vehicle_list("DVS HQ")
        a = people.save_vehicle({"plate": "abcd 1234", "list_id": main})
        people.save_vehicle({"plate": "ABCD-1234", "list_id": hq})
        people.move_vehicles([a], hq)
        self.assertEqual(len(people.load_vehicles(hq)), 1)
        self.assertEqual(people.vehicle_index(), {"ABCD1234": [hq]})
        self.assertEqual(alarms.plate_key("а 123 ВС"), "A123BC")

    def test_door_helpers(self):
        self.assertIn("alwaysOpen", people.door_command_xml("always_open"))
        states = people.parse_door_status(b'{"AcsWorkStatus":{"doorLockStatus":[0,1,4]}}')
        self.assertEqual(people.door_counters(states), {"Открыта": 1, "Закрыта": 1, "Неизвестно": 1})


class SecurityTests(_TempDb):
    def test_hash_and_login_lockout(self):
        self.assertTrue(security.verify_password("Secret123", security.hash_password("Secret123", iterations=1000)))
        security.ensure_admin("Strong12345")
        session = security.authenticate("admin", "Strong12345")
        self.assertTrue(session.can("users"))
        policy = {"max_failed_attempts": 2, "lock_minutes": 5}
        with self.assertRaises(security.LoginError):
            security.authenticate("admin", "bad", policy)
        with self.assertRaisesRegex(security.LoginError, "заблокирована"):
            security.authenticate("admin", "bad", policy)
        with self.assertRaisesRegex(security.LoginError, "заблокирована"):
            security.authenticate("admin", "Strong12345", policy)
        later = datetime.now().astimezone() + timedelta(minutes=6)
        self.assertEqual(security.authenticate("admin", "Strong12345", policy, later).username, "admin")

    def test_operator_role_and_password_rules(self):
        operator = next(r for r in security.load_roles() if r["name"] == "Оператор")
        uid = security.create_user("guard", "Watch2026!", operator["id"])
        session = security.authenticate("guard", "Watch2026!")
        self.assertFalse(session.can("users"))
        with self.assertRaises(ValueError):
            security.create_user("x", "short", operator["id"])
        with self.assertRaises(ValueError):
            security.change_password("guard", "wrong", "Another2026!")
        security.change_password("guard", "Watch2026!", "Another2026!")
        security.update_user(uid, enabled=False)
        with self.assertRaisesRegex(security.LoginError, "отключена"):
            security.authenticate("guard", "Another2026!")
        self.assertEqual(security.password_strength("abc"), 0)
        self.assertEqual(security.password_strength("Abcdefgh1234!"), 3)

    def test_backup_restore(self):
        with storage.database() as db:
            db.execute("INSERT INTO devices(name,host) VALUES('cam','1.1.1.1')")
        target = security.backup_database(self.data_path / "copy")
        with storage.database() as db:
            db.execute("DELETE FROM devices")
        security.restore_database(target)
        self.assertEqual(storage.load_devices()[0]["name"], "cam")
        with self.assertRaises(Exception):
            bogus = self.data_path / "bogus"
            bogus.write_bytes(b"not sqlite")
            security.restore_database(bogus)


class RecordingTests(_TempDb):
    def test_plan_and_segments_and_cleanup(self):
        plan = recording.load_plan(4)
        self.assertFalse(plan.enabled)
        plan.enabled = True
        plan.retention_days = 2
        recording.save_plan(plan)
        self.assertEqual([p.device_id for p in recording.enabled_plans()], [4])
        root = Path(self.temp_dir.name) / "rec"
        now = datetime(2026, 8, 26, 12, 0).astimezone()
        for moment in (now - timedelta(days=3), now - timedelta(minutes=20), now - timedelta(minutes=10)):
            folder = root / "4" / moment.strftime("%Y%m%d")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (moment.strftime(recording.FILE_PATTERN) + ".mkv")).write_bytes(b"x" * 10)
        segments = recording.list_segments(root, 4, now - timedelta(hours=1))
        self.assertEqual(len(segments), 2)
        path, offset = recording.segment_for(root, 4, now - timedelta(minutes=15))
        self.assertEqual((path.name, offset), ((now - timedelta(minutes=20)).strftime(recording.FILE_PATTERN) + ".mkv", 300))
        self.assertEqual(recording.cleanup(root, recording.enabled_plans(), now), 1)
        self.assertEqual(recording.usage(root)["days"], {4: 1})
        cmd = recording.segment_command("ffmpeg", "/tmp/l", root / "4")
        self.assertIn("-segment_time", cmd)
        self.assertFalse(any("@" in part for part in cmd))

    def test_should_record_schedule(self):
        plan = recording.Plan(1, True, "sub", people.weekends_only(), 30, True)
        self.assertTrue(recording.should_record(plan, datetime(2026, 8, 29, 3).astimezone()))
        self.assertFalse(recording.should_record(plan, datetime(2026, 8, 27, 3).astimezone()))


class DiscoveryTests(unittest.TestCase):
    MATCH = b"""<?xml version="1.0" encoding="UTF-8"?><ProbeMatch><Uuid>X</Uuid><Types>inquiry</Types>
      <DeviceType>139913</DeviceType><DeviceDescription>DS-7204HTHI-M1</DeviceDescription>
      <DeviceSN>DS-7204HTHI-M120200101</DeviceSN><CommandPort>8000</CommandPort><HttpPort>80</HttpPort>
      <MAC>aa-bb</MAC><IPv4Address>10.4.234.107</IPv4Address><SoftwareVersion>V4.83.200</SoftwareVersion>
      <Activated>true</Activated><AnalogChannelNum>4</AnalogChannelNum><DigitalChannelNum>2</DigitalChannelNum>
      </ProbeMatch>"""

    def test_parse(self):
        device = discovery.parse_probe_match(self.MATCH)
        self.assertEqual((device["host"], device["model"], device["channels"], device["activated"]),
                         ("10.4.234.107", "DS-7204HTHI-M1", 6, True))
        self.assertIsNone(discovery.parse_probe_match(b"<Probe/>"))
        self.assertIn(b"<Types>inquiry</Types>", discovery.probe_message("abc"))

    def test_discover_with_fake_socket(self):
        match = self.MATCH

        class FakeSocket:
            def __init__(self):
                self.replies = [match, match, b"junk"]
            def setsockopt(self, *a): pass
            def bind(self, *a): pass
            def settimeout(self, *a): pass
            def sendto(self, *a): pass
            def recvfrom(self, _n):
                if self.replies:
                    return self.replies.pop(), ("10.4.234.107", 37020)
                import socket
                raise socket.timeout()
            def close(self): pass

        found = discovery.discover(timeout=0.2, sock_factory=FakeSocket)
        self.assertEqual(len(found), 1)


class ReportTests(_TempDb):
    def test_reports_build(self):
        with storage.database() as db:
            db.execute("INSERT INTO devices(name,host,status) VALUES('c','1.1.1.1','offline')")
        alarms.create_alarm_record({"id": 1, "name": "r", "event_type": "anpr_mismatched", "priority": "high"},
                                   {"time": datetime.now().astimezone().isoformat(), "device_name": "Gate"})
        alarms.store_event({"time": datetime.now().astimezone().isoformat(), "event_type": "anpr_any",
                            "category": "vehicle", "plate": "AB12", "device_name": "Gate"})
        for key in reports.REPORTS:
            headers, rows = reports.build(key, "today")
            self.assertTrue(headers)
        self.assertEqual(reports.build("vehicles", "today")[1][0][2], "AB12")
        self.assertEqual(reports.summary()["offline"], 1)
        out = io.StringIO()
        reports.export_csv(("a",), [("-x",)], out)
        self.assertIn("'-x", out.getvalue())


if __name__ == "__main__":
    unittest.main()
