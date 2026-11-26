import sys, unittest, hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sentinel_tiletools as tt
import sentinel_talk as talk


class TileTools(unittest.TestCase):
    def test_video_rect_letterbox(self):
        self.assertEqual(tt.video_rect(400, 300, 1600, 900), (0.0, 37.5, 400.0, 225.0))
        self.assertEqual(tt.video_rect(400, 300, 0, 0), (0.0, 0.0, 400.0, 300.0))

    def test_pane_to_frame_outside_and_zoom(self):
        rect = (0.0, 37.5, 400.0, 225.0)
        self.assertIsNone(tt.pane_to_frame(200, 10, rect))
        self.assertEqual(tt.pane_to_frame(200, 150, rect), (0.5, 0.5))
        # ×2 по центру: левый верхний угол картинки — это точка (0.25, 0.25) кадра
        self.assertEqual(tt.pane_to_frame(0, 37.5, rect, 2.0, 0.5, 0.5), (0.25, 0.25))
        # ×2, центр у края — видимая часть прижата к краю кадра
        self.assertEqual(tt.pane_to_frame(0, 37.5, rect, 2.0, 0.0, 0.0), (0.0, 0.0))

    def test_position3d_uses_bottom_left_origin(self):
        xml = tt.position3d_xml((0.0, 0.0), (1.0, 1.0)).decode()
        self.assertIn("<StartPoint><positionX>0</positionX><positionY>255</positionY></StartPoint>", xml)
        self.assertIn("<EndPoint><positionX>255</positionX><positionY>0</positionY></EndPoint>", xml)
        center = tt.position3d_xml((0.5, 0.5)).decode()
        self.assertEqual(center.count("<positionX>128</positionX>"), 2)

    def test_keys_and_streams(self):
        self.assertEqual(tt.ptz_direction_for_key("Up"), "up")
        self.assertEqual(tt.ptz_direction_for_key("KP_9"), "up_right")
        self.assertEqual(tt.ptz_direction_for_key("plus"), "zoom_in")
        self.assertIsNone(tt.ptz_direction_for_key("a"))
        self.assertEqual(tt.next_stream("main"), "sub")
        self.assertEqual(tt.next_stream("sub"), "main")
        self.assertEqual(tt.next_stream("third"), "main")
        self.assertTrue(tt.is_click(2, -3))
        self.assertFalse(tt.is_click(20, 0))


class Talk(unittest.TestCase):
    def test_parse_challenge(self):
        c = talk.parse_challenge('Digest qop="auth", realm="IP Camera(C1234)", nonce="abc", stale=FALSE')
        self.assertEqual(c["realm"], "IP Camera(C1234)")
        self.assertEqual(c["qop"], "auth")
        self.assertEqual(c["stale"], "FALSE")
        self.assertIsNone(talk.parse_challenge("Basic realm=x"))

    def test_digest_rfc2617_example(self):
        challenge = {"realm": "testrealm@host.com", "qop": "auth,auth-int",
                     "nonce": "dcd98b7102dd2f0e8b11d0f600bfb0c093", "opaque": "5ccc069c403ebaf9f0171e9517f40e41"}
        header = talk.digest_authorization("GET", "/dir/index.html", "Mufasa", "Circle Of Life",
                                           challenge, cnonce="0a4f113b", nc=1)
        self.assertIn('response="6629fae49393a05397450978507c4ef1"', header)
        self.assertIn("nc=00000001", header)
        self.assertIn('opaque="5ccc069c403ebaf9f0171e9517f40e41"', header)

    def test_codec_and_pipeline(self):
        xml = b'<TwoWayAudioChannel xmlns="http://www.hikvision.com/ver20/XMLSchema"><id>1</id>' \
              b'<audioCompressionType>G.711alaw</audioCompressionType></TwoWayAudioChannel>'
        self.assertEqual(talk.parse_codec(xml), "G.711alaw")
        self.assertEqual(talk.parse_codec(b"not xml"), "G.711ulaw")
        self.assertIn("alawenc", talk.mic_pipeline_description("G.711alaw"))
        self.assertIn("mulawenc", talk.mic_pipeline_description("G.726"))


if __name__ == "__main__":
    unittest.main()
