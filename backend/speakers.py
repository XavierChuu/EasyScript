"""
Word-level speaker attribution for transcript segments.

Diarization gives speaker turns; Whisper gives segments with word timestamps.
Attributing a whole segment to its dominant speaker mislabels every line in
which two people speak (a question and its quick answer, an interjection).
Here each word gets the speaker that covers it, runs that are too short to be
a real turn are smoothed away (diarization boundaries jitter by a few hundred
ms), and a segment is split where the speaker really changes.

Segments whose text no longer matches their words (edited in the panel) are
not split; they keep a single, dominant speaker.
"""

import bisect
import re
import unicodedata

# A run of words shorter than this (in both words and seconds) is treated as
# boundary jitter and merged into its neighbours instead of becoming a line.
MIN_RUN_WORDS = 2
MIN_RUN_SEC = 0.6
# A word not covered by any turn takes the nearest turn within this distance.
MAX_SNAP_SEC = 1.0


def _norm(text):
    text = unicodedata.normalize("NFC", text or "").lower()
    return re.sub(r"[\W_]+", "", text)


class _Turns:
    """Sorted, non-overlapping speaker turns with overlap / nearest lookups."""

    def __init__(self, turns):
        self.turns = sorted((float(t["start"]), float(t["end"]), t["speaker"])
                            for t in turns if float(t["end"]) > float(t["start"]))
        self.starts = [t[0] for t in self.turns]

    def __bool__(self):
        return bool(self.turns)

    def overlaps(self, a, b):
        """{speaker: seconds} covered inside [a, b]."""
        out = {}
        i = max(0, bisect.bisect_right(self.starts, a) - 1)
        while i < len(self.turns) and self.turns[i][0] < b:
            s, e, spk = self.turns[i]
            ov = min(e, b) - max(s, a)
            if ov > 0:
                out[spk] = out.get(spk, 0.0) + ov
            i += 1
        return out

    def at(self, a, b):
        """Speaker covering most of [a, b]; else the nearest one within MAX_SNAP_SEC."""
        if b <= a:
            b = a + 0.01
        ov = self.overlaps(a, b)
        if ov:
            return max(ov.items(), key=lambda kv: kv[1])[0]
        mid = (a + b) / 2
        i = bisect.bisect_right(self.starts, mid)
        best, dist = None, MAX_SNAP_SEC
        for j in (i - 1, i):
            if 0 <= j < len(self.turns):
                s, e, spk = self.turns[j]
                d = s - mid if mid < s else mid - e
                if d <= dist:
                    best, dist = spk, d
        return best

    def dominant(self, a, b):
        ov = self.overlaps(a, b)
        return max(ov.items(), key=lambda kv: kv[1])[0] if ov else self.at(a, b)


def _runs(labels):
    """[(speaker, i0, i1)] maximal runs of equal labels (i1 exclusive)."""
    runs, i0 = [], 0
    for i in range(1, len(labels) + 1):
        if i == len(labels) or labels[i] != labels[i0]:
            runs.append([labels[i0], i0, i])
            i0 = i
    return runs


def _smooth(labels, words):
    """Merge runs too short to be a real turn into the neighbouring run."""
    labels = list(labels)
    for _ in range(3):
        runs = _runs(labels)
        if len(runs) <= 1:
            break
        changed = False
        for k, (spk, i0, i1) in enumerate(runs):
            n = i1 - i0
            dur = float(words[i1 - 1]["end"]) - float(words[i0]["start"])
            if n >= MIN_RUN_WORDS or dur >= MIN_RUN_SEC:
                continue
            prev = runs[k - 1] if k > 0 else None
            nxt = runs[k + 1] if k + 1 < len(runs) else None
            if prev and nxt:
                target = prev[0] if (prev[2] - prev[1]) >= (nxt[2] - nxt[1]) else nxt[0]
                if prev[0] == nxt[0]:
                    target = prev[0]
            else:
                target = (prev or nxt)[0]
            for i in range(i0, i1):
                labels[i] = target
            changed = True
        if not changed:
            break
    return labels


def _word_spans(text, words):
    """Character span of every word inside `text`, or None if they don't line up."""
    spans, cur = [], 0
    low = unicodedata.normalize("NFC", text)
    for w in words:
        tok = unicodedata.normalize("NFC", (w.get("word") or "").strip())
        if not tok:
            spans.append((cur, cur))
            continue
        j = low.find(tok, cur)
        if j < 0 or (j - cur) > 3 + len(tok):
            return None
        spans.append((j, j + len(tok)))
        cur = j + len(tok)
    return spans


def _text_matches(seg):
    words = seg.get("words") or []
    if not words:
        return False
    return _norm(seg.get("text")) == _norm("".join(w.get("word") or "" for w in words))


def _piece_text(text, spans, words, i0, i1, last):
    if spans is not None:
        a = spans[i0][0]
        b = len(text) if last else spans[i1][0]
        return text[a:b].strip()
    sep = "" if (text and " " not in text.strip()) else " "
    return sep.join((w.get("word") or "").strip() for w in words[i0:i1]).strip()


def assign_speakers(segments, turns, split=True):
    """Return new segments with `speaker` set, split at real speaker changes.

    segments: [{start, end, text, words?: [{word, start, end, ...}], ...}]
    turns:    [{start, end, speaker}] — exclusive (non-overlapping) diarization
    split:    False keeps one segment per input segment (e.g. when translations
              are indexed by segment) and gives it the speaker of most words.
    Non-speech segments pass through unchanged.
    """
    tl = _Turns(turns)
    out = []
    for seg in segments:
        if seg.get("type", "speech") != "speech" or not tl:
            out.append(dict(seg))
            continue
        s0, s1 = float(seg["start"]), float(seg["end"])
        words = [w for w in (seg.get("words") or [])
                 if w.get("start") is not None and w.get("end") is not None]
        if not words or not _text_matches(seg):
            new = dict(seg)
            spk = tl.dominant(s0, s1)
            if spk:
                new["speaker"] = spk
            out.append(new)
            continue

        labels = [tl.at(float(w["start"]), float(w["end"])) for w in words]
        # Uncovered words inherit from their neighbours.
        for i in range(len(labels)):
            if labels[i] is None and i > 0:
                labels[i] = labels[i - 1]
        for i in range(len(labels) - 2, -1, -1):
            if labels[i] is None:
                labels[i] = labels[i + 1]
        if all(lab is None for lab in labels):
            new = dict(seg)
            spk = tl.dominant(s0, s1)
            if spk:
                new["speaker"] = spk
            out.append(new)
            continue

        labels = _smooth(labels, words)
        if not split:
            new = dict(seg)
            share = {}
            for w, lab in zip(words, labels):
                if lab:
                    share[lab] = share.get(lab, 0.0) + max(0.01, float(w["end"]) - float(w["start"]))
            if share:
                new["speaker"] = max(share.items(), key=lambda kv: kv[1])[0]
            out.append(new)
            continue
        runs = _runs(labels)
        text = seg.get("text") or ""
        spans = _word_spans(text, words)
        for k, (spk, i0, i1) in enumerate(runs):
            last = k == len(runs) - 1
            piece = dict(seg)
            piece["words"] = words[i0:i1]
            piece["text"] = _piece_text(text, spans, words, i0, i1, last)
            piece["start"] = round(s0 if k == 0 else float(words[i0]["start"]), 3)
            piece["end"] = round(s1 if last else float(words[i1]["start"]), 3)
            if spk:
                piece["speaker"] = spk
            if len(runs) > 1:
                piece["split_from"] = round(s0, 3)
            out.append(piece)
    return out


def speaker_order(segments):
    """Speakers in order of first appearance in the transcript."""
    seen = []
    for seg in sorted(segments, key=lambda s: float(s.get("start", 0))):
        spk = seg.get("speaker")
        if spk and spk not in seen and spk != "UNKNOWN":
            seen.append(spk)
    return seen


def default_labels(speakers, named=None):
    """{speaker_id: display label}; `named` (e.g. matched voices) wins."""
    named = named or {}
    labels, n = {}, 0
    for spk in speakers:
        if spk in named:
            labels[spk] = named[spk]
            continue
        labels[spk] = f"Speaker {chr(65 + n)}" if n < 26 else f"Speaker {n + 1}"
        n += 1
    return labels
