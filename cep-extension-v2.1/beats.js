/**
 * EasyScript — beat markers.
 *
 * Detect beats on the backend (/beats job), derive the marker grid here
 * (subdivision, meter, downbeat offset, half/double tempo), review it on the
 * waveform (exclude/add markers, optional click track while playing), then add
 * the markers to the sequence or to the analysed clip in Premiere.
 *
 * Markers EasyScript adds carry TAG in their comment, so "Remove" only ever
 * deletes ours.
 *
 * Uses index.js globals (currentAudioPath, loadedClipInfo, fetchBackend,
 * progressTracker, showStatus, timebase helpers) — only from event handlers,
 * i.e. after index.js has run.
 */
(function (global) {
  "use strict";

  var TAG = "EasyScript beat";
  var COLORS = ["Green", "Red", "Purple", "Orange", "Yellow", "White", "Blue", "Cyan"];

  var state = {
    raw: [], bpm: 0, analyzed: "",   // backend beats [{t, s, lf}] for this audio path
    tempo: 1,                        // 0.5 = half time, 2 = double time
    meter: 4, offset: 0, every: "1",
    excluded: {}, added: [],
    markers: [],
  };

  function $(id) { return document.getElementById(id); }

  // ── Grid ──

  function baseBeats() {
    var b = state.raw.map(function (x) { return { t: x.t, s: x.s || 0, lf: x.lf || 0 }; });
    if (state.tempo === 0.5 && b.length > 1) {
      // Keep whichever alternate half carries the stronger accents.
      var even = [], odd = [], se = 0, so = 0;
      b.forEach(function (x, i) {
        if (i % 2 === 0) { even.push(x); se += x.s + x.lf; } else { odd.push(x); so += x.s + x.lf; }
      });
      b = (se / Math.max(1, even.length)) >= (so / Math.max(1, odd.length)) ? even : odd;
    } else if (state.tempo === 2) {
      var out = [];
      for (var i = 0; i < b.length; i++) {
        out.push(b[i]);
        if (i + 1 < b.length) out.push({ t: (b[i].t + b[i + 1].t) / 2, s: 0, lf: 0 });
      }
      b = out;
    }
    return b;
  }

  /** Bar positions: in sliding 8-bar windows, the phase whose beats carry the
   *  most low-band (kick) energy is the downbeat. `offset` shifts it by beats. */
  function assignBars(beats, meter, offset) {
    var n = beats.length;
    if (!n) return;
    var score = beats.map(function (b) { return 0.75 * b.lf + 0.25 * b.s; });
    var step = meter * 2, half = meter * 4, phase = new Array(n);
    for (var c = 0; c < n; c += step) {
      var lo = Math.max(0, c - half), hi = Math.min(n, c + step + half), acc = [];
      for (var p = 0; p < meter; p++) acc.push(0);
      for (var i = lo; i < hi; i++) acc[i % meter] += score[i];
      var best = 0;
      for (var q = 1; q < meter; q++) if (acc[q] > acc[best]) best = q;
      for (var k = c; k < Math.min(n, c + step); k++) phase[k] = best;
    }
    var bar = 0;
    for (var j = 0; j < n; j++) {
      var pos = (((j - phase[j] - offset) % meter) + meter) % meter;
      if (pos === 0) bar++;
      beats[j].pos = pos + 1;
      beats[j].bar = Math.max(1, bar);
      beats[j].down = pos === 0;
    }
  }

  function keyOf(t) { return String(Math.round(t * 1000)); }

  function computeMarkers() {
    var beats = baseBeats();
    assignBars(beats, state.meter, state.offset);
    var ev = state.every, out = [];
    function push(b, kind, label) { out.push({ t: b.t, kind: kind, bar: b.bar, pos: b.pos, label: label }); }
    if (ev === "0.25" || ev === "0.5") {
      var div = ev === "0.5" ? 2 : 4;
      beats.forEach(function (b, i) {
        push(b, b.down ? "down" : "beat", b.down ? "Bar " + b.bar : b.bar + "." + b.pos);
        var nb = beats[i + 1];
        if (!nb) return;
        var dt = (nb.t - b.t) / div;
        if (!(dt > 0 && dt < 1.5)) return;  // no subdivisions across gaps
        for (var k = 1; k < div; k++) {
          out.push({ t: b.t + k * dt, kind: "sub", bar: b.bar, pos: b.pos, label: b.bar + "." + b.pos + " +" + k + "/" + div });
        }
      });
    } else {
      beats.forEach(function (b) {
        var take = ev === "1" ? true
          : ev === "2" ? (b.pos - 1) % 2 === 0
          : ev === "bar" ? b.down
          : ev === "2bar" ? b.down && (b.bar - 1) % 2 === 0
          : ev === "4bar" ? b.down && (b.bar - 1) % 4 === 0 : true;
        if (take) push(b, b.down ? "down" : "beat", b.down ? "Bar " + b.bar : b.bar + "." + b.pos);
      });
    }
    state.added.forEach(function (t) { out.push({ t: t, kind: "manual", label: "Marker" }); });
    out.sort(function (a, b) { return a.t - b.t; });
    out.forEach(function (m) { m.key = keyOf(m.t); m.excluded = !!state.excluded[m.key] && m.kind !== "manual"; });
    state.markers = out;
    if (global.waveform) waveform.setBeatMarkers(out);
    render();
  }

  // ── UI ──

  function render() {
    var active = state.markers.filter(function (m) { return !m.excluded; }).length;
    var excluded = state.markers.length - active;
    var hasBeats = state.raw.length > 0;
    var bpm = state.bpm * state.tempo;
    if ($("beatSummary")) {
      $("beatSummary").textContent = hasBeats
        ? (Math.round(bpm * 10) / 10) + " BPM · " + state.raw.length + " beats detected"
        : "No beats yet";
    }
    if ($("beatCount")) {
      $("beatCount").textContent = hasBeats
        ? active + " marker" + (active === 1 ? "" : "s") + (excluded ? " · " + excluded + " excluded" : "")
        : "";
    }
    if ($("beatOffsetVal")) $("beatOffsetVal").textContent = (state.offset > 0 ? "+" : "") + state.offset;
    if ($("beatTempoVal")) $("beatTempoVal").textContent = state.tempo === 1 ? "1×" : state.tempo === 2 ? "2×" : "½×";
    ["beatApplyBtn", "beatResetBtn"].forEach(function (id) { if ($(id)) $(id).disabled = !hasBeats || !active; });
    if ($("beatApplyBtn") && hasBeats) $("beatApplyBtn").textContent = "Add " + active + " marker" + (active === 1 ? "" : "s");
    if ($("beatApplyBtn") && !hasBeats) $("beatApplyBtn").textContent = "Add markers";
    var clipOk = !!(loadedClipInfo && loadedClipInfo.nodeId && !loadedClipInfo.nested && loadedClipInfo.sourceMode === "selected");
    var clipRadio = document.querySelector('input[name="beatTarget"][value="clip"]');
    if (clipRadio) {
      clipRadio.disabled = !clipOk;
      if (!clipOk && clipRadio.checked) document.querySelector('input[name="beatTarget"][value="sequence"]').checked = true;
      var lbl = $("beatClipLabel");
      if (lbl) lbl.title = clipOk ? "Markers on the clip's source media — they move with the clip"
        : "Available when a single clip was loaded (Range: Selected clip)";
    }
  }

  function target() {
    var el = document.querySelector('input[name="beatTarget"]:checked');
    return el ? el.value : "sequence";
  }

  function onOptionChange() {
    state.every = $("beatEvery").value;
    state.meter = parseInt($("beatMeter").value, 10) || 4;
    if (state.offset >= state.meter) state.offset = 0;
    computeMarkers();
  }

  // ── Detection ──

  async function detect() {
    if (!currentAudioPath) { showStatus("Load audio from the timeline first.", true); return; }
    var btn = $("beatDetectBtn");
    btn.disabled = true;
    btn.classList.add("processing");
    progressTracker.show();
    try {
      var hint = parseFloat(($("beatBpmInput") || {}).value);
      var body = { audio_path: currentAudioPath };
      if (hint >= 20 && hint <= 400) body.bpm = hint;
      var minB = parseFloat(($("beatMinBpm") || {}).value), maxB = parseFloat(($("beatMaxBpm") || {}).value);
      if (minB > 0) body.min_bpm = minB;
      if (maxB > 0) body.max_bpm = maxB;
      var job = await fetchBackend("/beats", { method: "POST", body: JSON.stringify(body) });
      var result = await progressTracker.pollUntilDone("/jobs/" + job.job_id, null, function () {
        fetchBackend("/jobs/" + job.job_id + "/cancel", { method: "POST" }).catch(function () {});
      });
      state.raw = result.beats || [];
      state.bpm = result.bpm || 0;
      state.analyzed = currentAudioPath;
      state.tempo = 1; state.offset = 0; state.excluded = {}; state.added = [];
      progressTracker.update(1, "done", "Done — " + state.raw.length + " beats · " + state.bpm + " BPM");
      if (!state.raw.length) showStatus("No steady beat found in this audio.", true);
      computeMarkers();
      if (global.setActiveTab) setActiveTab("beats");
    } catch (err) {
      if (err.message !== "__CANCELLED__") {
        progressTracker.update(0, "error", "Error: " + err.message);
        showStatus("Beat detection failed: " + err.message, true);
      }
    } finally {
      btn.disabled = false;
      btn.classList.remove("processing");
      progressTracker.stopPolling();
      progressTracker.hide();
    }
  }

  /** Forget beats from a previous audio (called when new audio is loaded). */
  function reset() {
    state.raw = []; state.bpm = 0; state.analyzed = ""; state.markers = [];
    state.excluded = {}; state.added = []; state.tempo = 1; state.offset = 0;
    if (global.waveform) waveform.setBeatMarkers([]);
    render();
  }

  // ── Apply ──

  async function apply() {
    var active = state.markers.filter(function (m) { return !m.excluded; });
    if (!active.length) return;
    if (!global.bridge || !bridge.available()) { showStatus("Not running inside Premiere Pro.", true); return; }
    var tgt = target();
    var info = loadedClipInfo || {};
    var btn = $("beatApplyBtn");
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Adding…";
    var cancelled = false;
    progressTracker.show();
    progressTracker._onCancel = function () { cancelled = true; };
    try {
      await ensureSameSequence();
      var tb = await refreshTimebase();
      var color = parseInt($("beatColor").value, 10), down = parseInt($("beatDownColor").value, 10);
      var bpmTxt = (Math.round(state.bpm * state.tempo * 10) / 10) + " BPM";
      var items = active.map(function (m) {
        // Sequence markers: timeline time, on a frame. Clip markers: the clip's source time.
        var t = tgt === "clip"
          ? (info.srcIn || 0) + m.t
          : frameToSec(secToFrameRound(analysisToSeqTime(m.t), tb), tb);
        return { t: t, name: m.label, comment: TAG + " · " + bpmTxt,
                 color: m.kind === "down" || m.kind === "manual" ? down : color };
      });
      var res = await bridge.addMarkers(tgt, { nodeId: info.nodeId, seqStart: info.seqStart || 0 }, items,
        function (p, done, total) { progressTracker.update(p, "markers", "Adding markers… " + done + "/" + total); },
        function () { return cancelled; });
      progressTracker.update(1, "done", "Added " + res.added + " markers");
      showStatus((res.cancelled ? "Stopped — " : "") + "Added " + res.added + " " +
        (tgt === "clip" ? "clip" : "sequence") + " markers" + (res.errors ? " (" + res.errors + " failed)" : ""),
        false, "DONE");
    } catch (err) {
      showStatus("Adding markers failed: " + err.message, true);
    } finally {
      btn.textContent = label;
      render();
      progressTracker.hide();
    }
  }

  async function clearMarkers() {
    if (!global.bridge || !bridge.available()) { showStatus("Not running inside Premiere Pro.", true); return; }
    var info = loadedClipInfo || {};
    try {
      var tgt = target();
      var r = await bridge.clearMarkers(tgt, { nodeId: info.nodeId, seqStart: info.seqStart || 0 }, TAG);
      showStatus("Removed " + (r.removed || 0) + " EasyScript beat markers from the " +
        (tgt === "clip" ? "clip" : "sequence"), false, "DONE");
    } catch (err) {
      showStatus("Removing markers failed: " + err.message, true);
    }
  }

  // ── Review: toggle / add on the waveform, click track ──

  function toggle(marker) {
    if (marker.kind === "manual") {
      state.added = state.added.filter(function (t) { return keyOf(t) !== marker.key; });
    } else if (state.excluded[marker.key]) {
      delete state.excluded[marker.key];
    } else {
      state.excluded[marker.key] = true;
    }
    computeMarkers();
  }

  function addManual(t) {
    if (!state.raw.length) return;
    state.added.push(Math.max(0, t));
    computeMarkers();
  }

  var click = {
    ctx: null, lastT: -1, enabled: false,
    tick: function (t, playing) {
      if (!this.enabled || !playing || !state.markers.length) { this.lastT = t; return; }
      if (this.lastT < 0 || t < this.lastT || t - this.lastT > 0.4) { this.lastT = t; return; }  // seek
      var ms = state.markers, lo = 0, hi = ms.length;
      while (lo < hi) { var mid = (lo + hi) >> 1; if (ms[mid].t <= this.lastT) lo = mid + 1; else hi = mid; }
      for (var i = lo; i < ms.length && ms[i].t <= t; i++) if (!ms[i].excluded) this.beep(ms[i].kind);
      this.lastT = t;
    },
    beep: function (kind) {
      try {
        if (!this.ctx) this.ctx = new (global.AudioContext || global.webkitAudioContext)();
        var ctx = this.ctx, osc = ctx.createOscillator(), g = ctx.createGain(), now = ctx.currentTime;
        osc.frequency.value = kind === "down" ? 1600 : kind === "sub" ? 700 : 1050;
        g.gain.setValueAtTime(kind === "sub" ? 0.12 : 0.25, now);
        g.gain.exponentialRampToValueAtTime(0.0001, now + 0.05);
        osc.connect(g); g.connect(ctx.destination);
        osc.start(now); osc.stop(now + 0.06);
      } catch (e) {}
    },
  };

  function init() {
    var colorSel = $("beatColor"), downSel = $("beatDownColor");
    COLORS.forEach(function (name, i) {
      [colorSel, downSel].forEach(function (sel) {
        if (!sel) return;
        var o = document.createElement("option");
        o.value = String(i); o.textContent = name;
        sel.appendChild(o);
      });
    });
    if (colorSel) colorSel.value = "6";   // Blue
    if (downSel) downSel.value = "1";     // Red
    $("beatDetectBtn").addEventListener("click", detect);
    $("beatApplyBtn").addEventListener("click", apply);
    $("beatClearBtn").addEventListener("click", clearMarkers);
    $("beatResetBtn").addEventListener("click", function () { state.excluded = {}; state.added = []; computeMarkers(); });
    $("beatEvery").addEventListener("change", onOptionChange);
    $("beatMeter").addEventListener("change", onOptionChange);
    $("beatOffsetMinus").addEventListener("click", function () { state.offset = (state.offset + state.meter - 1) % state.meter; computeMarkers(); });
    $("beatOffsetPlus").addEventListener("click", function () { state.offset = (state.offset + 1) % state.meter; computeMarkers(); });
    $("beatHalfBtn").addEventListener("click", function () { state.tempo = state.tempo === 2 ? 1 : 0.5; state.excluded = {}; computeMarkers(); });
    $("beatDoubleBtn").addEventListener("click", function () { state.tempo = state.tempo === 0.5 ? 1 : 2; state.excluded = {}; computeMarkers(); });
    document.querySelectorAll('input[name="beatTarget"]').forEach(function (r) { r.addEventListener("change", render); });
    var clickCheck = $("beatClickCheck");
    if (clickCheck) clickCheck.addEventListener("change", function () { click.enabled = clickCheck.checked; });
    if (global.waveform) {
      waveform.onMarkerToggle = toggle;
      waveform.onMarkerAdd = addManual;
    }
    render();
  }

  global.beatUI = {
    init: init, reset: reset, render: render,
    onPlayback: function (t, playing) { click.tick(t, playing); },
    state: state,
  };
})(window);
