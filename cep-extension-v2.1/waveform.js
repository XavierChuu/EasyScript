/**
 * EasyScript — canvas waveform.
 *
 * Built for hour-long media: the backend sends min/max peaks at 5 ms (200 bins/s)
 * for the whole file once; coarser mip levels are derived here, so any zoom
 * level draws from ~1-2 bins per pixel. Zoomed past 5 ms per pixel, the visible
 * range is fetched exactly (per-pixel min/max, then raw samples) from
 * /waveform/slice. Only the visible window is ever drawn.
 *
 * Layers (base canvas): silence shading, cut regions, waveform (coloured by
 * silence / breath / speaker), frame grid when zoomed in, beat markers, ruler,
 * marker lane. The overlay canvas holds only the playhead + hover line, so
 * playback never repaints the waveform.
 *
 * Interaction: click = seek · drag = pan · Ctrl/Alt/⌘+wheel or pinch = zoom at
 * the cursor · Shift+wheel / horizontal swipe = pan · keys + − 0 ← → Home End.
 * Marker lane (strip under the ruler): click a marker to exclude/include it,
 * double-click to add one.
 */
(function (global) {
  "use strict";

  var RULER_H = 16;
  var LANE_H = 12;
  var MIN_VIEW = 0.02;          // seconds — deepest zoom
  var DETAIL_DEBOUNCE = 120;    // ms before fetching an exact slice

  function cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }

  function b64ToInt8(b64) {
    var bin = atob(b64 || "");
    var out = new Int8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = (bin.charCodeAt(i) << 24) >> 24;
    return out;
  }

  function buildLevels(min, max, binSec) {
    var levels = [{ binSec: binSec, min: min, max: max }];
    var mn = min, mx = max, bs = binSec;
    while (mn.length > 1024) {
      var n = Math.ceil(mn.length / 2);
      var nmn = new Int8Array(n), nmx = new Int8Array(n);
      for (var i = 0; i < n; i++) {
        var a = 2 * i, b = Math.min(a + 1, mn.length - 1);
        nmn[i] = mn[a] < mn[b] ? mn[a] : mn[b];
        nmx[i] = mx[a] > mx[b] ? mx[a] : mx[b];
      }
      mn = nmn; mx = nmx; bs *= 2;
      levels.push({ binSec: bs, min: mn, max: mx });
    }
    return levels;
  }

  function fmtClock(sec, decimals) {
    if (!isFinite(sec) || sec < 0) sec = 0;
    // Round once on the total, so 59.999 s reads 1:00.00 (not 0:60.00).
    var d = decimals || 0, unit = Math.pow(10, d);
    var total = Math.round(sec * unit);
    var whole = Math.floor(total / unit), frac = total % unit;
    var h = Math.floor(whole / 3600), m = Math.floor((whole % 3600) / 60), s = whole % 60;
    var ss = (s < 10 ? "0" : "") + s + (d ? "." + String(frac).padStart(d, "0") : "");
    return (h ? h + ":" + (m < 10 ? "0" : "") + m : m) + ":" + ss;
  }

  var TICK_STEPS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30,
                    60, 120, 300, 600, 900, 1800, 3600, 7200];

  var wf = {
    // Public state (index.js reads these)
    peaks: [],            // non-empty once data is loaded (level 0 of the mip pyramid)
    duration: 0,
    playheadPos: 0,
    cutMarkers: [],       // [{start, end}] analysis seconds
    beatMarkers: [],      // [{t, kind:"down"|"beat"|"sub"|"manual", label, excluded}]
    mode: "cut",          // overlay emphasis: "cut" | "text" | "beats"
    frameRate: 0,         // sequence fps (frame grid when zoomed in)

    // Hooks set by index.js / beats.js
    getSegments: function () { return []; },
    speakerColor: function () { return null; },
    fetchSlice: null,                  // async (start, end, bins, peak) → slice json
    onSeek: null,                      // (t) → void
    onMarkerToggle: null,              // (marker) → void
    onMarkerAdd: null,                 // (t) → void

    _levels: null, _peak: 1, _audioKey: "",
    _view: { start: 0, dur: 0 },
    _detail: null, _detailTimer: 0, _detailSeq: 0,
    _raf: 0, _overlayRaf: 0, _hoverX: -1, _hoverY: -1,
    _playing: false,

    init: function () {
      this.stage = document.getElementById("waveformWrap");
      this.canvas = document.getElementById("waveformCanvas");
      this.overlay = document.getElementById("waveformOverlay");
      this.ovWrap = document.getElementById("waveformOverview");
      this.ov = document.getElementById("overviewCanvas");
      this.tooltip = document.getElementById("waveformTooltip");
      this.zoomLabel = document.getElementById("zoomLevel");
      this.viewLabel = document.getElementById("waveformTime");
      this.ctx = this.canvas.getContext("2d");
      this.octx = this.overlay.getContext("2d");
      this.ovctx = this.ov.getContext("2d");
      this.readColors();
      this._bind();
      this.resize();
    },

    readColors: function () {
      this.colors = {
        bg: cssVar("--wave-bg", "#141414"),
        ruler: cssVar("--wave-ruler", "#1b1b1b"),
        grid: cssVar("--wave-grid", "rgba(255,255,255,0.06)"),
        text: cssVar("--text-3", "#8a8a8a"),
        speech: cssVar("--c-speech", "#46b483"),
        silence: cssVar("--c-silence", "#c9a24a"),
        breath: cssVar("--c-breath", "#c96a5c"),
        cut: cssVar("--c-cut", "rgba(0,0,0,0.5)"),
        cutEdge: cssVar("--c-cut-edge", "rgba(255,255,255,0.55)"),
        beat: cssVar("--c-beat", "#8f7cff"),
        down: cssVar("--c-downbeat", "#ffb347"),
        playhead: cssVar("--c-playhead", "#ff5c5c"),
        accent: cssVar("--accent", "#5b9cff"),
        frame: "rgba(255,255,255,0.10)",
        excluded: "rgba(160,160,160,0.55)",
      };
    },

    // ── Data ──

    /** /waveform response for `audioKey` (the analysed file path). */
    loadData: function (data, audioKey) {
      var mn = b64ToInt8(data.min), mx = b64ToInt8(data.max);
      this._peak = data.peak || 32767;
      this._binSec = 1 / (data.bins_per_sec || 200);
      this._sampleRate = data.sample_rate || 16000;
      this._levels = buildLevels(mn, mx, this._binSec);
      this.peaks = this._levels[0].min;
      this._audioKey = audioKey || "";
      this._detail = null;
      this.duration = data.duration || (mn.length * this._binSec);
      this.zoomFit();
    },

    /** Legacy 0..1 peak list (fallback when /waveform is unavailable). */
    loadPeaks: function (peaks, duration) {
      var n = (peaks && peaks.length) || 0;
      var mn = new Int8Array(n), mx = new Int8Array(n);
      for (var i = 0; i < n; i++) {
        var v = Math.max(0, Math.min(1, peaks[i] || 0)) * 127;
        mn[i] = -v; mx[i] = v;
      }
      this._peak = 0;
      this._binSec = n ? (duration || 0) / n : 1;
      this._sampleRate = 0;
      this._levels = n ? buildLevels(mn, mx, this._binSec) : null;
      this.peaks = n ? this._levels[0].min : [];
      this._audioKey = "";
      this._detail = null;
      this.duration = duration || 0;
      this.zoomFit();
    },

    hasDataFor: function (audioKey) {
      return !!this._levels && this._peak > 0 && this._audioKey === audioKey;
    },

    generateMockPeaks: function (duration, sampleCount) {
      var peaks = [], n = sampleCount || 500, segs = this.getSegments() || [];
      for (var i = 0; i < n; i++) {
        var t = (i / n) * duration, seg = null;
        for (var j = 0; j < segs.length; j++) { if (t >= segs[j].start && t < segs[j].end) { seg = segs[j]; break; } }
        var base = (!seg || seg.type === "silence") ? 0.03 : seg.type === "breath" ? 0.12 : 0.5;
        peaks.push(base);
      }
      return peaks;
    },

    clear: function () {
      this._levels = null; this.peaks = []; this.duration = 0; this.cutMarkers = [];
      this.beatMarkers = []; this.playheadPos = 0; this._detail = null; this._audioKey = "";
      this._view = { start: 0, dur: 0 };
      this.draw();
    },

    updateMarkers: function (cuts) { this.cutMarkers = cuts || []; this.draw(); },
    setBeatMarkers: function (markers) { this.beatMarkers = markers || []; this.draw(); },
    setMode: function (mode) { this.mode = mode || "cut"; this.draw(); },
    setFrameRate: function (fps) { this.frameRate = fps > 0 ? fps : 0; this.draw(); },

    // ── View ──

    viewStart: function () { return this._view.start; },
    viewDur: function () { return this._view.dur; },

    setView: function (start, dur) {
      var D = this.duration;
      if (!(D > 0)) { this._view = { start: 0, dur: 0 }; this.draw(); return; }
      var minDur = Math.min(D, Math.max(MIN_VIEW, (this.cssW || 400) * 0.5 / (this._sampleRate || 16000)));
      dur = Math.max(minDur, Math.min(D, dur));
      start = Math.max(0, Math.min(D - dur, start));
      this._view = { start: start, dur: dur };
      this._updateLabels();
      this._scheduleDetail();
      this.draw();
    },

    zoomAt: function (factor, anchorTime) {
      var v = this._view;
      if (!(v.dur > 0)) return;
      if (anchorTime === undefined || anchorTime === null) anchorTime = v.start + v.dur / 2;
      var ratio = (anchorTime - v.start) / v.dur;
      var nd = v.dur / factor;
      this.setView(anchorTime - ratio * nd, nd);
    },

    _zoomAnchor: function () {
      var v = this._view, p = this.playheadPos;
      return (p >= v.start && p <= v.start + v.dur) ? p : v.start + v.dur / 2;
    },
    zoomIn: function () { this.zoomAt(2, this._zoomAnchor()); },
    zoomOut: function () { this.zoomAt(0.5, this._zoomAnchor()); },
    zoomFit: function () { this.setView(0, this.duration); },

    _updateLabels: function () {
      var v = this._view, D = this.duration;
      if (this.zoomLabel) {
        var z = D > 0 && v.dur > 0 ? D / v.dur : 1;
        this.zoomLabel.textContent = z < 9.95 ? (Math.round(z * 10) / 10) + "×" :
          z < 1000 ? Math.round(z) + "×" : (Math.round(z / 100) / 10) + "k×";
      }
      if (this.viewLabel) {
        this.viewLabel.textContent = D > 0
          ? (v.dur < D - 1e-6 ? fmtClock(v.start, v.dur < 10 ? 2 : 0) + " – " + fmtClock(v.start + v.dur, v.dur < 10 ? 2 : 0) + " of " + fmtClock(D) : fmtClock(D))
          : "";
      }
    },

    setPlayhead: function (time, opts) {
      this.playheadPos = Math.max(0, time || 0);
      var v = this._view;
      // Page the view during playback, like a timeline does.
      if (opts && opts.follow && v.dur > 0 && v.dur < this.duration) {
        if (this.playheadPos > v.start + v.dur * 0.92 || this.playheadPos < v.start) {
          this.setView(this.playheadPos - v.dur * 0.08, v.dur);
        }
      }
      this._drawOverlaySoon();
    },

    // ── Rendering ──

    resize: function () {
      if (!this.stage) return;
      var dpr = global.devicePixelRatio || 1;
      var w = Math.max(50, this.stage.clientWidth), h = Math.max(40, this.stage.clientHeight);
      this.cssW = w; this.cssH = h; this.dpr = dpr;
      [this.canvas, this.overlay].forEach(function (c) {
        c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
        c.style.width = w + "px"; c.style.height = h + "px";
      });
      if (this.ovWrap && this.ov) {
        var ow = Math.max(50, this.ovWrap.clientWidth), oh = Math.max(10, this.ovWrap.clientHeight);
        this.ov.width = Math.round(ow * dpr); this.ov.height = Math.round(oh * dpr);
        this.ov.style.width = ow + "px"; this.ov.style.height = oh + "px";
        this.ovW = ow; this.ovH = oh;
      }
      this._scheduleDetail();
      this._drawNow();
    },

    draw: function () {
      var self = this;
      if (this._raf) return;
      this._raf = requestAnimationFrame(function () { self._raf = 0; self._drawNow(); });
    },

    _drawNow: function () {
      if (!this.ctx) return;
      this._drawBase();
      this._drawOverview();
      this._drawOverlay();
    },

    _drawOverlaySoon: function () {
      var self = this;
      if (this._overlayRaf) return;
      this._overlayRaf = requestAnimationFrame(function () {
        self._overlayRaf = 0;
        self._drawOverlay();
        self._drawOverview();
      });
    },

    _t2x: function (t) { return (t - this._view.start) / this._view.dur * this.cssW; },
    _x2t: function (x) { return this._view.start + (x / this.cssW) * this._view.dur; },

    /** min/max (int8) over [t0, t1) from the best source available. */
    _range: function (t0, t1, L) {
      var d = this._detail;
      if (d && d.mode === "minmax" && t0 >= d.start && t1 <= d.end + 1e-9) {
        var span = (d.end - d.start) / d.min.length;
        var a = Math.floor((t0 - d.start) / span), b = Math.max(a + 1, Math.ceil((t1 - d.start) / span));
        return this._agg(d.min, d.max, a, b);
      }
      var lv = this._levels[L];
      var i0 = Math.floor(t0 / lv.binSec), i1 = Math.max(i0 + 1, Math.ceil(t1 / lv.binSec));
      return this._agg(lv.min, lv.max, i0, i1);
    },

    _agg: function (mn, mx, a, b) {
      var n = mn.length;
      if (a < 0) a = 0;
      if (b > n) b = n;
      if (a >= b) return null;
      var lo = 127, hi = -127;
      for (var i = a; i < b; i++) {
        if (mn[i] < lo) lo = mn[i];
        if (mx[i] > hi) hi = mx[i];
      }
      return [lo, hi];
    },

    _drawBase: function () {
      var ctx = this.ctx, dpr = this.dpr || 1, W = this.cssW, H = this.cssH, c = this.colors;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = c.bg;
      ctx.fillRect(0, 0, W, H);
      var top = RULER_H + LANE_H;
      ctx.fillStyle = c.ruler;
      ctx.fillRect(0, 0, W, top);

      if (!this._levels || !(this.duration > 0) || !(this._view.dur > 0)) {
        ctx.fillStyle = c.text;
        ctx.font = "11px " + cssVar("--font", "sans-serif");
        ctx.textAlign = "center";
        ctx.fillText("Load audio from the timeline to see the waveform", W / 2, top + (H - top) / 2 + 4);
        ctx.textAlign = "left";
        return;
      }

      var v0 = this._view.start, vd = this._view.dur, v1 = v0 + vd;
      var waveTop = top, waveH = H - top, mid = waveTop + waveH / 2;
      var segs = this.getSegments() || [];
      var self = this;

      // 1) Silence / breath shading.
      var nonSpeech = [], speech = [];
      for (var s = 0; s < segs.length; s++) {
        var sg = segs[s];
        if (sg.end <= v0 || sg.start >= v1) continue;
        if (sg.type === "speech") speech.push(sg); else nonSpeech.push(sg);
      }
      nonSpeech.forEach(function (sg) {
        var x0 = self._t2x(sg.start), x1 = self._t2x(sg.end);
        ctx.fillStyle = sg.type === "breath" ? "rgba(201,106,92,0.10)" : "rgba(201,162,74,0.08)";
        ctx.fillRect(x0, waveTop, Math.max(1, x1 - x0), waveH);
      });

      // 2) Waveform columns (device pixels for crispness).
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      var Wd = Math.round(W * dpr), sppd = vd / Wd;
      var L = 0;
      while (L + 1 < this._levels.length && this._levels[L + 1].binSec <= sppd) L++;
      var d = this._detail;
      var samples = d && d.mode === "samples" && v0 >= d.start - 1e-9 && v1 <= d.end + 1e-9;
      var amp = (waveH * dpr / 2) * 0.96;
      var midD = mid * dpr;
      var cutsSorted = this.cutMarkers || [];
      var ci = 0, ni = 0, si = 0, last = null;
      nonSpeech.sort(function (a, b) { return a.start - b.start; });
      speech.sort(function (a, b) { return a.start - b.start; });
      var showSpeakers = this.mode !== "beats";
      if (!samples) {
        for (var x = 0; x < Wd; x++) {
          var t0 = v0 + x * sppd, t1 = t0 + sppd, tc = t0 + sppd / 2;
          var r = this._range(t0, t1, L);
          if (!r) continue;
          while (ni < nonSpeech.length && nonSpeech[ni].end <= tc) ni++;
          while (si < speech.length && speech[si].end <= tc) si++;
          while (ci < cutsSorted.length && cutsSorted[ci].end <= tc) ci++;
          var col = c.speech;
          if (ni < nonSpeech.length && nonSpeech[ni].start <= tc) {
            col = nonSpeech[ni].type === "breath" ? c.breath : c.silence;
          } else if (showSpeakers && si < speech.length && speech[si].start <= tc && speech[si].speaker) {
            col = this.speakerColor(speech[si].speaker) || c.speech;
          }
          var inCut = ci < cutsSorted.length && cutsSorted[ci].start <= tc;
          if (col !== last) { ctx.fillStyle = col; last = col; }
          ctx.globalAlpha = inCut ? 0.35 : 0.9;
          var y0 = this._shape(r[1]), y1 = this._shape(r[0]);
          var ya = midD - y0 * amp, yb = midD - y1 * amp;
          ctx.fillRect(x, ya, 1, Math.max(1, yb - ya));
        }
        ctx.globalAlpha = 1;
      } else {
        // Raw samples: a polyline through the actual sample values.
        var vals = d.values, sr = d.sample_rate, i0 = Math.max(0, Math.floor((v0 - d.start) * sr));
        var i1 = Math.min(vals.length, Math.ceil((v1 - d.start) * sr) + 1);
        ctx.strokeStyle = c.speech; ctx.lineWidth = Math.max(1, dpr);
        ctx.beginPath();
        for (var k = i0; k < i1; k++) {
          var tx = ((d.start + k / sr) - v0) / vd * Wd;
          var ty = midD - this._shape(vals[k]) * amp;
          if (k === i0) ctx.moveTo(tx, ty); else ctx.lineTo(tx, ty);
        }
        ctx.stroke();
        if ((i1 - i0) < Wd / 6) {  // dots once samples are far apart
          ctx.fillStyle = c.speech;
          for (var k2 = i0; k2 < i1; k2++) {
            var dx = ((d.start + k2 / sr) - v0) / vd * Wd, dy = midD - this._shape(vals[k2]) * amp;
            ctx.fillRect(dx - dpr, dy - dpr, 2 * dpr, 2 * dpr);
          }
        }
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      // Centre line.
      ctx.fillStyle = c.grid;
      ctx.fillRect(0, Math.round(mid), W, 1);

      // 3) Frame grid when individual frames are ≥ 6 px wide.
      if (this.frameRate > 0 && (W / vd) / this.frameRate >= 6) {
        var fd = 1 / this.frameRate;
        ctx.fillStyle = c.frame;
        for (var f = Math.ceil(v0 / fd); f * fd <= v1; f++) {
          ctx.fillRect(Math.round(this._t2x(f * fd)), waveTop, 1, waveH);
        }
      }

      // 4) Cut regions.
      var cutStrong = this.mode === "cut";
      for (var q = 0; q < cutsSorted.length; q++) {
        var cu = cutsSorted[q];
        if (cu.end <= v0 || cu.start >= v1) continue;
        var cx0 = this._t2x(cu.start), cx1 = this._t2x(cu.end);
        ctx.fillStyle = cutStrong ? c.cut : "rgba(0,0,0,0.28)";
        ctx.fillRect(cx0, waveTop, Math.max(1, cx1 - cx0), waveH);
        if (cx1 - cx0 > 3) {
          ctx.strokeStyle = cutStrong ? c.cutEdge : "rgba(255,255,255,0.25)";
          ctx.lineWidth = 1;
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(Math.round(cx0) + 0.5, waveTop); ctx.lineTo(Math.round(cx0) + 0.5, H);
          ctx.moveTo(Math.round(cx1) - 0.5, waveTop); ctx.lineTo(Math.round(cx1) - 0.5, H);
          ctx.stroke();
          ctx.setLineDash([]);
        }
        // Cut ranges also show in the lane, so they read even when tiny.
        ctx.fillStyle = cutStrong ? "rgba(255,92,92,0.55)" : "rgba(255,92,92,0.25)";
        ctx.fillRect(cx0, RULER_H + LANE_H - 3, Math.max(1, cx1 - cx0), 3);
      }

      // 5) Beat markers.
      this._drawBeats(ctx, W, H, waveTop);

      // 6) Ruler.
      this._drawRuler(ctx, W, v0, vd);
    },

    _shape: function (v) {
      // Mild compression so quiet passages (what silence detection is about) stay visible.
      var a = Math.abs(v) / 127;
      a = Math.pow(a, 0.75);
      return v < 0 ? -a : a;
    },

    _drawBeats: function (ctx, W, H, waveTop) {
      var ms = this.beatMarkers || [];
      if (!ms.length) return;
      var c = this.colors, v0 = this._view.start, v1 = v0 + this._view.dur;
      var lo = this._lowerBound(ms, v0), hi = this._lowerBound(ms, v1 + 1e-9);
      var count = hi - lo, strong = this.mode === "beats";
      var dense = count > W / 3;
      var laneY = RULER_H, laneMid = RULER_H + LANE_H / 2;
      ctx.font = "9px " + cssVar("--font", "sans-serif");
      ctx.textBaseline = "middle";
      var lastLabelX = -1e9;
      for (var i = lo; i < hi; i++) {
        var m = ms[i];
        if (dense && m.kind !== "down" && m.kind !== "manual") continue;
        var x = Math.round(this._t2x(m.t)) + 0.5;
        var col = m.excluded ? c.excluded : (m.kind === "down" ? c.down : m.kind === "manual" ? c.accent : c.beat);
        ctx.globalAlpha = strong ? (m.kind === "sub" ? 0.55 : 0.95) : 0.35;
        ctx.strokeStyle = col;
        ctx.lineWidth = m.kind === "down" ? 1.5 : 1;
        ctx.setLineDash(m.excluded || m.kind === "sub" ? [2, 3] : []);
        ctx.beginPath();
        var y0 = m.kind === "sub" ? waveTop + (H - waveTop) * 0.25 : waveTop;
        var y1 = m.kind === "sub" ? H - (H - waveTop) * 0.25 : H;
        ctx.moveTo(x, y0); ctx.lineTo(x, y1);
        ctx.stroke();
        ctx.setLineDash([]);
        // Lane handle.
        ctx.globalAlpha = strong ? 1 : 0.5;
        ctx.fillStyle = col;
        if (m.excluded) {
          ctx.fillRect(x - 3, laneMid - 0.5, 6, 1);
        } else if (m.kind === "down" || m.kind === "manual") {
          ctx.beginPath();
          ctx.moveTo(x - 3.5, laneY + 2); ctx.lineTo(x + 3.5, laneY + 2); ctx.lineTo(x, laneY + LANE_H - 2);
          ctx.closePath(); ctx.fill();
        } else {
          ctx.fillRect(x - 1, laneY + 3, 2, LANE_H - 6);
        }
        if (strong && m.kind === "down" && m.label && x - lastLabelX > 34 && !m.excluded) {
          ctx.fillStyle = c.down;
          ctx.fillText(m.label, x + 5, laneMid);
          lastLabelX = x;
        }
      }
      ctx.globalAlpha = 1;
      ctx.textBaseline = "alphabetic";
    },

    _lowerBound: function (ms, t) {
      var lo = 0, hi = ms.length;
      while (lo < hi) { var mid = (lo + hi) >> 1; if (ms[mid].t < t) lo = mid + 1; else hi = mid; }
      return lo;
    },

    _drawRuler: function (ctx, W, v0, vd) {
      var c = this.colors, pxPerSec = W / vd, major = TICK_STEPS[TICK_STEPS.length - 1];
      for (var i = 0; i < TICK_STEPS.length; i++) { if (TICK_STEPS[i] * pxPerSec >= 72) { major = TICK_STEPS[i]; break; } }
      var minor = major / (String(major).charAt(0) === "2" ? 4 : 5);
      if (minor * pxPerSec < 6) minor = major / 2;
      var decimals = major >= 1 ? 0 : major >= 0.1 ? 1 : major >= 0.01 ? 2 : 3;
      ctx.fillStyle = c.grid;
      for (var t = Math.ceil(v0 / minor) * minor; t <= v0 + vd; t += minor) {
        var x = Math.round((t - v0) * pxPerSec);
        var isMajor = Math.abs(t / major - Math.round(t / major)) < 1e-6;
        ctx.fillRect(x, isMajor ? 8 : 12, 1, isMajor ? RULER_H - 8 : RULER_H - 12);
      }
      ctx.fillStyle = c.text;
      ctx.font = "10px " + cssVar("--font", "sans-serif");
      ctx.textBaseline = "top";
      for (var tm = Math.ceil(v0 / major) * major; tm <= v0 + vd; tm += major) {
        var xm = Math.round((tm - v0) * pxPerSec);
        ctx.fillText(fmtClock(tm + 1e-9, decimals), xm + 3, 1);
      }
      ctx.textBaseline = "alphabetic";
      ctx.fillStyle = "rgba(255,255,255,0.05)";
      ctx.fillRect(0, RULER_H, W, 1);
      ctx.fillRect(0, RULER_H + LANE_H, W, 1);
    },

    _drawOverlay: function () {
      var ctx = this.octx, dpr = this.dpr || 1, W = this.cssW, H = this.cssH, c = this.colors;
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      if (!(this._view.dur > 0)) return;
      if (this._hoverX >= 0) {
        ctx.fillStyle = "rgba(255,255,255,0.35)";
        ctx.fillRect(Math.round(this._hoverX), RULER_H, 1, H - RULER_H);
      }
      var px = this._t2x(this.playheadPos);
      if (px >= -2 && px <= W + 2) {
        ctx.fillStyle = c.playhead;
        ctx.fillRect(Math.round(px) - 1, 0, 2, H);
        ctx.beginPath();
        ctx.moveTo(px - 5, 0); ctx.lineTo(px + 5, 0); ctx.lineTo(px, 6);
        ctx.closePath(); ctx.fill();
      }
    },

    _drawOverview: function () {
      var ctx = this.ovctx, dpr = this.dpr || 1, W = this.ovW, H = this.ovH, c = this.colors;
      if (!ctx || !W) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = c.ruler;
      ctx.fillRect(0, 0, W, H);
      if (!this._levels || !(this.duration > 0)) return;
      var D = this.duration, spp = D / W, L = 0;
      while (L + 1 < this._levels.length && this._levels[L + 1].binSec <= spp) L++;
      var lv = this._levels[L], mid = H / 2;
      ctx.fillStyle = "rgba(220,220,220,0.45)";
      for (var x = 0; x < W; x++) {
        var a = Math.floor(x * spp / lv.binSec), b = Math.max(a + 1, Math.ceil((x + 1) * spp / lv.binSec));
        var r = this._agg(lv.min, lv.max, a, b);
        if (!r) continue;
        var hh = Math.max(1, (this._shape(r[1]) - this._shape(r[0])) * (H / 2 - 1));
        ctx.fillRect(x, mid - hh / 2, 1, hh);
      }
      var cuts = this.cutMarkers || [];
      ctx.fillStyle = "rgba(255,92,92,0.45)";
      for (var i = 0; i < cuts.length; i++) {
        ctx.fillRect(cuts[i].start / D * W, H - 3, Math.max(1, (cuts[i].end - cuts[i].start) / D * W), 3);
      }
      var v = this._view;
      if (v.dur < D - 1e-6) {
        var vx = v.start / D * W, vw = Math.max(3, v.dur / D * W);
        ctx.fillStyle = "rgba(91,156,255,0.16)";
        ctx.fillRect(vx, 0, vw, H);
        ctx.strokeStyle = c.accent;
        ctx.lineWidth = 1;
        ctx.strokeRect(Math.round(vx) + 0.5, 0.5, Math.round(vw) - 1, H - 1);
      }
      ctx.fillStyle = c.playhead;
      ctx.fillRect(Math.round(this.playheadPos / D * W), 0, 1, H);
    },

    // ── Exact slices when zoomed past the overview resolution ──

    _scheduleDetail: function () {
      var self = this;
      clearTimeout(this._detailTimer);
      if (!this._levels || !this.fetchSlice || !(this._peak > 0) || !(this._view.dur > 0)) return;
      var Wd = Math.round((this.cssW || 400) * (this.dpr || 1));
      var sppd = this._view.dur / Wd;
      if (sppd >= this._binSec / 1.5) { this._detail = null; return; }
      var d = this._detail, v0 = this._view.start, v1 = v0 + this._view.dur;
      if (d && v0 >= d.start && v1 <= d.end && Math.abs(d.reqSpp - sppd) / sppd < 0.25) return;
      this._detailTimer = setTimeout(function () {
        var seq = ++self._detailSeq;
        var pad = self._view.dur * 0.5;  // fetch a bit beyond the view so small pans stay sharp
        var start = Math.max(0, v0 - pad), end = Math.min(self.duration, v1 + pad);
        var bins = Math.min(16384, Math.round(Wd * (end - start) / self._view.dur));
        self.fetchSlice(start, end, bins, self._peak).then(function (res) {
          if (seq !== self._detailSeq || !res || res.mode === "empty") return;
          var det = { start: res.start, end: res.end, mode: res.mode, reqSpp: sppd };
          if (res.mode === "samples") { det.values = b64ToInt8(res.values); det.sample_rate = res.sample_rate; }
          else { det.min = b64ToInt8(res.min); det.max = b64ToInt8(res.max); }
          self._detail = det;
          self.draw();
        }).catch(function () {});
      }, DETAIL_DEBOUNCE);
    },

    // ── Events ──

    _bind: function () {
      var self = this, stage = this.stage;
      try { new ResizeObserver(function () { self.resize(); }).observe(stage); } catch (e) {
        global.addEventListener("resize", function () { self.resize(); });
      }
      try { if (this.ovWrap) new ResizeObserver(function () { self.resize(); }).observe(this.ovWrap); } catch (e) {}

      var drag = null;
      stage.addEventListener("mousedown", function (e) {
        if (e.button !== 0 || !(self._view.dur > 0)) return;
        var rect = stage.getBoundingClientRect();
        var x = e.clientX - rect.left, y = e.clientY - rect.top;
        drag = { x0: e.clientX, x: x, y: y, start: self._view.start, moved: false,
                 lane: y < RULER_H + LANE_H && y >= RULER_H - 2 };
        stage.focus();
        e.preventDefault();
      });
      global.addEventListener("mousemove", function (e) {
        if (!drag) return;
        var dx = e.clientX - drag.x0;
        if (!drag.moved && Math.abs(dx) > 3) { drag.moved = true; stage.classList.add("panning"); }
        if (drag.moved) self.setView(drag.start - dx / self.cssW * self._view.dur, self._view.dur);
      });
      global.addEventListener("mouseup", function () {
        if (!drag) return;
        var d = drag; drag = null;
        stage.classList.remove("panning");
        if (d.moved) return;
        var t = self._x2t(d.x);
        if (d.lane && self.beatMarkers.length) {
          var m = self.markerNear(d.x);
          if (m && self.onMarkerToggle) { self.onMarkerToggle(m); return; }
        }
        if (self.onSeek) self.onSeek(Math.max(0, Math.min(self.duration, t)));
      });
      stage.addEventListener("dblclick", function (e) {
        var rect = stage.getBoundingClientRect(), y = e.clientY - rect.top;
        if (y < RULER_H + LANE_H && self.onMarkerAdd) self.onMarkerAdd(self._x2t(e.clientX - rect.left));
      });
      stage.addEventListener("mousemove", function (e) {
        var rect = stage.getBoundingClientRect();
        self._hoverX = e.clientX - rect.left; self._hoverY = e.clientY - rect.top;
        self._drawOverlaySoon();
        self._tooltip(self._hoverX, self._hoverY);
      });
      stage.addEventListener("mouseleave", function () {
        self._hoverX = -1;
        self._drawOverlaySoon();
        if (self.tooltip) self.tooltip.classList.add("hidden");
      });
      stage.addEventListener("wheel", function (e) {
        if (!(self._view.dur > 0)) return;
        var rect = stage.getBoundingClientRect();
        var t = self._x2t(e.clientX - rect.left);
        if (e.ctrlKey || e.metaKey || e.altKey) {
          e.preventDefault();
          self.zoomAt(Math.pow(1.0018, -e.deltaY * (e.deltaMode === 1 ? 33 : 1)), t);
        } else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
          e.preventDefault();
          var delta = (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) * (e.deltaMode === 1 ? 33 : 1);
          self.setView(self._view.start + delta / self.cssW * self._view.dur, self._view.dur);
        }
        // Plain vertical wheel: let the panel scroll.
      }, { passive: false });
      stage.addEventListener("keydown", function (e) {
        var v = self._view, handled = true;
        switch (e.key) {
          case "+": case "=": self.zoomIn(); break;
          case "-": case "_": self.zoomOut(); break;
          case "0": case "f": case "F": self.zoomFit(); break;
          case "ArrowLeft": self.setView(v.start - v.dur * 0.15, v.dur); break;
          case "ArrowRight": self.setView(v.start + v.dur * 0.15, v.dur); break;
          case "Home": self.setView(0, v.dur); break;
          case "End": self.setView(self.duration - v.dur, v.dur); break;
          default: handled = false;
        }
        if (handled) e.preventDefault();
      });

      if (this.ovWrap) {
        var ovDrag = false;
        var moveTo = function (e) {
          var rect = self.ovWrap.getBoundingClientRect();
          var t = (e.clientX - rect.left) / rect.width * self.duration;
          if (self._view.dur >= self.duration - 1e-6) {
            if (self.onSeek) self.onSeek(Math.max(0, Math.min(self.duration, t)));
            return;
          }
          self.setView(t - self._view.dur / 2, self._view.dur);
        };
        this.ovWrap.addEventListener("mousedown", function (e) { ovDrag = true; moveTo(e); e.preventDefault(); });
        global.addEventListener("mousemove", function (e) { if (ovDrag) moveTo(e); });
        global.addEventListener("mouseup", function () { ovDrag = false; });
      }

      var byId = function (id) { return document.getElementById(id); };
      if (byId("zoomInBtn")) byId("zoomInBtn").addEventListener("click", function () { self.zoomIn(); });
      if (byId("zoomOutBtn")) byId("zoomOutBtn").addEventListener("click", function () { self.zoomOut(); });
      if (byId("zoomFitBtn")) byId("zoomFitBtn").addEventListener("click", function () { self.zoomFit(); });
    },

    markerNear: function (x) {
      var ms = this.beatMarkers || [];
      if (!ms.length) return null;
      var t = this._x2t(x), i = this._lowerBound(ms, t), best = null, bestD = 7;
      for (var k = Math.max(0, i - 2); k < Math.min(ms.length, i + 2); k++) {
        var dx = Math.abs(this._t2x(ms[k].t) - x);
        if (dx < bestD) { bestD = dx; best = ms[k]; }
      }
      return best;
    },

    _tooltip: function (x, y) {
      var tip = this.tooltip;
      if (!tip || !(this._view.dur > 0)) return;
      var t = this._x2t(x), text = fmtClock(t, this._view.dur < 30 ? 3 : 2);
      if (y < RULER_H + LANE_H && y >= RULER_H - 2) {
        var m = this.markerNear(x);
        if (m) text = (m.label || "Marker") + " · " + fmtClock(m.t, 3) + (m.excluded ? " · excluded — click to include" : " · click to exclude");
        else if (this.beatMarkers.length) text += " · double-click to add a marker";
      }
      tip.textContent = text;
      tip.classList.remove("hidden");
      var w = tip.offsetWidth || 80;
      tip.style.left = Math.max(2, Math.min(this.cssW - w - 2, x + 10)) + "px";
    },
  };

  global.waveform = wf;
  global.fmtClock = fmtClock;
})(window);
