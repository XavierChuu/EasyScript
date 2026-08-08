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

/** Active sequence frame rate (fps), as a string. */
function esSequenceFps() {
    try {
        var seq = app.project.activeSequence;
        if (!seq) return "25";
        var fr = seq.getSettings().videoFrameRate.seconds; // seconds per frame
        if (fr > 0) return String(Math.round(1 / fr));
    } catch (e) {}
    return "25";
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

        var fps = parseInt(esSequenceFps(), 10) || 25;
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
        var start = 0, end = 0, inPoint = 0, outPoint = 0;
        try { start = found.start.seconds; end = found.end.seconds; } catch (e) {}
        try { inPoint = found.inPoint.seconds; } catch (e) {}
        try { outPoint = found.outPoint.seconds; } catch (e) {}

        // No media path → nested sequence (or generated clip). Return the clip
        // anyway with nested:true so the frontend renders the range instead.
        // start/end = sequence time; inPoint/outPoint = source time of the trimmed clip.
        return JSON.stringify({ ok: true, path: path || "", nested: !path, name: name, start: start, end: end, inPoint: inPoint, outPoint: outPoint, fps: fps });
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

/** seconds → "HH:MM:SS:FF" timecode at the given fps. */
function _toTimecode(sec, fps) {
    if (!fps || fps < 1) fps = 25;
    var f = Math.round(sec * fps);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return p(Math.floor(f / (fps * 3600))) + ":" + p(Math.floor(f / (fps * 60)) % 60) +
           ":" + p(Math.floor(f / fps) % 60) + ":" + p(f % fps);
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
        for (var i = 0; i < boundaries.length; i++) {
            var tc = _toTimecode(boundaries[i], fps);
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
        for (var i = 0; i < boundaries.length; i++) {
            var tc = _toTimecode(boundaries[i], fps);
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
 * dataJson:{ silenceRegions:[[s,e]], fps }
 * Returns: "OK|extracted|total|errSample"
 */
function proCutExtract(dataJson) {
    try {
        var data = JSON.parse(dataJson);
        var regions = (data.silenceRegions || []).slice();
        var fps = data.fps || 25; if (!fps || fps < 1) fps = 25;
        var frameDur = 1.0 / fps;
        try { app.enableQE(); } catch (e) { return "ERROR: Cannot enable QE: " + e.message; }
        var qeSeq = null;
        try { qeSeq = qe.project.getActiveSequence(); } catch (e) { return "ERROR: No QE sequence: " + e.message; }
        if (!qeSeq) return "ERROR: No active QE sequence";
        // In/Out is set on the REGULAR sequence; extract() reads it from there.
        // (Setting it on the QE sequence does NOT work — that was the bug.)
        var seq = app.project.activeSequence;
        if (!seq) return "ERROR: No active sequence";
        var TICKS = TICKS_PER_SECOND;
        var nV = seq.videoTracks.numTracks, nA = seq.audioTracks.numTracks;

        // Target ALL tracks — extract only affects targeted tracks.
        for (var t = 0; t < nV; t++) { try { seq.videoTracks[t].setTargeted(true, true); } catch (e) { try { seq.videoTracks[t].setTargeted(true); } catch (e2) {} } }
        for (var t = 0; t < nA; t++) { try { seq.audioTracks[t].setTargeted(true, true); } catch (e) { try { seq.audioTracks[t].setTargeted(true); } catch (e2) {} } }

        function snap(sec) { return Math.round(sec * fps) / fps; }

        // Right → left so each extract never shifts not-yet-processed regions.
        regions.sort(function (a, b) { return b[0] - a[0]; });

        var extracted = 0, errs = [], skip = 0;
        for (var i = 0; i < regions.length; i++) {
            var s = snap(regions[i][0]), e = snap(regions[i][1]);
            if (e - s < frameDur * 0.9) { skip++; continue; }
            var inTicks = Math.round(s * TICKS).toString();
            var outTicks = Math.round(e * TICKS).toString();
            try {
                seq.setInPoint(inTicks);
                seq.setOutPoint(outTicks);
                qeSeq.extract();
                extracted++;
            } catch (ex) { if (errs.length < 5) errs.push("ex@" + s.toFixed(2) + ":" + ex.message); }
        }
        // Reset the In point so we don't leave a stray In/Out range.
        try { seq.setInPoint("0"); } catch (ex) {}

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
        var fps = data.fps || 25; if (!fps || fps < 1) fps = 25;
        var frameDur = 1.0 / fps, tol = frameDur * 1.5;
        try { app.enableQE(); } catch (e) { return "ERROR: Cannot enable QE: " + e.message; }
        var qeSeq = null;
        try { qeSeq = qe.project.getActiveSequence(); } catch (e) { return "ERROR: No QE sequence: " + e.message; }
        if (!qeSeq) return "ERROR: No active QE sequence";
        var seq = app.project.activeSequence;
        if (!seq) return "ERROR: No active sequence";
        var TICKS = TICKS_PER_SECOND;
        var nV = seq.videoTracks.numTracks, nA = seq.audioTracks.numTracks;

        for (var t = 0; t < nV; t++) { try { seq.videoTracks[t].setTargeted(true, true); } catch (e) { try { seq.videoTracks[t].setTargeted(true); } catch (e2) {} } }
        for (var t = 0; t < nA; t++) { try { seq.audioTracks[t].setTargeted(true, true); } catch (e) { try { seq.audioTracks[t].setTargeted(true); } catch (e2) {} } }

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
        function snap(sec) { return Math.round(sec * fps) / fps; }

        boundaries.sort(function (a, b) { return b - a; }); // right → left
        var split = 0, skipped = 0, errs = [];
        for (var i = 0; i < boundaries.length; i++) {
            var tt = snap(boundaries[i]);
            if (tt <= 0) { skipped++; continue; }
            if (nearEdge(tt)) { skipped++; continue; } // already cut here (had silence)
            var inT = Math.round(tt * TICKS).toString();
            var outT = Math.round((tt + frameDur) * TICKS).toString();
            try { seq.setInPoint(inT); seq.setOutPoint(outT); qeSeq.extract(); split++; }
            catch (ex) { if (errs.length < 5) errs.push("ex@" + tt.toFixed(2) + ":" + ex.message); }
        }
        try { seq.setInPoint("0"); } catch (ex) {}
        return "OK|" + split + "|" + skipped + "|" + boundaries.length;
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
        var out = dir.fsName + "/render_" + (new Date().getTime()) + ".wav";
        log.push("out=" + out);

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
        if (!f.exists) return JSON.stringify({ ok: false, error: "no output file (status " + status + ")", log: log.join(" | ") });
        return JSON.stringify({ ok: true, path: out, start: rngStart, end: rngEnd, log: log.join(" | ") });
    } catch (e) {
        return JSON.stringify({ ok: false, error: e.message, log: log.join(" | ") });
    }
}

/** Import an SRT file into the project (creates a caption item). */
function esImportSubtitle(srtPath) {
    try {
        var ok = app.project.importFiles([srtPath], true, app.project.rootItem, false);
        return JSON.stringify({ ok: true, imported: ok });
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
