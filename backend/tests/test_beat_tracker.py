import os
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import beat_tracker  # noqa: E402

SR = 16000


def click_track(bpm, dur, start=0.0, accent_every=4, seed=0):
    """Broadband clicks on every beat, a 60 Hz kick on every bar's first beat."""
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 0.002, int(dur * SR)).astype(np.float32)
    times = np.arange(start, dur - 0.2, 60.0 / bpm)
    n = int(0.03 * SR)
    decay = np.exp(-np.arange(n) / (0.004 * SR))
    k = np.arange(int(0.12 * SR))
    kick = 0.8 * np.sin(2 * np.pi * 60 * k / SR) * np.exp(-k / (0.03 * SR))
    for i, t in enumerate(times):
        i0 = int(round(t * SR))
        click = rng.normal(0, 0.3, n) * decay
        x[i0:i0 + n] += click[: len(x) - i0]
        if i % accent_every == 0:
            x[i0:i0 + len(kick)] += kick[: len(x) - i0]
    return x, times


def write_wav(x):
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    sf.write(path, np.clip(x, -1, 1), SR, subtype="PCM_16")
    return path


def match(truth, found, tol=0.015):
    found = np.asarray(found)
    errs, hits = [], 0
    for t in truth:
        if len(found) == 0:
            break
        j = int(np.argmin(np.abs(found - t)))
        e = found[j] - t
        if abs(e) <= tol:
            hits += 1
            errs.append(e)
    return hits / max(1, len(truth)), (float(np.mean(errs)) if errs else 0.0)


class BeatTrackerTest(unittest.TestCase):
    def setUp(self):
        self.paths = []

    def tearDown(self):
        for p in self.paths:
            try:
                os.remove(p)
            except OSError:
                pass

    def detect(self, x, **kw):
        p = write_wav(x)
        self.paths.append(p)
        return beat_tracker.detect(p, **kw)

    def test_steady_tempo(self):
        x, truth = click_track(120, 40)
        res = self.detect(x)
        self.assertAlmostEqual(res["bpm"], 120, delta=1.0)
        recall, bias = match(truth[1:-1], [b["t"] for b in res["beats"]])
        self.assertGreater(recall, 0.95)
        self.assertLess(abs(bias), 0.008)

    def test_downbeat_feature(self):
        x, truth = click_track(100, 40)
        beats = self.detect(x)["beats"]
        # The low-band feature must peak on the kick (every 4th beat).
        by_phase = [[], [], [], []]
        for b in beats:
            i = int(round(b["t"] / (60.0 / 100)))
            by_phase[i % 4].append(b["lf"])
        means = [np.mean(v) if v else 0 for v in by_phase]
        self.assertEqual(int(np.argmax(means)), 0)

    def test_tempo_change(self):
        a, ta = click_track(100, 24, seed=1)
        b, tb = click_track(128, 24, seed=2)
        x = np.concatenate([a, b])
        truth = np.concatenate([ta, tb + 24])
        res = self.detect(x)
        found = [b["t"] for b in res["beats"]]
        r1, _ = match(ta[2:-4], found)
        r2, _ = match((tb + 24)[4:-2], found)
        self.assertGreater(r1, 0.9)
        self.assertGreater(r2, 0.9)

    def test_silence_has_no_beats(self):
        sil = np.random.default_rng(3).normal(0, 0.001, 12 * SR).astype(np.float32)
        music, truth = click_track(120, 20)
        res = self.detect(np.concatenate([sil, music]))
        early = [b for b in res["beats"] if b["t"] < 11.0]
        self.assertEqual(early, [])

    def test_speech_has_no_beats(self):
        # Speech-like bursts (irregular onsets) then music: beats only in the music.
        rng = np.random.default_rng(5)
        parts, total = [], 0.0
        while total < 40:
            d = rng.uniform(1.0, 5.0)
            n = int(d * SR)
            t = np.arange(n) / SR
            noise = np.convolve(rng.normal(0, 1, n), np.ones(6) / 6, mode="same")
            env = 0.5 + 0.5 * np.sin(2 * np.pi * rng.uniform(3, 5) * t) ** 2
            parts.append((noise * env * 0.25).astype(np.float32))
            gap = rng.uniform(0.2, 2.0)
            parts.append(rng.normal(0, 0.002, int(gap * SR)).astype(np.float32))
            total += d + gap
        speech = np.concatenate(parts)
        music, truth = click_track(120, 30)
        res = self.detect(np.concatenate([speech, music]))
        cut = len(speech) / SR
        # The 8 s tempo windows straddle the boundary, so a few beats may spill
        # up to ~6 s into the speech — none earlier.
        in_speech = [b for b in res["beats"] if b["t"] < cut - 6.0]
        self.assertEqual(in_speech, [])
        recall, _ = match(truth[2:-2] + cut, [b["t"] for b in res["beats"]])
        self.assertGreater(recall, 0.9)

    def test_fixed_bpm(self):
        x, truth = click_track(90, 30)
        res = self.detect(x, bpm=90)
        recall, _ = match(truth[1:-1], [b["t"] for b in res["beats"]])
        self.assertGreater(recall, 0.95)


if __name__ == "__main__":
    unittest.main()
