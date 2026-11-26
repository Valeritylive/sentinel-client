import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import gtk_stub

gtk_stub.install()
import sentinel_alarms as alarms
import sentinel_layouts as layouts
import sentinel_people as people
import sentinel_storage as storage
import sentinel_views as views
import app as app_module


class ShellSmoke(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name) / "s"
        for name, value in (("DATA", data), ("DB_PATH", data / "d.sqlite3")):
            p = patch.object(storage, name, value); p.start(); self.addCleanup(p.stop)
        self.win = app_module.SentinelWindow(None)

    def _add(self, host, channels, name=None, category="video", doors=0):
        with storage.database() as db:
            for ch in channels:
                db.execute("INSERT INTO devices(name,host,channel,host_name,category,door_count) VALUES(?,?,?,?,?,?)",
                           (f"{host} CH {ch}", host, ch, name or "", category, doors))
        self.win.refresh()


class LayoutSmoke(ShellSmoke):
    def test_starts_with_one_view_tab_and_grid(self):
        self.assertEqual(len(self.win.view_tabs.tabs), 1)
        self.assertEqual(self.win.view_tabs.current().name, "Новый вид 1")
        self.assertEqual(len(self.win.video_slots), 4)
        self.assertEqual(self.win.stack.get_visible_child_name(), "overview")

    def test_modules_open_inner_window_and_close(self):
        w = self.win
        for key, page in (("playback", "archive"), ("events", "alarms"), ("emap", "emap"),
                          ("summary", "dashboard"), ("local_files", "local_files"), ("access", "access")):
            w._show_module(key)
            self.assertEqual(w.stack.get_visible_child_name(), page)
        w._show_module("main_view")
        self.assertEqual(w.stack.get_visible_child_name(), "overview")

    def test_adaptive_layout_applies(self):
        decision = self.win._apply_adaptive(700)
        self.assertFalse(decision.left or decision.right)
        self.assertFalse(self.win.left_revealer.get_visible() or self.win.right_revealer.get_visible())
        decision = self.win._apply_adaptive(1600)
        self.assertTrue(decision.left and decision.right)

    def test_bottom_panel_toggle_is_remembered(self):
        self.assertTrue(self.win.playback_bar.get_visible())
        self.win.toggle_bottom_panel()
        self.assertFalse(self.win.playback_bar.get_visible())
        self.assertEqual(self.win.bottom_handle.get_label(), "˄")
        self.assertEqual(storage.get_setting("bottom_bar_visible"), "0")
        self.win.toggle_bottom_panel()
        self.assertTrue(self.win.playback_bar.get_visible())

    def test_notify_goes_to_bell(self):
        self.win.notify("проверка", error=True)
        self.assertEqual(self.win.notifications[0][1], "проверка")


class ViewTabSmoke(ShellSmoke):
    def test_new_switch_close_and_dirty(self):
        w = self.win
        first = w.view_tabs.current()
        w._set_division("s9")
        self.assertTrue(first.dirty)
        second = w._new_view_tab()
        self.assertEqual(w.layout_count, 4)
        w.switch_view_tab(first.key)
        self.assertEqual(w.layout_count, 9)
        w.close_view_tab(first.key)
        self.assertIs(w.view_tabs.current(), second)

    def test_save_view_and_reopen_with_web_cell(self):
        w = self.win
        page_id = views.add_web_page("Погода", "example.org")
        w._place_resource(1, {"kind": "web", "url": "https://example.org", "name": "Погода"})
        with patch.object(w, "_ask_name_and_group", lambda title, initial, groups, cb: cb("DVS", -1)):
            w.save_view_tab()
        saved = views.load_views()
        self.assertEqual(saved[0]["name"], "DVS")
        self.assertFalse(w.view_tabs.current().dirty)
        w._new_view_tab()
        self.assertEqual(w.slot_resources, {})
        w.load_custom_view(saved[0])
        self.assertEqual(w.slot_resources[1]["url"], "https://example.org")
        self.assertTrue(page_id)

    def test_divisions_apply_cells(self):
        w = self.win
        for key in ("s6", "s13", "w7", "s1", "s64"):
            self.assertTrue(w._set_division(key))
            d = layouts.DIVISIONS[key]
            self.assertEqual(len(w.video_slots), d.count)
            self.assertEqual(w._current_cells(), list(d.cells))
        self.assertEqual(w.layout_button.get_label(), "▦ 64 окна")
        w._set_division("s1")
        self.assertEqual(w.layout_button.get_label(), "▦ 1 окно")
        w._set_division("s25")
        self.assertEqual(w.layout_button.get_label(), "▦ 25 окон")
        self.assertFalse(w._set_division("nonexistent"))

    def test_desired_stream(self):
        w = self.win
        w._set_division("s1")
        self.assertEqual(w._desired_stream(0), "main")
        w._set_division("s16")
        self.assertEqual(w._desired_stream(0), "sub")
        w._set_stream_mode("third")
        self.assertEqual(w._desired_stream(0), "third")


class ResourceSmoke(ShellSmoke):
    def test_tree_with_doors_and_rename(self):
        self._add("10.0.0.1", [1, 2], "NVR")
        self._add("10.0.0.9", [1], "Домофон", "access", 2)
        did = next(d["id"] for d in self.win.devices if d["host"] == "10.0.0.1")
        with patch.object(self.win, "_ask_text", lambda title, initial, cb: cb("Новое имя")):
            self.win.rename_camera_dialog(next(d for d in self.win.devices if d["id"] == did))
        self.assertIn("Новое имя", [d["name"] for d in self.win.devices])
        controller = next(d for d in self.win.devices if d["category"] == "access")
        self.win._place_door(controller, 2)
        self.assertEqual(self.win.slot_resources[self.win.selected_slot]["door"], 2)

    def test_active_markers_follow_open_streams(self):
        self._add("10.0.0.1", [1, 2], "NVR")
        first, second = self.win.devices
        markers = {frozenset(ids): label for label, ids in self.win.active_markers}
        cam1, cam2 = markers[frozenset({first["id"]})], markers[frozenset({second["id"]})]
        nvr = markers[frozenset({first["id"], second["id"]})]
        self.assertTrue(cam1.has_css_class("cam-idle"))
        self.win.streams[0] = {"base_device": first}
        self.win._update_active_markers()
        self.assertTrue(cam1.has_css_class("cam-active") and nvr.has_css_class("cam-active"))
        self.assertTrue(cam2.has_css_class("cam-idle"))
        self.win.streams.clear()
        self.win._update_active_markers()
        self.assertTrue(cam1.has_css_class("cam-idle") and not cam1.has_css_class("cam-active"))

    def test_store_probed_access_device(self):
        device = {"name": "Панель", "host": "10.0.0.5", "sdk_port": 8000, "http_port": 80, "rtsp_port": 554,
                  "stream": "main", "protocol": "hikvision", "category": "access", "door_count": 2}
        with patch.object(app_module, "save_credentials"):
            self.win._store_probed_device(device, {"username": "u", "password": "p"}, [1])
        self.win.refresh()
        self.assertEqual((self.win.devices[0]["category"], self.win.devices[0]["door_count"]), ("access", 2))

    def test_delete_devices(self):
        self._add("10.0.0.1", [1, 2])
        self.win.delete_devices([d["id"] for d in self.win.devices])
        self.assertEqual(self.win.devices, [])


class EventSmoke(ShellSmoke):
    def test_event_triggers_rule_and_panel(self):
        self._add("10.0.0.1", [1], "Gate")
        list_id = people.load_vehicle_lists()[0]["id"]
        people.save_vehicle({"plate": "AB123", "list_id": list_id})
        alarms.save_rule({"name": "Чужой", "event_type": "anpr_mismatched", "linkage": {"popup": False}})
        device = self.win.devices[0]
        event = alarms.parse_event_xml(b"<EventNotificationAlert><eventType>ANPR</eventType>"
                                       b"<eventState>active</eventState><licensePlate>ZZ999</licensePlate>"
                                       b"</EventNotificationAlert>", device)
        self.win.handle_event(event)
        self.win.handle_event(dict(event))  # повтор в течение 10 с склеивается
        self.assertEqual(alarms.unacknowledged_count(), 1)
        record = alarms.load_alarm_records()[0]
        self.win.show_alarm_details(record["id"])
        self.win.trigger_user_event()
        self.assertEqual(len(alarms.load_events()), 2)

    def test_reports_and_event_center(self):
        self.win.open_report("alarms_by_type")
        self.assertEqual(self.win.stack.get_visible_child_name(), "dashboard")
        self.win._show_module("events")
        self.win.event_center_kind.set_selected = lambda *_: None
        self.win._render_event_center()


class SystemSmoke(ShellSmoke):
    def test_system_window_builds_and_sections_refresh(self):
        self._add("10.0.0.1", [1, 2], "NVR")
        self.win.open_system("devices")
        system = self.win.system_window
        for key in ("devices", "person", "vehicle", "access", "event", "accounts"):
            system._show(key)
        system.show_device_details(self.win.devices[0]["id"])
        system.show_recording_config(self.win.devices[0])
        system.edit_person(None)
        system.edit_vehicle(None)
        system.edit_level(None)
        system.edit_rule(None)
        system.manage_schedules()
        self.win.open_system("config", "storage")

    def test_shortcuts_run(self):
        for key in views.load_shortcuts():
            self.win.run_shortcut(key)


class AddDialogSmoke(ShellSmoke):
    def test_add_dialog_builds(self):
        self.win.add_device()


if __name__ == "__main__":
    unittest.main()


class TileToolbarSmoke(ShellSmoke):
    def _fake_stream(self, index=0):
        state = {"device": {"name": "Cam", "host": "10.0.0.5", "channel": 1, "stream": "main",
                            "protocol": "hikvision", "http_port": 80},
                 "base_device": {"id": 1, "name": "Cam", "host": "10.0.0.5", "channel": 1, "http_port": 80},
                 "credentials": {"username": "u", "password": "p"}, "playback": False,
                 "digital_zoom": 1.0, "digital_x": 0.5, "digital_y": 0.5, "cloud": None}
        self.win.streams[index] = state
        return state

    def test_every_tile_has_toolbar_and_ptz_pad(self):
        for slot in self.win.video_slots:
            self.assertIn("toolbar", slot)
            self.assertIn("ptz_pad", slot)
            self.assertEqual(set(slot["tool_buttons"]),
                             {"capture", "clip", "audio", "talk", "zoom", "stream", "ptz", "close"})

    def test_zoom_button_toggles_digital_zoom(self):
        state = self._fake_stream()
        self.win._tile_tool(0, "zoom")
        self.assertEqual(state["digital_zoom"], 2.0)
        self.win._tile_tool(0, "zoom")
        self.assertEqual(state["digital_zoom"], 1.0)

    def test_ptz_mode_click_sends_position3d(self):
        self._fake_stream()
        sent = []
        with patch.object(self.win, "send_position3d", lambda i, s, e=None: sent.append((i, s, e))), \
             patch.object(self.win, "_digital_zoom_region", lambda *a: sent.append("digital")):
            self.win._tile_drag_end(0, 100, 50, 0, 0, 400, 200)
            self.assertEqual(sent, ["digital"])
            self.win.toggle_tile_ptz(0)
            self.win._tile_drag_end(0, 100, 50, 0, 0, 400, 200)
            self.win._tile_drag_end(0, 100, 50, 200, 100, 400, 200)
        self.assertEqual(sent[1], (0, (0.25, 0.25), None))
        self.assertEqual(sent[2], (0, (0.25, 0.25), (0.75, 0.75)))
        self.win.toggle_tile_ptz(0)
        self.assertNotIn(0, self.win.tile_ptz)


class RoleSmoke(ShellSmoke):
    def test_board_opens_and_renders_default_widgets(self):
        self.win._show_module("board")
        self.assertEqual(self.win.stack.get_visible_child_name(), "board")
        self.assertEqual(len(self.win.board_flow._state["children"]), 6)

    def test_operator_menu_and_sections_follow_role(self):
        import sentinel_security as security
        operator = security.Session(2, "op", "Оператор", frozenset(security.BUILTIN_ROLES["Оператор"]), None)
        self.win.session = operator
        self.win.apply_role()
        titles = [b.get_label() for b in self.win.modules_menu_box._state["children"] if hasattr(b, "get_label")]
        self.assertIn("Поиск записей", titles)
        self.assertNotIn("Настройка устройств…", titles)
        no_maps = security.Session(3, "hr", "Кадры", frozenset({"persons"}), None)
        self.win.session = no_maps
        self.win.apply_role()
        self.assertFalse(self.win.left_sections["maps"].get_visible())
        self.assertFalse(self.win.left_sections["ptz"].get_visible())
        self.win._show_module("emap")
        self.assertNotEqual(self.win.stack.get_visible_child_name(), "emap")


class ServerViewsSmoke(ShellSmoke):
    def test_save_and_open_server_view(self):
        self._add("10.0.0.5", [1, 2])
        calls = []

        class Client:
            def create_view(self, name, public, layout):
                calls.append((name, public, layout))
                return 42

            def views(self):
                return {"items": [], "can_publish": True}

        class Link:
            connected = True
            client = Client()

        self.win.server_link = Link()
        self.assertEqual(self.win._server_view_targets(), ["local", "private", "public"])
        tab = self.win.view_tabs.current()
        tab.mapping = [{"slot": 0, "kind": "camera", "device_id": self.win.devices[1]["id"]}]
        with patch.object(self.win, "_server_call", lambda work, done, *_a: done(work())), \
             patch.object(self.win, "_capture_current_tab", lambda: None):
            self.win.save_tab_to_server(tab, "Общий", True)
        self.assertEqual(tab.server_view_id, 42)
        self.assertEqual(calls[0][2]["cells"][0]["device"], {"host": "10.0.0.5", "port": 8000, "channel": 2})
        view = {"id": 43, "name": "Чужой", "public": True, "owner": "admin", "editable": False,
                "layout": calls[0][2]}
        self.win.open_server_view(view)
        opened = self.win.view_tabs.current()
        self.assertEqual(opened.server_view_id, 43)
        self.assertEqual(opened.mapping[0]["device_id"], self.win.devices[1]["id"])


class AccessCenterSmoke(ShellSmoke):
    def test_access_center_tabs_render_and_feed(self):
        import sentinel_access_rules as ar
        import sentinel_visitors as visitors
        self._add("10.0.0.9", [1], "Проходная", category="access", doors=2)
        w = self.win
        ctrl = next(d for d in w.devices if d["category"] == "access")
        w._show_module("access")
        for page in range(5):
            w.access_notebook.get_current_page = lambda p=page: p
            w.refresh_access_center()
        ar.save_rule(ar.INTERLOCK, "Шлюз", ctrl["id"], {"groups": [[1, 2]]}, door_count=2)
        w._update_access_apply_indicator()
        self.assertIn("правил: 1", w.access_apply_label.get_text())
        pid = people.save_person({"name": "Иван", "employee_no": "7"})
        ar.save_rule(ar.FIRST_PERSON, "Утро", ctrl["id"], {"door": 1, "person_ids": [pid], "until": "23:59"},
                     door_count=2)
        sent = []
        w.door_command = lambda d, n, c, quiet=False: sent.append((n, c))
        w._access_credentials_for = lambda d: {"username": "a", "password": "b"}
        w.access_notebook.get_current_page = lambda: 0
        event = {"time": "2026-11-26T08:00:00", "device_id": ctrl["id"], "device_name": "Проходная",
                 "channel": "1", "raw_type": "AccessControllerEvent", "event_type": "access", "category": "event",
                 "state": "active", "plate": "", "details": {"employeeNoString": "7", "doorNo": "1", "minor": "75"}}
        w.handle_event(event)
        self.assertEqual(sent, [(1, "always_open")])
        self.assertEqual(w.access_feed[0]["who"], "Иван")
        self.assertIn("первый вошедший", w.access_feed[0]["note"])

    def test_visitor_check_in_out_from_ui(self):
        import sentinel_visitors as visitors
        w = self.win
        w._show_module("access")
        visitors.set_approval_required(False)
        vid = visitors.register({"name": "Гость"}, "admin")
        removed = []
        w.remove_person_from_devices = lambda emp, devs: removed.append(emp)
        w.check_in_visitor(vid)
        self.assertEqual(visitors.get(vid)["status"], visitors.INSIDE)
        w.print_badge(vid)
        self.assertTrue((storage.DATA / "badges" / f"badge_{visitors.get(vid)['badge']}.html").is_file())
        w.check_out_visitor(vid)
        self.assertEqual(removed, [f"V{vid}"])

    def test_attendance_render(self):
        w = self.win
        w._show_module("access")
        people.save_person({"name": "Анна", "employee_no": "21"})
        w.render_attendance()
        self.assertTrue(w.att_records)
        w.att_mode.get_selected = lambda: 1
        w.render_attendance()


class Phase4Smoke(ShellSmoke):
    def test_modules_render(self):
        w = self.win
        for key in ("parking", "wall", "patrol", "evidence"):
            w._show_module(key)
            self.assertEqual(w.stack.get_visible_child_name(), key)

    def test_parking_plate_opens_barrier(self):
        import sentinel_parking as parking
        self._add("10.0.0.5", [1, 2])
        w = self.win
        cams = [d["id"] for d in w.devices]
        staff = people.add_vehicle_list("Сотрудники")
        people.save_vehicle({"plate": "А123ВС77", "list_id": staff})
        parking.save_lot({"name": "P", "mode": parking.MODE_LISTS, "allow_lists": [staff],
                          "entry_cameras": [cams[0]], "exit_cameras": [cams[1]],
                          "barrier": {"device_id": cams[0], "output": 2, "auto_close": 3}})
        opened = []
        w.trigger_alarm_output = lambda d, o, a=0, quiet=False: opened.append((d, o, a))
        base = {"time": "2026-11-26T08:00:00", "device_name": "cam", "channel": "1", "raw_type": "ANPR",
                "event_type": "anpr_any", "category": "vehicle", "state": "active", "details": {}}
        w.handle_event(dict(base, device_id=cams[0], plate="А123ВС77"))
        self.assertEqual(opened, [(cams[0], 2, 3)])
        w.handle_event(dict(base, device_id=cams[0], plate="Е001ЕЕ77"))
        self.assertEqual(len(opened), 1)
        self.assertEqual(parking.day_stats(parking.load_lots()[0]["id"], "2026-11-26")["denied"], 1)
        w._show_module("parking")

    def test_wall_output_and_alarm(self):
        import sentinel_videowall as wall
        self._add("10.0.0.5", [1, 2])
        w = self.win
        cams = [d["id"] for d in w.devices]
        wid = wall.save_wall("Стена", 1, 2, alarm_screen=1, all_alarms=True)
        wall.save_screens(wid, wall.place_cameras(wall.get_wall(wid)["screens"], 0, cams))
        w.wall_choice.get_selected = lambda: 0
        w._show_module("wall")
        started = []

        class FakeWindow:
            def __init__(self, *a):
                self.session_credentials, self.streams = {}, {}
            def connect(self, *a): pass
            def present(self): pass
            def close(self): pass
            def get_visible(self): return True
            def start_assignments(self, assignments, *a): started.append(len(assignments))
            def _start_preview(self, slot, device, creds): started.append(("alarm", slot, device["id"]))

        import app as app_module
        with patch.object(app_module, "MonitorWindow", FakeWindow):
            w.toggle_wall_output()
        self.assertEqual(started[:2], [2, 0])
        w.wall_show_alarm(device_id=cams[1], any_alarm=True)
        self.assertEqual(started[-1], ("alarm", 0, cams[1]))

    def test_evidence_from_alarm(self):
        import sentinel_evidence as evidence
        w = self.win
        cid = evidence.create_case("Инцидент")
        w._alarm_to_case(cid, {"id": 5, "rule_name": "Движение", "event_type": "motion", "time": "t",
                               "device_name": "cam", "picture_path": ""})
        self.assertEqual(evidence.load_items(cid)[0]["kind"], "alarm")
        w._show_module("evidence")
        w.open_case(cid)
