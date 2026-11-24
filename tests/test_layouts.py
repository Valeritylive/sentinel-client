import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sentinel_layouts as L


class LayoutTests(unittest.TestCase):
    def test_all_divisions_cover_grid(self):
        for d in L.DIVISIONS.values():
            self.assertTrue(L.validate_cells(d.cols, d.rows, d.cells), d.key)

    def test_counts_match_keys(self):
        for d in L.DIVISIONS.values():
            self.assertEqual(d.count, int(d.key[1:]), d.key)

    def test_expected_standard_set(self):
        self.assertEqual([d.count for d in L.STANDARD_DIVISIONS],
                         [1, 4, 6, 8, 9, 13, 16, 25, 36, 64])

    def test_validate_rejects_bad(self):
        self.assertFalse(L.validate_cells(2, 2, [(0, 0, 2, 1)]))
        self.assertFalse(L.validate_cells(2, 2, [(0, 0, 2, 2), (1, 1, 1, 1)]))
        self.assertFalse(L.validate_cells(2, 2, [(0, 0, 3, 1), (0, 1, 2, 1)]))
        self.assertFalse(L.validate_cells(0, 2, []))

    def test_division_for_count(self):
        self.assertEqual(L.division_for_count(5).count, 6)
        self.assertEqual(L.division_for_count(1000).count, 64)
        self.assertTrue(L.division_for_count(5, wide=True).wide)

    def test_get_division_fallback(self):
        self.assertEqual(L.get_division("nope").key, "s4")

    def test_resolve_stream(self):
        self.assertEqual(L.resolve_stream("main"), 1)
        self.assertEqual(L.resolve_stream("sub"), 2)
        self.assertEqual(L.resolve_stream("third", available=(1, 2)), 2)
        self.assertEqual(L.resolve_stream("auto", 1), 1)
        self.assertEqual(L.resolve_stream("auto", 4), 2)
        self.assertEqual(L.resolve_stream("auto", 16), 3)
        self.assertEqual(L.resolve_stream("auto", 16, available=(1, 2)), 2)
        self.assertEqual(L.resolve_stream("auto", 16, maximized=True), 1)
        self.assertEqual(L.resolve_stream("bogus", 4), 2)


if __name__ == "__main__":
    unittest.main()
