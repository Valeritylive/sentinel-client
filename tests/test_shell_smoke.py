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
        decision = self.win._apply_adaptive(1600)
        self.assertTrue(decision.left and decision.right)

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
