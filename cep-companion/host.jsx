/**
 * Pro Cut Companion — ExtendScript Host
 * Runs inside Premiere Pro via CEP, provides razor/add-edit functionality
 * that UXP API does not support.
 */

/**
 * ExtendScript (4.5.6, still ES3 in Premiere 26.x) has no native JSON object,
 * so proCutApply's JSON.parse below throws before it ever runs. Guarded, so a
 * host that does ship JSON keeps its own.
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

/**
 * Add edit (razor) at multiple time positions on ALL tracks,
 * then remove clips that fall within silence regions.
 *
 * @param {string} dataJson - JSON string: {boundaries: number[], silenceRegions: [number,number][]}
 * @returns {string} Result string: "OK|editCount|removedCount|log" or "ERROR: message"
 */
function proCutApply(dataJson) {
    try {
        var data = JSON.parse(dataJson);
        var boundaries = data.boundaries || [];
        var silenceRegions = data.silenceRegions || [];
        var TICKS_PER_SECOND = 254016000000;
        var log = [];

        // Enable QE DOM
        try {
            app.enableQE();
        } catch(e) {
            return "ERROR: Cannot enable QE: " + e.message;
        }

        var qeSeq = null;
        try {
            qeSeq = qe.project.getActiveSequence();
        } catch(e) {
            return "ERROR: No QE sequence: " + e.message;
        }
        if (!qeSeq) return "ERROR: No active QE sequence";

        var seq = app.project.activeSequence;
        if (!seq) return "ERROR: No active sequence";

        log.push("Sequence: " + seq.name);
        log.push("V-tracks: " + qeSeq.numVideoTracks + ", A-tracks: " + qeSeq.numAudioTracks);
        log.push("Boundaries: " + boundaries.length + ", Silence: " + silenceRegions.length);

        // Step 1: Razor all tracks at each boundary
        var editCount = 0;
        for (var i = 0; i < boundaries.length; i++) {
            var ticks = Math.round(boundaries[i] * TICKS_PER_SECOND).toString();

            for (var t = 0; t < qeSeq.numVideoTracks; t++) {
                try {
                    qeSeq.getVideoTrackAt(t).razor(ticks);
                    editCount++;
                } catch(e) {}
            }
            for (var t = 0; t < qeSeq.numAudioTracks; t++) {
                try {
                    qeSeq.getAudioTrackAt(t).razor(ticks);
                    editCount++;
                } catch(e) {}
            }
        }
        log.push("Edits added: " + editCount);

        // Step 2: Count clips after razor to verify
        var totalClipsAfter = 0;
        for (var t = 0; t < seq.videoTracks.numTracks; t++) {
            var n = seq.videoTracks[t].clips.numItems;
            log.push("V" + t + ": " + n + " clips");
            totalClipsAfter += n;
        }
        for (var t = 0; t < seq.audioTracks.numTracks; t++) {
            var n = seq.audioTracks[t].clips.numItems;
            log.push("A" + t + ": " + n + " clips");
            totalClipsAfter += n;
        }
        log.push("Total clips after razor: " + totalClipsAfter);

        // Step 3: Remove silence clips
        // A clip is "silence" if its midpoint OR >50% of its duration falls within a silence region
        var removedCount = 0;
        var checkedCount = 0;
        var TOL = 0.15; // tolerance in seconds

        function isInSilence(clipStart, clipEnd) {
            var mid = (clipStart + clipEnd) / 2;
            var clipDur = clipEnd - clipStart;

            for (var r = 0; r < silenceRegions.length; r++) {
                var silStart = silenceRegions[r][0];
                var silEnd = silenceRegions[r][1];

                // Check if midpoint is inside silence region
                if (mid >= silStart - TOL && mid <= silEnd + TOL) return true;

                // Check if >50% of clip overlaps with silence
                var overlapStart = Math.max(clipStart, silStart);
                var overlapEnd = Math.min(clipEnd, silEnd);
                if (overlapEnd > overlapStart) {
                    var overlap = overlapEnd - overlapStart;
                    if (overlap > clipDur * 0.5) return true;
                }
            }
            return false;
        }

        // Video tracks — iterate backwards
        for (var t = seq.videoTracks.numTracks - 1; t >= 0; t--) {
            var track = seq.videoTracks[t];
            for (var c = track.clips.numItems - 1; c >= 0; c--) {
                try {
                    var clip = track.clips[c];
                    var clipStart = clip.start.seconds;
                    var clipEnd = clip.end.seconds;
                    checkedCount++;

                    if (isInSilence(clipStart, clipEnd)) {
                        clip.remove(false, true);
                        removedCount++;
                    }
                } catch(e) {}
            }
        }

        // Audio tracks — iterate backwards
        for (var t = seq.audioTracks.numTracks - 1; t >= 0; t--) {
            var track = seq.audioTracks[t];
            for (var c = track.clips.numItems - 1; c >= 0; c--) {
                try {
                    var clip = track.clips[c];
                    var clipStart = clip.start.seconds;
                    var clipEnd = clip.end.seconds;
                    checkedCount++;

                    if (isInSilence(clipStart, clipEnd)) {
                        clip.remove(false, true);
                        removedCount++;
                    }
                } catch(e) {}
            }
        }

        log.push("Checked: " + checkedCount + " clips, Removed: " + removedCount + " silence clips");
        return "OK|" + editCount + "|" + removedCount + "|" + log.join("\n");

    } catch(e) {
        return "ERROR: " + e.message;
    }
}

/**
 * Simple test to verify ExtendScript is working
 */
function proCutPing() {
    try {
        var seq = app.project.activeSequence;
        return "PONG|" + (seq ? seq.name : "no sequence");
    } catch(e) {
        return "ERROR: " + e.message;
    }
}
