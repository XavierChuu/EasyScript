/**
 * EasyScript — ExtendScript Host (CEP)
 * Drives Premiere Pro from the panel: read the selected clip, razor + remove
 * silence on the timeline, import subtitles, and color clips by speaker.
 * NOTE: ExtendScript is ES3 — no let/const/arrow functions/JSON spread.
 */

/**
 * ExtendScript (4.5.6, still ES3 in Premiere 26.x) has no native JSON object.
 * Every host function below returns JSON.stringify(...), and the catch blocks
 * stringify too — so without this the error escapes the catch and CEP reports
 * only the opaque "EvalScript error." Guarded, so a host that does ship JSON
 * keeps its own.
 */
if (typeof JSON !== "object") { JSON = {}; }

if (typeof JSON.stringify !== "function") {
    JSON.stringify = (function () {
        var ESC = { "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r", '"': '\\"', "\\": "\\\\" };
        // Escape everything outside printable ASCII as \uXXXX — evalScript hands
        // the result back through a byte channel whose encoding we don't control.
        function quote(s) {
            var out = '"', i, c, code, hex;
            for (i = 0; i < s.length; i++) {
                c = s.charAt(i);
                if (ESC[c]) { out += ESC[c]; continue; }
                code = s.charCodeAt(i);
                if (code < 32 || code > 126) {
                    hex = code.toString(16);
                    while (hex.length < 4) hex = "0" + hex;
                    out += "\\u" + hex;
                } else { out += c; }
            }
            return out + '"';
        }
        // Returns undefined for values JSON has no representation for, so object
        // members get dropped and array members become null (per the spec).
        function str(v) {
            var t = typeof v, i, k, parts, piece;
            if (v === null) return "null";
            if (t === "boolean") return String(v);
            if (t === "number") return isFinite(v) ? String(v) : "null";
            if (t === "string") return quote(v);
            if (t !== "object") return undefined;  // undefined, function
            if (Object.prototype.toString.call(v) === "[object Array]") {
                parts = [];
                for (i = 0; i < v.length; i++) {
                    piece = str(v[i]);
                    parts[parts.length] = (piece === undefined) ? "null" : piece;
                }
                return "[" + parts.join(",") + "]";
            }
            parts = [];
            for (k in v) {
                if (!Object.prototype.hasOwnProperty.call(v, k)) continue;
                piece = str(v[k]);
                if (piece !== undefined) parts[parts.length] = quote(String(k)) + ":" + piece;
            }
            return "{" + parts.join(",") + "}";
        }
        return function (value) { return str(value); };
    })();
}

if (typeof JSON.parse !== "function") {
    JSON.parse = function (text) {
        var t = String(text);
        // Reject anything that isn't well-formed JSON before handing it to eval:
        // blank out escapes, then literals, then leading brackets — what remains
        // must be structural characters only.
        var probe = t
            .replace(/\\(?:["\\\/bfnrt]|u[0-9a-fA-F]{4})/g, "@")
            .replace(/"[^"\\\n\r]*"|true|false|null|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?/g, "]")
            .replace(/(?:^|:|,)(?:\s*\[)+/g, "");
        if (!/^[\],:{}\s]*$/.test(probe)) throw new Error("JSON.parse: malformed JSON");
        return eval("(" + t + ")");
    };
}

var TICKS_PER_SECOND = 254016000000;

/**
 * Exact ticks per frame of a sequence. `seq.timebase` is the canonical value
 * (a string: 8475667200 for 29.97, 10594584000 for 23.976). Never derive the
 * frame grid from a rounded fps — 30 instead of 29.97 drifts 3.6 s per hour.
 */
function _seqTicksPerFrame(seq) {
    var tpf = 0;
    try { tpf = parseFloat(seq.timebase); } catch (e) {}
    if (!(tpf > 0)) { try { tpf = parseFloat(seq.getSettings().videoFrameRate.ticks); } catch (e2) {} }
    if (!(tpf > 0)) {
        try {
            var s = seq.getSettings().videoFrameRate.seconds;
            if (s > 0) tpf = Math.round(s * TICKS_PER_SECOND);
        } catch (e3) {}
    }
    if (!(tpf > 0)) tpf = TICKS_PER_SECOND / 25;
    return tpf;
}

/** Drop-frame display? The QE CTI timecode uses ';' separators when it is. */
function _isDropFrame() {
    try {
        app.enableQE();
        var q = qe.project.getActiveSequence();
        return String(q.CTI.timecode).indexOf(";") >= 0;
    } catch (e) { return false; }
}

function _pad2(n) { return (n < 10 ? "0" : "") + n; }

/** Frame count → SMPTE timecode string (drop-frame aware) for QE razor. */
function _framesToTimecode(frames, nominal, dropFrame) {
    var f = Math.max(0, Math.round(frames));
    if (dropFrame) {
        var drop = Math.round(nominal / 15);           // 2 for 29.97, 4 for 59.94
        var per10 = nominal * 600 - drop * 9;
        var perMin = nominal * 60 - drop;
        var d = Math.floor(f / per10), m = f % per10;
        f += drop * 9 * d + (m > drop ? drop * Math.floor((m - drop) / perMin) : 0);
    }
    var ff = f % nominal, ss = Math.floor(f / nominal) % 60;
    var mm = Math.floor(f / (nominal * 60)) % 60, hh = Math.floor(f / (nominal * 3600));
    return _pad2(hh) + ":" + _pad2(mm) + ":" + _pad2(ss) + (dropFrame ? ";" : ":") + _pad2(ff);
}

/** Timecode of `sec` on the active sequence's real frame grid. */
function _timecodeAt(seq, sec, dropFrame) {
    var tpf = _seqTicksPerFrame(seq);
    var nominal = Math.round(TICKS_PER_SECOND / tpf);
    return _framesToTimecode(Math.round(sec * TICKS_PER_SECOND / tpf), nominal, dropFrame);
}

/**
 * Inspect a timeline clip's color/label API via ExtendScript reflection, so we
 * can call the right per-clip method. Uses the selected clip, else the first.
 */
function esInspectClip() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return "No active sequence";
        var clip = null;
        try { var sel = seq.getSelection(); if (sel && sel.length) clip = sel[0]; } catch (e) {}
        if (!clip) {
            for (var t = 0; t < seq.audioTracks.numTracks; t++) {
                if (seq.audioTracks[t].clips.numItems > 0) { clip = seq.audioTracks[t].clips[0]; break; }
            }
        }
        if (!clip) for (var v = 0; v < seq.videoTracks.numTracks; v++) {
            if (seq.videoTracks[v].clips.numItems > 0) { clip = seq.videoTracks[v].clips[0]; break; }
        }
        if (!clip) return "No clip found";

        var out = ["clip=" + clip.name];
        try {
            var props = clip.reflect.properties, pn = [];
            for (var i = 0; i < props.length; i++) { var p = String(props[i].name); if (/color|label/i.test(p)) pn.push(p); }
            out.push("PROPS: " + (pn.join(", ") || "none"));
            var meths = clip.reflect.methods, mn = [];
            for (var j = 0; j < meths.length; j++) { var m = String(meths[j].name); if (/color|label/i.test(m)) mn.push(m); }
            out.push("METHODS: " + (mn.join(", ") || "none"));
        } catch (e1) { out.push("reflect ERR: " + e1.message); }
        try { out.push("clip name/select methods: " + (function () {
            var cm = clip.reflect.methods, r = [];
            for (var k = 0; k < cm.length; k++) { var x = String(cm[k].name); if (/name|select|link/i.test(x)) r.push(x); }
            return r.join(", ") || "none";
        })()); } catch (e2) {}
        try { out.push("SEQUENCE link/sync methods: " + (function () {
            var sm = seq.reflect.methods, r = [];
            for (var k = 0; k < sm.length; k++) { var x = String(sm[k].name); if (/link|sync|select/i.test(x)) r.push(x); }
            return r.join(", ") || "none";
        })()); } catch (e3) {}
        return out.join("\n");
    } catch (e) { return "ERR: " + e.message; }
}

/** Connectivity test. */
function proCutPing() {
    try {
        var seq = app.project.activeSequence;
        return "PONG|" + (seq ? seq.name : "no sequence");
    } catch (e) {
        return "ERROR: " + e.message;
    }
}

/** Active sequence frame rate (fps, NOT rounded: "29.97002997..."), as a string. */
function esSequenceFps() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return "25";
        return String(TICKS_PER_SECOND / _seqTicksPerFrame(seq));
    } catch (e) {}
    return "25";
}

/** Exact timebase of the active sequence. JSON {ok, ticksPerFrame, fps, sequenceID, name, endTicks}. */
function esSequenceTimebase() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence — open a sequence first." });
        var tpf = _seqTicksPerFrame(seq);
        var endTicks = "0";
        try { endTicks = String(seq.end); } catch (e) {}
        return JSON.stringify({
            ok: true, ticksPerFrame: tpf, fps: TICKS_PER_SECOND / tpf,
            sequenceID: String(seq.sequenceID), name: seq.name, endTicks: endTicks
        });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message });
    }
}

/** Move the Premiere playhead (CTI) to `ticks` (string). */
function esSetPlayerPosition(ticks) {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return "NOSEQ";
        seq.setPlayerPosition(String(ticks));
        return "OK";
    } catch (e) { return "ERROR: " + e.message; }
}

/** Premiere playhead position in ticks (string), or "" without a sequence. */
function esGetPlayerPosition() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return "";
        return String(seq.getPlayerPosition().ticks);
    } catch (e) { return ""; }
}

/** Audio tracks of the active sequence: JSON {ok, sequenceID, tracks:[{index, name, clips}]}. */
function esListAudioTracks() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });
        var out = [];
        for (var i = 0; i < seq.audioTracks.numTracks; i++) {
            var t = seq.audioTracks[i], nm = "";
            try { nm = t.name; } catch (e) {}
            out.push({ index: i, name: nm || ("Audio " + (i + 1)), clips: t.clips.numItems });
        }
        return JSON.stringify({ ok: true, sequenceID: String(seq.sequenceID), name: seq.name, tracks: out });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message });
    }
}

/**
 * The backend's per-launch access token (written to ~/.easyscript/token-<port>).
 * Returned raw (URL-safe characters only). "~" is resolved several ways because
 * ExtendScript's home and Python's home have differed on some Windows setups.
 */
function esReadBackendToken(port) {
    var bases = [];
    try { bases.push(Folder("~").fsName); } catch (e) {}
    try { if ($.getenv("USERPROFILE")) bases.push($.getenv("USERPROFILE")); } catch (e2) {}
    try { if ($.getenv("HOME")) bases.push($.getenv("HOME")); } catch (e3) {}
    for (var i = 0; i < bases.length; i++) {
        try {
            var f = new File(bases[i] + "/.easyscript/token-" + parseInt(port, 10));
            if (!f.exists) continue;
            f.encoding = "UTF-8";
            if (!f.open("r")) continue;
            var s = f.read();
            f.close();
            s = String(s).replace(/[^A-Za-z0-9_\-]/g, "");
            if (s) return s;
        } catch (e4) {}
    }
    return "";
}

/**
 * Resolve the audio clip to analyze.
 * mode: "selected" | "entire" | "inout"   trackIdx: number or -1 (all)
 * Returns JSON: {ok, path, name, start, end, fps} | {ok:false, error}
 */
function esGetSelectedClip(mode, trackIdx) {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence — open a sequence first." });

        var tpf = _seqTicksPerFrame(seq);
        var fps = TICKS_PER_SECOND / tpf;
        var ti = parseInt(trackIdx, 10);
        var tracks = seq.audioTracks;
        var startT = (ti >= 0) ? ti : 0;
        var endT = (ti >= 0) ? (ti + 1) : tracks.numTracks;
        var found = null;

        if (mode === "selected") {
            // Prefer the official selection API when available.
            try {
                var sel = seq.getSelection();
                if (sel && sel.length) {
                    for (var s = 0; s < sel.length; s++) {
                        // pick the first item that has a media path
                        try { if (sel[s].projectItem && sel[s].projectItem.getMediaPath()) { found = sel[s]; break; } } catch (e) {}
                    }
                    if (!found) found = sel[0];
                }
            } catch (e) {}

            // Fallback: scan for a single clip.
            if (!found) {
                var all = [];
                for (var t = startT; t < endT && t < tracks.numTracks; t++) {
                    var cl = tracks[t].clips;
                    for (var c = 0; c < cl.numItems; c++) all.push(cl[c]);
                }
                if (all.length === 1) found = all[0];
                else if (all.length === 0) return JSON.stringify({ ok: false, error: "No audio clips on the timeline." });
                else return JSON.stringify({ ok: false, error: "No clip selected. Select an audio clip, or use Entire Timeline." });
            }
        } else {
            // entire / inout: first clip with a media path
            for (var t2 = startT; t2 < endT && t2 < tracks.numTracks; t2++) {
                var cl2 = tracks[t2].clips;
                if (cl2.numItems > 0) { found = cl2[0]; break; }
            }
        }

        if (!found) return JSON.stringify({ ok: false, error: "No clip found." });

        var name = "";  try { name = found.name; } catch (e) {}
        var path = "";  try { path = found.projectItem.getMediaPath(); } catch (e) {}
        var nodeId = ""; try { nodeId = String(found.projectItem.nodeId); } catch (e) {}
        var start = 0, end = 0, inPoint = 0, outPoint = 0, startTicks = "0";
        try { start = found.start.seconds; end = found.end.seconds; startTicks = String(found.start.ticks); } catch (e) {}
        try { inPoint = found.inPoint.seconds; } catch (e) {}
        try { outPoint = found.outPoint.seconds; } catch (e) {}

        // No media path → nested sequence (or generated clip). Return the clip
        // anyway with nested:true so the frontend renders the range instead.
        // start/end = sequence time; inPoint/outPoint = source time of the trimmed clip.
        return JSON.stringify({
            ok: true, path: path || "", nested: !path, name: name, start: start, end: end,
            startTicks: startTicks, inPoint: inPoint, outPoint: outPoint, fps: fps,
            ticksPerFrame: tpf, nodeId: nodeId, sequenceID: String(seq.sequenceID)
        });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message });
    }
}

/**
 * IMPORTANT: razor (QE) and clip removal/coloring MUST run in SEPARATE
 * evalScript calls. After a QE razor, the regular DOM clip list doesn't refresh
 * within the same call — so we split into proCutRazor + proCutRemove. The panel
 * awaits the razor, then issues a second call by which time the DOM is updated.
 */

/**
 * seconds → timecode for QE razor, on the active sequence's REAL frame grid
 * (drop-frame aware). The old version used an integer fps, so on 29.97/23.976
 * sequences razor points drifted ~3.6 s/hour late — cutting into speech.
 * `fps` is ignored; kept so existing callers still work.
 */
function _toTimecode(sec, fps, dropFrame) {
    var seq = app.project.activeSequence;
    if (seq) return _timecodeAt(seq, sec, dropFrame === undefined ? _isDropFrame() : dropFrame);
    var nominal = Math.round(fps) || 25;
    return _framesToTimecode(Math.round(sec * (fps || 25)), nominal, false);
}

/**
 * Razor all tracks at the given boundary times. dataJson:{boundaries:number[], fps}
 * QE razor expects a TIMECODE string (not ticks). Returns QE clip counts so we
 * can verify the split actually happened.
 * Returns: "OK|edits|qeBeforeV|qeAfterV|qeAfterA" | "ERROR: ..."
 */
function proCutRazor(dataJson) {
    try {
        var data = JSON.parse(dataJson);
        var boundaries = data.boundaries || [];
        var fps = data.fps || 25;
        try { app.enableQE(); } catch (e) { return "ERROR: Cannot enable QE: " + e.message; }
        var qeSeq = null;
        try { qeSeq = qe.project.getActiveSequence(); } catch (e) { return "ERROR: No QE sequence: " + e.message; }
        if (!qeSeq) return "ERROR: No active QE sequence";

        var beforeV = (qeSeq.numVideoTracks > 0) ? qeSeq.getVideoTrackAt(0).numItems : -1;
        var edits = 0;
        var df = _isDropFrame();
        for (var i = 0; i < boundaries.length; i++) {
            var tc = _toTimecode(boundaries[i], fps, df);
            for (var v = 0; v < qeSeq.numVideoTracks; v++) { try { qeSeq.getVideoTrackAt(v).razor(tc); edits++; } catch (e) {} }
            for (var a = 0; a < qeSeq.numAudioTracks; a++) { try { qeSeq.getAudioTrackAt(a).razor(tc); edits++; } catch (e) {} }
        }
        var afterV = (qeSeq.numVideoTracks > 0) ? qeSeq.getVideoTrackAt(0).numItems : -1;
        var afterA = (qeSeq.numAudioTracks > 0) ? qeSeq.getAudioTrackAt(0).numItems : -1;
        return "OK|" + edits + "|" + beforeV + "|" + afterV + "|" + afterA;
    } catch (e) { return "ERROR: " + e.message; }
}

/** Remove clips that sit inside a silence region. dataJson:{silenceRegions:[[s,e]]} */
function proCutRemove(dataJson) {
    try {
        var data = JSON.parse(dataJson);
        var silenceRegions = data.silenceRegions || [];
        var seq = app.project.activeSequence;
        if (!seq) return "ERROR: No active sequence";

        // Remove a clip if its midpoint sits in a silence region AND the clip is
        // not much longer than that region (so a big un-split clip is never
        // deleted, but a razored silence clip — dur ≈ region — is).
        function isInSilence(cs, ce) {
            var mid = (cs + ce) / 2, dur = ce - cs;
            for (var r = 0; r < silenceRegions.length; r++) {
                var s0 = silenceRegions[r][0], s1 = silenceRegions[r][1], rlen = s1 - s0;
                if (mid >= s0 && mid <= s1 && dur <= rlen * 1.5 + 0.06) return true;
            }
            return false;
        }
        var removed = 0, scanned = 0, sample = [];
        function ripple(tracks) {
            for (var t = tracks.numTracks - 1; t >= 0; t--) {
                var track = tracks[t];
                for (var c = track.clips.numItems - 1; c >= 0; c--) {
                    try {
                        var clip = track.clips[c];
                        var cs = clip.start.seconds, ce = clip.end.seconds;
                        scanned++;
                        if (sample.length < 8) sample.push(cs.toFixed(2) + "-" + ce.toFixed(2));
                        // remove(ripple, alignToVideo): ripple=true closes the gap.
                        // Iterating each track end→start keeps A/V in sync because
                        // matching silence regions shift every track equally.
                        if (isInSilence(cs, ce)) { clip.remove(true, false); removed++; }
                    } catch (e) {}
                }
            }
        }
        ripple(seq.videoTracks);
        ripple(seq.audioTracks);
        // OK|removed|scanned|sampleClipTimes
        return "OK|" + removed + "|" + scanned + "|" + sample.join(",");
    } catch (e) { return "ERROR: " + e.message; }
}

/**
 * Number of clips on video track 0 in the regular DOM. Used by the panel to
 * poll until the DOM reflects the QE razor edits (the DOM lags the QE/UI on
 * long timelines), so removal runs only once the clips are actually split.
 */
function esDomVClipCount() {
    try {
        var seq = app.project.activeSequence;
        if (!seq || seq.videoTracks.numTracks === 0) return "0";
        return String(seq.videoTracks[0].clips.numItems);
    } catch (e) { return "0"; }
}

/**
 * Razor AND remove silence for one chunk — entirely in the QE DOM, in a SINGLE
 * call. Because both the razor and the removal use QE, the QE DOM already
 * reflects the new (split) clips within the same call, so there is NO wait for
 * the Scripting DOM to sync (that was the pause between cut and remove).
 *
 * dataJson:{ boundaries:number[], silenceRegions:[[s,e]], fps }
 * Returns: "OK|edits|removed|scanned" | "ERROR: ..."
 */
function proCutChunkQE(dataJson) {
    try {
        var data = JSON.parse(dataJson);
        var boundaries = data.boundaries || [];
        var silenceRegions = data.silenceRegions || [];
        var fps = data.fps || 25;
        if (!fps || fps < 1) fps = 25;
        try { app.enableQE(); } catch (e) { return "ERROR: Cannot enable QE: " + e.message; }
        var qeSeq = null;
        try { qeSeq = qe.project.getActiveSequence(); } catch (e) { return "ERROR: No QE sequence: " + e.message; }
        if (!qeSeq) return "ERROR: No active QE sequence";

        // 1) Razor at every boundary on every track.
        var edits = 0;
        var df = _isDropFrame();
        for (var i = 0; i < boundaries.length; i++) {
            var tc = _toTimecode(boundaries[i], fps, df);
            for (var v = 0; v < qeSeq.numVideoTracks; v++) { try { qeSeq.getVideoTrackAt(v).razor(tc); edits++; } catch (e) {} }
            for (var a = 0; a < qeSeq.numAudioTracks; a++) { try { qeSeq.getAudioTrackAt(a).razor(tc); edits++; } catch (e) {} }
        }

        // QE Timecode → seconds (handles {ticks}, {secs}/{seconds}, or "HH:MM:SS:FF").
        function tcSecs(t) {
            if (t == null) return null;
            try {
                if (typeof t === "object") {
                    if (t.ticks != null) { var v = parseFloat(String(t.ticks)); if (!isNaN(v)) return v / TICKS_PER_SECOND; }
                    if (t.secs != null) { var s = parseFloat(String(t.secs)); if (!isNaN(s)) return s; }
                    if (t.seconds != null) { var ss = parseFloat(String(t.seconds)); if (!isNaN(ss)) return ss; }
                }
                var str = String(t);
                var parts = str.split(/[:;]/);
                if (parts.length === 4) {
                    return (parseFloat(parts[0]) * 3600) + (parseFloat(parts[1]) * 60) + parseFloat(parts[2]) + (parseFloat(parts[3]) / fps);
                }
                var f = parseFloat(str);
                return isNaN(f) ? null : f;
            } catch (e) { return null; }
        }
        function inSilence(cs, ce) {
            var mid = (cs + ce) / 2, dur = ce - cs;
            for (var r = 0; r < silenceRegions.length; r++) {
                var s0 = silenceRegions[r][0], s1 = silenceRegions[r][1], rl = s1 - s0;
                if (mid >= s0 && mid <= s1 && dur <= rl * 1.5 + 0.06) return true;
            }
            return false;
        }
        function qeRemove(it) {
            // QE TrackItem.remove(inRippleDelete, inAlignToVideo) — try a few signatures.
            try { it.remove(true, false); return true; } catch (e1) {}
            try { it.remove(true); return true; } catch (e2) {}
            try { it.remove(); return true; } catch (e3) {}
            return false;
        }
        var removed = 0, scanned = 0;
        function removeTrack(track) {
            for (var c = track.numItems - 1; c >= 0; c--) {
                var it;
                try { it = track.getItemAt(c); } catch (e) { continue; }
                if (!it) continue;
                // Skip empty/gap items.
                var ty = ""; try { ty = String(it.type); } catch (e) {}
                if (ty === "Empty") continue;
                var cs = tcSecs(it.start), ce = tcSecs(it.end);
                if (cs == null || ce == null) continue;
                scanned++;
                if (inSilence(cs, ce)) { if (qeRemove(it)) removed++; }
            }
        }
        for (var vt = 0; vt < qeSeq.numVideoTracks; vt++) removeTrack(qeSeq.getVideoTrackAt(vt));
        for (var at = 0; at < qeSeq.numAudioTracks; at++) removeTrack(qeSeq.getAudioTrackAt(at));

        return "OK|" + edits + "|" + removed + "|" + scanned;
    } catch (e) { return "ERROR: " + e.message; }
}

/**
 * Remove silence using QE sequence.extract(): for each silence region, set the
 * sequence In/Out to [s,e] and extract — one operation removes that range from
 * ALL tracks, ripples the gap closed, and (because no clip is split) preserves
 * A/V links. No razor, no per-clip scan, no relink. ~6× fewer edits than the
 * razor+remove path.
 *
 * Regions are processed RIGHT→LEFT so each extract never shifts the (still
 * original) coordinates of regions not yet processed.
 *
 * dataJson:{ regionsTicks:[[inTicks, outTicks]], sequenceID }
 *   Frame-aligned sequence ticks, already snapped INWARD by the panel (cut
 *   start rounded up, end rounded down) so a cut never reaches past its padded
 *   silence into speech. Legacy {silenceRegions:[[s,e]]} seconds are snapped
 *   inward here on the sequence's real frame grid.
 * The user's In/Out points and track targeting are put back afterwards.
 * Returns: "OK|extracted|total|errSample"
 */
function _ticksStr(x) { return String(Math.round(x)); }

function _getInOutTicks(seq) {
    var inT = NaN, outT = NaN;
    try { inT = parseFloat(seq.getInPointAsTime().ticks); outT = parseFloat(seq.getOutPointAsTime().ticks); } catch (e) {}
    if (isNaN(inT)) {
        try {
            inT = parseFloat(seq.getInPoint()) * TICKS_PER_SECOND;
            outT = parseFloat(seq.getOutPoint()) * TICKS_PER_SECOND;
        } catch (e2) {}
    }
    return { inT: inT, outT: outT };
}

function _saveTimelineState(seq) {
    var st = { io: _getInOutTicks(seq), endT: parseFloat(seq.end), v: [], a: [] };
    var t;
    for (t = 0; t < seq.videoTracks.numTracks; t++) { try { st.v.push(seq.videoTracks[t].isTargeted()); } catch (e) { st.v.push(null); } }
    for (t = 0; t < seq.audioTracks.numTracks; t++) { try { st.a.push(seq.audioTracks[t].isTargeted()); } catch (e2) { st.a.push(null); } }
    return st;
}

/** Ticks removed before position t by already-extracted [a, b) regions. */
function _removedBefore(removed, t) {
    var sum = 0;
    for (var i = 0; i < removed.length; i++) {
        var a = removed[i][0], b = removed[i][1];
        if (a < t) sum += Math.min(b, t) - a;
    }
    return sum;
}

function _setTargeted(track, on) {
    try { track.setTargeted(on, true); } catch (e) { try { track.setTargeted(on); } catch (e2) {} }
}

function _restoreTimelineState(seq, st, removed, tpf) {
    var t;
    for (t = 0; t < st.v.length && t < seq.videoTracks.numTracks; t++) { if (st.v[t] !== null) _setTargeted(seq.videoTracks[t], st.v[t]); }
    for (t = 0; t < st.a.length && t < seq.audioTracks.numTracks; t++) { if (st.a[t] !== null) _setTargeted(seq.audioTracks[t], st.a[t]); }
    try {
        var io = st.io, newEnd = parseFloat(seq.end);
        seq.setInPoint("0");
        if (isNaN(io.inT)) return;
        var hadNone = io.inT <= 0 && (isNaN(io.outT) || io.outT >= st.endT - tpf);
        if (hadNone) { seq.setOutPoint(_ticksStr(newEnd)); return; }
        var ni = Math.max(0, io.inT - _removedBefore(removed, io.inT));
        var no = Math.max(ni, io.outT - _removedBefore(removed, io.outT));
        seq.setOutPoint(_ticksStr(no));
        seq.setInPoint(_ticksStr(ni));
    } catch (e) {}
}

function _targetAll(seq) {
    var t;
    for (t = 0; t < seq.videoTracks.numTracks; t++) _setTargeted(seq.videoTracks[t], true);
    for (t = 0; t < seq.audioTracks.numTracks; t++) _setTargeted(seq.audioTracks[t], true);
}

function proCutExtract(dataJson) {
    try {
        var data = JSON.parse(dataJson);
        try { app.enableQE(); } catch (e) { return "ERROR: Cannot enable QE: " + e.message; }
        var qeSeq = null;
        try { qeSeq = qe.project.getActiveSequence(); } catch (e) { return "ERROR: No QE sequence: " + e.message; }
        if (!qeSeq) return "ERROR: No active QE sequence";
        // In/Out is set on the REGULAR sequence; extract() reads it from there.
        // (Setting it on the QE sequence does NOT work — that was the bug.)
        var seq = app.project.activeSequence;
        if (!seq) return "ERROR: No active sequence";
        if (data.sequenceID && String(seq.sequenceID) !== String(data.sequenceID)) {
            return "ERROR: The active sequence changed since the audio was loaded — load it again.";
        }
        var tpf = _seqTicksPerFrame(seq);
        var regions = [], i, a, b;
        if (data.regionsTicks) {
            for (i = 0; i < data.regionsTicks.length; i++) {
                a = parseFloat(data.regionsTicks[i][0]);
                b = parseFloat(data.regionsTicks[i][1]);
                if (b - a >= tpf * 0.5) regions.push([a, b]);
            }
        } else {
            var legacy = data.silenceRegions || [];
            for (i = 0; i < legacy.length; i++) {
                a = Math.ceil(legacy[i][0] * TICKS_PER_SECOND / tpf - 1e-6) * tpf;
                b = Math.floor(legacy[i][1] * TICKS_PER_SECOND / tpf + 1e-6) * tpf;
                if (b - a >= tpf * 0.5) regions.push([a, b]);
            }
        }

        var saved = _saveTimelineState(seq);
        // Target ALL tracks — extract only affects targeted tracks.
        _targetAll(seq);

        // Right → left so each extract never shifts not-yet-processed regions.
        regions.sort(function (x, y) { return y[0] - x[0]; });

        var extracted = 0, errs = [], removed = [];
        for (i = 0; i < regions.length; i++) {
            try {
                seq.setInPoint(_ticksStr(regions[i][0]));
                seq.setOutPoint(_ticksStr(regions[i][1]));
                qeSeq.extract();
                extracted++;
                removed.push(regions[i]);
            } catch (ex) {
                if (errs.length < 5) errs.push("ex@" + (regions[i][0] / TICKS_PER_SECOND).toFixed(2) + ":" + ex.message);
            }
        }
        _restoreTimelineState(seq, saved, removed, tpf);

        return "OK|" + extracted + "|" + regions.length + "|" + errs.join(";");
    } catch (e) {
        return "ERROR: " + e.message;
    }
}

/**
 * Split clips at speaker-change points using a 1-frame extract (keeps A/V
 * linked, fast, no razor). Skips any boundary that already sits on an existing
 * clip edge (within ~1.5 frames) — i.e. where Apply Cut already cut because
 * there was silence — so we never double-cut or lose a frame needlessly.
 *
 * dataJson:{ boundaries:number[] (sequence time), fps }
 * Returns: "OK|split|skipped|total"
 */
function esSplitSpeakers(dataJson) {
    try {
        var data = JSON.parse(dataJson);
        var boundaries = (data.boundaries || []).slice();
        try { app.enableQE(); } catch (e) { return "ERROR: Cannot enable QE: " + e.message; }
        var qeSeq = null;
        try { qeSeq = qe.project.getActiveSequence(); } catch (e) { return "ERROR: No QE sequence: " + e.message; }
        if (!qeSeq) return "ERROR: No active QE sequence";
        var seq = app.project.activeSequence;
        if (!seq) return "ERROR: No active sequence";
        var TICKS = TICKS_PER_SECOND;
        // Real frame grid (29.97 ≠ 30): boundaries land on the frame Premiere uses.
        var tpf = _seqTicksPerFrame(seq);
        var fps = TICKS / tpf;
        var frameDur = 1.0 / fps, tol = frameDur * 1.5;
        var saved = _saveTimelineState(seq);
        _targetAll(seq);

        // Existing edit points (clip starts/ends) across all tracks.
        var edges = [];
        function addEdges(tracks) {
            for (var k = 0; k < tracks.numTracks; k++) {
                var cl = tracks[k].clips;
                for (var c = 0; c < cl.numItems; c++) {
                    try { edges.push(cl[c].start.seconds); edges.push(cl[c].end.seconds); } catch (e) {}
                }
            }
        }
        addEdges(seq.videoTracks); addEdges(seq.audioTracks);
        function nearEdge(tt) {
            for (var i = 0; i < edges.length; i++) { if (Math.abs(edges[i] - tt) <= tol) return true; }
            return false;
        }
        boundaries.sort(function (a, b) { return b - a; }); // right → left
        var split = 0, skipped = 0, errs = [], removed = [];
        for (var i = 0; i < boundaries.length; i++) {
            var frame = Math.round(boundaries[i] * TICKS / tpf);
            var tt = frame * tpf / TICKS;
            if (frame <= 0) { skipped++; continue; }
            if (nearEdge(tt)) { skipped++; continue; } // already cut here (had silence)
            var inT = frame * tpf, outT = (frame + 1) * tpf;
            try {
                seq.setInPoint(_ticksStr(inT)); seq.setOutPoint(_ticksStr(outT)); qeSeq.extract();
                split++; removed.push([inT, outT]);
            }
            catch (ex) { if (errs.length < 5) errs.push("ex@" + tt.toFixed(2) + ":" + ex.message); }
        }
        _restoreTimelineState(seq, saved, removed, tpf);
        // Removed frames (sequence seconds, pre-edit) so the caller can map its
        // segment times onto the shifted timeline before naming clips.
        var rem = [];
        for (var r = 0; r < removed.length; r++) rem.push((removed[r][0] / TICKS).toFixed(6));
        return "OK|" + split + "|" + skipped + "|" + boundaries.length + "|" + rem.join(",");
    } catch (e) {
        return "ERROR: " + e.message;
    }
}

/**
 * Re-link audio + video clips that share the same start time (broken apart by
 * the per-track razor). For each video clip, select it plus the audio clip(s)
 * at the same position and link them. Returns JSON {ok, linked}.
 */
function esRelinkAV(dummy) {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });
        var TOL = 0.06, linked = 0;

        // Bucket audio clips by start time (key = floor(s/TOL)) so each video
        // clip finds its matches in O(1) instead of scanning every audio clip.
        // (The old O(videoClips × audioClips) scan froze Premiere on long files.)
        var buckets = {};
        for (var at = 0; at < seq.audioTracks.numTracks; at++) {
            var acl = seq.audioTracks[at].clips;
            for (var ac = 0; ac < acl.numItems; ac++) {
                var a = acl[ac];
                try {
                    var s = a.start.seconds;
                    var bk = Math.floor(s / TOL);
                    if (!buckets[bk]) buckets[bk] = [];
                    buckets[bk].push({ clip: a, s: s });
                } catch (e) {}
            }
        }
        for (var vt = 0; vt < seq.videoTracks.numTracks; vt++) {
            var vcl = seq.videoTracks[vt].clips;
            for (var vc = 0; vc < vcl.numItems; vc++) {
                var v = vcl[vc], vs;
                try { vs = v.start.seconds; } catch (e) { continue; }
                var sel = [v];
                var base = Math.floor(vs / TOL);
                for (var bb = base - 1; bb <= base + 1; bb++) {
                    var arr = buckets[bb];
                    if (!arr) continue;
                    for (var k = 0; k < arr.length; k++) {
                        if (Math.abs(arr[k].s - vs) < TOL) sel.push(arr[k].clip);
                    }
                }
                if (sel.length >= 2) {
                    try {
                        seq.setSelection(sel);
                        seq.linkSelection();
                        linked++;
                    } catch (e) {
                        // Fallback: select via setSelected then link.
                        try {
                            for (var z = 0; z < sel.length; z++) { try { sel[z].setSelected(true, (z === 0)); } catch (ez) {} }
                            seq.linkSelection();
                            linked++;
                        } catch (e2) {}
                    }
                }
            }
        }
        return JSON.stringify({ ok: true, linked: linked });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message });
    }
}

/* ===== TEMP DIAGNOSTIC (v2.1 label research) — remove after API found ===== */
function esDumpLabelAPI() {
    function refl(obj) {
        var o = { methods: [], props: [] };
        try { var m = obj.reflect.methods; for (var i = 0; i < m.length; i++) o.methods.push(String(m[i].name)); o.methods.sort(); }
        catch (e) { o.methods = "ERR:" + e.message; }
        try { var p = obj.reflect.properties; for (var i = 0; i < p.length; i++) o.props.push(String(p[i].name)); o.props.sort(); }
        catch (e) { o.props = "ERR:" + e.message; }
        return o;
    }
    var out = {};
    try {
        var seq = app.project.activeSequence;
        if (!seq) { out.err = "no active sequence"; }
        else {
            var clip = null;
            for (var t = 0; t < seq.videoTracks.numTracks && !clip; t++) {
                var cl = seq.videoTracks[t].clips;
                if (cl.numItems > 0) clip = cl[0];
            }
            if (!clip) { out.err = "no video clip on timeline"; }
            else {
                out.trackItem = refl(clip);
                // Probe candidate per-clip label APIs (type of each).
                var cand = ["setColorLabel", "getColorLabel", "colorLabel", "label", "setLabel",
                            "getLabel", "labelColor", "setLabelColor", "color", "setColor"];
                out.trackItem_candidates = {};
                for (var i = 0; i < cand.length; i++) {
                    try { out.trackItem_candidates[cand[i]] = typeof clip[cand[i]]; }
                    catch (e) { out.trackItem_candidates[cand[i]] = "ERR"; }
                }
                try { out.projectItem = refl(clip.projectItem); } catch (e) { out.projectItem = "ERR:" + e.message; }
            }
        }
    } catch (e) { out.fatal = e.message; }
    try {
        var f = new File(Folder("~").fsName + "/.easyscript/label_api.json");
        if (!f.parent.exists) f.parent.create();
        f.encoding = "UTF-8"; f.open("w"); f.write(JSON.stringify(out)); f.close();
    } catch (e) {}
    return JSON.stringify(out);
}
/* ===== END TEMP DIAGNOSTIC ===== */

/** List all sequence methods (for API discovery). */
function esListSeqMethods() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return "No sequence";
        var m = seq.reflect.methods, r = [];
        for (var i = 0; i < m.length; i++) r.push(String(m[i].name));
        return r.join(", ");
    } catch (e) { return "ERR: " + e.message; }
}

/**
 * Render the in/out range (or a given clip range, or the whole sequence) to a
 * temp 16k-mono WAV using a bundled preset. Works for trimmed clips, nested
 * sequences, and timeline in/out spanning multiple clips.
 * Returns JSON {ok, path, start, end, log} | {ok:false, error, log}
 */
function esRenderRange(presetPath, mode, startSec, endSec) {
    var log = [];
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });
        var TICKS = 254016000000;
        // Render into ~/.easyscript/renders (the macOS Temp/TemporaryItems dir
        // is TCC-protected and the backend can't read it).
        var dir = new Folder(Folder("~").fsName + "/.easyscript/renders");
        if (!dir.exists) dir.create();
        // exportAsMediaDirect wants native paths. On Windows the two we build
        // disagree: CEP's getSystemPath returns forward slashes (it only strips
        // "file:///"), while Folder.fsName returns backslashes — and we then
        // concatenate "/" onto it. Premiere rejects the mix with a bare
        // "Error: Unknown Error". File(...).fsName normalises both, and is a
        // no-op on macOS where everything is "/" already.
        var out = new File(dir.fsName + "/render_" + (new Date().getTime()) + ".wav").fsName;
        log.push("out=" + out);

        presetPath = new File(presetPath).fsName;
        log.push("preset=" + presetPath);
        if (!new File(presetPath).exists) {
            return JSON.stringify({ ok: false, error: "preset not found: " + presetPath, log: log.join(" | ") });
        }

        var rngStart = startSec, rngEnd = endSec;
        var setRange = (mode !== "inout" && mode !== "entire" && startSec >= 0 && endSec > startSec);

        if (setRange) {
            // Set sequence in/out to the requested range (try seconds, then ticks).
            var ok1 = false;
            try { seq.setInPoint(startSec); seq.setOutPoint(endSec); ok1 = true; log.push("setInPoint(sec) OK"); } catch (e) { log.push("setInPoint(sec) ERR " + e.message); }
            if (!ok1) {
                try {
                    seq.setInPoint(String(Math.round(startSec * TICKS)));
                    seq.setOutPoint(String(Math.round(endSec * TICKS)));
                    ok1 = true; log.push("setInPoint(ticks) OK");
                } catch (e2) { log.push("setInPoint(ticks) ERR " + e2.message); }
            }
        } else if (mode === "inout") {
            // Use existing timeline in/out marks; read them back for mapping.
            try { rngStart = seq.getInPointAsTime().seconds; rngEnd = seq.getOutPointAsTime().seconds; log.push("inout=" + rngStart + ".." + rngEnd); }
            catch (e3) { try { rngStart = parseFloat(seq.getInPoint()); rngEnd = parseFloat(seq.getOutPoint()); } catch (e4) { rngStart = 0; rngEnd = 0; } }
        } else {
            rngStart = 0; rngEnd = 0;
        }

        var work = (mode === "entire") ? 0 : 1; // 0=entire, 1=in/out
        var status;
        try { status = seq.exportAsMediaDirect(out, presetPath, work); log.push("exportStatus=" + status); }
        catch (ee) {
            log.push("exportAsMediaDirect ERR " + ee.message);
            try { var sm = seq.reflect.methods, rr = []; for (var z = 0; z < sm.length; z++) { var nn = String(sm[z].name); if (/export|inpoint|outpoint|encode|render/i.test(nn)) rr.push(nn); } log.push("candidates: " + rr.join(",")); } catch (e5) {}
            return JSON.stringify({ ok: false, error: "export failed", log: log.join(" | ") });
        }

        var f = new File(out);
        // Fold the log into `error` too: the panel only surfaces `error`, and
        // "Unknown Error" on its own says nothing about which path was rejected.
        if (!f.exists) return JSON.stringify({ ok: false, error: "no output file (status " + status + ") — " + log.join(" | "), log: log.join(" | ") });
        return JSON.stringify({ ok: true, path: out, start: rngStart, end: rngEnd, log: log.join(" | ") });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message, log: log.join(" | ") });
    }
}

/** Newest root-bin item whose media path is `path` (just-imported files land there). */
function _findImportedItem(path) {
    var want = new File(path).fsName.toLowerCase();
    var kids = app.project.rootItem.children;
    for (var i = kids.numItems - 1; i >= 0; i--) {
        var it = kids[i], mp = "";
        try { mp = it.getMediaPath(); } catch (e) {}
        if (mp && new File(mp).fsName.toLowerCase() === want) return it;
    }
    return null;
}

/**
 * Import an SRT and, when possible, place it on a new caption track of the
 * active sequence starting at `startSec` (the analysed clip's start).
 * Returns JSON {ok, imported, captionTrack}.
 */
function esImportSubtitle(srtPath, startSec) {
    try {
        var ok = app.project.importFiles([srtPath], true, app.project.rootItem, false);
        var placed = false, why = "";
        var seq = app.project.activeSequence;
        var item = _findImportedItem(srtPath);
        if (seq && item) {
            try {
                seq.createCaptionTrack(item, Number(startSec) || 0, Sequence.CAPTION_FORMAT_SUBTITLE);
                placed = true;
            } catch (e) { why = e.message; }
        } else {
            why = seq ? "imported item not found" : "no active sequence";
        }
        return JSON.stringify({ ok: true, imported: ok, captionTrack: placed, reason: why });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message });
    }
}

/** Child bin of the project root called `name` (created when missing). */
function _rootBin(name) {
    var kids = app.project.rootItem.children;
    for (var i = 0; i < kids.numItems; i++) {
        try { if (kids[i].type === ProjectItemType.BIN && kids[i].name === name) return kids[i]; } catch (e) {}
    }
    return app.project.rootItem.createBin(name);
}

/** Item in `bin` whose media path is `path`, or null. */
function _findItemInBin(bin, path) {
    var want = new File(path).fsName.toLowerCase();
    for (var i = bin.children.numItems - 1; i >= 0; i--) {
        var it = bin.children[i], mp = "";
        try { mp = it.getMediaPath(); } catch (e) {}
        if (mp && new File(mp).fsName.toLowerCase() === want) return it;
    }
    return null;
}

/**
 * First unlocked audio track with no clip overlapping [start, end) ticks, or -1.
 * Clips that merely touch the range (end == start) don't count.
 */
function _freeAudioTrack(seq, startTicks, endTicks) {
    var tracks = seq.audioTracks;
    for (var i = 0; i < tracks.numTracks; i++) {
        var t = tracks[i], locked = false;
        try { locked = t.isLocked(); } catch (e) {}
        if (locked) continue;
        var busy = false;
        for (var c = 0; c < t.clips.numItems && !busy; c++) {
            var cl = t.clips[c];
            busy = parseFloat(cl.start.ticks) < endTicks && parseFloat(cl.end.ticks) > startTicks;
        }
        if (!busy) return i;
    }
    return -1;
}

/** Append one stereo audio track (QE DOM). A separate evalScript call from
 *  esPlaceStem: the regular DOM only sees the new track in the next call. */
function esAddAudioTrack() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });
        var before = seq.audioTracks.numTracks;
        app.enableQE();
        // (videoTracks, afterVideo, audioTracks, audioType 0 mono / 1 stereo / 2 5.1,
        //  afterAudio, submixTracks, submixType)
        qe.project.getActiveSequence().addTracks(0, 0, 1, 1, before, 0, 1);
        return JSON.stringify({ ok: true, before: before });
    } catch (e) {
        return JSON.stringify({ ok: false, error: "Could not add an audio track: " + e.message });
    }
}

/**
 * Import a separated stem (into the "EasyScript Stems" bin) and lay it at
 * `startTicks` on the first audio track that is empty for its whole length —
 * never over existing audio. When every track is busy it returns
 * {ok:false, needTrack:true}; the panel then calls esAddAudioTrack and retries.
 * data: {path, startTicks, durationTicks}
 * Returns JSON {ok, track, startTicks, name}.
 */
function esPlaceStem(dataJson) {
    try {
        var d = JSON.parse(dataJson);
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence — open a sequence first." });
        var start = parseFloat(d.startTicks) || 0;
        var end = start + (parseFloat(d.durationTicks) || 0);

        var ti = _freeAudioTrack(seq, start, end);
        if (ti < 0) return JSON.stringify({ ok: false, needTrack: true, tracks: seq.audioTracks.numTracks });

        var bin = _rootBin("EasyScript Stems");
        var item = _findItemInBin(bin, d.path);
        if (!item) {
            app.project.importFiles([d.path], true, bin, false);
            item = _findItemInBin(bin, d.path);
        }
        if (!item) return JSON.stringify({ ok: false, error: "Import failed: " + d.path });

        var track = seq.audioTracks[ti];
        var at = new Time();
        at.ticks = String(Math.round(start));
        try { track.overwriteClip(item, at); }
        catch (e1) { track.overwriteClip(item, String(Math.round(start))); }

        // Confirm where it landed (within one frame).
        var tpf = _seqTicksPerFrame(seq), placed = null;
        for (var c = 0; c < track.clips.numItems; c++) {
            var cl = track.clips[c];
            try {
                if (String(cl.projectItem.nodeId) === String(item.nodeId) &&
                    Math.abs(parseFloat(cl.start.ticks) - start) < tpf) { placed = cl; break; }
            } catch (e2) {}
        }
        if (!placed) return JSON.stringify({ ok: false, error: "The stem was imported but not found on A" + (ti + 1) + " at the expected time." });
        return JSON.stringify({ ok: true, track: ti, startTicks: String(placed.start.ticks), name: item.name,
                                nodeId: String(item.nodeId) });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message });
    }
}

/**
 * Export the active sequence as FCP XML into ~/.easyscript/xml.
 * Returns JSON {ok, path, name, sequenceID, ticksPerFrame}.
 */
function esExportSequenceXML() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });
        var dir = new Folder(Folder("~").fsName + "/.easyscript/xml");
        if (!dir.exists) dir.create();
        var out = new File(dir.fsName + "/export_" + (new Date().getTime()) + ".xml").fsName;
        var res = seq.exportAsFinalCutProXML(out, 1);  // 1 = suppress UI
        if (!new File(out).exists) {
            return JSON.stringify({ ok: false, error: "Premiere did not write the XML (" + res + ")" });
        }
        return JSON.stringify({
            ok: true, path: out, name: seq.name, sequenceID: String(seq.sequenceID),
            ticksPerFrame: _seqTicksPerFrame(seq)
        });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message });
    }
}

/**
 * Import an FCP XML and open the sequence it creates (preferring `expectName`).
 * Returns JSON {ok, name, sequenceID}.
 */
function esImportSequenceXML(xmlPath, expectName) {
    try {
        var proj = app.project, i, before = {};
        for (i = 0; i < proj.sequences.numSequences; i++) before[String(proj.sequences[i].sequenceID)] = 1;
        var ok = proj.importFiles([new File(xmlPath).fsName], true, proj.rootItem, false);
        var found = null;
        for (i = 0; i < proj.sequences.numSequences; i++) {
            var s = proj.sequences[i];
            if (before[String(s.sequenceID)]) continue;
            if (!found || s.name === expectName) found = s;
        }
        if (!found) return JSON.stringify({ ok: false, error: "Imported, but no new sequence appeared (" + ok + ")" });
        try { proj.openSequence(found.sequenceID); } catch (e) {}
        return JSON.stringify({ ok: true, name: found.name, sequenceID: String(found.sequenceID) });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message });
    }
}

/** Project item of the analysed clip: same nodeId, starting where it started. */
/** Timeline clip (TrackItem) of `info` {nodeId, seqStart}: the one starting at
 *  seqStart, else any clip of that media. */
function _findClipByNode(seq, info) {
    var groups = [seq.audioTracks, seq.videoTracks], fallback = null;
    for (var g = 0; g < groups.length; g++) {
        for (var t = 0; t < groups[g].numTracks; t++) {
            var clips = groups[g][t].clips;
            for (var c = 0; c < clips.numItems; c++) {
                var pi = null;
                try { pi = clips[c].projectItem; } catch (e) {}
                if (!pi || String(pi.nodeId) !== String(info.nodeId)) continue;
                var st = 0;
                try { st = clips[c].start.seconds; } catch (e2) {}
                if (Math.abs(st - (info.seqStart || 0)) < 0.01) return clips[c];
                if (!fallback) fallback = clips[c];
            }
        }
    }
    return fallback;
}

/** The clip selected in the timeline (an audio clip preferred), or null. */
function _selectedClip(seq) {
    var sel = null, pick = null;
    try { sel = seq.getSelection(); } catch (e) {}
    if (!sel || !sel.length) return null;
    for (var i = 0; i < sel.length; i++) {
        var pi = null;
        try { pi = sel[i].projectItem; } catch (e2) {}
        if (!pi) continue;
        var audio = false;
        try { audio = String(sel[i].mediaType) === "Audio"; } catch (e3) {}
        if (audio) return sel[i];
        if (!pick) pick = sel[i];
    }
    return pick;
}

/**
 * Where markers go. Sequence target: the sequence's markers. Clip target: the
 * media of the clip selected in the timeline — or, with nothing selected, the
 * clip of the panel's current audio (data.clip {nodeId, seqStart}).
 * Returns {coll, clip} (clip = TrackItem for clip targets) or null.
 */
function _markerTarget(seq, data) {
    if (data.target !== "clip") return { coll: seq.markers, clip: null };
    var clip = _selectedClip(seq) || (data.clip && data.clip.nodeId ? _findClipByNode(seq, data.clip) : null);
    if (!clip) return null;
    return { coll: clip.projectItem.getMarkers(), clip: clip };
}

/**
 * Add markers. dataJson: {target:"sequence"|"clip", clip:{nodeId, seqStart},
 *   items:[{t (sequence seconds), name, comment, color (0-7)}]}. Clip markers
 *   are converted to the clip's source time (so they move with the clip);
 *   ones outside the clip's visible range are skipped.
 * Returns JSON {ok, added, errors, skipped, clipName}.
 */
function esAddMarkers(dataJson) {
    try {
        var data = JSON.parse(dataJson);
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });
        var tgt = _markerTarget(seq, data);
        if (!tgt) return JSON.stringify({ ok: false, error: "Select the clip to mark in the timeline" });
        var coll = tgt.coll, clip = tgt.clip;
        var cStart = 0, cEnd = 0, cIn = 0, clipName = "";
        if (clip) {
            cStart = clip.start.seconds; cEnd = clip.end.seconds; cIn = clip.inPoint.seconds;
            try { clipName = clip.name; } catch (e0) {}
        }
        var items = data.items || [], added = 0, errors = 0, skipped = 0;
        for (var i = 0; i < items.length; i++) {
            try {
                var t = Number(items[i].t);
                if (clip) {
                    if (t < cStart - 0.0005 || t > cEnd + 0.0005) { skipped++; continue; }
                    t = cIn + (t - cStart);
                }
                var m = coll.createMarker(t);
                if (items[i].name) m.name = items[i].name;
                if (items[i].comment) m.comments = items[i].comment;
                if (items[i].color !== undefined && items[i].color !== null) {
                    try { m.setColorByIndex(items[i].color); } catch (ec) {}
                }
                added++;
            } catch (e) { errors++; }
        }
        return JSON.stringify({ ok: true, added: added, errors: errors, skipped: skipped, clipName: clipName });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message });
    }
}

/**
 * Delete markers whose comment contains `tag` (only ones EasyScript added).
 * dataJson: {target, clip, tag}. Returns JSON {ok, removed}.
 */
function esClearMarkers(dataJson) {
    try {
        var data = JSON.parse(dataJson);
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });
        var tgt = _markerTarget(seq, data);
        if (!tgt) return JSON.stringify({ ok: false, error: "Select the clip in the timeline" });
        var coll = tgt.coll;
        var tag = String(data.tag || "EasyScript"), doomed = [];
        var m = coll.getFirstMarker();
        while (m) {
            var c = "";
            try { c = String(m.comments); } catch (e) {}
            if (c.indexOf(tag) >= 0) doomed.push(m);
            m = coll.getNextMarker(m);
        }
        for (var i = 0; i < doomed.length; i++) { try { coll.deleteMarker(doomed[i]); } catch (e2) {} }
        var clipName = "";
        try { if (tgt.clip) clipName = tgt.clip.name; } catch (e3) {}
        return JSON.stringify({ ok: true, removed: doomed.length, clipName: clipName });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message });
    }
}

/**
 * Color each timeline clip (audio + video) by its dominant speaker. Razoring at
 * speaker boundaries is done first via a SEPARATE proCutRazor call (so the DOM
 * is refreshed before we read the split clips here).
 * mapJson: {segments:[{start,end,speaker}] (sequence sec), speakerColor:{...}}
 * Returns JSON: {ok, labeled, api} | {ok:false, error}
 */
function esLabelColor(mapJson) {
    try {
        var data = JSON.parse(mapJson);
        var segs = data.segments || [];
        var spkColor = data.speakerColor || {};
        var seq = app.project.activeSequence;
        if (!seq) return JSON.stringify({ ok: false, error: "No active sequence" });

        function dominant(cs, ce) {
            var ov = {}, i, s, o;
            for (i = 0; i < segs.length; i++) {
                s = segs[i];
                if (!s.speaker) continue;
                o = Math.min(s.end, ce) - Math.max(s.start, cs);
                if (o > 0) ov[s.speaker] = (ov[s.speaker] || 0) + o;
            }
            var best = 0, dom = null, k;
            for (k in ov) { if (ov[k] > best) { best = ov[k]; dom = k; } }
            return dom;
        }

        var spkName = data.speakerName || {};
        var renamed = 0, colored = 0, nameApi = "", colorApi = "";
        function labelTrack(tracks) {
            for (var t = 0; t < tracks.numTracks; t++) {
                var clips = tracks[t].clips;
                for (var c = 0; c < clips.numItems; c++) {
                    var clip = clips[c], cs, ce;
                    try { cs = clip.start.seconds; ce = clip.end.seconds; } catch (e) { continue; }
                    var spk = dominant(cs, ce);
                    if (!spk) continue;
                    var nm = spkName[spk] || spk;

                    // Rename the timeline clip to the speaker name (per-clip).
                    var rdone = false;
                    try { clip.name = nm; rdone = true; nameApi = "clip.name="; } catch (e1) {}
                    if (!rdone) { try { clip.setName(nm); rdone = true; nameApi = "clip.setName"; } catch (e2) {} }
                    if (rdone) renamed++;

                    // Also color via project item (only differs per source — bonus
                    // for multicam where each speaker is its own source clip).
                    var idx = spkColor[spk];
                    if (idx !== undefined && idx !== null) {
                        try { clip.projectItem.setColorLabel(idx); colored++; colorApi = "projectItem.setColorLabel"; } catch (e3) {}
                    }
                }
            }
        }
        labelTrack(seq.videoTracks);
        labelTrack(seq.audioTracks);

        return JSON.stringify({ ok: true, renamed: renamed, colored: colored, nameApi: nameApi || "none", colorApi: colorApi || "none" });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message });
    }
}
