import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sentinel_shared_views as shared
import sentinel_views as views

LOCAL = [{"id": 7, "host": "10.0.0.5", "sdk_port": 8000, "channel": 1},
         {"id": 8, "host": "10.0.0.5", "sdk_port": 8000, "channel": 2}]
OTHER_CLIENT = [{"id": 31, "host": "10.0.0.5", "sdk_port": 8000, "channel": 2},
                {"id": 30, "host": "10.0.0.5", "sdk_port": 8000, "channel": 1}]


class SharedViews(unittest.TestCase):
    def test_round_trip_between_clients_with_different_ids(self):
        mapping = [{"slot": 0, "kind": "camera", "device_id": 8}, {"slot": 1, "kind": "door", "device_id": 7, "door": 2},
                   {"slot": 2, "kind": "web", "url": "https://example.org", "name": "Сайт"},
                   {"slot": 3, "kind": "map", "map_id": 4}, {"slot": 4, "kind": "camera", "device_id": 99}]
        portable = shared.portable_mapping(mapping, LOCAL)
        self.assertEqual([p["slot"] for p in portable], [0, 1, 2])
        local, missing = shared.local_mapping(portable, OTHER_CLIENT)
        self.assertEqual(missing, 0)
        self.assertEqual(local[0], {"slot": 0, "kind": "camera", "device_id": 31})
        self.assertEqual(local[1], {"slot": 1, "kind": "door", "device_id": 30, "door": 2})
        self.assertEqual(local[2]["url"], "https://example.org")
        _, missing = shared.local_mapping(portable, OTHER_CLIENT[:1])
        self.assertEqual(missing, 1)

    def test_clean_layout_limits(self):
        with self.assertRaises(ValueError):
            shared.clean_layout({"layout": 65})
        clean = shared.clean_layout({"layout": 9, "grid_columns": 99, "cells": [{"slot": -1}, "x"]})
        self.assertEqual(clean["grid_columns"], 16)
        self.assertEqual(clean["cells"], [])

    def test_tab_layout_and_view_values(self):
        tab = views.ViewTab(1, "Вид", division="s9", layout=9, grid_columns=3, grid_rows=3,
                            mapping=[{"slot": 5, "kind": "camera", "device_id": 7}])
        item = {"layout": shared.tab_layout(tab, LOCAL)}
        values, missing = shared.view_values(item, OTHER_CLIENT)
        self.assertEqual(missing, 0)
        self.assertEqual(values["mapping"], [{"slot": 5, "kind": "camera", "device_id": 30}])
        self.assertEqual((values["layout"], values["division"]), (9, "s9"))


if __name__ == "__main__":
    unittest.main()
