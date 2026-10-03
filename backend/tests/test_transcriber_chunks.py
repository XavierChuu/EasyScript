import os
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transcriber import Transcriber  # noqa: E402

SR = 16000


class ChunkBoundsTest(unittest.TestCase):
    def test_boundaries_snap_to_silence(self):
        # 25 min of noise with 1 s silences at 9:50 and 19:45.
        rng = np.random.default_rng(0)
        y = (rng.normal(size=SR * 1500) * 0.1).astype(np.float32)
        for t in (590.0, 1185.0):
            y[int(t * SR):int((t + 1.0) * SR)] = 0.0
        path = os.path.join(tempfile.mkdtemp(), "noise.wav")
        sf.write(path, y, SR, subtype="PCM_16")
        b = Transcriber._chunk_bounds(path, SR, 0.0, 1500.0)
        self.assertEqual(len(b), 4)
        self.assertEqual((b[0], b[-1]), (0.0, 1500.0))
        self.assertTrue(590.0 <= b[1] <= 591.0, b)
        self.assertTrue(1185.0 <= b[2] <= 1186.0, b)

    def test_short_file_single_chunk_and_resume(self):
        path = os.path.join(tempfile.mkdtemp(), "short.wav")
        sf.write(path, np.zeros(SR * 5, np.float32), SR, subtype="PCM_16")
        self.assertEqual(Transcriber._chunk_bounds(path, SR, 0.0, 300.0), [0.0, 300.0])
        self.assertEqual(Transcriber._chunk_bounds(path, SR, 120.0, 700.0), [120.0, 700.0])


if __name__ == "__main__":
    unittest.main()
