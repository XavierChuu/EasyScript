import os
import shutil
import sys
import tempfile
import time
import unittest

import numpy as np
import soundfile as sf
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EASYSCRIPT_TOKEN"] = "test-token-123"
os.environ.pop("EASYSCRIPT_AUTH", None)

from fastapi.testclient import TestClient  # noqa: E402

import roformer  # noqa: E402
import separator  # noqa: E402
import server  # noqa: E402

H = {"X-EasyScript-Token": "test-token-123"}


def tones(sr, dur, channels):
    """Band-limited test signal (< 12 kHz, survives 44.1 kHz resampling)."""
    t = np.arange(int(sr * dur)) / sr
    x = 0.2 * np.sin(2 * np.pi * 220 * t) + 0.1 * np.sin(2 * np.pi * 3100 * t) + 0.05 * np.sin(2 * np.pi * 11000 * t)
    x = x.astype(np.float32)
    return np.stack([x, 0.5 * x], axis=1) if channels == 2 else x


def identity_separator():
    """A Separator whose 'model' returns its input as the vocals: exercises the
    chunking / overlap-add / resampling path without the 900 MB checkpoint."""
    sep = separator.Separator.__new__(separator.Separator)
    sep.batch, sep.label, sep.device = 2, "test", "cpu"
    sep._infer = lambda parts: parts.clone()
    return sep


class RotaryTest(unittest.TestCase):
    def test_matches_rotate_half_reference(self):
        rot = roformer.RotaryEmbedding(64)
        t = torch.randn(2, 3, 8, 37, 64)
        pos = torch.arange(37).float()
        freqs = torch.einsum("i,f->if", pos, rot.freqs).repeat_interleave(2, dim=-1)
        x = t.view(*t.shape[:-1], 32, 2)
        half = torch.stack((-x[..., 1], x[..., 0]), dim=-1).view_as(t)
        ref = t * freqs.cos() + half * freqs.sin()
        self.assertLess((rot.rotate_queries_or_keys(t) - ref).abs().max().item(), 1e-5)

    def test_band_layout_covers_every_bin(self):
        m = roformer.MelBandRoformer(32, depth=1, stereo=True, num_bands=60, stft_hop_length=441,
                                     mask_estimator_depth=2)
        self.assertEqual(m.freqs_per_band.shape, (60, 1025))
        self.assertTrue(m.freqs_per_band.any(dim=0).all())


class SeparateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_sep(self, sr, dur, channels, **kw):
        src = os.path.join(self.tmp, f"src_{sr}_{channels}_{dur}.wav")
        x = tones(sr, dur, channels)
        sf.write(src, x, sr, subtype="FLOAT")
        out = os.path.join(self.tmp, f"out_{sr}_{channels}_{dur}")
        r = identity_separator().separate(src, out, **kw)
        v, vsr = sf.read(r["vocals"], dtype="float32", always_2d=True)
        m, _ = sf.read(r["music"], dtype="float32", always_2d=True)
        return r, x.reshape(len(x), -1), v, m, vsr

    def test_long_stereo_44k_is_exact(self):
        # > 2 borders long: reflect padding, overlapping windows, partial last chunk
        r, x, v, m, sr = self.run_sep(44100, 21.37, 2)
        self.assertEqual((sr, r["channels"], v.shape), (44100, 2, x.shape))
        self.assertLess(np.abs(v - x).max(), 1e-5)
        self.assertLess(np.abs(m).max(), 1e-5)
        self.assertEqual([d for d in os.listdir(os.path.dirname(r["vocals"])) if d.startswith("sep_")], [])

    def test_short_clip_without_padding(self):
        r, x, v, m, sr = self.run_sep(44100, 3.0, 2)
        self.assertEqual(v.shape, x.shape)
        self.assertLess(np.abs(v - x).max(), 1e-5)

    def test_mono_48k_keeps_rate_channels_and_sums_to_source(self):
        r, x, v, m, sr = self.run_sep(48000, 12.5, 1)
        self.assertEqual((sr, r["channels"], v.shape), (48000, 1, x.shape))
        np.testing.assert_allclose(v + m, x, atol=1e-6)        # stems add up to the source
        self.assertLess(np.sqrt((m ** 2).mean()), 0.01 * np.sqrt((x ** 2).mean()))  # only resampling error
        # no time shift from the 48 → 44.1 → 48 kHz round trip
        lags = [np.abs(np.roll(v[:, 0], k)[4800:-4800] - x[4800:-4800, 0]).mean() for k in (-1, 0, 1)]
        self.assertEqual(int(np.argmin(lags)), 1)

    def test_range(self):
        r, x, v, m, sr = self.run_sep(44100, 10.0, 2, start=2.5, duration=4.0)
        self.assertEqual(len(v), 4 * 44100)
        self.assertLess(np.abs(v - x[int(2.5 * 44100):int(6.5 * 44100)]).max(), 1e-4)

    def test_cancel(self):
        src = os.path.join(self.tmp, "cancel.wav")
        sf.write(src, tones(44100, 30, 2), 44100)
        with self.assertRaises(separator.SeparationCancelled):
            identity_separator().separate(src, os.path.join(self.tmp, "cancel"), cancelled=lambda: True)


class FakeSeparator:
    label = "GPU · Test"

    def __init__(self):
        self.calls = 0

    def separate(self, src, out_dir, start=0.0, duration=0.0, progress=None, cancelled=None):
        self.calls += 1
        os.makedirs(out_dir, exist_ok=True)
        x = tones(44100, 2.0, 2)
        for name in ("vocals", "music"):
            sf.write(os.path.join(out_dir, name + ".wav"), x, 44100, subtype="FLOAT")


class SeparateEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app, base_url="http://127.0.0.1:9876")
        cls.tmp = tempfile.mkdtemp()
        cls.src = os.path.join(cls.tmp, "mix.wav")
        sf.write(cls.src, tones(44100, 2.0, 2), 44100)
        cls.saved = (server.STEMS_DIR, server.export_dir, server._separator, separator.is_downloaded)
        server.STEMS_DIR = os.path.join(cls.tmp, "stems")
        server.export_dir = os.path.join(cls.tmp, "export")
        server._separator = cls.fake = FakeSeparator()
        separator.is_downloaded = lambda: True

    @classmethod
    def tearDownClass(cls):
        server.STEMS_DIR, server.export_dir, server._separator, separator.is_downloaded = cls.saved
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def wait(self, job_id):
        for _ in range(200):
            job = self.client.get(f"/jobs/{job_id}", headers=H).json()
            if job["status"] != "processing":
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_separate_cache_and_export(self):
        body = {"audio_path": self.src, "start": 0.5, "end": 1.5}
        job = self.wait(self.client.post("/separate", headers=H, json=body).json()["job_id"])
        self.assertEqual(job["status"], "done", job)
        res = job["result"]
        self.assertFalse(res["cached"])
        self.assertTrue(res["vocals"].startswith(server.STEMS_DIR))
        self.assertEqual(res["device"], "GPU · Test")

        again = self.wait(self.client.post("/separate", headers=H, json=body).json()["job_id"])
        self.assertTrue(again["result"]["cached"])
        self.assertEqual(self.fake.calls, 1)
        other = self.wait(self.client.post("/separate", headers=H, json={"audio_path": self.src}).json()["job_id"])
        self.assertNotEqual(other["result"]["vocals"], res["vocals"])   # another range → another cache entry

        r1 = self.client.post("/separate/export", headers=H, json={"path": res["vocals"], "name": "Take: 1 - Voice"})
        r2 = self.client.post("/separate/export", headers=H, json={"path": res["vocals"], "name": "Take: 1 - Voice"})
        self.assertEqual(r1.status_code, 200, r1.text)
        p1, p2 = r1.json()["path"], r2.json()["path"]
        self.assertEqual(os.path.basename(p1), "Take_ 1 - Voice.wav")
        self.assertEqual(os.path.basename(p2), "Take_ 1 - Voice (2).wav")   # never overwrites
        self.assertEqual(os.path.dirname(p1), os.path.join(server.export_dir, "EasyScript Stems"))

    def test_export_refuses_files_outside_the_stem_cache(self):
        r = self.client.post("/separate/export", headers=H, json={"path": self.src, "name": "x"})
        self.assertEqual(r.status_code, 400)

    def test_missing_file(self):
        r = self.client.post("/separate", headers=H, json={"audio_path": os.path.join(self.tmp, "nope.wav")})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
