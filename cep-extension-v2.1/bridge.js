/**
 * EasyScript CEP bridge — calls host.jsx (ExtendScript) via CSInterface.
 * Exposes window.bridge with promise-returning helpers.
 */
(function (global) {
  var cs = null;
  try { cs = new CSInterface(); } catch (e) { cs = null; }

  // Run an ExtendScript expression, resolve with the returned string.
  function es(call) {
    return new Promise(function (resolve, reject) {
      if (!cs) { reject(new Error("Not running inside Premiere (CSInterface unavailable).")); return; }
      try {
        cs.evalScript(call, function (res) {
          if (res === "EvalScript error.") { reject(new Error("ExtendScript error in: " + call)); return; }
          resolve(res);
        });
      } catch (e) { reject(e); }
    });
  }

  function delay(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function esJson(call) {
    return es(call).then(function (res) {
      var data;
      try { data = JSON.parse(res); }
      catch (e) { throw new Error("Bad response from Premiere: " + String(res).slice(0, 200)); }
      if (data && data.ok === false) throw new Error(data.error || "Premiere call failed");
      return data;
    });
  }

  // Escape a JS string for embedding inside a single-quoted ExtendScript arg.
  function q(str) {
    return String(str)
      .replace(/\\/g, "\\\\")
      .replace(/'/g, "\\'")
      .replace(/\r?\n/g, "\\n");
  }

  global.bridge = {
    available: function () { return !!cs; },
    ping: function () { return es("proCutPing()"); },
    inspectClip: function () { return es("esInspectClip()"); },
    homeDir: function () { return es("(function(){return Folder('~').fsName})()"); },
    // Backend access token for this port (raw string, "" if the file is missing).
    readToken: function (port) { return es("esReadBackendToken(" + (parseInt(port, 10) || 9876) + ")"); },
    // Unrounded fps (29.97002997…). Prefer sequenceTimebase() for frame math.
    sequenceFps: function () {
      return es("esSequenceFps()").then(function (s) { return parseFloat(s) || 25; });
    },
    // {ticksPerFrame, fps, sequenceID, name, endTicks} of the active sequence.
    sequenceTimebase: function () { return esJson("esSequenceTimebase()"); },
    setPlayerPosition: function (ticks) { return es("esSetPlayerPosition('" + q(String(ticks)) + "')"); },
    getPlayerPosition: function () { return es("esGetPlayerPosition()"); },
    listAudioTracks: function () { return esJson("esListAudioTracks()"); },
    exportSequenceXML: function () { return esJson("esExportSequenceXML()"); },
    importSequenceXML: function (path, expectName) {
      return esJson("esImportSequenceXML('" + q(path) + "','" + q(expectName || "") + "')");
    },
    // Add markers in batches so a long beat grid never blocks Premiere for long.
    // target: "sequence" | "clip"; items: [{t, name, comment, color}].
    addMarkers: function (target, clip, items, onProgress, isCancelled) {
      var BATCH = 200, i = 0, added = 0, errors = 0;
      function next() {
        if (i >= items.length || (isCancelled && isCancelled())) {
          return Promise.resolve({ added: added, errors: errors, cancelled: i < items.length });
        }
        var chunk = items.slice(i, i + BATCH);
        var j = q(JSON.stringify({ target: target, clip: clip || {}, items: chunk }));
        return esJson("esAddMarkers('" + j + "')").then(function (r) {
          added += r.added || 0; errors += r.errors || 0; i += chunk.length;
          try { if (onProgress) onProgress(i / items.length, i, items.length); } catch (e) {}
          return delay(10).then(next);
        });
      }
      return next();
    },
    clearMarkers: function (target, clip, tag) {
      var j = q(JSON.stringify({ target: target, clip: clip || {}, tag: tag || "EasyScript" }));
      return esJson("esClearMarkers('" + j + "')");
    },
    getSelectedClip: function (mode, track) {
      return esJson("esGetSelectedClip('" + q(mode) + "','" + q(String(track)) + "')");
    },
    // Import a stem and lay it at startTicks on the first audio track that is
    // empty for its whole length; adds a stereo track at the bottom when every
    // track is busy there. Resolves {track, startTicks, name, addedTrack}.
    placeStem: function (path, startTicks, durationTicks) {
      var j = q(JSON.stringify({ path: path, startTicks: String(startTicks), durationTicks: String(durationTicks) }));
      function place() {
        return es("esPlaceStem('" + j + "')").then(function (res) {
          try { return JSON.parse(res); }
          catch (e) { throw new Error("Bad response from Premiere: " + String(res).slice(0, 200)); }
        });
      }
      return place().then(function (r) {
        if (r.ok) { r.addedTrack = false; return r; }
        if (!r.needTrack) throw new Error(r.error || "Import failed");
        return esJson("esAddAudioTrack()").then(function () { return delay(150); }).then(place).then(function (r2) {
          if (!r2.ok) throw new Error(r2.needTrack ? "Could not add a free audio track." : (r2.error || "Import failed"));
          r2.addedTrack = true;
          return r2;
        });
      });
    },
    seqMethods: function () { return es("esListSeqMethods()"); },
    dumpLabelAPI: function () { return es("esDumpLabelAPI()"); }, // TEMP v2.1
    // quality "hq" = 48 kHz stereo (stem separation, material that goes back
    // on the timeline); default 16 kHz mono (analysis).
    renderRange: function (mode, start, end, quality) {
      var ext = cs ? cs.getSystemPath(SystemPath.EXTENSION) : "";
      var preset = ext + "/presets/" + (quality === "hq" ? "WAV_Stereo_16bit_48kHz.epr" : "WAV_Mono_16bit_16kHz.epr");
      return esJson("esRenderRange('" + q(preset) + "','" + q(mode) + "'," + (Number(start) || 0) + "," + (Number(end) || 0) + ")");
    },
    // Razor and remove in TWO separate evalScript calls so the DOM refreshes
    // between them (QE razor edits aren't visible to the DOM in the same call).
    // Razor + sync + remove for ONE chunk (no re-link). Used to process a long
    // timeline in ~10-minute windows so each freeze is short. Returns
    // {edits, removed, scanned}. Sync is a quick poll (small chunk → resolves
    // almost instantly), so there is no long blind wait anymore.
    applyCutsChunk: function (boundaries, silenceRegions, fps) {
      var rj = q(JSON.stringify({ boundaries: boundaries, fps: fps || 25 }));
      var sj = q(JSON.stringify({ silenceRegions: silenceRegions }));
      return es("proCutRazor('" + rj + "')").then(function (r1) {
        if (!r1 || r1.indexOf("OK") !== 0) throw new Error(r1 || "Razor failed");
        var rp = r1.split("|");
        var edits = parseInt(rp[1], 10) || 0;
        var beforeV = parseInt(rp[2], 10);
        var expectedV = parseInt(rp[3], 10);
        var maxMs = 8000, t0 = Date.now();
        function waitSync() {
          if (!(expectedV > 0) || expectedV <= beforeV) return delay(120);
          return es("esDomVClipCount()").then(function (s) {
            var n = parseInt(s, 10) || 0;
            if (n >= expectedV) return;
            if (Date.now() - t0 >= maxMs) return;
            return delay(120).then(waitSync);
          }).catch(function () {
            if (Date.now() - t0 >= maxMs) return;
            return delay(120).then(waitSync);
          });
        }
        return waitSync().then(function () {
          return es("proCutRemove('" + sj + "')").then(function (r2) {
            if (!r2 || r2.indexOf("OK") !== 0) throw new Error(r2 || "Remove failed");
            var p = r2.split("|");
            return {
              edits: edits,
              removed: parseInt(p[1], 10) || 0,
              scanned: parseInt(p[2], 10) || 0,
            };
          });
        });
      });
    },
    // Razor + remove for ONE chunk, done ENTIRELY in QE in a single call — no
    // wait for the Scripting DOM, so there's no pause between cut and remove.
    // Returns {edits, removed, scanned}.
    applyCutChunkQE: function (boundaries, silenceRegions, fps) {
      var j = q(JSON.stringify({ boundaries: boundaries, silenceRegions: silenceRegions, fps: fps || 25 }));
      return es("proCutChunkQE('" + j + "')").then(function (r) {
        if (!r || r.indexOf("OK") !== 0) throw new Error(r || "QE cut failed");
        var p = r.split("|");
        return { edits: parseInt(p[1], 10) || 0, removed: parseInt(p[2], 10) || 0, scanned: parseInt(p[3], 10) || 0 };
      });
    },
    // Remove silence via QE sequence.extract() — one op per region, removes the
    // range from all tracks, ripples, and preserves A/V links (no relink).
    // regionsTicks: [[inTicks, outTicks]] frame-aligned (snapped inward).
    // sequenceID: refuse to cut if the user switched sequences meanwhile.
    // Returns {extracted, total}.
    extractTicks: function (regionsTicks, sequenceID) {
      var j = q(JSON.stringify({ regionsTicks: regionsTicks, sequenceID: sequenceID || "" }));
      return es("proCutExtract('" + j + "')").then(function (r) {
        if (!r || r.indexOf("OK") !== 0) throw new Error((r || "Extract failed").replace(/^ERROR:\s*/, ""));
        var p = r.split("|");
        return { extracted: parseInt(p[1], 10) || 0, total: parseInt(p[2], 10) || 0, err: p[3] || "" };
      });
    },
    // Legacy seconds-based variant (snapped inward on the real frame grid in host.jsx).
    extractChunk: function (silenceRegions, fps) {
      var j = q(JSON.stringify({ silenceRegions: silenceRegions, fps: fps || 25 }));
      return es("proCutExtract('" + j + "')").then(function (r) {
        if (!r || r.indexOf("OK") !== 0) throw new Error(r || "Extract failed");
        var p = r.split("|");
        return { extracted: parseInt(p[1], 10) || 0, total: parseInt(p[2], 10) || 0, err: p[3] || "" };
      });
    },
    // Re-link all A/V pairs once (called after all chunks are removed).
    // Re-link reads the Scripting DOM, so first wait for it to settle after the
    // QE edits (poll until the clip count stops changing — one wait, at the end).
    relinkAV: function () {
      var t0 = Date.now(), maxMs = 8000, last = -1, stable = 0;
      function settle() {
        return es("esDomVClipCount()").then(function (s) {
          var n = parseInt(s, 10) || 0;
          if (n === last) stable++; else { stable = 0; last = n; }
          if (stable >= 2 && n > 0) return;
          if (Date.now() - t0 >= maxMs) return;
          return delay(200).then(settle);
        }).catch(function () {
          if (Date.now() - t0 >= maxMs) return;
          return delay(200).then(settle);
        });
      }
      return settle().then(function () {
        return esJson("esRelinkAV('x')").then(function (lr) {
          return (lr && lr.linked) || 0;
        }).catch(function () { return 0; });
      });
    },
    applyCuts: function (boundaries, silenceRegions, fps, onProgress) {
      function report(phase) { try { if (onProgress) onProgress(phase); } catch (e) {} }
      var rj = q(JSON.stringify({ boundaries: boundaries, fps: fps || 25 }));
      var sj = q(JSON.stringify({ silenceRegions: silenceRegions }));
      report("razor");
      return es("proCutRazor('" + rj + "')").then(function (r1) {
        if (!r1 || r1.indexOf("OK") !== 0) throw new Error(r1 || "Razor failed");
        var rp = r1.split("|");
        var edits = parseInt(rp[1], 10) || 0;
        var beforeV = parseInt(rp[2], 10);
        var expectedV = parseInt(rp[3], 10);
        console.log("[EasyScript] razor QE clips before/afterV/afterA:", rp[2], rp[3], rp[4]);
        // The DOM lags the QE razor on long timelines. Instead of a blind wait,
        // poll the DOM clip count until it reflects the new (split) clips, so
        // removal runs the instant the timeline is ready — quick on short files,
        // patient on long ones. Cap at 20s as a safety net.
        report("sync");
        var maxMs = 20000, t0 = Date.now();
        function waitSync() {
          if (!(expectedV > 0) || expectedV <= beforeV) return delay(500);
          return es("esDomVClipCount()").then(function (s) {
            var n = parseInt(s, 10) || 0;
            if (n >= expectedV) return;
            if (Date.now() - t0 >= maxMs) return;
            return delay(250).then(waitSync);
          }).catch(function () {
            if (Date.now() - t0 >= maxMs) return;
            return delay(250).then(waitSync);
          });
        }
        return waitSync().then(function () {
          report("remove");
          return es("proCutRemove('" + sj + "')").then(function (r2) {
            if (!r2 || r2.indexOf("OK") !== 0) throw new Error(r2 || "Remove failed");
            var p = r2.split("|");
            var result = {
              edits: edits,
              removed: parseInt(p[1], 10) || 0,
              scanned: parseInt(p[2], 10) || 0,
              sample: p[3] || "",
              linked: 0,
            };
            // Re-link A/V (broken by the per-track razor) in a 3rd call.
            report("link");
            return delay(150).then(function () {
              return esJson("esRelinkAV('x')").then(function (lr) {
                result.linked = (lr && lr.linked) || 0;
                return result;
              }).catch(function () { return result; });
            });
          });
        });
      });
    },
    // Import an SRT and place it on a new caption track at startSec.
    importSubtitle: function (path, startSec) {
      return esJson("esImportSubtitle('" + q(path) + "'," + (Number(startSec) || 0) + ")");
    },
    labelSpeaker: function (boundaries, segments, speakerColor, fps, speakerName) {
      // 1) Split clips at speaker-change points via 1-frame extract (keeps A/V
      //    linked, no razor, no hang). Boundaries already on an existing clip
      //    edge (Apply Cut cut there because of silence) are skipped.
      // 2) Wait for the DOM to reflect the new splits.
      // 3) Rename each clip to its dominant speaker.
      var bj = q(JSON.stringify({ boundaries: boundaries, fps: fps || 25 }));
      var cj = q(JSON.stringify({ segments: segments, speakerColor: speakerColor, speakerName: speakerName || {} }));
      return es("esSplitSpeakers('" + bj + "')").then(function (r1) {
        var parts = (r1 && r1.indexOf("OK") === 0) ? r1.split("|") : [];
        var edits = parts.length ? (parseInt(parts[1], 10) || 0) : 0;
        // Every 1-frame extract shifts what follows one frame left: move the
        // segments the same way so each clip is named after its own speaker.
        var removed = (parts[4] || "").split(",").filter(Boolean).map(Number).sort(function (a, b) { return a - b; });
        var frame = 1 / (fps || 25);
        if (removed.length) {
          var shift = function (t) {
            var n = 0;
            while (n < removed.length && removed[n] < t - 1e-6) n++;
            return t - n * frame;
          };
          segments = segments.map(function (s) {
            return { start: shift(s.start), end: shift(s.end), speaker: s.speaker };
          });
          cj = q(JSON.stringify({ segments: segments, speakerColor: speakerColor, speakerName: speakerName || {} }));
        }
        // Settle the DOM after the QE split-extracts, then rename.
        var t0 = Date.now(), maxMs = 8000, last = -1, stable = 0;
        function settle() {
          return es("esDomVClipCount()").then(function (s) {
            var n = parseInt(s, 10) || 0;
            if (n === last) stable++; else { stable = 0; last = n; }
            if (stable >= 2 && n > 0) return;
            if (Date.now() - t0 >= maxMs) return;
            return delay(150).then(settle);
          }).catch(function () {
            if (Date.now() - t0 >= maxMs) return;
            return delay(150).then(settle);
          });
        }
        return settle().then(function () {
          return esJson("esLabelColor('" + cj + "')").then(function (res) {
            res.edits = edits;
            return res;
          });
        });
      });
    },
  };
})(window);
