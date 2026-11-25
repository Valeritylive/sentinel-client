import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import gtk_stub

gtk_stub.install()
import sentinel_layouts as layouts
import sentinel_storage as storage
import app as app_module


class ShellSmoke(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name) / "s"
        for name, value in (("DATA", data), ("DB_PATH", data / "d.sqlite3")):
            p = patch.object(storage, name, value); p.start(); self.addCleanup(p.stop)
        self.win = app_module.SentinelWindow(None)

    def test_starts_on_home(self):
        self.assertEqual(self.win.stack.get_visible_child_name(), "home")
        self.assertTrue(self.win.nav_buttons["home"].get_active())

    def test_open_and_close_tabs(self):
        w = self.win
        w._show_module("main_view")
        self.assertEqual(w.stack.get_visible_child_name(), "overview")
        w._show_module("devices")
        self.assertEqual(w.stack.get_visible_child_name(), "devices")
        self.assertFalse(w.nav_buttons["main_view"].get_active())
        w._close_tab("devices")
        self.assertEqual(w.stack.get_visible_child_name(), "overview")
        w._close_tab("main_view")
        self.assertEqual(w.stack.get_visible_child_name(), "home")

    def test_legacy_page_names_and_stub(self):
        self.win._show_module("archive")
        self.assertEqual(self.win.stack.get_visible_child_name(), "archive")
        self.win._show_module("logs")
        self.assertEqual(self.win.stack.get_visible_child_name(), "stub:logs")

    def test_tab_click_switches(self):
        w = self.win
        w._show_module("main_view"); w._show_module("devices")
        w.nav_buttons["main_view"].set_active(True)
        self.assertEqual(w.stack.get_visible_child_name(), "overview")
        w.nav_buttons["home"].set_active(True)
        self.assertEqual(w.stack.get_visible_child_name(), "home")


if __name__ == "__main__":
    unittest.main()


class DeviceManagementSmoke(ShellSmoke):
    def _add(self, host, channels, name=None):
        with storage.database() as db:
            for ch in channels:
                db.execute("INSERT INTO devices(name,host,channel,host_name) VALUES(?,?,?,?)",
                           (f"{host} CH {ch}", host, ch, name or ""))

    def test_rows_total_and_filter(self):
        self._add("10.0.0.1", [1, 2, 3], "Склад")
        self._add("10.0.0.2", [1])
        self.win.refresh()
        self.assertEqual(self.win.device_total_label.get_text(), "Всего: 2")
        self.assertEqual(len(self.win.device_row_checks), 2)
        self.assertEqual(sorted(len(ids) for _, ids in self.win.device_row_checks), [1, 3])

    def test_groups_by_device_and_delete(self):
        self._add("10.0.0.1", [1, 2], "Склад")
        self._add("10.0.0.2", [1])
        self.win.refresh()
        self.win._create_groups_by_device()
        self.assertEqual({g["name"] for g in self.win.groups}, {"Склад", "10.0.0.2"})
        self.assertTrue(all(d["group_id"] is not None for d in self.win.devices))
        gid = next(g["id"] for g in self.win.groups if g["name"] == "Склад")
        self.win.selected_group_id = gid
        self.win._render_groups()
        app_module.delete_group(gid)
        self.win.refresh()
        self.assertIsNone(self.win.selected_group_id)

    def test_store_probed_device(self):
        device = {"name": "NVR", "host": "10.0.0.5", "sdk_port": 8000, "http_port": 80, "rtsp_port": 554,
                  "stream": "main", "protocol": "custom", "group_name": "Офис",
                  "channel_names": {1: "Вход"}, "device_model": "DS-7", "serial_no": "S1"}
        with patch.object(app_module, "save_credentials"):
            ids = self.win._store_probed_device(device, {"username": "u", "password": "p"}, [1, 2])
        self.assertEqual(len(ids), 2)
        self.win.refresh()
        names = sorted(d["name"] for d in self.win.devices)
        self.assertEqual(names, ["NVR CH 2", "Вход"])
        self.assertEqual(self.win.devices[0]["device_model"], "DS-7")
        self.assertEqual(self.win.groups[0]["name"], "Офис")
        with self.assertRaises(ValueError):
            self.win._store_probed_device(device, {}, [])

    def test_delete_selected(self):
        self._add("10.0.0.1", [1, 2])
        self.win.refresh()
        check, ids = self.win.device_row_checks[0]
        check.set_active(True)
        self.win.delete_devices(ids)
        self.assertEqual(self.win.devices, [])


class MainViewSmoke(ShellSmoke):
    def test_divisions_apply_cells(self):
        w = self.win
        for key in ("s6", "s13", "w7", "s1", "s64"):
            self.assertTrue(w._set_division(key))
            d = layouts.DIVISIONS[key]
            self.assertEqual(w.layout_count, d.count)
            self.assertEqual(len(w.video_slots), d.count)
            self.assertEqual(w._current_cells(), list(d.cells))
        self.assertFalse(w._set_division("nonexistent"))

    def test_set_layout_resets_division(self):
        w = self.win
        w._set_division("s6")
        w._set_layout(None, 12, 4, 3)
        self.assertEqual(w.division, "")
        self.assertEqual(len(w._current_cells()), 12)

    def test_desired_stream(self):
        w = self.win
        w._set_division("s1")
        self.assertEqual(w._desired_stream(0), "main")
        w._set_division("s16")
        self.assertEqual(w._desired_stream(0), "sub")
        w._set_stream_mode("third")
        self.assertEqual(w._desired_stream(0), "third")
        w._set_stream_mode("bogus")
        self.assertEqual(w.stream_mode, "third")

    def test_custom_division_lookup_and_saved_view(self):
        w = self.win
        storage.save_custom_division("мой", 2, 2, [(0, 0, 2, 1), (0, 1, 1, 1), (1, 1, 1, 1)])
        self.assertEqual(w._lookup_division("c:мой").count, 3)
        self.assertTrue(w._set_division("c:мой"))
        self.assertEqual(w.layout_count, 3)
        view = {"layout": 6, "mapping": "[]", "division": "s6", "grid_columns": 3, "grid_rows": 3}
        w.load_custom_view(view)
        self.assertEqual(w.division, "s6")
        self.assertEqual(w.layout_count, 6)
        old = {"layout": 4, "mapping": "[]", "grid_columns": 2, "grid_rows": 2}
        w.load_custom_view(old)
        self.assertEqual(w.division, "")
        self.assertEqual(w.layout_count, 4)

    def test_resource_tree_and_rename(self):
        with storage.database() as db:
            db.execute("INSERT INTO devices(name,host,channel) VALUES('A','10.0.0.1',1),('B','10.0.0.1',2)")
        self.win.refresh()
        did = self.win.devices[0]["id"]
        with patch.object(self.win, "_ask_text", lambda title, initial, cb: cb("Новое имя")):
            self.win.rename_camera_dialog(self.win.devices[0])
        self.assertIn("Новое имя", [d["name"] for d in self.win.devices])
        self.assertEqual(len(self.win.devices), 2)
        self.assertEqual(did, self.win.devices[0]["id"])


class AddDialogSmoke(ShellSmoke):
    def test_add_dialog_builds(self):
        self.win.add_device()
