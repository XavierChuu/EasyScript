import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voices  # noqa: E402


class VoicesTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "voices.json")
        rng = np.random.default_rng(0)
        self.lan, self.minh, self.other = (rng.normal(size=256) for _ in range(3))

    def noisy(self, v, k=0.3, seed=1):
        return v + k * np.linalg.norm(v) / 16 * np.random.default_rng(seed).normal(size=v.shape)

    def test_save_match_one_to_one(self):
        voices.save_voice("Lan", self.lan, self.path)
        voices.save_voice("Minh", self.minh, self.path)
        m = voices.match({"S0": self.noisy(self.minh), "S1": self.noisy(self.lan, seed=2),
                          "S2": self.other}, path=self.path)
        self.assertEqual({k: v["name"] for k, v in m.items()}, {"S0": "Minh", "S1": "Lan"})

    def test_same_voice_not_given_twice(self):
        voices.save_voice("Lan", self.lan, self.path)
        m = voices.match({"S0": self.noisy(self.lan, seed=3), "S1": self.noisy(self.lan, k=0.6, seed=4)},
                         path=self.path)
        self.assertEqual(len(m), 1)

    def test_resave_averages_and_delete(self):
        voices.save_voice("Lan", self.lan, self.path)
        voices.save_voice("lan", self.noisy(self.lan), self.path)
        lst = voices.list_voices(self.path)
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["samples"], 2)
        self.assertTrue(voices.delete_voice("LAN", self.path))
        self.assertEqual(voices.list_voices(self.path), [])

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            voices.save_voice("", self.lan, self.path)
        with self.assertRaises(ValueError):
            voices.save_voice("x", np.zeros(256), self.path)
        self.assertEqual(voices.match({"S0": self.lan}, path=self.path), {})


if __name__ == "__main__":
    unittest.main()
