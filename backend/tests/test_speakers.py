import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import speakers  # noqa: E402


def seg(text, words, start=None, end=None, **kw):
    ws = [{"word": w, "start": s, "end": e} for w, s, e in words]
    return {"type": "speech", "text": text, "words": ws,
            "start": ws[0]["start"] if start is None else start,
            "end": ws[-1]["end"] if end is None else end, **kw}


class AssignSpeakersTest(unittest.TestCase):
    def test_split_at_real_change_keeps_punctuation(self):
        s = seg("Bạn có khỏe không? Tôi khỏe, cảm ơn bạn.", [
            ("Bạn", 0.0, 0.3), ("có", 0.3, 0.5), ("khỏe", 0.5, 0.8), ("không?", 0.8, 1.2),
            ("Tôi", 1.6, 1.8), ("khỏe,", 1.8, 2.1), ("cảm", 2.2, 2.4), ("ơn", 2.4, 2.6), ("bạn.", 2.6, 3.0)])
        turns = [{"start": 0.0, "end": 1.3, "speaker": "S0"}, {"start": 1.5, "end": 3.1, "speaker": "S1"}]
        out = speakers.assign_speakers([s], turns)
        self.assertEqual([(o["text"], o["speaker"]) for o in out],
                         [("Bạn có khỏe không?", "S0"), ("Tôi khỏe, cảm ơn bạn.", "S1")])
        self.assertEqual((out[0]["start"], out[0]["end"]), (0.0, 1.6))
        self.assertEqual((out[1]["start"], out[1]["end"]), (1.6, 3.0))
        self.assertEqual([w["word"] for w in out[1]["words"]][0], "Tôi")

    def test_no_split_takes_majority_speaker(self):
        s = seg("a b c d e", [("a", 0.0, 0.5), ("b", 0.5, 1.0), ("c", 1.0, 1.5), ("d", 2.0, 4.0), ("e", 4.0, 6.0)])
        turns = [{"start": 0.0, "end": 1.6, "speaker": "S0"}, {"start": 1.9, "end": 6.1, "speaker": "S1"}]
        out = speakers.assign_speakers([s], turns, split=False)
        self.assertEqual([(o["text"], o["speaker"]) for o in out], [("a b c d e", "S1")])

    def test_single_word_jitter_is_smoothed(self):
        # Diarization boundary lands one word late: "là" is covered by S1.
        s = seg("Tôi là một bác sĩ", [
            ("Tôi", 0.0, 0.3), ("là", 0.3, 0.5), ("một", 0.5, 0.7), ("bác", 0.7, 0.9), ("sĩ", 0.9, 1.2)])
        turns = [{"start": 0.0, "end": 0.32, "speaker": "S1"}, {"start": 0.32, "end": 1.3, "speaker": "S0"}]
        out = speakers.assign_speakers([s], turns)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["speaker"], "S0")
        self.assertEqual(out[0]["text"], "Tôi là một bác sĩ")

    def test_edited_text_is_not_split(self):
        s = seg("Hello there friend", [("Hello", 0.0, 0.5), ("their", 0.5, 1.0), ("friend", 2.0, 2.5)])
        turns = [{"start": 0.0, "end": 1.1, "speaker": "S0"}, {"start": 1.9, "end": 2.6, "speaker": "S1"}]
        out = speakers.assign_speakers([s], turns)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["speaker"], "S0")

    def test_uncovered_words_snap_to_nearest_turn(self):
        s = seg("one two three four", [("one", 5.0, 5.2), ("two", 5.2, 5.4), ("three", 5.4, 5.6), ("four", 5.6, 5.8)])
        out = speakers.assign_speakers([s], [{"start": 6.2, "end": 9.0, "speaker": "S2"}])
        self.assertEqual(out[0]["speaker"], "S2")

    def test_segment_without_words_uses_dominant(self):
        s = {"type": "speech", "text": "x", "start": 0.0, "end": 4.0}
        turns = [{"start": 0.0, "end": 1.0, "speaker": "A"}, {"start": 1.0, "end": 4.0, "speaker": "B"}]
        self.assertEqual(speakers.assign_speakers([s], turns)[0]["speaker"], "B")

    def test_non_speech_passthrough_and_order(self):
        segs = [{"type": "silence", "start": 0, "end": 1},
                seg("b b", [("b", 3.0, 3.4), ("b", 3.4, 3.9)]),
                seg("a a", [("a", 1.0, 1.4), ("a", 1.4, 1.9)])]
        turns = [{"start": 1.0, "end": 2.0, "speaker": "X"}, {"start": 3.0, "end": 4.0, "speaker": "Y"}]
        out = speakers.assign_speakers(segs, turns)
        self.assertNotIn("speaker", out[0])
        self.assertEqual(speakers.speaker_order(out), ["X", "Y"])
        self.assertEqual(speakers.default_labels(["X", "Y", "Z"], {"Y": "Lan"}),
                         {"X": "Speaker A", "Y": "Lan", "Z": "Speaker B"})

    def test_cjk_text_without_spaces(self):
        s = seg("你好我很好", [("你", 0.0, 0.2), ("好", 0.2, 0.4), ("我", 1.0, 1.2), ("很", 1.2, 1.4), ("好", 1.4, 1.6)])
        turns = [{"start": 0.0, "end": 0.6, "speaker": "A"}, {"start": 0.9, "end": 1.7, "speaker": "B"}]
        out = speakers.assign_speakers([s], turns)
        self.assertEqual([o["text"] for o in out], ["你好", "我很好"])


if __name__ == "__main__":
    unittest.main()
