import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sentinel_shell as S


class ShellTests(unittest.TestCase):
    def test_catalog_unique_and_pages(self):
        keys = [m.key for m in S.MODULES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(S.page_name("main_view"), "live")
        self.assertEqual(S.page_name("playback"), "playback")
        self.assertIsNone(S.page_name("devices"))
        self.assertFalse(S.is_tab("devices"))
        self.assertEqual(S.page_name(S.HOME), "home")
        self.assertIsNone(S.page_name("zzz"))

    def test_sections_grouped(self):
        names = [n for n, _ in S.sections()]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(sum(len(i) for _, i in S.sections()), len(S.MODULES))

    def test_tabs(self):
        t = S.TabModel()
        t.open("main_view"); t.open("access"); t.open("main_view")
        self.assertEqual(t.tabs, ["main_view", "access"])
        self.assertEqual(t.active, "main_view")
        t.open("access")
        self.assertEqual(t.close("access"), "main_view")
        self.assertEqual(t.close("main_view"), S.HOME)
        self.assertEqual(t.close(S.HOME), S.HOME)
        with self.assertRaises(KeyError):
            t.open("nope")
        with self.assertRaises(KeyError):
            t.open("devices")

    def test_close_inactive_keeps_active(self):
        t = S.TabModel()
        t.open("main_view"); t.open("access")
        self.assertEqual(t.close("main_view"), "access")

    def test_every_module_has_art(self):
        import sentinel_module_art as art
        import xml.etree.ElementTree as ET
        for item in S.MODULES:
            markup = art.svg(item.key, item.section)
            self.assertIsNotNone(markup, item.key)
            ET.fromstring(markup)   # корректный SVG/XML


if __name__ == "__main__":
    unittest.main()
