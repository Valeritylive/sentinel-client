import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sentinel_geomap as geo
import sentinel_storage as storage


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        data = Path(self.temp_dir.name) / "sentinel"
        for target, value in (("DATA", data), ("DB_PATH", data / "devices.sqlite3")):
            patcher = patch.object(storage, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {"XDG_CACHE_HOME": str(Path(self.temp_dir.name) / "cache")})
        env.start()
        self.addCleanup(env.stop)


class ProjectionTests(unittest.TestCase):
    def test_round_trip(self):
        for lon, lat, zoom in ((37.6173, 55.7558, 12), (-122.42, 37.77, 17), (0, 0, 1), (179.9, -60, 5)):
            x, y = geo.lonlat_to_world(lon, lat, zoom)
            back_lon, back_lat = geo.world_to_lonlat(x, y, zoom)
            self.assertAlmostEqual(back_lon, lon, places=6)
            self.assertAlmostEqual(back_lat, lat, places=6)

    def test_known_tile(self):
        # Москва, z=10 → тайл x=619, y=320 (стандартная схема XYZ)
        x, y = geo.lonlat_to_world(37.6173, 55.7558, 10)
        self.assertEqual((int(x // 256), int(y // 256)), (619, 320))

    def test_visible_tiles_cover_window(self):
        tiles = geo.visible_tiles(37.6173, 55.7558, 12, 800, 600)
        self.assertTrue(all(t[0] == 12 for t in tiles))
        left = min(t[3] for t in tiles)
        top = min(t[4] for t in tiles)
        right = max(t[3] + t[5] for t in tiles)
        bottom = max(t[4] + t[5] for t in tiles)
        self.assertLessEqual(left, 0)
        self.assertLessEqual(top, 0)
        self.assertGreaterEqual(right, 800)
        self.assertGreaterEqual(bottom, 600)
        # центральный тайл — первым
        first = tiles[0]
        self.assertTrue(first[3] <= 400 <= first[3] + first[5] and first[4] <= 300 <= first[4] + first[5])

    def test_overzoom_scales_tiles(self):
        tiles = geo.visible_tiles(37.6, 55.7, 20, 512, 512, max_tile_zoom=18)
        self.assertTrue(all(t[0] == 18 and t[5] == 1024 for t in tiles))

    def test_wraps_x_and_clips_y(self):
        tiles = geo.visible_tiles(179.99, 84.9, 2, 2000, 2000)
        self.assertTrue(all(0 <= t[1] < 4 and 0 <= t[2] < 4 for t in tiles))

    def test_fit_bounds(self):
        points = [(37.60, 55.75), (37.64, 55.76)]
        lon, lat, zoom = geo.fit_bounds(points, 800, 600)
        self.assertAlmostEqual(lon, 37.62, places=3)
        self.assertTrue(55.75 < lat < 55.76)
        x1, y1 = geo.lonlat_to_world(37.60, 55.75, zoom)
        x2, y2 = geo.lonlat_to_world(37.64, 55.76, zoom)
        self.assertLessEqual(abs(x2 - x1), 800 - 96)
        self.assertGreater(abs(x2 - x1) * 2, 800 - 96)  # следующий масштаб уже не влезает
        self.assertIsNone(geo.fit_bounds([], 800, 600))
        self.assertEqual(geo.fit_bounds([(10, 20)], 800, 600)[2], 17)

    def test_parse_coordinates(self):
        self.assertEqual(geo.parse_coordinates("55.7558, 37.6173"), (55.7558, 37.6173))
        self.assertEqual(geo.parse_coordinates("55,7558 37,6173"), (55.7558, 37.6173))
        self.assertIsNone(geo.parse_coordinates("Тверская 1"))
        self.assertIsNone(geo.parse_coordinates("95, 10"))


class ProviderTests(_TempDb):
    def test_tile_url_templates(self):
        topo = geo.provider_by_id("topo")
        self.assertRegex(geo.tile_url(topo, 3, 1, 2), r"^https://[abc]\.tile\.opentopomap\.org/3/1/2\.png$")
        sat = geo.provider_by_id("satellite")
        self.assertTrue(geo.tile_url(sat, 3, 1, 2).endswith("/tile/3/2/1"))
        local = geo.normalize_provider({"name": "Папка", "url": "/srv/tiles/{z}/{x}/{-y}.png"})
        self.assertEqual(geo.tile_url(local, 2, 1, 0), "file:///srv/tiles/2/1/3.png")

    def test_validate_template(self):
        self.assertEqual(geo.validate_template("https://t.example/{z}/{x}/{y}.png"), "")
        self.assertEqual(geo.validate_template("/srv/tiles/{z}/{x}/{y}.png"), "")
        self.assertIn("{z}", geo.validate_template("https://t.example/tile.png"))
        self.assertTrue(geo.validate_template(""))
        self.assertTrue(geo.validate_template("ftp://t/{z}/{x}/{y}"))

    def test_settings_round_trip_and_custom_provider(self):
        self.assertEqual(geo.load_settings()["default_provider"], "osm")
        saved = geo.save_settings({**geo.load_settings(), "default_provider": "my",
                                   "custom_providers": [{"id": "my", "name": "Мой сервер",
                                                         "url": "https://maps.local/{z}/{x}/{y}.png",
                                                         "max_zoom": 40}],
                                   "cache_mb": 5})
        self.assertEqual(saved["cache_mb"], 20)
        self.assertEqual(saved["custom_providers"][0]["max_zoom"], 22)
        self.assertEqual(geo.provider_by_id(None)["id"], "my")
        self.assertEqual(geo.provider_by_id("missing")["id"], "my")
        self.assertEqual(len(geo.providers()), len(geo.BUILTIN_PROVIDERS) + 1)

    def test_builtin_names_have_attribution(self):
        for provider in geo.BUILTIN_PROVIDERS:
            self.assertTrue(provider["attribution"])
            self.assertEqual(geo.validate_template(provider["url"]), "")


class FetcherTests(_TempDb):
    def _fetch(self, fetcher, provider, settings, key=(3, 1, 2)):
        done = threading.Event()
        box = {}

        def callback(k, data):
            box["data"] = data
            done.set()
        fetcher.request(provider, *key, callback, settings)
        self.assertTrue(done.wait(5))
        return box["data"]

    def test_network_then_cache_then_offline(self):
        calls = []

        def reader(url):
            calls.append(url)
            return b"PNGDATA"
        fetcher = geo.TileFetcher(workers=1, reader=reader)
        self.addCleanup(fetcher.shutdown)
        provider = geo.provider_by_id("osm")
        settings = geo.load_settings()
        self.assertEqual(self._fetch(fetcher, provider, settings), b"PNGDATA")
        self.assertEqual(len(calls), 1)
        self.assertTrue(geo.cache_path(provider, 3, 1, 2).is_file())
        # свежий тайл берётся с диска
        self.assertEqual(self._fetch(fetcher, provider, settings), b"PNGDATA")
        self.assertEqual(len(calls), 1)
        # устаревший — перезапрашивается, но в автономном режиме нет
        path = geo.cache_path(provider, 3, 1, 2)
        old = time.time() - 90 * 86400
        os.utime(path, (old, old))
        self.assertEqual(self._fetch(fetcher, provider, dict(settings, offline=True)), b"PNGDATA")
        self.assertEqual(len(calls), 1)
        self._fetch(fetcher, provider, settings)
        self.assertEqual(len(calls), 2)
        self.assertGreater(geo.cache_size_bytes(), 0)
        geo.clear_cache()
        self.assertEqual(geo.cache_size_bytes(), 0)

    def test_failure_is_backed_off(self):
        def reader(url):
            raise OSError("no network")
        fetcher = geo.TileFetcher(workers=1, reader=reader)
        self.addCleanup(fetcher.shutdown)
        provider = geo.provider_by_id("osm")
        self.assertIsNone(self._fetch(fetcher, provider, geo.load_settings()))
        self.assertFalse(fetcher.request(provider, 3, 1, 2, lambda *a: None, geo.load_settings()))

    def test_trim_cache(self):
        root = Path(self.temp_dir.name) / "trim"
        for index in range(5):
            path = root / "p" / f"{index}.tile"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 400_000)
            os.utime(path, (index, index))
        self.assertEqual(geo.trim_cache(1, root), 3)
        self.assertEqual(sorted(p.name for p in root.rglob("*.tile")), ["3.tile", "4.tile"])


class GeocodeTests(_TempDb):
    def test_coordinates_without_network(self):
        self.assertEqual(geo.geocode("55.75, 37.61", reader=lambda u: self.fail("network"))[0][1:], (55.75, 37.61))

    def test_service_results(self):
        seen = []

        def reader(url):
            seen.append(url)
            return json.dumps([{"display_name": "Красная площадь", "lat": "55.7539", "lon": "37.6208"},
                               {"display_name": "битая", "lat": "x"}]).encode()
        results = geo.geocode("Красная площадь", reader=reader)
        self.assertEqual(results, [("Красная площадь", 55.7539, 37.6208)])
        self.assertIn("format=json", seen[0])
        self.assertEqual(geo.geocode("адрес", dict(geo.load_settings(), geocoder_url=""), reader=reader), [])


class MapSchemaTests(_TempDb):
    def test_online_map_columns(self):
        with storage.database() as db:
            db.execute("INSERT INTO devices(name,host,http_port,channel) VALUES('cam','10.0.0.2',80,1)")
            map_id = db.execute("INSERT INTO maps(name,image_path,kind,provider,center_lat,center_lon,zoom) "
                                "VALUES('Город','', 'online','osm',55.7,37.6,14)").lastrowid
            db.execute("INSERT INTO map_hotspots(map_id,device_id,label,x,y,kind,number,lat,lon) "
                       "VALUES(?,1,'Вход',0,0,'camera',0,55.71,37.61)", (map_id,))
            row = db.execute("SELECT m.kind, h.lat, h.lon FROM maps m JOIN map_hotspots h ON h.map_id=m.id").fetchone()
            self.assertEqual(tuple(row), ("online", 55.71, 37.61))
            plan = db.execute("INSERT INTO maps(name,image_path) VALUES('План','/tmp/p.png')").lastrowid
            self.assertEqual(db.execute("SELECT kind FROM maps WHERE id=?", (plan,)).fetchone()[0], "image")


if __name__ == "__main__":
    unittest.main()
