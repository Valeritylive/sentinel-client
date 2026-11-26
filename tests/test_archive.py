import json, sys, tempfile, unittest, zipfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sentinel_archive as archive
import sentinel_timeline as tl

RESPONSE = b"""<?xml version="1.0" encoding="UTF-8"?>
<CMSearchResult xmlns="http://www.hikvision.com/ver20/XMLSchema"><responseStatusStrg>MORE</responseStatusStrg>
<numOfMatches>3</numOfMatches><matchList>
<searchMatchItem><trackID>101</trackID><timeSpan><startTime>2026-11-26T08:00:00Z</startTime><endTime>2026-11-26T09:00:00Z</endTime></timeSpan>
<mediaSegmentDescriptor><playbackURI>rtsp://10.0.0.5/Streaming/tracks/101?starttime=a</playbackURI></mediaSegmentDescriptor>
<metadataMatches><metadataDescriptor>recordType.meta.std-cgi.com/CMR</metadataDescriptor></metadataMatches></searchMatchItem>
<searchMatchItem><timeSpan><startTime>2026-11-26T09:10:00Z</startTime><endTime>2026-11-26T09:11:00Z</endTime></timeSpan>
<mediaSegmentDescriptor><playbackURI>rtsp://x/2</playbackURI></mediaSegmentDescriptor>
<metadataMatches><metadataDescriptor>recordType.meta.std-cgi.com/MOTION</metadataDescriptor></metadataMatches></searchMatchItem>
<searchMatchItem><timeSpan><startTime>2026-11-26T09:20:00Z</startTime><endTime>2026-11-26T09:20:00Z</endTime></timeSpan></searchMatchItem>
</matchList></CMSearchResult>"""


class ArchiveLogic(unittest.TestCase):
    def test_record_kinds(self):
        self.assertEqual(archive.record_kind("recordType.meta.std-cgi.com/CMR"), "continuous")
        self.assertEqual(archive.record_kind("//recordType.meta.std-cgi.com/MOTION"), "motion")
        self.assertEqual(archive.record_kind("recordType.meta.std-cgi.com/AlarmOrMotion"), "alarm")
        self.assertEqual(archive.record_kind("recordType.meta.hikvision.com/lineDetection"), "smart")
        self.assertEqual(archive.record_kind(""), "continuous")

    def test_query_and_parse(self):
        query = archive.search_query(201, "2026-11-26T00:00:00+04:00", "2026-11-27T00:00:00+04:00", 50, "motion",
                                     search_id="S").decode()
        self.assertIn("<trackID>201</trackID>", query)
        self.assertIn("<searchResultPosition>50</searchResultPosition>", query)
        self.assertIn("recordType.meta.std-cgi.com/MOTION", query)
        self.assertNotIn("metadataList", archive.search_query(101, "a", "b").decode())
        items, more, count = archive.parse_search_response(RESPONSE)
        self.assertTrue(more)
        self.assertEqual(count, 3)
        self.assertEqual([i["kind"] for i in items], ["continuous", "motion"])
        self.assertTrue(items[0]["uri"].startswith("rtsp://10.0.0.5"))

    def test_search_all_pages(self):
        pages = [RESPONSE, RESPONSE.replace(b"MORE", b"OK")]
        sent = []

        def post(body):
            sent.append(body)
            return pages[len(sent) - 1]
        items = archive.search_all(post, 101, "a", "b")
        self.assertEqual(len(items), 4)
        self.assertIn(b"<searchResultPosition>3</searchResultPosition>", sent[1])

    def test_scope_and_grouping(self):
        devices = [{"id": 1, "name": "Б", "host": "a", "sdk_port": 8000, "channel": 1, "group_id": 5},
                   {"id": 2, "name": "А", "host": "a", "sdk_port": 8000, "channel": 2, "group_id": 6},
                   {"id": 3, "name": "В", "host": "b", "sdk_port": 8000, "channel": 1, "group_id": 5},
                   {"id": 4, "name": "Дверь", "host": "c", "sdk_port": 8000, "channel": 1, "category": "access"}]
        self.assertEqual([d["id"] for d in archive.scope_devices(devices, devices[0], "channel")], [1])
        self.assertEqual([d["id"] for d in archive.scope_devices(devices, devices[0], "device")], [1, 2])
        self.assertEqual([d["id"] for d in archive.scope_devices(devices, devices[0], "area")], [1, 3])
        t = datetime(2026, 1, 1).astimezone()
        grouped = archive.group_by_camera([(devices[0], [{"start": t + timedelta(hours=1)}, {"start": t}]),
                                           (devices[1], [{"start": t}]), (devices[2], [])])
        self.assertEqual([d["name"] for d, _ in grouped], ["А", "Б"])
        self.assertEqual(grouped[1][1][0]["start"], t)

    def test_export_command_keeps_credentials_out_of_argv(self):
        argv = archive.ffmpeg_export_command("ffmpeg", "/tmp/list.txt", "/x/out.mp4", 59.6)
        self.assertIn("/tmp/list.txt", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "59")
        self.assertFalse(any("rtsp://" in a for a in argv))

    def test_finalize_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "pkg"
            folder.mkdir()
            (folder / "01_cam.mp4").write_bytes(b"video-bytes")
            start = datetime(2026, 11, 26, 9, 0).astimezone()
            clips = [{"file": "01_cam.mp4", "camera": "Вход <1>", "host": "10.0.0.5", "channel": 1,
                      "start": start, "end": start + timedelta(minutes=1), "kind": "motion"},
                     {"file": "02_missing.mp4", "camera": "X", "host": "h", "channel": 2,
                      "start": start, "end": start, "kind": "continuous"}]
            manifest, bundle = archive.finalize_package(folder, clips, "operator", case="Дело 7")
            self.assertEqual(len(manifest["clips"]), 1)
            digest = manifest["clips"][0]["sha256"]
            self.assertEqual((folder / "SHA256SUMS").read_text(), f"{digest}  01_cam.mp4\n")
            self.assertEqual(json.loads((folder / "manifest.json").read_text())["case"], "Дело 7")
            page = (folder / "index.html").read_text()
            self.assertIn("Вход &lt;1&gt;", page)
            self.assertNotIn("Вход <1>", page.split("<script>")[0])
            with zipfile.ZipFile(bundle) as z:
                self.assertEqual(sorted(Path(n).name for n in z.namelist()),
                                 ["01_cam.mp4", "README.txt", "SHA256SUMS", "index.html", "manifest.json"])


class TimelineMarkers(unittest.TestCase):
    def test_markers_not_merged_and_found_by_x(self):
        center = datetime(2026, 11, 26, 12, 0).astimezone()
        model = tl.TimelineModel(center, 3600)
        a = center - timedelta(minutes=10)
        events = [tl.Segment(a - timedelta(seconds=5), a + timedelta(seconds=10), "event", {"moment": a, "event": {"id": 1}}),
                  tl.Segment(a, a + timedelta(seconds=15), "event", {"moment": a + timedelta(seconds=5), "event": {"id": 2}})]
        records = [tl.Segment(center - timedelta(hours=1), center, "continuous"),
                   tl.Segment(center, center + timedelta(seconds=1), "continuous"),
                   tl.Segment(a, a + timedelta(minutes=1), "motion")]
        model.segments = tl.merge_segments(records + events)
        self.assertEqual(len([s for s in model.segments if s.is_marker]), 2)
        self.assertEqual(len([s for s in model.segments if s.kind == "continuous"]), 1)
        x = model.x_for(a, 1000)
        self.assertEqual(model.marker_near(x, 1000).payload["event"]["id"], 1)
        self.assertIsNone(model.marker_near(x + 50, 1000))
        model.filter = "vehicle"
        self.assertIsNone(model.marker_near(x, 1000))


if __name__ == "__main__":
    unittest.main()
