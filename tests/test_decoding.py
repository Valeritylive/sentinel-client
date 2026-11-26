import sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sentinel_decoding as decoding
import sentinel_storage as storage


class Feature:
    def __init__(self, name, klass, rank):
        self.name, self.klass, self.rank = name, klass, rank

    def get_name(self):
        return self.name

    def get_metadata(self, key):
        return self.klass

    def get_rank(self):
        return self.rank

    def set_rank(self, rank):
        self.rank = rank


class Registry:
    def __init__(self, features):
        self.features = {f.name: f for f in features}

    def lookup_feature(self, name):
        return self.features.get(name)


HW = "Codec/Decoder/Video/Hardware"
SW = "Codec/Decoder/Video"


def registry():
    return Registry([Feature("avdec_h264", SW, 256), Feature("avdec_h265", SW, 256),
                     Feature("vah264dec", HW, 256), Feature("vah265dec", HW, 256),
                     Feature("vaapih264dec", SW, 128), Feature("nvh264dec", HW, 257),
                     Feature("x264enc", "Codec/Encoder/Video", 128)])


class Decoding(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name) / "s"
        for name, value in (("DATA", data), ("DB_PATH", data / "d.sqlite3")):
            p = patch.object(storage, name, value); p.start(); self.addCleanup(p.stop)
        decoding._original.clear()
        self.reg = registry()
        p = patch.object(decoding, "video_decoders", lambda reg=None: [
            (f.name, f.klass, decoding._original.get(f.name, f.rank)) for f in (reg or self.reg).features.values()
            if "Decoder" in f.klass])
        p.start(); self.addCleanup(p.stop)

    def ranks(self):
        return {n: f.rank for n, f in self.reg.features.items()}

    def test_backend_detection(self):
        self.assertEqual(decoding.backend_of("vah264dec"), "va")
        self.assertEqual(decoding.backend_of("vaapih265dec"), "vaapi")
        self.assertEqual(decoding.backend_of("nvh265dec"), "nvcodec")
        self.assertEqual(decoding.backend_of("v4l2slh264dec"), "v4l2")
        self.assertIsNone(decoding.backend_of("avdec_h264", SW))
        self.assertEqual(decoding.backend_of("qsvh264dec", HW), "other")
        hw = decoding.hardware_decoders(decoding.video_decoders(self.reg))
        self.assertEqual(set(hw), {"va", "vaapi", "nvcodec"})

    def test_settings_defaults_and_validation(self):
        self.assertEqual(decoding.load_settings(), {"mode": "auto", "backend": "any"})
        self.assertEqual(decoding.save_settings("gpu", "bogus"), {"mode": "gpu", "backend": "any"})
        self.assertEqual(decoding.load_settings()["mode"], "gpu")

    def test_gpu_va_then_cpu_then_auto_restores(self):
        backend, text = decoding.apply({"mode": "gpu", "backend": "va"}, self.reg)
        self.assertEqual(backend, "va")
        self.assertIn("видеокарте", text)
        ranks = self.ranks()
        self.assertGreater(ranks["vah264dec"], ranks["avdec_h264"])
        self.assertEqual(ranks["nvh264dec"], 0)
        backend, text = decoding.apply({"mode": "cpu", "backend": "any"}, self.reg)
        self.assertIsNone(backend)
        self.assertEqual({self.ranks()[n] for n in ("vah264dec", "nvh264dec", "vaapih264dec")}, {0})
        self.assertEqual(self.ranks()["avdec_h264"], 256)
        decoding.apply({"mode": "auto", "backend": "any"}, self.reg)
        self.assertEqual(self.ranks(), {n: f.rank for n, f in registry().features.items()})

    def test_gpu_any_prefers_nvidia_and_missing_backend(self):
        backend, _ = decoding.apply({"mode": "gpu", "backend": "any"}, self.reg)
        self.assertEqual(backend, "nvcodec")
        backend, text = decoding.apply({"mode": "gpu", "backend": "v4l2"}, self.reg)
        self.assertIsNone(backend)
        self.assertIn("недоступна", text)
        self.assertEqual(self.ranks()["vah264dec"], 256)     # ничего не меняли

    def test_decoder_title(self):
        self.assertIn("видеокарта", decoding.decoder_title("vah264dec"))
        self.assertIn("процессор", decoding.decoder_title("avdec_h264"))
        self.assertIn("не выбран", decoding.decoder_title(""))


if __name__ == "__main__":
    unittest.main()
