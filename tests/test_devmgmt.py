import io, os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sentinel_devmgmt as D
from sentinel_protocols import parse_channel_names


def dev(i, host, ch, group=None, **kw):
    d = {"id": i, "name": f"c{i}", "host": host, "sdk_port": 8000, "http_port": 80, "rtsp_port": 554,
         "channel": ch, "protocol": "hikvision", "group_id": group, "serial_no": "", "device_model": "", "host_name": ""}
    d.update(kw)
    return d


class DevMgmtTests(unittest.TestCase):
    def test_physical_grouping(self):
        rows = D.physical_devices([dev(1, "10.0.0.2", 1, 5), dev(2, "10.0.0.2", 2, 5, device_model="DS-7"),
                                   dev(3, "10.0.0.1", 1)], [{"id": 5, "name": "Офис"}])
        self.assertEqual([r["host"] for r in rows], ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(rows[1]["channels"], 2)
        self.assertEqual(rows[1]["group_name"], "Офис")
        self.assertEqual(rows[1]["device_model"], "DS-7")
        self.assertEqual(rows[0]["group_name"], "")

    def test_filter(self):
        rows = D.physical_devices([dev(1, "10.0.0.2", 1, 5), dev(2, "10.0.0.9", 1)], [{"id": 5, "name": "Офис"}])
        self.assertEqual(len(D.filter_rows(rows, "")), 2)
        self.assertEqual(len(D.filter_rows(rows, "офис")), 1)
        self.assertEqual(len(D.filter_rows(rows, "0.9")), 1)
        self.assertEqual(D.filter_rows(rows, "zzz"), [])

    def test_csv_escapes_formulas(self):
        rows = D.physical_devices([dev(1, "10.0.0.2", 1, host_name="=cmd()")])
        out = io.StringIO(); D.export_csv(rows, out)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("'=cmd()"))

    def test_ip_segment(self):
        self.assertEqual(D.expand_ip_segment("192.168.1.254", "192.168.2.1"),
                         ["192.168.1.254", "192.168.1.255", "192.168.2.0", "192.168.2.1"])
        for bad in (("x", "1.1.1.1"), ("1.1.1.5", "1.1.1.1"), ("1.1.1.1", "::1"), ("10.0.0.0", "10.0.5.0")):
            with self.assertRaises(ValueError):
                D.expand_ip_segment(*bad)

    def test_channel_title(self):
        self.assertEqual(D.channel_title("NVR", 2, {2: "Вход"}), "Вход")
        self.assertEqual(D.channel_title("NVR", 2), "NVR CH 2")
        self.assertEqual(D.channel_title("Cam", 1, single=True), "Cam")

    def test_group_tree(self):
        groups = [{"id": 2, "name": "б"}, {"id": 1, "name": "а"}]
        rows = D.physical_devices([dev(1, "h1", 1, 1), dev(2, "h2", 1), dev(3, "h3", 1, 99)])
        tree = D.group_tree(groups, rows)
        self.assertEqual([g["name"] if g else None for g, _ in tree], ["а", "б", None])
        self.assertEqual(len(tree[2][1]), 2)

    def test_parse_channel_names(self):
        xml = (b'<InputProxyChannelList xmlns="http://www.isapi.org/ver20/XMLSchema">'
               b'<InputProxyChannel><id>1</id><name>Door</name></InputProxyChannel>'
               b'<InputProxyChannel><id>2</id><name></name></InputProxyChannel></InputProxyChannelList>')
        self.assertEqual(parse_channel_names(xml, "InputProxyChannel"), {1: "Door"})


if __name__ == "__main__":
    unittest.main()


class TimeXmlTests(unittest.TestCase):
    def test_build_time_xml(self):
        from datetime import datetime, timezone
        from sentinel_protocols import build_time_xml
        import xml.etree.ElementTree as ET
        xml = build_time_xml(datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc))
        root = ET.fromstring(xml)
        text = {n.tag.rsplit("}", 1)[-1]: n.text for n in root}
        self.assertEqual(text["timeMode"], "manual")
        self.assertRegex(text["localTime"], r"^2026-11-2\dT\d\d:\d\d:00[+-]\d\d:\d\d$")
