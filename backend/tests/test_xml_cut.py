import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import xml_cut  # noqa: E402
from xml_cut import TICKS_PER_SECOND  # noqa: E402

TPF25 = TICKS_PER_SECOND // 25


def rate(tb=25, ntsc="FALSE"):
    return f"<rate><timebase>{tb}</timebase><ntsc>{ntsc}</ntsc></rate>"


def file_def(fid="file-1", name="A001.mp4"):
    return (f'<file id="{fid}"><name>{name}</name>'
            f"<pathurl>file://localhost/C%3a/media/{name}</pathurl>{rate()}"
            "<duration>5000</duration><media><video/><audio/></media></file>")


def clip(cid, start, end, in_, out, links, full_file=True, kind="video", tpf=TPF25,
         extra="", clip_rate=None):
    f = file_def() if full_file else '<file id="file-1"/>'
    link_xml = "".join(
        f"<link><linkclipref>{ref}</linkclipref><mediatype>{mt}</mediatype>"
        f"<trackindex>{ti}</trackindex><clipindex>{ci}</clipindex></link>"
        for ref, mt, ti, ci in links)
    src = "" if kind == "video" else "<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>"
    return (f'<clipitem id="{cid}"><masterclipid>masterclip-1</masterclipid><name>A001.mp4</name>'
            f"<enabled>TRUE</enabled><duration>5000</duration>{clip_rate or rate()}"
            f"<start>{start}</start><end>{end}</end><in>{in_}</in><out>{out}</out>"
            f"<pproTicksIn>{in_ * tpf}</pproTicksIn><pproTicksOut>{out * tpf}</pproTicksOut>"
            f"{f}{src}{extra}{link_xml}</clipitem>")


def sequence(video_items, audio_tracks, duration, markers="", tb=25, ntsc="FALSE"):
    audio = "".join(f"<track>{items}<enabled>TRUE</enabled></track>" for items in audio_tracks)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
            f'<sequence id="sequence-1"><uuid>old-uuid</uuid><duration>{duration}</duration>'
            f"{rate(tb, ntsc)}<name>Interview</name><media>"
            f"<video><format/><track>{video_items}<enabled>TRUE</enabled><locked>FALSE</locked></track></video>"
            f"<audio>{audio}</audio></media>{markers}</sequence></xmeml>")


def linked_av(start=0, end=750, in_=100, out=850):
    links = [("clipitem-1", "video", 1, 1), ("clipitem-2", "audio", 1, 1), ("clipitem-3", "audio", 2, 1)]
    v = clip("clipitem-1", start, end, in_, out, links)
    a1 = clip("clipitem-2", start, end, in_, out, links, full_file=False, kind="audio")
    a2 = clip("clipitem-3", start, end, in_, out, links, full_file=False, kind="audio")
    return v, [a1, a2]


def frames(*pairs):
    return [[a * TPF25, b * TPF25] for a, b in pairs]


class XmlCutTest(unittest.TestCase):
    def run_cut(self, xml_text, cuts_ticks):
        d = tempfile.mkdtemp()
        src, dst = os.path.join(d, "in.xml"), os.path.join(d, "out.xml")
        with open(src, "w", encoding="utf-8") as f:
            f.write(xml_text)
        res = xml_cut.cut_sequence_xml(src, dst, cuts_ticks)
        with open(dst, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>'))
        return res, ET.fromstring(text.split("\n", 2)[2])

    @staticmethod
    def items(root, kind, track=1):
        tr = root.findall(f"sequence/media/{kind}/track")[track - 1]
        return [c for c in tr if c.tag in ("clipitem", "generatoritem", "transitionitem")]

    @staticmethod
    def se(c):
        return tuple(int(c.findtext(k)) for k in ("start", "end", "in", "out"))

    def test_linked_clip_split(self):
        v, a = linked_av()
        res, root = self.run_cut(sequence(v, a, 750), frames((100, 150), (400, 500)))
        self.assertEqual(res["removed_frames"], 150)
        self.assertEqual(root.findtext("sequence/duration"), "600")
        self.assertEqual(root.findtext("sequence/name"), "Interview (EasyScript cut)")
        self.assertNotEqual(root.findtext("sequence/uuid"), "old-uuid")
        for kind, track in (("video", 1), ("audio", 1), ("audio", 2)):
            got = [self.se(c) for c in self.items(root, kind, track)]
            self.assertEqual(got, [(0, 100, 100, 200), (100, 350, 250, 500), (350, 600, 600, 850)])
        # pproTicks follow the new source range exactly.
        c = self.items(root, "video")[1]
        self.assertEqual(int(c.findtext("pproTicksIn")), 250 * TPF25)
        self.assertEqual(int(c.findtext("pproTicksOut")), 500 * TPF25)
        # Each video piece links to the audio pieces of the same kept interval.
        for k, vc in enumerate(self.items(root, "video"), start=1):
            refs = {l.findtext("linkclipref") for l in vc.findall("link")}
            self.assertEqual(refs, {f"clipitem-1-{k}", f"clipitem-2-{k}", f"clipitem-3-{k}"})
            for l in vc.findall("link"):
                self.assertEqual(l.findtext("clipindex"), str(k))

    def test_file_definition_survives_removed_holder(self):
        # The first clip carries the only full <file> definition and is cut away.
        links1 = [("clipitem-1", "video", 1, 1)]
        links2 = [("clipitem-4", "video", 1, 2)]
        v = (clip("clipitem-1", 0, 100, 0, 100, links1)
             + clip("clipitem-4", 100, 300, 200, 400, links2, full_file=False))
        _, root = self.run_cut(sequence(v, [""], 300), frames((0, 100)))
        items = self.items(root, "video")
        self.assertEqual(len(items), 1)
        self.assertEqual(self.se(items[0]), (0, 200, 200, 400))
        f = items[0].find("file")
        self.assertIsNotNone(f.find("pathurl"))   # full definition moved here

    def test_duplicate_file_defs_collapsed(self):
        v, a = linked_av()
        _, root = self.run_cut(sequence(v, a, 750), frames((100, 150)))
        files = list(root.iter("file"))
        full = [f for f in files if len(f)]
        self.assertEqual(len(full), 1)
        self.assertIs(files[0], full[0])

    def test_clip_inside_cut_removed_and_links_dropped(self):
        links = [("clipitem-1", "video", 1, 1), ("clipitem-2", "audio", 1, 1)]
        v = clip("clipitem-1", 0, 100, 0, 100, links) + clip("clipitem-9", 100, 200, 0, 100, [("clipitem-9", "video", 1, 2)], full_file=False)
        a = [clip("clipitem-2", 0, 60, 0, 60, links, full_file=False, kind="audio")]
        res, root = self.run_cut(sequence(v, a, 200), frames((0, 60)))
        self.assertEqual([self.se(c) for c in self.items(root, "audio")], [])
        vids = self.items(root, "video")
        self.assertEqual([self.se(c) for c in vids], [(0, 40, 60, 100), (40, 140, 0, 100)])
        refs = {l.findtext("linkclipref") for l in vids[0].findall("link")}
        self.assertEqual(refs, {"clipitem-1"})

    def test_markers_shift(self):
        v, a = linked_av()
        markers = ("<marker><name>M1</name><in>300</in><out>-1</out></marker>"
                   "<marker><name>M2</name><in>120</in><out>-1</out></marker>")
        _, root = self.run_cut(sequence(v, a, 750, markers), frames((100, 150)))
        ins = [m.findtext("in") for m in root.findall("sequence/marker")]
        self.assertEqual(ins, ["250", "100"])  # M2 sat inside the cut → cut start

    def test_transition_kept_or_dropped(self):
        links_a = [("clipitem-1", "video", 1, 1)]
        links_b = [("clipitem-5", "video", 1, 2)]
        trans = ("<transitionitem><start>190</start><end>210</end><alignment>center</alignment>"
                 "<effect><name>Cross Dissolve</name></effect></transitionitem>")
        v = (clip("clipitem-1", 0, -1, 0, 200, links_a) + trans
             + clip("clipitem-5", -1, 400, 300, 500, links_b, full_file=False))
        xml_text = sequence(v, [""], 400)
        # Cut far from the transition: transition shifted, -1 edges preserved.
        _, root = self.run_cut(xml_text, frames((50, 100)))
        items = self.items(root, "video")
        self.assertEqual([i.tag for i in items], ["clipitem", "clipitem", "transitionitem", "clipitem"])
        self.assertEqual(self.se(items[0]), (0, 50, 0, 50))
        self.assertEqual((items[1].findtext("start"), items[1].findtext("end")), ("50", "-1"))
        self.assertEqual(items[1].findtext("in"), "100")
        self.assertEqual((items[2].findtext("start"), items[2].findtext("end")), ("140", "160"))
        self.assertEqual(items[3].findtext("start"), "-1")
        self.assertEqual(items[3].findtext("end"), "350")
        # Cut through the transition: dropped, edges made explicit at the edit point.
        res, root = self.run_cut(xml_text, frames((195, 205)))
        items = self.items(root, "video")
        self.assertEqual([i.tag for i in items], ["clipitem", "clipitem"])
        self.assertEqual(items[0].findtext("end"), "195")
        self.assertEqual(items[1].findtext("start"), "195")
        self.assertTrue(any("transition" in w for w in res["warnings"]))

    def test_ntsc_exact_ticks(self):
        tpf = xml_cut.ticks_per_frame(30, True)
        self.assertEqual(tpf, 8475667200)
        self.assertEqual(xml_cut.ticks_per_frame(24, True), 10594584000)
        links = [("clipitem-1", "video", 1, 1)]
        v = clip("clipitem-1", 0, 108000, 0, 108000, links, tpf=tpf, clip_rate=rate(30, "TRUE"))
        res, root = self.run_cut(sequence(v, [""], 108000, tb=30, ntsc="TRUE"),
                                 [[53946 * tpf, 53976 * tpf]])
        self.assertEqual(res["removed_frames"], 30)
        got = [self.se(c) for c in self.items(root, "video")]
        self.assertEqual(got, [(0, 53946, 0, 53946), (53946, 107970, 53976, 108000)])

    def test_clip_rate_differs_from_sequence(self):
        # 25 fps media in a 29.97 sequence: in/out are in the clip's own frames.
        tpf = xml_cut.ticks_per_frame(30, True)
        links = [("clipitem-1", "video", 1, 1)]
        v = clip("clipitem-1", 0, 3000, 0, 2502, links, tpf=TPF25)
        _, root = self.run_cut(sequence(v, [""], 3000, tb=30, ntsc="TRUE"),
                               [[1200 * tpf, 1230 * tpf]])
        second = self.items(root, "video")[1]
        # 1230 sequence frames = 41.041 s = 1026 frames at 25 fps
        self.assertEqual(int(second.findtext("in")), round(1230 * tpf / TPF25))
        self.assertEqual(int(second.findtext("pproTicksIn")), 1230 * tpf)

    def test_cut_list_normalized(self):
        v, a = linked_av()
        res, _ = self.run_cut(sequence(v, a, 750), frames((400, 500), (100, 150), (140, 160), (700, 900)))
        self.assertEqual(res["cuts"], 3)
        self.assertEqual(res["removed_frames"], 60 + 100 + 50)

    def run_split(self, xml_text, cuts=(), splits=(), labels=(), only_media=None):
        d = tempfile.mkdtemp()
        src, dst = os.path.join(d, "in.xml"), os.path.join(d, "out.xml")
        with open(src, "w", encoding="utf-8") as f:
            f.write(xml_text)
        res = xml_cut.cut_sequence_xml(src, dst, list(cuts), splits_ticks=list(splits), labels=list(labels),
                                       only_media=only_media)
        with open(dst, encoding="utf-8") as f:
            return res, ET.fromstring(f.read().split("\n", 2)[2])

    def test_split_points_without_removal(self):
        v, a = linked_av()
        res, root = self.run_split(sequence(v, a, 750), splits=[300 * TPF25, 520 * TPF25 + 7])
        self.assertEqual((res["removed_frames"], res["splits"]), (0, 2))
        self.assertEqual(root.findtext("sequence/duration"), "750")
        for kind, track in (("video", 1), ("audio", 1), ("audio", 2)):
            got = [self.se(c) for c in self.items(root, kind, track)]
            self.assertEqual(got, [(0, 300, 100, 400), (300, 520, 400, 620), (520, 750, 620, 850)])
        for k, vc in enumerate(self.items(root, "video"), start=1):
            refs = {l.findtext("linkclipref") for l in vc.findall("link")}
            self.assertEqual(refs, {f"clipitem-1-{k}", f"clipitem-2-{k}", f"clipitem-3-{k}"})

    def test_split_with_cut_and_labels(self):
        v, a = linked_av()
        labels = [{"start_ticks": 0, "end_ticks": 300 * TPF25, "name": "Lan", "color": 4},
                  {"start_ticks": 300 * TPF25, "end_ticks": 750 * TPF25, "name": "Minh", "color": "mango"}]
        # A split inside the cut is dropped; the cut removes 100..150.
        res, root = self.run_split(sequence(v, a, 750), cuts=frames((100, 150)),
                                   splits=[120 * TPF25, 300 * TPF25], labels=labels)
        self.assertEqual(res["splits"], 1)
        got = [self.se(c) for c in self.items(root, "video")]
        self.assertEqual(got, [(0, 100, 100, 200), (100, 250, 250, 400), (250, 700, 400, 850)])
        for kind, track in (("video", 1), ("audio", 2)):
            names = [c.findtext("name") for c in self.items(root, kind, track)]
            colors = [c.findtext("labels/label2") for c in self.items(root, kind, track)]
            self.assertEqual(names, ["Lan", "Lan", "Minh"])
            self.assertEqual(colors, ["Cerulean", "Cerulean", "Mango"])
        self.assertEqual(res["labeled"], 9)

    def test_only_media_leaves_music_untouched(self):
        v, a = linked_av()
        music = ('<clipitem id="clipitem-9"><name>song.wav</name><duration>9000</duration>'
                 f"{rate()}<start>0</start><end>750</end><in>0</in><out>750</out>"
                 '<file id="file-9"><name>song.wav</name>'
                 "<pathurl>file://localhost/C%3a/music/song.wav</pathurl></file></clipitem>")
        labels = [{"start_ticks": 0, "end_ticks": 750 * TPF25, "name": "Lan", "color": 1}]
        res, root = self.run_split(sequence(v, a + [music], 750), splits=[300 * TPF25], labels=labels,
                                   only_media=[r"C:\media\A001.mp4"])
        self.assertEqual(len(self.items(root, "video")), 2)
        self.assertEqual(len(self.items(root, "audio", 2)), 2)   # linked to the video: split
        m = self.items(root, "audio", 3)
        self.assertEqual([self.se(c) for c in m], [(0, 750, 0, 750)])
        self.assertEqual(m[0].findtext("name"), "song.wav")
        self.assertIsNone(m[0].find("labels"))
        self.assertEqual(res["labeled"], 6)

    def test_split_skips_transition_edges(self):
        links = []
        c1 = clip("clipitem-1", 0, -1, 0, 260, links)
        tr = ('<transitionitem><start>240</start><end>260</end><alignment>center</alignment>'
              f"{rate()}<effect><name>Cross Dissolve</name></effect></transitionitem>")
        c2 = clip("clipitem-2", -1, 500, 10, 260, links, full_file=False)
        res, root = self.run_split(sequence(c1 + tr + c2, [], 500), splits=[250 * TPF25, 400 * TPF25])
        self.assertEqual(res["splits"], 1)
        kinds = [c.tag for c in self.items(root, "video")]
        self.assertEqual(kinds, ["clipitem", "transitionitem", "clipitem", "clipitem"])


if __name__ == "__main__":
    unittest.main()
