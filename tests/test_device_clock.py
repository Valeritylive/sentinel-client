"""Время архива в часах регистратора (timeType=local / UTC)."""
import os, sys, unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sentinel_archive as archive

PLUS4 = timezone(timedelta(hours=4))
TIME_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Time version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<timeMode>NTP</timeMode><localTime>2026-11-26T13:21:00+04:00</localTime>
<timeZone>CST-4:00:00</timeZone></Time>"""
CAPS_LOCAL = b"""<TimeCap xmlns="http://www.hikvision.com/ver20/XMLSchema">
<timeType opt="local,UTC">local</timeType></TimeCap>"""
CAPS_UTC = b"""<TimeCap><timeType opt="local,UTC">UTC</timeType></TimeCap>"""
SEARCH = b"""<CMSearchResult xmlns="http://www.hikvision.com/ver20/XMLSchema">
<responseStatusStrg>OK</responseStatusStrg><matchList><searchMatchItem>
<timeSpan><startTime>2026-11-26T12:20:47Z</startTime><endTime>2026-11-26T13:21:53Z</endTime></timeSpan>
<mediaSegmentDescriptor><playbackURI>rtsp://192.168.88.4:554/Streaming/tracks/201/?starttime=20261126T122047Z</playbackURI>
</mediaSegmentDescriptor></searchMatchItem></matchList></CMSearchResult>"""


class DeviceClockTests(unittest.TestCase):
    def test_local_device_gets_its_wall_time_with_z(self):
        # 09:00:13 по Еревану на NVR с timeType=local — это 090013Z, а не 050013Z (UTC).
        clock = archive.DeviceClock(PLUS4, utc=False)
        moment = datetime(2026, 11, 26, 9, 0, 13, tzinfo=PLUS4)
        self.assertEqual(clock.rtsp(moment), "20261126T090013Z")
        self.assertEqual(clock.iso(moment), "2026-11-26T09:00:13Z")

    def test_local_device_uses_nvr_zone_not_computer_zone(self):
        clock = archive.DeviceClock(PLUS4, utc=False)
        moment = datetime(2026, 11, 26, 5, 0, 13, tzinfo=timezone.utc)
        self.assertEqual(clock.rtsp(moment), "20261126T090013Z")

    def test_utc_device_gets_real_utc(self):
        clock = archive.DeviceClock(PLUS4, utc=True)
        moment = datetime(2026, 11, 26, 9, 0, 13, tzinfo=PLUS4)
        self.assertEqual(clock.rtsp(moment), "20261126T050013Z")

    def test_parse_local_z_as_nvr_local(self):
        parsed = archive.DeviceClock(PLUS4).parse("2026-11-26T13:21:53Z")
        self.assertEqual(parsed, datetime(2026, 11, 26, 13, 21, 53, tzinfo=PLUS4))

    def test_parse_utc_z_as_utc(self):
        parsed = archive.DeviceClock(PLUS4, utc=True).parse("2026-11-26T09:21:53Z")
        self.assertEqual(parsed, datetime(2026, 11, 26, 13, 21, 53, tzinfo=PLUS4))

    def test_parse_explicit_offset_is_respected(self):
        parsed = archive.DeviceClock(PLUS4).parse("2026-11-26T13:21:53+04:00")
        self.assertEqual(parsed, datetime(2026, 11, 26, 9, 21, 53, tzinfo=timezone.utc))

    def test_parse_time_xml(self):
        clock = archive.parse_device_clock(TIME_XML)
        self.assertEqual(clock.tz.utcoffset(None), timedelta(hours=4))
        self.assertFalse(clock.utc)

    def test_time_type_from_capabilities(self):
        self.assertIs(archive.parse_time_type(CAPS_LOCAL), False)
        self.assertIs(archive.parse_time_type(CAPS_UTC), True)
        self.assertIsNone(archive.parse_time_type(TIME_XML))

    def test_search_round_trip_on_local_nvr(self):
        clock = archive.DeviceClock(PLUS4)
        query = archive.search_query(201, datetime(2026, 11, 26, 0, 0, tzinfo=PLUS4),
                                     datetime(2026, 11, 27, 0, 0, tzinfo=PLUS4), clock=clock).decode()
        self.assertIn("<startTime>2026-11-26T00:00:00Z</startTime>", query)
        self.assertIn("<endTime>2026-11-27T00:00:00Z</endTime>", query)
        items, more, count = archive.parse_search_response(SEARCH, clock)
        self.assertEqual(count, 1)
        self.assertEqual(items[0]["end"], datetime(2026, 11, 26, 13, 21, 53, tzinfo=PLUS4))


if __name__ == "__main__":
    unittest.main()
