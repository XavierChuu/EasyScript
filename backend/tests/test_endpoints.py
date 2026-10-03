import base64
import os
import sys
import tempfile
import time
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EASYSCRIPT_TOKEN"] = "test-token-123"
os.environ.pop("EASYSCRIPT_AUTH", None)

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from tests.test_beat_tracker import click_track  # noqa: E402
from tests.test_xml_cut import TPF25, linked_av, sequence  # noqa: E402

H = {"X-EasyScript-Token": "test-token-123"}


class EndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app, base_url="http://127.0.0.1:9876")
        cls.tmp = tempfile.mkdtemp()
        x, cls.truth = click_track(120, 30)
        cls.wav = os.path.join(cls.tmp, "music.wav")
        sf.write(cls.wav, np.clip(x, -1, 1), 16000, subtype="PCM_16")
        cls._export_dir = server.export_dir
        server.export_dir = cls.tmp

    @classmethod
    def tearDownClass(cls):
        server.export_dir = cls._export_dir

    def test_waveform_overview_and_slice(self):
        r = self.client.post("/waveform", headers=H, json={"audio_path": self.wav})
        self.assertEqual(r.status_code, 200, r.text)
        ov = r.json()
        self.assertEqual(ov["bins_per_sec"], 200)
        self.assertEqual(ov["bins"], 6000)
        mx = np.frombuffer(base64.b64decode(ov["max"]), dtype=np.int8)
        self.assertEqual(len(mx), 6000)
        self.assertEqual(int(mx.max()), 127)   # scaled to the loudest sample
        r = self.client.post("/waveform/slice", headers=H, json={
            "audio_path": self.wav, "start": 10.0, "end": 10.5, "bins": 400, "peak": ov["peak"]})
        sl = r.json()
        self.assertEqual(sl["mode"], "minmax")
        self.assertEqual(len(base64.b64decode(sl["min"])), 400)
        r = self.client.post("/waveform/slice", headers=H, json={
            "audio_path": self.wav, "start": 10.0, "end": 10.01, "bins": 400, "peak": ov["peak"]})
        self.assertEqual(r.json()["mode"], "samples")   # 160 samples < 400 bins

    def test_beats_job(self):
        r = self.client.post("/beats", headers=H, json={"audio_path": self.wav})
        self.assertEqual(r.status_code, 200, r.text)
        job_id = r.json()["job_id"]
        for _ in range(200):
            job = self.client.get(f"/jobs/{job_id}", headers=H).json()
            if job["status"] != "processing":
                break
            time.sleep(0.05)
        self.assertEqual(job["status"], "done", job)
        self.assertAlmostEqual(job["result"]["bpm"], 120, delta=1.0)
        self.assertGreater(len(job["result"]["beats"]), 50)
        self.assertEqual(self.client.get("/jobs/nope", headers=H).status_code, 404)

    def test_beats_rejects_bad_bpm(self):
        r = self.client.post("/beats", headers=H, json={"audio_path": self.wav, "bpm": 5000})
        self.assertEqual(r.status_code, 400)

    def test_xml_cut_endpoint(self):
        src = os.path.join(self.tmp, "export_123.xml")
        v, a = linked_av()
        with open(src, "w", encoding="utf-8") as f:
            f.write(sequence(v, a, 750))
        r = self.client.post("/xml/cut", headers=H, json={
            "xml_path": src, "cuts_ticks": [[100 * TPF25, 150 * TPF25], [400 * TPF25, 500 * TPF25]]})
        self.assertEqual(r.status_code, 200, r.text)
        res = r.json()
        self.assertEqual(res["name"], "Interview (EasyScript cut)")
        self.assertEqual(res["removed_frames"], 150)
        self.assertTrue(os.path.isfile(res["path"]))
        self.assertEqual(os.path.dirname(res["path"]), self.tmp)   # saved to the export folder
        self.assertEqual(res["clips_after"], 9)

    def test_xml_cut_rejects_non_xml(self):
        r = self.client.post("/xml/cut", headers=H, json={"xml_path": self.wav, "cuts_ticks": []})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
