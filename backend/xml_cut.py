"""
Cut a Premiere-exported FCP7 XML (xmeml) sequence.

Removes sequence-time ranges from every track and closes the gaps, producing a
new sequence that Premiere imports in one step — instead of one timeline edit
(and one undo step) per silence region.

Times inside the XML are integer frames of the sequence <rate>. Cut ranges come
in as sequence ticks (254016000000 per second) and are converted with the exact
ticks-per-frame of that rate, so NTSC rates (29.97, 23.976, 59.94) stay exact.

Handled: linked A/V pieces (links rewritten per kept interval), clip in/out and
pproTicks offsets (with constant speed / reverse), transitions (kept and shifted
when no cut touches them, dropped otherwise), start/end = -1 edges next to
transitions, sequence markers, duplicate <file>/nested <sequence> definitions.
Not adjusted: effect keyframes inside split clips (reported as a warning).
"""

import bisect
import copy
import os
import re
import uuid
import xml.etree.ElementTree as ET

TICKS_PER_SECOND = 254016000000
CLIP_TAGS = ("clipitem", "generatoritem")
ITEM_TAGS = CLIP_TAGS + ("transitionitem",)


class XmlCutError(Exception):
    pass


# ── Small helpers ──

def _int(text, default=0):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        try:
            return int(round(float(str(text).strip())))
        except (TypeError, ValueError):
            return default


def _set_text(parent, tag, value):
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    el.text = str(value)
    return el


def ticks_per_frame(timebase, ntsc):
    if timebase <= 0:
        raise XmlCutError(f"invalid timebase {timebase}")
    if ntsc:
        return TICKS_PER_SECOND * 1001 // (timebase * 1000)
    return TICKS_PER_SECOND // timebase


def _rate_tpf(el, default=None):
    rate = el.find("rate") if el is not None else None
    if rate is None:
        return default
    tb = _int(rate.findtext("timebase"), 0)
    if tb <= 0:
        return default
    ntsc = (rate.findtext("ntsc") or "").strip().upper() == "TRUE"
    return ticks_per_frame(tb, ntsc)


def _speed_of(clip):
    """Constant playback speed of a clip from its Time Remapping effect.
    Returns (speed_factor, reversed, variable)."""
    speed, reverse, variable = 1.0, False, False
    for effect in clip.iter("effect"):
        if (effect.findtext("effectid") or "").strip().lower() != "timeremap":
            continue
        for param in effect.iter("parameter"):
            pid = (param.findtext("parameterid") or "").strip().lower()
            if pid == "speed":
                try:
                    v = float(param.findtext("value") or 100)
                    if v != 0:
                        speed = abs(v) / 100.0
                        reverse = reverse or v < 0
                except ValueError:
                    pass
            elif pid == "reverse":
                reverse = reverse or (param.findtext("value") or "").strip().upper() == "TRUE"
            elif pid == "graphdict" and param.find("keyframe") is not None:
                variable = True
    return speed, reverse, variable


def _transition_cut_point(t):
    """Edit point under a transition (same rule as OpenTimelineIO's FCP7 adapter)."""
    align = (t.findtext("alignment") or "center").strip().lower()
    start, end = _int(t.findtext("start")), _int(t.findtext("end"))
    if align in ("end", "end-black"):
        return end
    if align in ("start", "start-black"):
        return start
    return (start + end) // 2


class CutMap:
    """Sorted, merged cut ranges [a, b) in frames, with removed-time lookups."""

    def __init__(self, cuts):
        self.cuts = cuts
        self.starts = [a for a, _ in cuts]
        self.ends = [b for _, b in cuts]
        self.prefix = [0]
        for a, b in cuts:
            self.prefix.append(self.prefix[-1] + (b - a))

    @property
    def total(self):
        return self.prefix[-1]

    def removed_before(self, f):
        i = bisect.bisect_right(self.starts, f) - 1
        if i < 0:
            return 0
        a, b = self.cuts[i]
        if f >= b:
            return self.prefix[i + 1]
        return self.prefix[i] + (f - a)

    def map(self, f):
        """New position of frame f (a frame inside a cut collapses to the cut start)."""
        return f - self.removed_before(f)

    def keep_index(self, f):
        """Index of the kept interval containing f = number of cuts ending at or before f."""
        return bisect.bisect_right(self.ends, f)

    def pieces(self, s, e):
        """[s, e) minus all cuts."""
        out, cur = [], s
        i = bisect.bisect_right(self.ends, s)
        while i < len(self.cuts) and cur < e:
            a, b = self.cuts[i]
            if a >= e:
                break
            if a > cur:
                out.append((cur, min(a, e)))
            cur = max(cur, b)
            i += 1
        if cur < e:
            out.append((cur, e))
        return out

    def touches(self, a, b):
        """True if any cut removes frames inside [a, b)."""
        i = bisect.bisect_right(self.ends, a)
        return i < len(self.cuts) and self.cuts[i][0] < b


def normalize_cuts(cuts, total):
    rng = sorted((max(0, int(a)), min(int(total), int(b))) for a, b in cuts)
    merged = []
    for a, b in rng:
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


# ── Core ──

def _top_sequence(root):
    seq = root.find("sequence")
    if seq is None:
        # Some exports wrap the sequence in a <project><children> bin.
        seq = root.find(".//children/sequence")
    if seq is None:
        seq = root.find(".//sequence")
    if seq is None:
        raise XmlCutError("No <sequence> found in XML")
    return seq


def _tracks(seq):
    media = seq.find("media")
    if media is None:
        return []
    out = []
    for kind in ("video", "audio"):
        group = media.find(kind)
        if group is None:
            continue
        for idx, track in enumerate(group.findall("track"), start=1):
            out.append((kind, idx, track))
    return out


def _resolve_track(items):
    """Resolve clip edges that are -1 because a transition sits there."""
    resolved = []
    for i, it in enumerate(items):
        if it.tag == "transitionitem":
            resolved.append({"el": it, "kind": "t",
                             "s": _int(it.findtext("start")), "e": _int(it.findtext("end"))})
            continue
        s, e = _int(it.findtext("start"), -1), _int(it.findtext("end"), -1)
        prev_t = items[i - 1] if i > 0 and items[i - 1].tag == "transitionitem" else None
        next_t = items[i + 1] if i + 1 < len(items) and items[i + 1].tag == "transitionitem" else None
        s_trans = s < 0 and prev_t is not None
        e_trans = e < 0 and next_t is not None
        if s < 0:
            s = _transition_cut_point(prev_t) if prev_t is not None else None
        if e < 0:
            e = _transition_cut_point(next_t) if next_t is not None else None
        if s is None or e is None:
            length = _int(it.findtext("out")) - _int(it.findtext("in"))
            if s is None and e is not None:
                s = e - length
            elif e is None and s is not None:
                e = s + length
            else:
                s, e = 0, max(0, length)
        resolved.append({"el": it, "kind": "c", "s": s, "e": e,
                         "s_trans": s_trans, "e_trans": e_trans,
                         "prev_t": prev_t, "next_t": next_t})
    return resolved


def _clip_like_index_mode(seq):
    """Does <clipindex> count transitions too? Infer from the original links."""
    positions = {}
    for _kind, _idx, track in _tracks(seq):
        all_i = clip_i = 0
        for ch in track:
            if ch.tag in ITEM_TAGS:
                all_i += 1
                if ch.tag in CLIP_TAGS:
                    clip_i += 1
                    positions[ch.get("id")] = (clip_i, all_i)
    votes_all = votes_clip = 0
    for link in seq.iter("link"):
        ref = link.findtext("linkclipref")
        ci = _int(link.findtext("clipindex"), -1)
        if ref in positions and ci > 0:
            c, a = positions[ref]
            if ci == a and ci != c:
                votes_all += 1
            elif ci == c and ci != a:
                votes_clip += 1
    return "all" if votes_all > votes_clip else "clip"


def _collect_definitions(root, top_seq):
    """Full <sequence id>/<file id> definitions, captured before any clipitem
    is removed (the one holding a definition may be cut away entirely)."""
    defs = {}
    for tag in ("sequence", "file"):
        full = {}
        for el in root.iter(tag):
            if el is top_seq:
                continue
            fid = el.get("id")
            if fid and len(el) and fid not in full:
                full[fid] = copy.deepcopy(el)
        defs[tag] = full
    return defs


def _normalize_definitions(root, top_seq, defs):
    """After duplicating/removing clipitems, make the first remaining occurrence
    of every <file id> (and nested <sequence id>) the full definition and turn
    later ones into bare references, as Premiere/FCP expect."""
    for tag in ("sequence", "file"):
        full = defs.get(tag, {})
        seen = set()
        for el in list(root.iter(tag)):
            if el is top_seq:
                continue
            fid = el.get("id")
            if not fid:
                continue
            if fid not in seen:
                seen.add(fid)
                if len(el) == 0 and fid in full:
                    for child in list(full[fid]):
                        el.append(copy.deepcopy(child))
                    for k, v in full[fid].attrib.items():
                        el.set(k, v)
            elif len(el):
                for child in list(el):
                    el.remove(child)


def cut_sequence_xml(src_path, dst_path, cuts_ticks, name_suffix=" (EasyScript cut)"):
    """Write a cut copy of the sequence in `src_path` to `dst_path`.

    cuts_ticks: [[start_ticks, end_ticks], ...] in sequence time.
    Returns a summary dict (name, removed frames/seconds, clip counts, warnings).
    """
    try:
        tree = ET.parse(src_path)
    except ET.ParseError as e:
        raise XmlCutError(f"Cannot parse XML: {e}")
    root = tree.getroot()
    seq = _top_sequence(root)
    tpf = _rate_tpf(seq)
    if not tpf:
        raise XmlCutError("Sequence has no <rate>")

    tracks = _tracks(seq)
    duration = _int(seq.findtext("duration"), 0)
    for _k, _i, track in tracks:
        for ch in track:
            if ch.tag in ITEM_TAGS:
                duration = max(duration, _int(ch.findtext("end"), 0))

    cuts = normalize_cuts(
        [(round(int(a) / tpf), round(int(b) / tpf)) for a, b in cuts_ticks], duration)
    cm = CutMap(cuts)
    warnings = []
    index_mode = _clip_like_index_mode(seq)
    defs = _collect_definitions(root, seq)

    pieces_by_key = {}   # (orig_id, keep_index) -> piece
    all_pieces = []
    clips_before = clips_after = 0
    dropped_transitions = keyframe_clips = variable_speed = 0

    for kind, track_index, track in tracks:
        items = [ch for ch in track if ch.tag in ITEM_TAGS]
        if not items:
            continue
        resolved = _resolve_track(items)
        keep_t = {}
        for r in resolved:
            if r["kind"] == "t":
                keep_t[id(r["el"])] = not cm.touches(r["s"], max(r["e"], r["s"] + 1))

        new_items = []
        for r in resolved:
            el = r["el"]
            if r["kind"] == "t":
                if keep_t[id(el)]:
                    ns = cm.map(r["s"])
                    _set_text(el, "start", ns)
                    _set_text(el, "end", ns + (r["e"] - r["s"]))
                    new_items.append(el)
                else:
                    dropped_transitions += 1
                continue

            clips_before += 1
            s, e = r["s"], r["e"]
            parts = cm.pieces(s, e)
            if not parts:
                continue
            clip_tpf = _rate_tpf(el, tpf)
            speed, reverse, variable = _speed_of(el)
            if variable:
                variable_speed += 1
            orig_id = el.get("id") or f"item-{uuid.uuid4().hex[:8]}"
            orig_in, orig_out = _int(el.findtext("in")), _int(el.findtext("out"))
            p_in = el.findtext("pproTicksIn")
            p_out = el.findtext("pproTicksOut")
            p_in = _int(p_in, None) if p_in is not None else None
            p_out = _int(p_out, None) if p_out is not None else None
            prev_kept = r["prev_t"] is not None and keep_t.get(id(r["prev_t"]), False)
            next_kept = r["next_t"] is not None and keep_t.get(id(r["next_t"]), False)
            if len(parts) > 1 and el.find(".//keyframe") is not None:
                keyframe_clips += 1

            untouched = len(parts) == 1 and parts[0] == (s, e)
            for k, (a, b) in enumerate(parts):
                piece = el if len(parts) == 1 else copy.deepcopy(el)
                new_id = orig_id if len(parts) == 1 else f"{orig_id}-{k + 1}"
                piece.set("id", new_id)
                first_edge = a == s and r["s_trans"] and prev_kept
                last_edge = b == e and r["e_trans"] and next_kept
                ns = cm.map(a)
                _set_text(piece, "start", -1 if first_edge else ns)
                _set_text(piece, "end", -1 if last_edge else ns + (b - a))
                if untouched:
                    # Only moved: keep the source range exactly as exported.
                    info = {"el": piece, "orig": orig_id, "id": new_id,
                            "keep": cm.keep_index(a), "kind": kind, "track": track_index}
                    pieces_by_key[(orig_id, info["keep"])] = info
                    all_pieces.append(info)
                    new_items.append(piece)
                    clips_after += 1
                    continue

                # Source offsets: timeline frames → source ticks → clip frames.
                off_ticks = round((a - s) * tpf * speed)
                len_ticks = round((b - a) * tpf * speed)
                if not reverse:
                    new_in = orig_in + round(off_ticks / clip_tpf)
                    new_out = orig_out if last_edge else new_in + round(len_ticks / clip_tpf)
                else:
                    # Reversed: the timeline runs from source out back towards in.
                    new_out = orig_out - round(off_ticks / clip_tpf)
                    new_in = orig_in if last_edge else new_out - round(len_ticks / clip_tpf)
                _set_text(piece, "in", new_in)
                _set_text(piece, "out", new_out)
                if p_in is not None and p_out is not None:
                    if not reverse:
                        np_in = p_in + off_ticks
                        np_out = p_out if last_edge else np_in + len_ticks
                    else:
                        np_out = p_out - off_ticks
                        np_in = p_in if last_edge else np_out - len_ticks
                    _set_text(piece, "pproTicksIn", np_in)
                    _set_text(piece, "pproTicksOut", np_out)

                info = {"el": piece, "orig": orig_id, "id": new_id,
                        "keep": cm.keep_index(a), "kind": kind, "track": track_index}
                pieces_by_key[(orig_id, info["keep"])] = info
                all_pieces.append(info)
                new_items.append(piece)
                clips_after += 1

        # Rebuild the track: items in timeline order, other children kept.
        others = [ch for ch in list(track) if ch.tag not in ITEM_TAGS]
        for ch in list(track):
            track.remove(ch)
        for it in new_items:
            track.append(it)
        for ch in others:
            track.append(ch)

        # Positions for <clipindex>.
        all_i = clip_i = 0
        pos = {}
        for ch in track:
            if ch.tag in ITEM_TAGS:
                all_i += 1
                if ch.tag in CLIP_TAGS:
                    clip_i += 1
                    pos[ch.get("id")] = all_i if index_mode == "all" else clip_i
        for p in all_pieces:
            if p["kind"] == kind and p["track"] == track_index and p["id"] in pos:
                p["clipindex"] = pos[p["id"]]

    # Links: a piece links to the pieces of its partners in the same kept interval.
    for p in all_pieces:
        for link in list(p["el"].findall("link")):
            ref = link.findtext("linkclipref")
            target = pieces_by_key.get((ref, p["keep"]))
            if target is None:
                p["el"].remove(link)
                continue
            _set_text(link, "linkclipref", target["id"])
            if link.find("clipindex") is not None and "clipindex" in target:
                _set_text(link, "clipindex", target["clipindex"])

    # Sequence-level: duration, name, uuid, markers.
    new_duration = max(0, duration - cm.removed_before(duration))
    _set_text(seq, "duration", new_duration)
    name = (seq.findtext("name") or "Sequence") + name_suffix
    _set_text(seq, "name", name)
    if seq.find("uuid") is not None:
        _set_text(seq, "uuid", str(uuid.uuid4()))
    for marker in seq.findall("marker"):
        m_in = _int(marker.findtext("in"), -1)
        m_out = _int(marker.findtext("out"), -1)
        if m_in >= 0:
            new_in = cm.map(m_in)
            _set_text(marker, "in", new_in)
            if m_out >= 0:
                _set_text(marker, "out", max(new_in, cm.map(m_out)))

    _normalize_definitions(root, seq, defs)

    if dropped_transitions:
        warnings.append(f"{dropped_transitions} transition(s) touched by a cut were removed")
    if keyframe_clips:
        warnings.append(f"{keyframe_clips} split clip(s) have keyframes — check their animation")
    if variable_speed:
        warnings.append(f"{variable_speed} clip(s) use variable speed — cut points inside them are approximate")

    os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
    body = ET.tostring(root, encoding="unicode")
    with open(dst_path, "w", encoding="utf-8", newline="\n") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        f.write(body)
        f.write("\n")

    return {
        "path": dst_path,
        "name": name,
        "cuts": len(cuts),
        "removed_frames": cm.total,
        "removed_seconds": round(cm.total * tpf / TICKS_PER_SECOND, 3),
        "duration_frames": new_duration,
        "clips_before": clips_before,
        "clips_after": clips_after,
        "ticks_per_frame": tpf,
        "warnings": warnings,
    }


def safe_filename(name, default="sequence"):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", (name or "").strip()).strip(" .")
    return (name or default)[:120]
