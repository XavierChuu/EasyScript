// EasyScript — Premiere Pro panel (CEP)
// Browser dev mode can point at another backend with ?port=… (Premiere never
// passes query params, so the panel always uses 9876 there).
const BACKEND_PORT = (() => {
  try { return parseInt(new URLSearchParams(window.location.search).get("port"), 10) || 9876; } catch { return 9876; }
})();
const BACKEND_URL = `http://localhost:${BACKEND_PORT}`;
const TICKS = 254016000000;   // Premiere ticks per second
let segments = [];
let backendConnected = false;
let currentAudioPath = "";
let hasTranscription = false; // Whether speech-to-text has been run
let audioDuration = 0; // Duration from autocut/transcribe

/** Escape text for HTML templates. Clip names, transcripts and translations
 *  are untrusted: with cep.process available, panel XSS = code execution. */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const isNativeControl = (el) => el && (el.tagName === "BUTTON" || el.tagName === "INPUT" || el.tagName === "SELECT");

// Helper: disabled state for buttons (native) and legacy div[role="button"]
function setDisabled(el, disabled) {
  if (!el) return;
  if (isNativeControl(el)) { el.disabled = !!disabled; return; }
  if (disabled) {
    el.classList.add("disabled");
    el.setAttribute("data-disabled", "true");
  } else {
    el.classList.remove("disabled");
    el.removeAttribute("data-disabled");
  }
}

// Proxy: make .disabled work on divs via getter/setter (native buttons already have it)
function patchDisabled(el) {
  if (!el || el._disabledPatched || isNativeControl(el)) return;
  el._disabledPatched = true;
  Object.defineProperty(el, "disabled", {
    get() { return el.classList.contains("disabled"); },
    set(v) { setDisabled(el, v); },
  });
}

// Helper: get element by ID and ensure .disabled works on divs
function getBtn(id) {
  const el = document.getElementById(id);
  if (el) patchDisabled(el);
  return el;
}

// Patch all role="button" elements on init
document.querySelectorAll('[role="button"]').forEach(patchDisabled);

// ── Backend start ──
// The panel launches the bundled backend itself (tryLaunchBackend, via
// cep.process) when /health doesn't answer.

let _autoStartAttempted = false;

async function autoStartServer() {
  if (_autoStartAttempted) return;
  _autoStartAttempted = true;
  try {
    const res = await fetch(`${BACKEND_URL}/health`);
    const data = await res.json();
    if (data.status === "ok") console.log("[EasyScript] Backend already running");
  } catch {
    tryLaunchBackend();
  }
}

// ── Backend communication ──
// Every request carries the backend's per-launch token (security.py). In
// Premiere it is read from ~/.easyscript/token-<port> through ExtendScript;
// in browser dev mode pass ?token=… in the panel URL.

let backendToken = "";

async function loadBackendToken(force = false) {
  if (backendToken && !force) return backendToken;
  let t = "";
  try { t = new URLSearchParams(window.location.search).get("token") || window.EASYSCRIPT_TOKEN || ""; } catch {}
  if (!t && window.bridge && bridge.available()) {
    try { t = (await bridge.readToken(BACKEND_PORT)) || ""; } catch {}
  }
  backendToken = t;
  return t;
}

/** URL with the token as a query param, for <audio src> (no custom headers there). */
function withToken(url) {
  return backendToken ? `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(backendToken)}` : url;
}

async function fetchBackend(endpoint, options = {}, retried = false) {
  const url = `${BACKEND_URL}${endpoint}`;
  await loadBackendToken();
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (backendToken) headers["X-EasyScript-Token"] = backendToken;
  let res;
  try {
    res = await fetch(url, { ...options, headers });
  } catch (e) {
    console.error(`[EasyScript] fetch failed: ${url}`, e);
    throw new Error(`Cannot connect to backend at ${BACKEND_URL}. Is the server running?`);
  }
  // A restarted backend has a new token — re-read it once and retry.
  if (res.status === 401 && !retried) {
    await loadBackendToken(true);
    return fetchBackend(endpoint, options, true);
  }
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) {
    throw new Error(data.error || data.detail || `Backend error: ${res.status}`);
  }
  return data;
}

let backendDevice = "";

let _lastLaunchAttempt = 0;

// Launch the bundled backend (installed at ~/.easyscript/backend) when it isn't
// already running, so the user doesn't have to start it manually. Throttled so
// a backend that died gets relaunched automatically (no need to reopen panel).
async function tryLaunchBackend() {
  const now = Date.now();
  if (now - _lastLaunchAttempt < 20000) return; // wait 20s between attempts
  _lastLaunchAttempt = now;
  try {
    if (!window.cep || !window.cep.process || !window.cep.process.createProcess) return;
    let home = "";
    try { home = await bridge.homeDir(); } catch (e) {}
    if (!home) return;
    const isWin = (navigator.platform || "").toLowerCase().indexOf("win") >= 0;
    const path = isWin
      ? home + "\\.easyscript\\backend\\EasyScript-backend.exe"
      : home + "/.easyscript/backend/EasyScript-backend";
    setConnected(false);
    const status = document.getElementById("statusText");
    if (status) status.textContent = "Starting…";
    console.log("[EasyScript] launching backend:", path);
    const res = window.cep.process.createProcess(path);
    console.log("[EasyScript] createProcess:", JSON.stringify(res));
  } catch (e) {
    console.warn("[EasyScript] launch backend failed:", e);
  }
}

async function checkBackend() {
  try {
    const data = await fetchBackend("/health");
    if (!backendConnected) console.log("[EasyScript] Backend connected:", JSON.stringify(data));
    setConnected(data.status === "ok");
    if (data.status === "ok") {
      backendDevice = data.device || "";
      updateModelInfo(data.model);
    }
  } catch (e) {
    setConnected(false);
    // Backend not reachable → try to start the bundled one once. The periodic
    // re-check (setInterval) will connect automatically once it's up.
    tryLaunchBackend();
  }
}

function updateModelInfo(loadedModel) {
  const info = document.getElementById("modelInfo");
  const selected = document.getElementById("modelSelect").value;
  if (!info) return;

  const deviceTag = backendDevice ? ` · ${esc(backendDevice)}` : "";
  const sel = esc(selected);

  // Check cached models from backend
  fetchBackend("/models").then(data => {
    const model = (data.models || []).find(m => m.id === selected);
    const isCached = model ? model.cached : false;
    const isLoaded = loadedModel === selected;
    const size = model ? esc(model.size) : "";

    if (isLoaded) {
      info.innerHTML = `<span class="model-tag tag-loaded">READY</span>${sel}${deviceTag}`;
    } else if (isCached) {
      info.innerHTML = `<span class="model-tag tag-loaded">CACHED</span>${sel} — loads on first run${deviceTag}`;
    } else {
      info.innerHTML = `<span class="model-tag tag-download">DOWNLOAD</span>${sel} (${size}) — downloads on first run${deviceTag}`;
    }
  }).catch(() => {
    info.innerHTML = `${sel}${deviceTag}`;
  });
}

document.getElementById("modelSelect")?.addEventListener("change", () => {
  updateModelInfo(null);
});

function setConnected(connected) {
  backendConnected = connected;
  const badge = document.getElementById("statusBadge");
  const text = document.getElementById("statusText");

  badge.className = connected ? "status-badge online" : "status-badge offline";
  text.textContent = connected ? "Connected" : "Offline";
  updateActionButtons();
}

// State for loaded timeline audio
let loadedClipInfo = null;   // { name, duration, mediaPath, sourceMode, trackIndex }
let lastAppliedFps = 25;     // fps detected from last Apply Cuts (for SRT frame-snapping)

// Segment display settings
let segLineBreakMode = "natural"; // "natural" | "word" | "punctuation" | "maxWords"
let segMaxWords = 5;              // max words per line (for "maxWords" mode)
let segTextView = false;          // true = text-only view (no badges, timestamps, etc.)

// Status line under the source card. Plain text only (built with DOM nodes,
// never innerHTML) — messages include clip names, paths and backend errors.
// `tag` adds a small pill such as "DONE".
function showStatus(msg, isError = false, tag = "") {
  console.log(`[EasyScript] ${isError ? "ERROR" : "INFO"}: ${msg}`);
  const info = document.getElementById("clipInfo");
  if (!info) return;
  info.classList.remove("hidden");
  info.classList.toggle("error", !!isError);
  info.textContent = "";
  if (tag) {
    const pill = document.createElement("span");
    pill.className = `model-tag ${isError ? "tag-error" : "tag-loaded"}`;
    pill.textContent = tag;
    info.appendChild(pill);
  }
  info.appendChild(document.createTextNode(String(msg)));
}

/**
 * List the active sequence's audio tracks in the Track dropdown (via
 * ExtendScript). Only rebuilt when the track layout changes, and the user's
 * choice is kept.
 */
let _trackSignature = "";
async function scanAudioTracks() {
  const trackSelect = document.getElementById("trackSelect");
  if (!trackSelect || !window.bridge || !bridge.available()) return;
  try {
    const info = await bridge.listAudioTracks();
    const tracks = info.tracks || [];
    const signature = info.sequenceID + "|" + tracks.map(t => `${t.name}:${t.clips}`).join(",");
    if (signature === _trackSignature) return;
    _trackSignature = signature;
    const previous = trackSelect.value;
    trackSelect.textContent = "";
    let firstWithClips = null;
    tracks.forEach((t) => {
      const opt = document.createElement("option");
      opt.value = String(t.index);
      opt.textContent = `A${t.index + 1} · ${t.name}` + (t.clips ? ` (${t.clips} clip${t.clips > 1 ? "s" : ""})` : " (empty)");
      if (!t.clips) opt.disabled = true;
      else if (firstWithClips === null) firstWithClips = String(t.index);
      trackSelect.appendChild(opt);
    });
    const all = document.createElement("option");
    all.value = "all";
    all.textContent = "All (mix)";
    trackSelect.appendChild(all);
    const keep = [...trackSelect.options].find(o => o.value === previous && !o.disabled);
    trackSelect.value = keep ? previous : (firstWithClips !== null ? firstWithClips : "all");
  } catch (e) {
    // No sequence open yet — keep whatever is listed.
  }
}

// Fetch waveform data for the loaded clip (multi-resolution, canvas) and draw.
async function loadWaveformPeaks(audioPath, fallbackDuration) {
  try {
    const data = await fetchBackend("/waveform", {
      method: "POST",
      body: JSON.stringify({ audio_path: audioPath }),
    });
    audioDuration = data.duration || fallbackDuration || 0;
    waveform.loadData(data, audioPath);
  } catch (e) {
    console.warn("[EasyScript] waveform load failed, trying legacy peaks:", e);
    try {
      const result = await fetchBackend("/peaks", {
        method: "POST",
        body: JSON.stringify({ audio_path: audioPath, num_peaks: 2000 }),
      });
      const dur = result.audio_duration || fallbackDuration || 0;
      audioDuration = dur;
      waveform.loadPeaks(result.peaks && result.peaks.length ? result.peaks : waveform.generateMockPeaks(dur, 800), dur);
    } catch (e2) {
      if (fallbackDuration) waveform.loadPeaks(waveform.generateMockPeaks(fallbackDuration, 800), fallbackDuration);
    }
  }
}

// ── Exact frame math ──
// Frame boundaries come from the sequence's real ticks-per-frame (29.97 is
// 8475667200 ticks, not 1/30 s). Cuts are snapped INWARD: the start rounds up
// to the next frame and the end rounds down, so a removed range can only ever
// be shorter than the padded silence, never reach into speech.

let seqTimebase = { tpf: TICKS / 25, fps: 25 };

async function refreshTimebase() {
  try {
    if (window.bridge && bridge.available()) {
      const tb = await bridge.sequenceTimebase();
      if (tb && tb.ticksPerFrame > 0) {
        seqTimebase = { tpf: tb.ticksPerFrame, fps: tb.fps, sequenceID: tb.sequenceID };
        waveform.setFrameRate(tb.fps);
      }
    }
  } catch (e) {}
  return seqTimebase;
}

function secToFrameCeil(sec, tb = seqTimebase) { return Math.ceil(sec * TICKS / tb.tpf - 1e-6); }
function secToFrameFloor(sec, tb = seqTimebase) { return Math.floor(sec * TICKS / tb.tpf + 1e-6); }
function secToFrameRound(sec, tb = seqTimebase) { return Math.round(sec * TICKS / tb.tpf); }
function frameToSec(f, tb = seqTimebase) { return f * tb.tpf / TICKS; }
function frameToTicks(f, tb = seqTimebase) { return Math.round(f * tb.tpf); }

/**
 * Cut points (analysis time) → frame-aligned sequence cuts.
 * Returns [{f0, f1, start, end}] where f0/f1 are sequence frames and
 * start/end the snapped range back in analysis time.
 */
function snapCutsToFrames(cuts, tb = seqTimebase) {
  const seqStart = (loadedClipInfo && loadedClipInfo.seqStart) || 0;
  const outP = (loadedClipInfo && loadedClipInfo.outPoint) || Infinity;
  const out = [];
  [...cuts].sort((a, b) => a.start - b.start).forEach((c) => {
    const cs = Math.max(c.start, 0), ce = Math.min(c.end, outP);
    if (ce <= cs) return;
    const f0 = secToFrameCeil(seqStart + cs, tb), f1 = secToFrameFloor(seqStart + ce, tb);
    if (f1 - f0 < 1) return;
    out.push({ f0, f1, start: frameToSec(f0, tb) - seqStart, end: frameToSec(f1, tb) - seqStart });
  });
  return out;
}

// Cuts actually applied to the active sequence (analysis time, frame-snapped),
// used for "after cut" subtitle timing, Tag Speaker and markers.
let lastAppliedCuts = null;
// True once cuts were removed from the active sequence (in place or via XML).
let cutsApplied = false;

/** Applied cuts if any, else what Apply would cut now. */
function effectiveCuts() {
  return lastAppliedCuts || snapCutsToFrames(getFilteredCutPoints());
}

function removedBeforeTime(t, cuts) {
  let r = 0;
  for (const c of cuts) {
    if (c.end <= t) r += c.end - c.start;
    else if (c.start < t) r += t - c.start;
  }
  return r;
}

/** Analysis time → time on the active sequence (accounts for applied cuts). */
function analysisToSeqTime(t) {
  const seqStart = (loadedClipInfo && loadedClipInfo.seqStart) || 0;
  return seqStart + t - (cutsApplied && lastAppliedCuts ? removedBeforeTime(t, lastAppliedCuts) : 0);
}

/** Throw if the user switched to a different sequence since loading the audio. */
async function ensureSameSequence() {
  if (!window.bridge || !bridge.available() || !loadedClipInfo || !loadedClipInfo.sequenceID) return;
  const tb = await bridge.sequenceTimebase();
  if (String(tb.sequenceID) !== String(loadedClipInfo.sequenceID)) {
    throw new Error(`The active sequence is "${tb.name}", not the one the audio was loaded from. Switch back or load the audio again.`);
  }
}

/** Move Premiere's playhead to an analysis time (best effort). */
function syncPremierePlayhead(t) {
  if (!window.bridge || !bridge.available() || !loadedClipInfo) return;
  const ticks = frameToTicks(secToFrameRound(analysisToSeqTime(t)));
  bridge.setPlayerPosition(ticks).catch(() => {});
}

// ── Progress Tracker ──

const progressTracker = {
  startTime: 0,
  polling: false,
  lastProgress: 0,
  endpoint: "/autocut/progress",
  _cancelled: false,
  _onCancel: null,

  show() {
    const container = document.getElementById("progressContainer");
    container.classList.remove("hidden");
    this.startTime = Date.now();
    this.lastProgress = 0;
    this._cancelled = false;
    this.update(0, "preparing", "Preparing...");
  },

  hide() {
    this.polling = false;
    this._onCancel = null;
    setTimeout(() => {
      document.getElementById("progressContainer").classList.add("hidden");
      document.getElementById("progressFill").classList.remove("indeterminate");
    }, 1200);
  },

  cancel() {
    this._cancelled = true;
    this.polling = false;
    this.update(0, "error", "Cancelled by user");
    if (this._onCancel) this._onCancel();
    if (this._rejectPolling) this._rejectPolling(new Error("__CANCELLED__"));
    this.hide();
  },

  update(progress, stage, detail) {
    const pct = Math.round(progress * 100);
    const fill = document.getElementById("progressFill");
    const percentEl = document.getElementById("progressPercent");
    const stageEl = document.getElementById("progressStage");
    const elapsedEl = document.getElementById("progressElapsed");
    const etaEl = document.getElementById("progressEta");

    fill.style.width = `${pct}%`;
    fill.classList.remove("indeterminate");
    percentEl.textContent = `${pct}%`;

    // Show indeterminate bar for downloading stage
    if (stage === "downloading") {
      fill.classList.add("indeterminate");
    }

    stageEl.textContent = detail || stage;
    stageEl.classList.toggle("error", stage === "error");

    const elapsed = (Date.now() - this.startTime) / 1000;
    elapsedEl.textContent = `⏱ ${this.formatDuration(elapsed)}`;

    if (progress > 0.05 && progress < 1) {
      const rate = progress / elapsed;
      const remaining = (1 - progress) / rate;
      etaEl.textContent = `~${this.formatDuration(remaining)} left`;
    } else if (progress >= 1) {
      etaEl.textContent = `Done in ${this.formatDuration(elapsed)}`;
    } else {
      etaEl.textContent = "Estimating...";
    }

    this.lastProgress = progress;
  },

  formatDuration(sec) {
    if (sec < 60) return `${Math.round(sec)}s`;
    const m = Math.floor(sec / 60);
    const s = Math.round(sec % 60);
    return `${m}m ${String(s).padStart(2, "0")}s`;
  },

  /** Poll progress endpoint until status is "done" or "error". Returns the final result. */
  pollUntilDone(endpoint, onPartial, onCancel) {
    this.endpoint = endpoint || "/autocut/progress";
    this.polling = true;
    this._onPartial = onPartial || null;
    this._onCancel = onCancel || null;
    this._cancelled = false;
    // Must see "processing" before accepting "done" — except for id-addressed
    // /jobs/<id>, which can't report a previous run (and may finish before the
    // first poll, e.g. cached stems).
    this._seenProcessing = this.endpoint.indexOf("/jobs/") === 0;
    return new Promise((resolve, reject) => {
      this._resolvePolling = resolve;
      this._rejectPolling = reject;
      this._poll();
    });
  },

  stopPolling() {
    this.polling = false;
  },

  async _poll() {
    if (!this.polling) return;
    try {
      const data = await fetchBackend(this.endpoint);
      if (data.progress !== undefined) {
        let detail = data.detail || "";
        if (data.audio_duration && data.stage === "silence") {
          const dm = Math.floor(data.audio_duration / 60);
          const ds = Math.round(data.audio_duration % 60);
          detail += ` (${dm}m ${String(ds).padStart(2, "0")}s audio)`;
        }
        this.update(data.progress, data.stage || "processing", detail);
      }

      // Stream partial results (e.g. transcribe chunks)
      if (data.partial_segments && this._onPartial) {
        this._onPartial(data.partial_segments, data.chunk, data.total_chunks);
      }

      // Track if we've seen a "processing" state — ignore stale "done" from previous runs
      if (data.status === "processing" || data.stage === "loading_model" || data.stage === "downloading") {
        this._seenProcessing = true;
      }

      if (data.status === "done" && this._seenProcessing) {
        this.polling = false;
        if (this._resolvePolling) this._resolvePolling(data.result || data);
        return;
      }

      if (data.status === "error" && this._seenProcessing) {
        this.polling = false;
        if (this._rejectPolling) this._rejectPolling(new Error(data.detail || "Processing failed"));
        return;
      }

      // Keep polling (covers "idle", stale "done", "processing", and any other state)
      if (this.polling) {
        setTimeout(() => this._poll(), 800);
      }
    } catch {
      // Network error — retry with backoff
      if (this.polling) setTimeout(() => this._poll(), 2000);
    }
  },
};

// ── Detect Silence (fast — ffmpeg silencedetect) ──

async function runAutoCut() {
  const audioPath = document.getElementById("audioPathInput").value.trim();
  if (!audioPath) return;

  const btn = getBtn("autoCutBtn");
  btn.disabled = true;
  btn.classList.add("processing");
  progressTracker.show();

  try {
    // 1. Fire POST to start background processing (returns immediately)
    const minSilenceMs = parseInt(document.getElementById("minSilence").value) || 500;
    const silenceThreshDb = parseInt(document.getElementById("silenceThresh").value) || -30;
    await fetchBackend("/autocut", {
      method: "POST",
      body: JSON.stringify({
        audio_path: audioPath,
        min_silence_ms: minSilenceMs,
        silence_thresh_db: silenceThreshDb,
      }),
    });

    // 2. Poll progress until done — result comes back from progress endpoint
    const result = await progressTracker.pollUntilDone("/autocut/progress");

    progressTracker.update(1, "done", `Done — ${(result.segments || []).length} segments`);

    // Merge silence segments into global segments (keep existing speech if transcribed)
    const newSilenceSegs = (result.segments || []).map(s => ({
      ...s, type: s.type || "silence", text: ""
    }));

    if (hasTranscription) {
      segments = segments.filter(s => s.type === "speech").concat(newSilenceSegs);
    } else {
      segments = newSilenceSegs;
    }
    segments.sort((a, b) => a.start - b.start);

    audioDuration = result.audio_duration || audioDuration || 0;
    // Keep the full-resolution waveform; the 800 peaks in this result are only
    // a fallback when nothing better is loaded yet.
    if (!waveform.hasDataFor(audioPath)) await loadWaveformPeaks(audioPath, audioDuration);

    renderSegments(segments);
    updateSegmentCount(segments);

    const cuts = getFilteredCutPoints();
    waveform.updateMarkers(cuts);
    updateCutStats();
    updateExportButtons();

    if (currentAudioPath !== audioPath) {
      currentAudioPath = audioPath;
      audioPlayback.loadAudio(audioPath);
    }
    showAudioInfo(result);

  } catch (err) {
    if (err.message !== "__CANCELLED__") {
      progressTracker.update(0, "error", `Error: ${err.message}`);
      showStatus(`Detect Silence failed: ${err.message}`, true);
    }
  } finally {
    btn.disabled = false;
    btn.classList.remove("processing");
    progressTracker.stopPolling();
    progressTracker.hide();
    updateActionButtons();
  }
}

// ── Transcribe (slow — speech to text, chunked with resume) ──

let _parallelDiarizePromise = null;

async function runTranscribe(resumeFromPlayhead = false) {
  const audioPath = document.getElementById("audioPathInput").value.trim();
  if (!audioPath) return;

  const btn = getBtn("transcribeBtn");
  btn.disabled = true;
  btn.classList.add("processing");
  progressTracker.show();

  _parallelDiarizePromise = null;

  try {
    const selectedModel = document.getElementById("modelSelect").value;
    const selectedLang = document.getElementById("languageSelect").value || null;
    const includeSpeakers = document.getElementById("includeSpeakersCheck")?.checked;

    // Determine start position: from playhead if resuming, else 0
    let startFrom = 0;
    if (resumeFromPlayhead && audioPlayback.audio) {
      startFrom = audioPlayback.audio.currentTime || 0;
    }

    // 1. Fire POST to start transcription (returns immediately)
    await fetchBackend("/transcribe", {
      method: "POST",
      body: JSON.stringify({
        audio_path: audioPath,
        model: selectedModel,
        language: selectedLang,
        start_from: startFrom,
        vocabulary: (document.getElementById("vocabularyInput")?.value || "").trim() || null,
      }),
    });

    // 2. If "Include speakers" is checked, start diarization in PARALLEL
    if (includeSpeakers) {
      _parallelDiarizePromise = startDiarizeBackend(audioPath)
        .then(() => pollDiarizeProgress());
    }

    // 3. Poll transcribe with partial result streaming
    const result = await progressTracker.pollUntilDone("/transcribe/progress",
      (partialSegs, chunkNum, totalChunks) => {
        // Render partial results as each chunk completes
        mergeTranscribeSegments(partialSegs, startFrom);
        renderSegments(segments);
        updateSegmentCount(segments);
        if (waveform.peaks.length > 0) waveform.draw();
      }
    );

    progressTracker.update(1, "done", `Done — ${(result.segments || []).length} speech segments`);

    // Final merge
    mergeTranscribeSegments(result.segments || [], startFrom);

    if (!audioDuration && result.audio_duration) {
      audioDuration = result.audio_duration;
    }

    renderSegments(segments);
    updateSegmentCount(segments);
    if (waveform.peaks.length > 0) waveform.draw();
    updateExportButtons();
    updateModelInfo(result.model);

    // If "Include speakers" was checked, diarize was started in parallel.
    // Wait for it to finish and merge results.
    if (_parallelDiarizePromise && !progressTracker._cancelled) {
      try {
        progressTracker.update(0.95, "diarizing", "Waiting for speaker identification...");
        const diarizeResult = await _parallelDiarizePromise;
        await applyDiarizeResult(diarizeResult);
        progressTracker.update(1, "done",
          `Done — ${(result.segments || []).length} segments, ${diarizeResult.num_speakers} speakers`);
      } catch (dErr) {
        if (dErr.message !== "__CANCELLED__") {
          console.warn("Parallel diarize failed:", dErr.message);
        }
      }
      _parallelDiarizePromise = null;
    }

  } catch (err) {
    if (err.message !== "__CANCELLED__") {
      progressTracker.update(0, "error", `Error: ${err.message}`);
      showStatus(`Transcribe failed: ${err.message}`, true);
    }
  } finally {
    // Cancel parallel diarize if still running
    if (_parallelDiarizePromise && progressTracker._cancelled) {
      if (pollDiarizeProgress._stop) pollDiarizeProgress._stop();
      _parallelDiarizePromise = null;
    }
    btn.disabled = false;
    btn.classList.remove("processing");
    progressTracker.stopPolling();
    progressTracker.hide();
    updateActionButtons();
  }
}

function mergeTranscribeSegments(newSpeechSegs, startFrom) {
  /**
   * Merge new speech segments into global segments array.
   * If resuming (startFrom > 0), keep existing speech before startFrom.
   */
  hasTranscription = true;

  const speechSegs = (newSpeechSegs || []).map(s => ({ ...s, type: "speech" }));
  const silenceSegs = segments.filter(s => s.type !== "speech");

  if (startFrom > 0) {
    // Keep existing speech segments before startFrom, add new ones after
    const existingSpeechBefore = segments.filter(
      s => s.type === "speech" && s.end <= startFrom + 0.5
    );
    segments = silenceSegs.concat(existingSpeechBefore, speechSegs);
  } else {
    segments = silenceSegs.concat(speechSegs);
  }

  segments.sort((a, b) => a.start - b.start);
}

// ── UI State Management ──

function showAudioInfo(result) {
  const info = document.getElementById("audioInfo");
  if (!result.audio_duration) { info.classList.add("hidden"); return; }
  const dur = result.audio_duration;
  const dm = Math.floor(dur / 60);
  const ds = Math.round(dur % 60);
  const silCount = (result.segments || []).filter(s => s.type === "silence").length;
  const breathCount = (result.segments || []).filter(s => s.type === "breath").length;
  info.textContent = `Duration: ${dm}m ${String(ds).padStart(2,"0")}s — ${silCount} silence, ${breathCount} breath segments`;
  info.classList.remove("hidden");
}

function updateExportButtons() {
  const hasCuts = getFilteredCutPoints().length > 0;
  const hasSpeech = hasTranscription && segments.some(s => s.type === "speech" && s.text);

  // Apply cuts / Cut to new sequence: need cuts from autocut
  ["applyCutBtn", "exportXmlBtn"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) { patchDisabled(el); el.disabled = !hasCuts; }
  });

  // Subtitle buttons: needs transcription
  const makeSubBtn = document.getElementById("makeSubtitleBtn");
  if (makeSubBtn) { patchDisabled(makeSubBtn); makeSubBtn.disabled = !hasSpeech; }
  const exportSrtBtn = document.getElementById("exportSrtBtn");
  if (exportSrtBtn) { patchDisabled(exportSrtBtn); exportSrtBtn.disabled = !hasSpeech; }

  // Show/hide transcribe hint (may be absent in the compact CEP layout)
  const hint = document.getElementById("subtitleHint") || document.getElementById("transcribeHint");
  if (hint) hint.classList.toggle("hidden", hasSpeech);
}

function updateCutStats() {
  const cuts = getFilteredCutPoints();
  const el = document.getElementById("cutStats");
  if (cuts.length === 0) {
    el.textContent = "";
    return;
  }
  const totalRemoved = cuts.reduce((sum, c) => sum + (c.end - c.start), 0);
  el.textContent = `${cuts.length} cuts, -${formatTime(totalRemoved)}`;
}

// ── Audio Playback ──

const audioPlayback = {
  audio: null,
  playing: false,
  animFrame: null,
  skipSilence: false,

  init() {
    this.audio = document.getElementById("audioPlayer");
    this.setupEvents();
  },

  setupEvents() {
    document.getElementById("playBtn").addEventListener("click", () => this.togglePlay());
    document.getElementById("stopBtn").addEventListener("click", () => this.stop());

    const skipCheck = document.getElementById("skipSilenceCheck");
    if (skipCheck) {
      skipCheck.addEventListener("change", () => { this.skipSilence = skipCheck.checked; });
    }

    this.audio.addEventListener("timeupdate", () => {
      if (this.playing && this.skipSilence && this.maybeSkipSilence(this.audio.currentTime)) {
        return;
      }
      if (!this.playing) waveform.setPlayhead(this.audio.currentTime);
      this.updateTimeUI();
      this.autoFocusSegment(this.audio.currentTime);
    });

    this.audio.addEventListener("ended", () => {
      this.playing = false;
      this.updatePlayIcon();
    });

    this.audio.addEventListener("loadedmetadata", () => {
      document.getElementById("totalTime").textContent = formatTime(this.audio.duration);
    });

    // Space toggles playback while the waveform has focus.
    document.getElementById("waveformWrap").addEventListener("keydown", (e) => {
      if (e.code === "Space") { e.preventDefault(); this.togglePlay(); }
    });
  },

  loadAudio(audioPath) {
    // Streamed through the backend (token in the query: <audio> can't send headers).
    loadBackendToken().then(() => {
      this.audio.src = withToken(`${BACKEND_URL}/audio?path=${encodeURIComponent(audioPath)}`);
      try { this.audio.load(); } catch {}
    });
    this.playing = false;
    this.updatePlayIcon();
    document.getElementById("currentTime").textContent = "0:00.00";
  },

  togglePlay() {
    if (!this.audio.src) return;
    if (this.playing) {
      this.audio.pause();
      this.playing = false;
    } else {
      this.audio.play();
      this.playing = true;
      this.animatePlayhead();
    }
    this.updatePlayIcon();
  },

  stop() {
    this.audio.pause();
    this.audio.currentTime = 0;
    this.playing = false;
    this.updatePlayIcon();
    waveform.setPlayhead(0);
    this.updateTimeUI();
  },

  seekTo(time) {
    this.audio.currentTime = time;
    waveform.setPlayhead(time);
    this.updateTimeUI();
    this.autoFocusSegment(time);
  },

  // When "Skip cuts" is on, jump over every cut region the playhead enters,
  // so review only plays what the edit will keep.
  maybeSkipSilence(time) {
    const cuts = waveform.cutMarkers || [];
    let lo = 0, hi = cuts.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (cuts[mid].end <= time) lo = mid + 1; else hi = mid; }
    const cut = cuts[lo];
    if (cut && time >= cut.start - 0.02 && time < cut.end - 0.05) {
      const target = Math.min(cut.end + 0.01, this.audio.duration || cut.end);
      if (target > this.audio.currentTime) {
        this.audio.currentTime = target;
        waveform.setPlayhead(target, { follow: true });
        this.updateTimeUI();
        this.autoFocusSegment(target);
      }
      return true;
    }
    return false;
  },

  animatePlayhead() {
    if (!this.playing) return;
    const t = this.audio.currentTime;
    waveform.setPlayhead(t, { follow: true });
    if (window.beatUI) beatUI.onPlayback(t, true);
    this.updateTimeUI();
    this.animFrame = requestAnimationFrame(() => this.animatePlayhead());
  },

  updatePlayIcon() {
    const icon = document.getElementById("playIcon");
    icon.innerHTML = this.playing ? "&#10074;&#10074;" : "&#9654;";
  },

  updateTimeUI() {
    document.getElementById("currentTime").textContent = formatTime(this.audio.currentTime);
  },

  // Highlight the segment under the playhead. The rendered rows are indexed
  // once per render (segmentIndex), so this is a binary search, not a DOM scan.
  _activeEls: [],
  autoFocusSegment(time) {
    const idx = segmentIndex;
    if (!idx.length) return;
    let lo = 0, hi = idx.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (idx[mid].start <= time) lo = mid + 1; else hi = mid; }
    const matches = [];
    for (let i = lo - 1; i >= 0 && i >= lo - 4; i--) {
      if (time >= idx[i].start && time < idx[i].end) matches.push(idx[i].el);
    }
    const cls = segTextView ? "text-view-active" : "segment-active";
    const same = matches.length === this._activeEls.length && matches.every((el, k) => el === this._activeEls[k]);
    if (same) return;
    this._activeEls.forEach(el => el.classList.remove("segment-active", "text-view-active"));
    matches.forEach(el => el.classList.add(cls));
    this._activeEls = matches;
    const first = matches[matches.length - 1];
    if (first) {
      const list = document.getElementById("segmentList");
      const lr = list.getBoundingClientRect(), r = first.getBoundingClientRect();
      if (r.top < lr.top || r.bottom > lr.bottom) first.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  },
};

// Rendered segment rows, sorted by start: [{start, end, el}] (see renderSegments).
let segmentIndex = [];

// ── Segment rendering ──

// m:ss.cc, or h:mm:ss.cc from one hour (long videos). Rounds once on the
// total so 0.999 s shows 0:01.00, not 0:00.100.
function formatTime(seconds) {
  const cs = Math.max(0, Math.round((seconds || 0) * 100));
  const h = Math.floor(cs / 360000), m = Math.floor(cs / 6000) % 60;
  const s = Math.floor(cs / 100) % 60, c = cs % 100;
  const tail = `${String(s).padStart(2, "0")}.${String(c).padStart(2, "0")}`;
  return h ? `${h}:${String(m).padStart(2, "0")}:${tail}` : `${m}:${tail}`;
}

function formatTimeMM(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function updateSegmentCount(segs) {
  const el = document.getElementById("segmentCount");
  if (el) el.textContent = `${segs.length} found`;
}

let currentFilter = "all";
let speakerMap = {};  // { "SPEAKER_00": "Speaker A", ... }
let hasSpeakers = false;
const PREF_IDS = ["modelSelect", "languageSelect", "vocabularyInput", "speakerSensitivitySelect",
  "speakerSpeedSelect", "includeSpeakersCheck", "matchVoicesCheck", "rememberVoicesCheck", "tagSpeakerMode"];
let speakerEmbeddings = {};   // speaker id → centroid embedding (voice library)
let diarizeExclusive = null;  // exclusive turns of the last diarization
let recognisedSpeakers = {};  // speaker id → {name, score} matched from saved voices

function getSpeakerColorIndex(speakerId) {
  const speakers = Object.keys(speakerMap);
  const idx = speakers.indexOf(speakerId);
  return idx >= 0 ? idx % 6 : 0;
}

// Solid speaker colors for the waveform bars — matches the speaker-tag palette.
const SPEAKER_WAVE_COLORS = [
  "#5b9cff", "#ff9f43", "#a78bfa", "#34d399", "#fbbf24", "#f472b6",
];

function emptyNote(list, text) {
  const d = document.createElement("div");
  d.className = "segment-empty";
  d.textContent = text;
  list.appendChild(d);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function renderSegments(segs) {
  const list = document.getElementById("segmentList");
  list.textContent = "";
  list.classList.toggle("text-view", segTextView);
  segmentIndex = [];
  audioPlayback._activeEls = [];

  if (currentFilter === "translation") {
    renderTranslationSegments();
    return;
  }

  if (segs.length === 0) {
    emptyNote(list, segments.length ? "No segments match the filter" : "Load audio from the timeline and run an analysis");
    return;
  }

  // ── Text-only view: flowing paragraph ──
  if (segTextView) {
    const speechSegs = segs.filter(s => s.type === "speech" && s.text);
    if (speechSegs.length === 0) {
      emptyNote(list, "No speech segments");
      return;
    }
    const block = el("div", "text-view-block");
    speechSegs.forEach((seg) => {
      const span = el("span", "text-view-span", seg.text.trim());
      span.title = `${formatTime(seg.start)} – ${formatTime(seg.end)}`;
      span.addEventListener("click", () => seekToSegment(seg));
      block.appendChild(span);
      block.appendChild(document.createTextNode(" "));
      segmentIndex.push({ start: seg.start, end: seg.end, el: span });
    });
    list.appendChild(block);
    return;
  }

  // ── Normal view ──
  // Apply display splitting based on mode. Split copies keep a reference to
  // their source segment (_orig) so edits always land on the right one — the
  // index into a filtered list is NOT an index into `segments`.
  const needsSplit = segLineBreakMode !== "natural";
  let displaySegs = segs;
  if (needsSplit) {
    displaySegs = [];
    segs.forEach((seg, i) => {
      const tagged = { ...seg, _origIndex: i, _orig: seg };
      displaySegs.push(...splitSegmentForDisplay(tagged));
    });
  }

  const frag = document.createDocumentFragment();
  displaySegs.forEach((seg) => {
    const source = seg._orig || seg;
    const item = el("div", "segment-item");
    const row = el("div", "segment-row");
    row.appendChild(el("span", `segment-badge ${seg.type === "speech" || seg.type === "silence" || seg.type === "breath" ? seg.type : ""}`, seg.type));

    // Speaker tag if available
    if (seg.speaker && speakerMap[seg.speaker]) {
      const tag = el("span", "speaker-tag", speakerMap[seg.speaker] || seg.speaker);
      tag.dataset.speaker = seg.speaker;
      tag.dataset.color = String(getSpeakerColorIndex(seg.speaker));
      if (recognisedSpeakers[seg.speaker]) tag.classList.add("recognised");
      tag.title = recognisedSpeakers[seg.speaker]
        ? `Recognised from saved voices (${Math.round(recognisedSpeakers[seg.speaker].score * 100)}%) · click for options`
        : "Click to rename or move this line to another speaker";
      tag.addEventListener("click", (e) => { e.stopPropagation(); openSpeakerMenu(tag, source); });
      row.appendChild(tag);
    }
    row.appendChild(el("span", "segment-time", `${formatTime(seg.start)} – ${formatTime(seg.end)}`));
    row.addEventListener("click", () => seekToSegment(seg));
    item.appendChild(row);

    // Text — editable for speech segments (unless display-split)
    if (seg.type === "speech" || seg.text) {
      const textEl = el("div", "segment-text", seg.text || "");
      if (seg.type === "speech" && !seg._displaySplit) {
        textEl.contentEditable = "true";
        textEl._seg = source;
        textEl.addEventListener("blur", () => { source.text = textEl.textContent.trim(); });
        textEl.addEventListener("click", (e) => e.stopPropagation());
      }
      item.appendChild(textEl);
    }

    frag.appendChild(item);
    segmentIndex.push({ start: seg.start, end: seg.end, el: item });
  });
  list.appendChild(frag);
  segmentIndex.sort((a, b) => a.start - b.start);
}

function startSpeakerRename(tagEl) {
  const speakerId = tagEl.dataset.speaker;
  const currentLabel = speakerMap[speakerId] || speakerId;

  // Replace tag with input
  const input = document.createElement("input");
  input.className = "speaker-tag-input";
  input.type = "text";
  input.value = currentLabel;
  tagEl.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finishRename = () => {
    if (done) return;
    done = true;
    const newLabel = input.value.trim() || currentLabel;
    if (newLabel !== currentLabel) renameSpeaker(speakerId, newLabel);
    rerenderSegments();
  };

  input.addEventListener("blur", finishRename);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") { input.value = currentLabel; input.blur(); }
  });
}

function rerenderSegments() {
  const filtered = currentFilter === "all" ? segments : segments.filter(s => s.type === currentFilter);
  renderSegments(filtered);
  if (waveform.peaks.length > 0) waveform.draw();
}

/** Rename a speaker. A name another speaker already has merges the two. */
function renameSpeaker(speakerId, newLabel) {
  const target = Object.keys(speakerMap).find(
    (k) => k !== speakerId && (speakerMap[k] || "").toLowerCase() === newLabel.toLowerCase());
  if (target) {
    segments.forEach((s) => {
      if (s.speaker === speakerId) { s.speaker = target; s.speakerLabel = speakerMap[target]; }
    });
    delete speakerMap[speakerId];
    showStatus(`Merged into ${speakerMap[target]}`, false, "SPEAKERS");
  } else {
    speakerMap[speakerId] = newLabel;
    segments.forEach((s) => { if (s.speaker === speakerId) s.speakerLabel = newLabel; });
  }
  rememberVoice(speakerId, target ? speakerMap[target] : newLabel);
}

/** Save a named speaker's voice so later videos recognise them. */
function rememberVoice(speakerId, name) {
  if (!document.getElementById("rememberVoicesCheck")?.checked) return;
  const emb = speakerEmbeddings[speakerId];
  if (!emb || !name || /^Speaker [A-Z0-9]+$/i.test(name)) return;
  fetchBackend("/voices", { method: "POST", body: JSON.stringify({ name, embedding: emb }) })
    .then(() => { showStatus(`Voice "${name}" saved — it will be recognised in other videos`, false, "VOICES"); refreshVoiceList(); })
    .catch((e) => console.warn("[EasyScript] save voice failed:", e));
}

function closeSpeakerMenu() {
  document.querySelectorAll(".speaker-menu").forEach((m) => m.remove());
}

/** Speaker tag menu: rename the speaker, or move this line to another one. */
function openSpeakerMenu(tagEl, seg) {
  closeSpeakerMenu();
  const spk = tagEl.dataset.speaker;
  const menu = el("div", "speaker-menu");
  const item = (label, fn, color) => {
    const b = el("button", null, label);
    if (color) { const sw = el("span", "swatch"); sw.style.background = color; b.prepend(sw); }
    b.addEventListener("click", (e) => { e.stopPropagation(); closeSpeakerMenu(); fn(); });
    menu.appendChild(b);
  };
  menu.appendChild(el("div", "speaker-menu-title", speakerMap[spk] || spk));
  item("Rename speaker…", () => startSpeakerRename(tagEl));
  menu.appendChild(el("div", "sep"));
  menu.appendChild(el("div", "speaker-menu-title", "Move this line to"));
  Object.keys(speakerMap).filter((k) => k !== spk).forEach((k) => {
    item(speakerMap[k], () => reassignSegmentSpeaker(seg, k), SPEAKER_WAVE_COLORS[getSpeakerColorIndex(k)]);
  });
  item("+ New speaker", () => reassignSegmentSpeaker(seg, null));
  menu.addEventListener("click", (e) => e.stopPropagation());
  document.body.appendChild(menu);
  const r = tagEl.getBoundingClientRect();
  const w = menu.offsetWidth, h = menu.offsetHeight;
  menu.style.left = `${Math.max(4, Math.min(r.left, window.innerWidth - w - 4))}px`;
  menu.style.top = `${r.bottom + h + 4 > window.innerHeight ? Math.max(4, r.top - h - 4) : r.bottom + 4}px`;
  setTimeout(() => document.addEventListener("click", closeSpeakerMenu, { once: true }), 0);
}

/** Move one line to another speaker (null = a new speaker). */
function reassignSegmentSpeaker(seg, speakerId) {
  if (!speakerId) {
    let n = 1;
    while (speakerMap[`MANUAL_${n}`]) n++;
    speakerId = `MANUAL_${n}`;
    const used = new Set(Object.values(speakerMap));
    let i = 0;
    while (used.has(i < 26 ? `Speaker ${String.fromCharCode(65 + i)}` : `Speaker ${i + 1}`)) i++;
    speakerMap[speakerId] = i < 26 ? `Speaker ${String.fromCharCode(65 + i)}` : `Speaker ${i + 1}`;
  }
  seg.speaker = speakerId;
  seg.speakerLabel = speakerMap[speakerId];
  hasSpeakers = true;
  rerenderSegments();
}

async function refreshVoiceList() {
  const list = document.getElementById("voiceList");
  const count = document.getElementById("voiceCount");
  if (!list || !backendConnected) return;
  try {
    const data = await fetchBackend("/voices");
    const voices = data.voices || [];
    list.textContent = "";
    if (count) count.textContent = String(voices.length);
    voices.forEach((v) => {
      const row = el("div", "voice-item");
      row.appendChild(el("span", "voice-name", v.name));
      row.appendChild(el("span", "voice-meta", `${v.samples}×`));
      const del = el("button", null, "✕");
      del.title = `Forget ${v.name}`;
      del.addEventListener("click", async () => {
        await fetchBackend("/voices/delete", { method: "POST", body: JSON.stringify({ name: v.name }) });
        refreshVoiceList();
      });
      row.appendChild(del);
      list.appendChild(row);
    });
  } catch (e) {
    console.warn("[EasyScript] voices:", e);
  }
}

function renderTranslationSegments() {
  const list = document.getElementById("segmentList");
  list.innerHTML = "";
  list.classList.toggle("text-view", segTextView);

  const speechSegs = segments.filter(s => s.type === "speech" && s.text);
  if (speechSegs.length === 0) {
    list.innerHTML = '<div class="segment-empty">Run Transcribe first to enable translation</div>';
    return;
  }

  if (transLangs.length === 0 || !activeTransLang) {
    list.innerHTML = '<div class="segment-empty">Click + to add a target language</div>';
    return;
  }

  // Ensure translationData for active language matches speech segments
  if (!translationData[activeTransLang] ||
      translationData[activeTransLang].length !== speechSegs.length) {
    translationData[activeTransLang] = speechSegs.map((s, i) => ({
      text: (translationData[activeTransLang] && translationData[activeTransLang][i])
        ? translationData[activeTransLang][i].text : "",
    }));
  }

  const langData = translationData[activeTransLang];

  // ── Text-only view: original italic on top, translation below ──
  if (segTextView) {
    const block = document.createElement("div");
    block.className = "text-view-block text-view-trans";
    // Original text — italic, smaller
    const origP = document.createElement("p");
    origP.className = "text-view-original";
    speechSegs.forEach((seg) => {
      const span = document.createElement("span");
      span.className = "text-view-span";
      span.dataset.start = seg.start;
      span.dataset.end = seg.end;
      span.textContent = seg.text.trim();
      span.title = `${formatTime(seg.start)} – ${formatTime(seg.end)}`;
      span.addEventListener("click", () => seekToSegment(seg));
      origP.appendChild(span);
      origP.appendChild(document.createTextNode(" "));
    });
    block.appendChild(origP);
    // Translation text
    const transP = document.createElement("p");
    transP.className = "text-view-translation";
    speechSegs.forEach((seg, i) => {
      const transText = langData[i] ? langData[i].text : "";
      if (transText) {
        const span = document.createElement("span");
        span.className = "text-view-span";
        span.dataset.start = seg.start;
        span.dataset.end = seg.end;
        span.textContent = transText.trim();
        span.title = `${formatTime(seg.start)} – ${formatTime(seg.end)}`;
        span.addEventListener("click", () => seekToSegment(seg));
        transP.appendChild(span);
        transP.appendChild(document.createTextNode(" "));
      }
    });
    block.appendChild(transP);
    list.appendChild(block);
    return;
  }

  // ── Normal view ──
  const lang = activeTransLang;
  const frag = document.createDocumentFragment();
  speechSegs.forEach((seg, i) => {
    const item = el("div", "segment-item");
    const row = el("div", "segment-row");
    row.appendChild(el("span", "segment-badge speech", "speech"));
    row.appendChild(el("span", "segment-time", `${formatTime(seg.start)} – ${formatTime(seg.end)}`));
    row.addEventListener("click", () => seekToSegment(seg));
    item.appendChild(row);
    item.appendChild(el("div", "segment-text trans-original", seg.text));

    const transRow = el("div", "trans-text-row");
    const transEl = el("div", "segment-text translation-text", langData[i] ? langData[i].text : "");
    transEl.contentEditable = "true";
    transEl.dataset.transIndex = String(i);
    transEl.dataset.transLang = lang;
    transEl.setAttribute("placeholder", `Translation (${lang.toUpperCase()})…`);
    transEl.addEventListener("blur", () => {
      if (translationData[lang] && translationData[lang][i]) {
        translationData[lang][i].text = transEl.textContent.trim();
      }
    });
    transEl.addEventListener("click", (e) => e.stopPropagation());

    // Per-row translate icon
    const retranslateBtn = el("button", "trans-retranslate", "↻");
    retranslateBtn.title = "Re-translate this segment";
    retranslateBtn.dataset.idx = String(i);
    retranslateBtn.dataset.lang = lang;
    retranslateBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      translateSingleSegment(i, lang);
    });
    transRow.appendChild(transEl);
    transRow.appendChild(retranslateBtn);
    item.appendChild(transRow);

    frag.appendChild(item);
    segmentIndex.push({ start: seg.start, end: seg.end, el: item });
  });
  list.appendChild(frag);
}

async function seekToSegment(seg) {
  audioPlayback.seekTo(seg.start);
  syncPremierePlayhead(seg.start);
}

// ── Filter tabs ──

document.querySelectorAll(".filter-bar [data-filter]").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".filter-bar [data-filter]").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    currentFilter = tab.dataset.filter;

    const transTabs = document.getElementById("transTabs");

    // Show/hide translation tabs bar
    if (currentFilter === "translation") {
      transTabs.classList.remove("hidden");
      renderSegments(segments);
    } else {
      transTabs.classList.add("hidden");
      const filtered = currentFilter === "all" ? segments : segments.filter((s) => s.type === currentFilter);
      renderSegments(filtered);
    }
  });
});

// ── Translation Multi-Language Tab System ──

const TRANSLATION_LANGUAGES = [
  // ── Common ──
  { code: "vi", name: "Vietnamese" },
  { code: "en", name: "English" },
  { code: "zh", name: "Chinese" },
  { code: "ja", name: "Japanese" },
  { code: "ko", name: "Korean" },
  // ── Southeast Asia ──
  { code: "th", name: "Thai" },
  { code: "id", name: "Indonesian" },
  { code: "ms", name: "Malay" },
  { code: "tl", name: "Filipino" },
  { code: "my", name: "Burmese" },
  { code: "km", name: "Khmer" },
  { code: "lo", name: "Lao" },
  // ── South Asia ──
  { code: "hi", name: "Hindi" },
  { code: "bn", name: "Bengali" },
  { code: "ta", name: "Tamil" },
  { code: "te", name: "Telugu" },
  { code: "ur", name: "Urdu" },
  { code: "ne", name: "Nepali" },
  { code: "si", name: "Sinhala" },
  // ── Western Europe ──
  { code: "fr", name: "French" },
  { code: "de", name: "German" },
  { code: "es", name: "Spanish" },
  { code: "pt", name: "Portuguese" },
  { code: "it", name: "Italian" },
  { code: "nl", name: "Dutch" },
  { code: "ca", name: "Catalan" },
  { code: "gl", name: "Galician" },
  { code: "eu", name: "Basque" },
  // ── Northern Europe ──
  { code: "sv", name: "Swedish" },
  { code: "da", name: "Danish" },
  { code: "fi", name: "Finnish" },
  { code: "no", name: "Norwegian" },
  // ── Eastern Europe ──
  { code: "ru", name: "Russian" },
  { code: "pl", name: "Polish" },
  { code: "uk", name: "Ukrainian" },
  { code: "cs", name: "Czech" },
  { code: "hu", name: "Hungarian" },
  { code: "ro", name: "Romanian" },
  { code: "bg", name: "Bulgarian" },
  { code: "hr", name: "Croatian" },
  { code: "sk", name: "Slovak" },
  { code: "sl", name: "Slovenian" },
  { code: "lt", name: "Lithuanian" },
  { code: "lv", name: "Latvian" },
  { code: "et", name: "Estonian" },
  // ── Southern Europe & Middle East ──
  { code: "el", name: "Greek" },
  { code: "tr", name: "Turkish" },
  { code: "ar", name: "Arabic" },
  { code: "he", name: "Hebrew" },
  { code: "fa", name: "Persian" },
  // ── Central Asia & Caucasus ──
  { code: "ka", name: "Georgian" },
  { code: "az", name: "Azerbaijani" },
  { code: "uz", name: "Uzbek" },
  { code: "kk", name: "Kazakh" },
  { code: "mn", name: "Mongolian" },
  // ── Africa ──
  { code: "af", name: "Afrikaans" },
  { code: "sw", name: "Swahili" },
];

/**
 * translationData: { [langCode]: [ { text: "" }, ... ] }
 * Each array corresponds 1:1 with speech segments.
 */
let translationData = {};
let activeTransLang = ""; // Currently focused translation tab
let transLangs = []; // Ordered list of added language codes

function getSourceLang() {
  const speechSegs = segments.filter(s => s.type === "speech" && s.language);
  return speechSegs.length > 0 ? speechSegs[0].language : "";
}

function renderTransTabs() {
  const tabList = document.getElementById("transTabList");
  const btn = getBtn("translateBtn");
  tabList.innerHTML = "";

  transLangs.forEach(code => {
    const tab = document.createElement("button");
    tab.className = "trans-tab" + (code === activeTransLang ? " active" : "");
    tab.innerHTML = `${code}<span class="trans-tab-close">&times;</span>`;

    tab.addEventListener("click", (e) => {
      if (e.target.classList.contains("trans-tab-close")) {
        // Remove this language tab
        transLangs = transLangs.filter(c => c !== code);
        delete translationData[code];
        if (activeTransLang === code) {
          activeTransLang = transLangs[0] || "";
        }
        renderTransTabs();
        renderSegments(segments);
        return;
      }
      // Switch to this tab
      activeTransLang = code;
      renderTransTabs();
      renderSegments(segments);
    });

    tabList.appendChild(tab);
  });

  btn.disabled = transLangs.length === 0;
}

// "+" button to add language
document.getElementById("transAddBtn").addEventListener("click", () => {
  const picker = document.getElementById("langPicker");
  if (picker.classList.contains("hidden")) {
    showLangPicker();
  } else {
    picker.classList.add("hidden");
  }
});

function showLangPicker() {
  const picker = document.getElementById("langPicker");
  const list = document.getElementById("langPickerList");
  const sourceLang = getSourceLang();
  list.innerHTML = "";

  TRANSLATION_LANGUAGES.forEach(lang => {
    const item = document.createElement("button");
    const isSource = lang.code === sourceLang;
    const isAdded = transLangs.includes(lang.code);
    item.className = "lang-picker-item" + ((isSource || isAdded) ? " disabled" : "");
    item.innerHTML = `<span class="lang-code">${lang.code}</span> ${lang.name}${isSource ? " (source)" : ""}${isAdded ? " (added)" : ""}`;

    if (!isSource && !isAdded) {
      item.addEventListener("click", () => {
        transLangs.push(lang.code);
        activeTransLang = lang.code;
        // Initialize empty translation data
        if (!translationData[lang.code]) {
          const speechSegs = segments.filter(s => s.type === "speech" && s.text);
          translationData[lang.code] = speechSegs.map(() => ({ text: "" }));
        }
        picker.classList.add("hidden");
        renderTransTabs();
        renderSegments(segments);
      });
    }

    list.appendChild(item);
  });

  picker.classList.remove("hidden");
}

// Close lang picker when clicking elsewhere
document.addEventListener("click", (e) => {
  const picker = document.getElementById("langPicker");
  const addBtn = document.getElementById("transAddBtn");
  if (!picker.classList.contains("hidden") &&
      !picker.contains(e.target) && e.target !== addBtn) {
    picker.classList.add("hidden");
  }
});

// Translate button — show picker dialog
document.getElementById("translateBtn").addEventListener("click", () => {
  if (transLangs.length === 0) return;
  showTranslateDialog();
});

function showTranslateDialog() {
  const dialog = document.getElementById("translateDialog");
  const container = document.getElementById("translateDialogLangs");
  container.innerHTML = "";

  transLangs.forEach(code => {
    const langName = TRANSLATION_LANGUAGES.find(l => l.code === code)?.name || code;
    const hasData = translationData[code] && translationData[code].some(t => t.text);
    const label = document.createElement("label");
    label.className = "modal-option";
    label.innerHTML = `
      <input type="checkbox" value="${code}" checked />
      <span>${code.toUpperCase()} — ${langName}${hasData ? " (re-translate)" : ""}</span>
    `;
    container.appendChild(label);
  });

  dialog.classList.remove("hidden");
}

document.getElementById("translateDialogCancel")?.addEventListener("click", () => {
  document.getElementById("translateDialog")?.classList.add("hidden");
});

document.getElementById("translateDialogConfirm")?.addEventListener("click", () => {
  const dialog = document.getElementById("translateDialog");
  if (!dialog) return;
  const checked = [...dialog.querySelectorAll("input[type='checkbox']:checked")].map(cb => cb.value);
  dialog.classList.add("hidden");

  if (checked.length > 0) {
    runTranslation(checked);
  }
});

// ── Search & Replace ──

let searchMatches = [];
let searchMatchIndex = -1;

// Toggle search panel
document.getElementById("searchToggleBtn").addEventListener("click", () => {
  const panel = document.getElementById("searchPanel");
  const btn = document.getElementById("searchToggleBtn");
  const isOpen = !panel.classList.contains("hidden");
  if (isOpen) {
    panel.classList.add("hidden");
    btn.classList.remove("active");
    clearSearchHighlights();
  } else {
    panel.classList.remove("hidden");
    btn.classList.add("active");
    document.getElementById("searchInput").focus();
  }
});

document.getElementById("searchInput").addEventListener("input", () => {
  runSearch();
});

document.getElementById("searchPrevBtn").addEventListener("click", () => {
  navigateSearch(-1);
});

document.getElementById("searchNextBtn").addEventListener("click", () => {
  navigateSearch(1);
});

document.getElementById("replaceOneBtn").addEventListener("click", () => {
  replaceCurrent();
});

document.getElementById("replaceAllBtn").addEventListener("click", () => {
  replaceAll();
});

// Keyboard shortcuts in search input
document.getElementById("searchInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    navigateSearch(e.shiftKey ? -1 : 1);
  } else if (e.key === "Escape") {
    document.getElementById("searchPanel").classList.add("hidden");
    document.getElementById("searchToggleBtn").classList.remove("active");
    clearSearchHighlights();
  }
});

function clearSearchHighlights() {
  searchMatches = [];
  searchMatchIndex = -1;
  document.getElementById("searchCount").textContent = "";
  document.querySelectorAll(".search-highlight").forEach(el => {
    const parent = el.parentNode;
    el.replaceWith(document.createTextNode(el.textContent));
    if (parent) parent.normalize();
  });
}

function runSearch() {
  const query = document.getElementById("searchInput").value.trim();
  const countEl = document.getElementById("searchCount");

  // Clear previous highlights
  clearSearchHighlights();

  if (!query) return;

  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  // Find and highlight in visible segment text elements. Highlights are built
  // as DOM nodes — the text itself is never parsed as HTML.
  const textEls = document.querySelectorAll(".segment-text[contenteditable='true']");
  textEls.forEach(node => {
    if (node.classList.contains("translation-text")) return;
    const text = node.textContent;
    const allMatches = [...text.matchAll(new RegExp(escaped, "gi"))];
    if (allMatches.length === 0) return;
    node.textContent = "";
    let pos = 0;
    allMatches.forEach(m => {
      searchMatches.push({ el: node, index: m.index, length: m[0].length });
      if (m.index > pos) node.appendChild(document.createTextNode(text.slice(pos, m.index)));
      node.appendChild(el("mark", "search-highlight", m[0]));
      pos = m.index + m[0].length;
    });
    if (pos < text.length) node.appendChild(document.createTextNode(text.slice(pos)));
  });

  if (searchMatches.length > 0) {
    searchMatchIndex = 0;
    highlightActiveMatch();
  }

  updateSearchCount();
}

function updateSearchCount() {
  const countEl = document.getElementById("searchCount");
  if (searchMatches.length === 0) {
    countEl.textContent = document.getElementById("searchInput").value.trim() ? "0" : "";
  } else {
    countEl.textContent = `${searchMatchIndex + 1}/${searchMatches.length}`;
  }
}

function highlightActiveMatch() {
  // Remove previous active
  document.querySelectorAll(".search-highlight.active").forEach(el => {
    el.classList.remove("active");
  });

  if (searchMatchIndex < 0 || searchMatchIndex >= searchMatches.length) return;

  // Find the Nth highlight mark across all text elements
  const allMarks = document.querySelectorAll(".search-highlight");
  if (allMarks[searchMatchIndex]) {
    allMarks[searchMatchIndex].classList.add("active");
    allMarks[searchMatchIndex].scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

function navigateSearch(direction) {
  if (searchMatches.length === 0) return;
  searchMatchIndex = (searchMatchIndex + direction + searchMatches.length) % searchMatches.length;
  highlightActiveMatch();
  updateSearchCount();
}

function replaceCurrent() {
  const query = document.getElementById("searchInput").value.trim();
  const replacement = document.getElementById("replaceInput").value;
  if (!query || searchMatches.length === 0 || searchMatchIndex < 0) return;

  // Find the segment that contains the current match
  const match = searchMatches[searchMatchIndex];
  {
    const seg = match.el._seg;
    if (seg) {
      // Replace first occurrence in this segment's text
      const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const regex = new RegExp(escaped, "i");
      seg.text = seg.text.replace(regex, replacement);
    }
  }

  // Re-render and re-search
  const filtered = currentFilter === "all" ? segments : segments.filter(s => s.type === currentFilter);
  renderSegments(filtered);
  runSearch();
}

function replaceAll() {
  const query = document.getElementById("searchInput").value.trim();
  const replacement = document.getElementById("replaceInput").value;
  if (!query) return;

  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const regex = new RegExp(escaped, "gi");

  segments.forEach(seg => {
    if (seg.type === "speech" && seg.text) {
      seg.text = seg.text.replace(regex, replacement);
    }
  });

  // Re-render and re-search
  const filtered = currentFilter === "all" ? segments : segments.filter(s => s.type === currentFilter);
  renderSegments(filtered);
  runSearch();
}

// ── Slider controls ──

["paddingBefore", "paddingAfter", "minSilence"].forEach((id) => {
  const slider = document.getElementById(id);
  const label = document.getElementById(id + "Val");
  slider.addEventListener("input", () => {
    label.textContent = `${slider.value}ms`;
    // Live update cut markers & stats
    const cuts = getFilteredCutPoints();
    waveform.updateMarkers(cuts);
    updateCutStats();
    updateExportButtons();
  });
});

// Silence threshold slider
{
  const slider = document.getElementById("silenceThresh");
  const label = document.getElementById("silenceThreshVal");
  if (slider && label) {
    slider.addEventListener("input", () => {
      label.textContent = `${slider.value}dB`;
    });
  }
}

// ── Cut operations ──

function getFilteredCutPoints() {
  const paddingBefore = parseInt(document.getElementById("paddingBefore").value) / 1000;
  const paddingAfter = parseInt(document.getElementById("paddingAfter").value) / 1000;
  const minSilence = parseInt(document.getElementById("minSilence").value) / 1000;

  const silenceSegs = segments.filter((s) => s.type !== "speech" && (s.end - s.start) >= minSilence);
  if (silenceSegs.length === 0) return [];

  // Determine audio duration for edge detection
  const audioDur = loadedClipInfo?.duration || (segments.length > 0 ? Math.max(...segments.map(s => s.end)) : 0);

  return silenceSegs
    .map((s, i) => {
      // First silence (starts near 0): no paddingBefore — remove silence fully from start
      const isFirst = s.start < 0.2;
      // Last silence (ends near audio duration): no paddingAfter — remove silence fully at end
      const isLast = audioDur > 0 && (audioDur - s.end) < 0.2;

      const pBefore = isFirst ? 0 : paddingBefore;
      const pAfter = isLast ? 0 : paddingAfter;

      return {
        start: Math.max(0, s.start + pBefore),
        end: s.end - pAfter,
        type: s.type,
      };
    })
    .filter((s) => s.end > s.start);
}

// ── SRT Subtitle Export ──

function formatSrtTime(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const ms = Math.round((seconds % 1) * 1000);
  return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")},${String(ms).padStart(3,"0")}`;
}

function getSpeechSegments() {
  return segments.filter((s) => s.type === "speech" && s.text && s.text.trim());
}

/**
 * Get speech segments split according to current display mode.
 * Each sub-segment has proportional start/end timestamps.
 */
function getSplitSpeechSegments() {
  const speeches = getSpeechSegments();
  if (segLineBreakMode === "natural") {
    const parentMap = new Map();
    speeches.forEach((_, i) => parentMap.set(i, i));
    return { segs: speeches, parentMap };
  }
  const result = [];
  const parentMap = new Map();
  speeches.forEach((seg, origIdx) => {
    const tagged = { ...seg, _origIndex: origIdx };
    const splits = splitSegmentForDisplay(tagged);
    splits.forEach(sub => {
      parentMap.set(result.length, origIdx);
      result.push(sub);
    });
  });
  return { segs: result, parentMap };
}

function generateSrtAfterCuts() {
  const { segs } = getSplitSpeechSegments();
  // Exactly the frames that were (or will be) removed: snapped inward on the
  // sequence's real frame grid, the same list Apply / XML cut uses.
  const cuts = effectiveCuts();
  const getRemovedBefore = (time) => removedBeforeTime(time, cuts);
  function isFullyCut(seg) {
    for (const cut of cuts) {
      if (cut.start <= seg.start + 0.01 && cut.end >= seg.end - 0.01) return true;
    }
    return false;
  }
  let srt = "", idx = 1;
  segs.forEach((seg) => {
    if (isFullyCut(seg)) return;
    const newStart = seg.start - getRemovedBefore(seg.start);
    const newEnd = seg.end - getRemovedBefore(seg.end);
    if (newEnd <= newStart + 0.01) return;
    srt += `${idx}\n${formatSrtTime(Math.max(0, newStart))} --> ${formatSrtTime(newEnd)}\n${seg.text.trim()}\n\n`;
    idx++;
  });
  return srt;
}

// Original timing (before any cut) — subtitles keep the source clip's timecodes.
function generateSrtOriginal() {
  const { segs } = getSplitSpeechSegments();
  let srt = "", idx = 1;
  segs.forEach((seg) => {
    if (seg.end <= seg.start + 0.01) return;
    srt += `${idx}\n${formatSrtTime(Math.max(0, seg.start))} --> ${formatSrtTime(seg.end)}\n${(seg.text || "").trim()}\n\n`;
    idx++;
  });
  return srt;
}

// Pick SRT timing based on the Timeline "Subtitle timing" selector.
function generateSrtForMode() {
  const mode = document.getElementById("subtitleTimingMode")?.value || "after";
  return mode === "before" ? generateSrtOriginal() : generateSrtAfterCuts();
}

async function downloadFile(content, filename, mimeType) {
  // Save through the backend into the export folder
  try {
    const data = await fetchBackend("/save-file", {
      method: "POST",
      body: JSON.stringify({ filename, content }),
    });
    if (data.path) {
      console.log(`[EasyScript] Saved: ${data.path}`);
      return data.path;
    }
  } catch (e) {
    console.warn("[EasyScript] Backend save failed, falling back to blob download:", e);
  }

  // Fallback: blob download (works in browser dev mode)
  try {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch (e) {
    console.error("[Pro Cut] Download failed:", e);
    showStatus(`Could not save file: ${filename}`, true);
  }
}


// ── Speaker Diarization ──

/**
 * Split speech segments at speaker change boundaries using raw diarization data.
 *
 * Example: if a speech segment [0s–10s] contains diarize data showing
 * Speaker A [0s–6s] and Speaker B [6s–10s], it gets split into two segments:
 *   [0s–6s] Speaker A, [6s–10s] Speaker B
 *
 * This ensures each clip in the exported XML belongs to exactly one speaker,
 * so the editor can assign cameras per clip.
 */
function hasTranslations() {
  return Object.values(translationData || {}).some((arr) => Array.isArray(arr) && arr.some((x) => x && x.text));
}

function matchVoicesEnabled() {
  return !!document.getElementById("matchVoicesCheck")?.checked;
}

// Speech segment as sent to the backend (words drive word-level attribution).
function speechPayload(s) {
  return { start: s.start, end: s.end, text: s.text, language: s.language, type: "speech", words: s.words || [] };
}

function round3(n) { return Math.round(n * 1000) / 1000; }

/**
 * Start diarization on backend only (POST + return immediately).
 * Does NOT poll or show progress — caller handles that.
 */
async function startDiarizeBackend(audioPath) {
  const speechSegs = segments.filter(s => s.type === "speech");
  const knownInput = parseInt(document.getElementById("knownSpeakersInput")?.value ?? "0", 10);
  const numSpeakers = Number.isFinite(knownInput) && knownInput > 0 ? knownInput : null;
  const sensitivity = document.getElementById("speakerSensitivitySelect")?.value || "standard";
  const speed = document.getElementById("speakerSpeedSelect")?.value || null;
  await fetchBackend("/diarize", {
    method: "POST",
    body: JSON.stringify({
      audio_path: audioPath,
      segments: speechSegs.map(speechPayload),
      num_speakers: numSpeakers,
      sensitivity: sensitivity,
      speed: speed,
      match_voices: matchVoicesEnabled(),
      // Translations are stored per segment: don't split lines once they exist.
      split: !hasTranslations(),
    }),
  });
}

/**
 * Apply diarization result to current segments.
 * If no speech segments exist yet (standalone mode), create segments from diarize_raw.
 */
async function applyDiarizeResult(result) {
  speakerEmbeddings = result.speaker_embeddings || {};
  diarizeExclusive = result.exclusive || null;
  recognisedSpeakers = result.voice_matches || {};
  speakerMap = result.speaker_map || {};

  const nonSpeech = segments.filter(s => s.type !== "speech");
  const speech = segments.filter(s => s.type === "speech");
  const asSpeech = (list) => list.map((s) => ({ ...s, type: "speech" }));

  if (speech.length && (result.segments || []).length) {
    // Backend attributed every word and split lines at real speaker changes.
    segments = nonSpeech.concat(asSpeech(result.segments)).sort((a, b) => a.start - b.start);
  } else if (speech.length && diarizeExclusive) {
    // Transcribed in parallel with diarization: attribute the words now.
    const res = await fetchBackend("/speakers/assign", {
      method: "POST",
      body: JSON.stringify({
        segments: speech.map(speechPayload), exclusive: diarizeExclusive,
        speaker_embeddings: speakerEmbeddings, speaker_map: speakerMap,
        match_voices: matchVoicesEnabled(), split: !hasTranslations(),
      }),
    });
    speakerMap = res.speaker_map || speakerMap;
    recognisedSpeakers = res.voice_matches || recognisedSpeakers;
    segments = nonSpeech.concat(asSpeech(res.segments || speech)).sort((a, b) => a.start - b.start);
  } else if (result.diarize_raw && result.diarize_raw.length > 0) {
    // No transcription yet — one speech segment per speaker turn.
    const dRaw = result.diarize_raw;
    const grouped = [];
    let cur = { speaker: dRaw[0].speaker, start: dRaw[0].start, end: dRaw[0].end };
    for (let i = 1; i < dRaw.length; i++) {
      if (dRaw[i].speaker === cur.speaker && dRaw[i].start - cur.end < 0.5) {
        cur.end = dRaw[i].end;
      } else {
        grouped.push(cur);
        cur = { speaker: dRaw[i].speaker, start: dRaw[i].start, end: dRaw[i].end };
      }
    }
    grouped.push(cur);
    const newSegs = grouped.map(g => ({
      type: "speech", start: round3(g.start), end: round3(g.end), text: "",
      speaker: g.speaker, speakerLabel: speakerMap[g.speaker] || g.speaker,
    }));
    segments = [...nonSpeech, ...newSegs].sort((a, b) => a.start - b.start);
  }

  // Every speaker on a line needs a label.
  segments.forEach((s) => {
    if (s.type === "speech" && s.speaker && !speakerMap[s.speaker]) speakerMap[s.speaker] = s.speakerLabel || s.speaker;
  });
  hasSpeakers = Object.keys(speakerMap).length > 0;

  renderSegments(segments);
  updateSegmentCount(segments);
  if (waveform.peaks.length > 0) waveform.draw();
  const names = Object.values(recognisedSpeakers).map((m) => m.name);
  if (names.length) showStatus(`Recognised ${names.join(", ")} from saved voices`, false, "VOICES");
}

/**
 * Poll diarize progress endpoint until done. Returns result.
 */
async function pollDiarizeProgress() {
  return new Promise((resolve, reject) => {
    let polling = true;
    let seenProcessing = false;
    const poll = async () => {
      if (!polling) return;
      try {
        const data = await fetchBackend("/diarize/progress");
        if (data.status === "processing" || data.stage === "loading_model" || data.stage === "diarizing") {
          seenProcessing = true;
        }
        if (data.status === "done" && seenProcessing) {
          polling = false;
          resolve(data.result || data);
          return;
        }
        if (data.status === "error" && seenProcessing) {
          polling = false;
          reject(new Error(data.detail || "Diarization failed"));
          return;
        }
        if (polling) setTimeout(poll, 800);
      } catch {
        if (polling) setTimeout(poll, 2000);
      }
    };
    poll();
    // Allow external cancellation
    pollDiarizeProgress._stop = () => { polling = false; reject(new Error("__CANCELLED__")); };
  });
}

/**
 * Standalone speakers mode — runs diarize independently with its own progress UI.
 */
async function runDiarize() {
  const audioPath = document.getElementById("audioPathInput").value.trim();
  if (!audioPath) return;

  const btn = getBtn("diarizeBtn");
  btn.disabled = true;
  btn.classList.add("processing");
  progressTracker.show();

  try {
    await startDiarizeBackend(audioPath);
    const result = await progressTracker.pollUntilDone("/diarize/progress", null,
      () => fetchBackend("/diarize/cancel", { method: "POST" }).catch(() => {}));

    progressTracker.update(1, "done", `Done — ${result.num_speakers} speakers identified`);
    await applyDiarizeResult(result);

  } catch (err) {
    if (err.message !== "__CANCELLED__") {
      progressTracker.update(0, "error", `Error: ${err.message}`);
      showStatus(`Diarization failed: ${err.message}`, true);
    }
  } finally {
    btn.disabled = false;
    btn.classList.remove("processing");
    progressTracker.stopPolling();
    progressTracker.hide();
    updateActionButtons();
  }
}

// ── Translation Engine ──

async function runTranslation(targetLangs) {
  const speechSegs = segments.filter(s => s.type === "speech" && s.text);
  if (speechSegs.length === 0) {
    showStatus("No speech segments to translate.", true);
    return;
  }

  const btn = getBtn("translateBtn");
  btn.disabled = true;
  progressTracker.show();

  const sourceLang = getSourceLang() || "auto";

  try {
    for (const targetLang of targetLangs) {
      // Ensure translationData array exists
      if (!translationData[targetLang] || translationData[targetLang].length !== speechSegs.length) {
        translationData[targetLang] = speechSegs.map(() => ({ text: "" }));
      }

      // Switch to this language tab to show live updates
      activeTransLang = targetLang;
      renderTransTabs();
      if (currentFilter === "translation") {
        renderSegments(segments);
      }

      progressTracker.update(0.02, "translating",
        `Translating to ${targetLang.toUpperCase()}...`);

      const provider = document.getElementById("translationProvider")?.value || "ollama";

      await fetchBackend("/translate", {
        method: "POST",
        body: JSON.stringify({
          segments: speechSegs.map(s => ({ text: s.text, start: s.start, end: s.end })),
          source_lang: sourceLang,
          target_lang: targetLang,
          provider: provider,
        }),
      });

      // Poll with partial result streaming — push into segments live
      const result = await progressTracker.pollUntilDone("/translate/progress",
        (partialSegs) => {
          if (!partialSegs) return;
          // Update translationData with partial results
          partialSegs.forEach((t, i) => {
            if (t.text && translationData[targetLang][i]) {
              translationData[targetLang][i].text = t.text;
            }
          });
          // Live update UI — update text in existing DOM elements
          if (currentFilter === "translation" && activeTransLang === targetLang) {
            updateTranslationTextsInPlace(targetLang);
          }
        }
      );

      // Final merge
      if (result.segments) {
        result.segments.forEach((t, i) => {
          if (translationData[targetLang][i]) {
            translationData[targetLang][i].text = t.text;
          }
        });
      }

      progressTracker.update(1, "done",
        `Done — ${(result.segments || []).length} segments translated to ${targetLang.toUpperCase()}`);
    }

    // Final re-render
    if (currentFilter === "translation") {
      renderSegments(segments);
    }

  } catch (err) {
    progressTracker.update(0, "error", `Error: ${err.message}`);
    showStatus(`Translation failed: ${err.message}`, true);
  } finally {
    btn.disabled = transLangs.length === 0;
    progressTracker.stopPolling();
    progressTracker.hide();
  }
}

/** Update translation texts in-place without re-rendering the whole list */
function updateTranslationTextsInPlace(lang) {
  const langData = translationData[lang];
  if (!langData) return;
  document.querySelectorAll(".translation-text[data-trans-lang='" + lang + "']").forEach(el => {
    const idx = parseInt(el.dataset.transIndex);
    if (langData[idx] && langData[idx].text && !el.matches(":focus")) {
      el.textContent = langData[idx].text;
    }
  });
}

/** Translate a single segment (per-row icon) */
async function translateSingleSegment(segIndex, lang) {
  const speechSegs = segments.filter(s => s.type === "speech" && s.text);
  if (segIndex < 0 || segIndex >= speechSegs.length) return;

  const seg = speechSegs[segIndex];
  const sourceLang = getSourceLang() || "auto";
  const provider = document.getElementById("translationProvider")?.value || "ollama";

  // Show spinner on the icon
  const icon = document.querySelector(`.trans-retranslate[data-idx="${segIndex}"][data-lang="${lang}"]`);
  if (icon) {
    icon.textContent = "...";
    icon.style.pointerEvents = "none";
  }

  try {
    const result = await fetchBackend("/translate/one", {
      method: "POST",
      body: JSON.stringify({
        text: seg.text,
        source_lang: sourceLang,
        target_lang: lang,
        provider: provider,
      }),
    });

    // Update translationData
    if (!translationData[lang]) {
      translationData[lang] = speechSegs.map(() => ({ text: "" }));
    }
    if (translationData[lang][segIndex]) {
      translationData[lang][segIndex].text = result.text || "";
    }

    // Update the text element in place
    const textEl = document.querySelector(
      `.translation-text[data-trans-index="${segIndex}"][data-trans-lang="${lang}"]`
    );
    if (textEl) textEl.textContent = result.text || "";

  } catch (err) {
    showStatus(`Translation failed: ${err.message}`, true);
  } finally {
    if (icon) {
      icon.textContent = "↻";
      icon.style.pointerEvents = "";
    }
  }
}

// ── Segment Display Settings ──

function initSegmentSettings() {
  const btn = document.getElementById("segmentSettingsBtn");
  const panel = document.getElementById("segmentSettingsPanel");
  if (!btn || !panel) return;

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = !panel.classList.contains("hidden");
    panel.classList.toggle("hidden");
    btn.classList.toggle("active", !isOpen);
  });

  const sliderRow = document.getElementById("maxWordsRow");
  const slider = document.getElementById("maxWordsSlider");
  const valEl = document.getElementById("maxWordsVal");

  const radios = panel.querySelectorAll('input[name="lineBreakMode"]');
  radios.forEach(r => {
    r.addEventListener("change", () => {
      segLineBreakMode = r.value;
      if (r.value === "maxWords") {
        segMaxWords = parseInt(slider.value) || 5;
        if (sliderRow) sliderRow.classList.remove("hidden");
      } else {
        if (sliderRow) sliderRow.classList.add("hidden");
      }
      reRenderCurrentSegments();
    });
  });

  if (slider && valEl) {
    slider.addEventListener("input", () => {
      segMaxWords = parseInt(slider.value);
      valEl.textContent = String(segMaxWords);
      reRenderCurrentSegments();
    });
  }

  // Text-only view toggle
  const textViewBtn = document.getElementById("textViewToggle");
  if (textViewBtn) {
    textViewBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      segTextView = !segTextView;
      textViewBtn.classList.toggle("active", segTextView);
      reRenderCurrentSegments();
    });
  }
}

function reRenderCurrentSegments() {
  // Re-render using current filter
  if (currentFilter === "translation") {
    renderTranslationSegments();
  } else {
    const filtered = currentFilter === "all"
      ? segments
      : segments.filter(s => s.type === currentFilter);
    renderSegments(filtered);
  }
}

/**
 * Split a speech segment's text based on display settings.
 * Returns an array of sub-segments (each with start/end/text/speaker etc.)
 */
// ── Word timing ──
// Whisper's word timestamps — unless the line was edited and no longer matches.
function _normText(t) { return (t || "").normalize("NFC").toLowerCase().replace(/[\s\p{P}\p{S}]+/gu, ""); }
function segmentWords(seg) {
  const w = seg && seg.words;
  if (!w || !w.length) return null;
  return _normText(w.map((x) => x.word).join("")) === _normText(seg.text) ? w : null;
}

// Character span of every word inside `text` (null if they don't line up).
function wordSpans(text, words) {
  const spans = [];
  let cur = 0;
  for (const w of words) {
    const tok = (w.word || "").trim().normalize("NFC");
    if (!tok) { spans.push([cur, cur]); continue; }
    const j = text.indexOf(tok, cur);
    if (j < 0 || j - cur > 3 + tok.length) return null;
    spans.push([j, j + tok.length]);
    cur = j + tok.length;
  }
  return spans;
}

// Word index groups [[i0, i1), ...] for the current display mode.
function wordGroups(words) {
  const n = words.length;
  if (n <= 1) return null;
  const groups = [];
  if (segLineBreakMode === "word") {
    for (let i = 0; i < n; i++) groups.push([i, i + 1]);
  } else if (segLineBreakMode === "punctuation") {
    let i0 = 0;
    for (let i = 0; i < n - 1; i++) {
      if (/[.!?;,。！？，；]$/.test((words[i].word || "").trim())) { groups.push([i0, i + 1]); i0 = i + 1; }
    }
    groups.push([i0, n]);
  } else if (segLineBreakMode === "maxWords" && segMaxWords > 0) {
    for (let i = 0; i < n; i += segMaxWords) groups.push([i, Math.min(n, i + segMaxWords)]);
  } else {
    return null;
  }
  return groups.length > 1 ? groups : null;
}

function wordPieces(seg, words, groups) {
  const text = seg.text.trim().normalize("NFC");
  const spans = wordSpans(text, words);
  return groups.map(([i0, i1], k) => {
    const last = k === groups.length - 1;
    const piece = spans
      ? text.slice(spans[i0][0], last ? text.length : spans[i1][0]).trim()
      : words.slice(i0, i1).map((w) => (w.word || "").trim()).join(" ");
    return {
      ...seg, text: piece, words: words.slice(i0, i1),
      start: k === 0 ? seg.start : words[i0].start,
      end: last ? seg.end : Math.max(words[i1 - 1].end, words[i0].start + 0.05),
      _displaySplit: true, _parentIndex: seg._origIndex,
    };
  });
}

function splitSegmentForDisplay(seg) {
  if (seg.type !== "speech" || !seg.text) return [seg];

  const text = seg.text.trim();
  if (!text) return [seg];
  const words = segmentWords(seg);
  if (words) {
    const groups = wordGroups(words);
    return groups ? wordPieces(seg, words, groups) : [seg];
  }
  // No word timings (edited line, translation): split time by characters.
  const duration = seg.end - seg.start;

  function makeChunks(chunks) {
    if (chunks.length <= 1) return [seg];
    const totalChars = Math.max(1, chunks.reduce((s, c) => s + c.length, 0));
    let offset = seg.start;
    return chunks.map((chunk) => {
      const chunkDur = (chunk.length / totalChars) * duration;
      const sub = { ...seg, text: chunk, start: offset, end: offset + chunkDur, _displaySplit: true, _parentIndex: seg._origIndex };
      offset += chunkDur;
      return sub;
    });
  }

  // Word-by-word: each word on its own line
  if (segLineBreakMode === "word") {
    const words = text.split(/\s+/).filter(w => w);
    if (words.length <= 1) return [seg];
    const wordDur = duration / words.length;
    return words.map((word, i) => ({
      ...seg, text: word,
      start: seg.start + i * wordDur, end: seg.start + (i + 1) * wordDur,
      _displaySplit: true, _parentIndex: seg._origIndex,
    }));
  }

  // By punctuation: split at .!?,; boundaries
  if (segLineBreakMode === "punctuation") {
    const parts = text.split(/(?<=[.!?;,。！？，；])\s*/).filter(p => p.trim());
    return makeChunks(parts);
  }

  // Max words per line
  if (segLineBreakMode === "maxWords" && segMaxWords > 0) {
    const words = text.split(/\s+/).filter(w => w);
    if (words.length <= segMaxWords) return [seg];
    const chunks = [];
    for (let i = 0; i < words.length; i += segMaxWords) {
      chunks.push(words.slice(i, i + segMaxWords).join(" "));
    }
    return makeChunks(chunks);
  }

  return [seg];
}

// ── Cut Settings Toggle (inside Waveform header) ──

// ── Settings ──

function initSettings() {
  // Settings popup overlay — opened by the gear icon, closed by ✕ / backdrop / Esc.
  const overlay = document.getElementById("settingsOverlay");
  const openSettings = () => { if (overlay) overlay.classList.remove("hidden"); refreshVoiceList(); };
  const closeSettings = () => overlay && overlay.classList.add("hidden");
  document.getElementById("settingsBtn")?.addEventListener("click", openSettings);
  document.getElementById("settingsCloseBtn")?.addEventListener("click", closeSettings);
  overlay?.addEventListener("click", (e) => { if (e.target === overlay) closeSettings(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && overlay && !overlay.classList.contains("hidden")) closeSettings();
  });

  // Translation provider toggle — show/hide each provider's settings block.
  function updateProviderUI(provider) {
    document.getElementById("ollamaSettings")?.classList.toggle("hidden", provider !== "ollama");
    document.getElementById("hymt2Settings")?.classList.toggle("hidden", provider !== "hymt2");
    document.getElementById("nllbSettings")?.classList.toggle("hidden", provider !== "nllb");
    document.getElementById("claudeKeyRow")?.classList.toggle("hidden", provider !== "claude");
    if (provider === "hymt2") refreshHyMT2Status();
    if (provider === "nllb") refreshNLLBStatus();
  }
  const providerSelect = document.getElementById("translationProvider");
  if (providerSelect) {
    providerSelect.addEventListener("change", () => updateProviderUI(providerSelect.value));
    updateProviderUI(providerSelect.value);
  }

  document.getElementById("ollamaUrlInput")?.addEventListener("change", () => refreshOllamaModels());
  document.getElementById("refreshOllamaModelsBtn")?.addEventListener("click", () => refreshOllamaModels());
  document.getElementById("hymt2ModelSize")?.addEventListener("change", refreshHyMT2Status);
  document.getElementById("nllbModelSize")?.addEventListener("change", refreshNLLBStatus);

  // NLLB / Hy-MT2 download buttons (poll status until done)
  const wireDownload = (btnId, statusId, sizeId, endpoint, defSize) => {
    document.getElementById(btnId)?.addEventListener("click", async () => {
      const btn = document.getElementById(btnId);
      const statusText = document.getElementById(statusId);
      const size = document.getElementById(sizeId)?.value || defSize;
      btn.disabled = true; btn.textContent = "Downloading...";
      statusText.textContent = "Downloading model — this may take several minutes...";
      statusText.style.color = "";
      try {
        await fetchBackend(`/${endpoint}/download`, { method: "POST", body: JSON.stringify({ model_size: size }) });
        let polling = true;
        while (polling) {
          await new Promise(r => setTimeout(r, 3000));
          const data = await fetchBackend(`/${endpoint}/status?model_size=${size}`);
          const dlp = data.download_progress || {};
          if (data.downloaded || dlp.status === "done") {
            statusText.textContent = `✓ ${data.model_id} ready`; statusText.style.color = "var(--accent)"; polling = false;
          } else if (dlp.status === "error") {
            statusText.textContent = `Error: ${dlp.detail || "download failed"}`; polling = false;
          } else if (dlp.detail) { statusText.textContent = dlp.detail; }
        }
      } catch (err) {
        statusText.textContent = `Error: ${err.message}`;
      } finally { btn.disabled = false; btn.textContent = "Re-download"; }
    });
  };
  wireDownload("nllbDownloadBtn", "nllbStatusText", "nllbModelSize", "nllb", "600M");
  wireDownload("hymt2DownloadBtn", "hymt2StatusText", "hymt2ModelSize", "hymt2", "1.8B");

  // Save settings
  document.getElementById("saveSettingsBtn").addEventListener("click", async () => {
    const settings = {
      hf_token: document.getElementById("hfTokenInput").value.trim(),
      translation_provider: document.getElementById("translationProvider")?.value || "nllb",
      ollama_url: document.getElementById("ollamaUrlInput")?.value.trim() || "http://localhost:11434",
      ollama_model: document.getElementById("ollamaModelSelect")?.value || "",
      hymt2_model_size: document.getElementById("hymt2ModelSize")?.value || "1.8B",
      nllb_model_size: document.getElementById("nllbModelSize")?.value || "600M",
      anthropic_api_key: document.getElementById("claudeApiKeyInput")?.value.trim() || "",
    };
    try {
      await fetchBackend("/settings", { method: "POST", body: JSON.stringify(settings) });
      showStatus("Settings saved!");
      closeSettings();  // Save = persist + close. Closing any other way does NOT save.
    } catch (err) {
      showStatus(`Failed to save settings: ${err.message}`, true);
    }
  });

  // Load saved settings
  loadSavedSettings();
}

async function refreshOllamaModels(savedModel) {
  const select = document.getElementById("ollamaModelSelect");
  if (!select) return;
  try {
    const data = await fetchBackend("/ollama/status");
    const models = data.models || [];
    select.innerHTML = "";
    if (models.length === 0) { select.innerHTML = '<option value="">No models found</option>'; return; }
    models.forEach(name => {
      const opt = document.createElement("option");
      opt.value = name; opt.textContent = name; select.appendChild(opt);
    });
    select.value = (savedModel && models.includes(savedModel)) ? savedModel : models[0];
  } catch {
    select.innerHTML = '<option value="">Ollama not reachable</option>';
  }
}

async function refreshHyMT2Status() {
  const statusText = document.getElementById("hymt2StatusText");
  const downloadBtn = document.getElementById("hymt2DownloadBtn");
  if (!statusText || !downloadBtn) return;
  const size = document.getElementById("hymt2ModelSize")?.value || "1.8B";
  try {
    const data = await fetchBackend(`/hymt2/status?model_size=${size}`);
    if (data.downloaded) { statusText.textContent = `✓ ${data.model_id} ready`; statusText.style.color = "var(--accent)"; downloadBtn.textContent = "Re-download"; }
    else { statusText.textContent = `Not downloaded — ${data.model_id}`; statusText.style.color = ""; downloadBtn.textContent = "Download"; }
  } catch { statusText.textContent = "Status check failed"; statusText.style.color = ""; }
}

async function refreshNLLBStatus() {
  const statusText = document.getElementById("nllbStatusText");
  const downloadBtn = document.getElementById("nllbDownloadBtn");
  if (!statusText || !downloadBtn) return;
  const size = document.getElementById("nllbModelSize")?.value || "600M";
  try {
    const data = await fetchBackend(`/nllb/status?model_size=${size}`);
    if (data.downloaded) { statusText.textContent = `✓ ${data.model_id} ready`; statusText.style.color = "var(--accent)"; downloadBtn.textContent = "Re-download"; }
    else { statusText.textContent = `Not downloaded — ${data.model_id}`; statusText.style.color = ""; downloadBtn.textContent = "Download"; }
  } catch { statusText.textContent = "Status check failed"; statusText.style.color = ""; }
}

async function loadSavedSettings() {
  try {
    const settings = await fetchBackend("/settings");
    if (settings.hf_token) {
      document.getElementById("hfTokenInput").value = settings.hf_token;
    }
    if (settings.translation_provider) {
      const providerSelect = document.getElementById("translationProvider");
      if (providerSelect) {
        providerSelect.value = settings.translation_provider;
        providerSelect.dispatchEvent(new Event("change"));
      }
    }
    if (settings.ollama_url) {
      document.getElementById("ollamaUrlInput").value = settings.ollama_url;
    }
    if (settings.anthropic_api_key) {
      document.getElementById("claudeApiKeyInput").value = settings.anthropic_api_key;
    }
    if (settings.hymt2_model_size) {
      const el = document.getElementById("hymt2ModelSize"); if (el) el.value = settings.hymt2_model_size;
    }
    if (settings.nllb_model_size) {
      const el = document.getElementById("nllbModelSize"); if (el) el.value = settings.nllb_model_size;
    }
    await refreshOllamaModels(settings.ollama_model);
  } catch {
    try { await refreshOllamaModels(); } catch {}
    // Settings not available yet
  }
}

// ── Event bindings ──

document.getElementById("progressCancelBtn").addEventListener("click", () => progressTracker.cancel());
document.getElementById("autoCutBtn").addEventListener("click", runAutoCut);
document.getElementById("transcribeBtn").addEventListener("click", () => runTranscribe(false));
// Right-click transcribe = resume from playhead
document.getElementById("transcribeBtn").addEventListener("contextmenu", (e) => {
  e.preventDefault();
  const playheadTime = audioPlayback.audio ? audioPlayback.audio.currentTime : 0;
  if (playheadTime > 1) {
    runTranscribe(true);
  } else {
    runTranscribe(false);
  }
});
document.getElementById("diarizeBtn").addEventListener("click", runDiarize);
document.getElementById("labelSpeakerBtn")?.addEventListener("click", labelSpeakerClips);
// Export SRT button (save to file via backend)
document.getElementById("exportSrtBtn")?.addEventListener("click", async () => {
  const srt = generateSrtForMode();
  if (!srt) { showStatus("No transcription to export.", true); return; }
  const name = loadedClipInfo ? loadedClipInfo.name.replace(/\.[^.]+$/, "") : "easyscript";
  const path = await downloadFile(srt, `${name}.srt`, "text/srt");
  if (path) setExportInfo("SAVED", path);
});

function setExportInfo(tag, text) {
  const info = document.getElementById("exportInfo");
  if (!info) return;
  info.textContent = "";
  info.appendChild(el("span", "model-tag tag-loaded", tag));
  info.appendChild(document.createTextNode(text));
}

// Update action buttons
function updateActionButtons() {
  const hasInput = !!currentAudioPath;
  ["autoCutBtn", "transcribeBtn", "diarizeBtn", "beatDetectBtn"].forEach(id => {
    const b = document.getElementById(id);
    if (b) {
      patchDisabled(b);
      b.disabled = !backendConnected || !hasInput;
    }
  });
  stemUI.render();
}

// ── Workflow tabs (Cut · Transcript · Beats) ──

function setActiveTab(name) {
  document.querySelectorAll(".tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${name}`));
  waveform.setMode(name);
  try { localStorage.setItem("easyscript.tab", name); } catch (e) {}
}
document.querySelectorAll(".tabs .tab").forEach((t) => t.addEventListener("click", () => setActiveTab(t.dataset.tab)));

// ── Export folder (Settings) ──

async function refreshExportDir() {
  try {
    const d = await fetchBackend("/export-dir");
    const t = document.getElementById("exportDirText");
    if (t) { t.textContent = d.path || "—"; t.title = d.path || ""; }
  } catch (e) {}
}

document.getElementById("settingsBtn")?.addEventListener("click", refreshExportDir);
document.getElementById("exportDirBtn")?.addEventListener("click", async () => {
  try {
    let chosen = "";
    const fs = window.cep && window.cep.fs;
    if (fs && (fs.showOpenDialogEx || fs.showOpenDialog)) {
      const r = fs.showOpenDialogEx
        ? fs.showOpenDialogEx(false, true, "Choose export folder", "", [])
        : fs.showOpenDialog(false, true, "Choose export folder", "", []);
      if (r && r.data && r.data.length) chosen = r.data[0];
    } else {
      // Browser dev mode: the backend's own folder dialog.
      await fetchBackend("/choose-folder", { method: "POST" });
      await refreshExportDir();
      return;
    }
    if (!chosen) return;
    await fetchBackend("/export-dir", { method: "POST", body: JSON.stringify({ path: chosen }) });
    await refreshExportDir();
  } catch (err) {
    showStatus(`Could not set the export folder: ${err.message}`, true);
  }
});

// ── Init ──

waveform.getSegments = () => segments;
waveform.speakerColor = (spk) => (hasSpeakers ? SPEAKER_WAVE_COLORS[getSpeakerColorIndex(spk)] : null);
waveform.fetchSlice = (start, end, bins, peak) => fetchBackend("/waveform/slice", {
  method: "POST",
  body: JSON.stringify({ audio_path: currentAudioPath, start, end, bins, peak }),
});
waveform.onSeek = (t) => { audioPlayback.seekTo(t); syncPremierePlayhead(t); };
waveform.init();
audioPlayback.init();
initSettings();
initSegmentSettings();
restorePrefs();
beatUI.init();
stemUI.init();
updateExportButtons();   // Apply / XML / subtitle buttons stay off until there is something to apply
updateActionButtons();
{
  let tab = "cut";
  try { tab = localStorage.getItem("easyscript.tab") || "cut"; } catch (e) {}
  setActiveTab(["cut", "text", "beats"].includes(tab) ? tab : "cut");
}

// Options remembered between sessions (per viewer, best effort).
function restorePrefs() {
  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem("easyscript.prefs") || "{}") || {}; } catch (e) {}
  PREF_IDS.forEach((id) => {
    const e = document.getElementById(id);
    if (!e || !(id in prefs)) return;
    if (e.type === "checkbox") e.checked = !!prefs[id];
    else if (e.tagName !== "SELECT" || [...e.options].some((o) => o.value === prefs[id])) e.value = prefs[id];
  });
  PREF_IDS.forEach((id) => document.getElementById(id)?.addEventListener("change", savePrefs));
}

function savePrefs() {
  const prefs = {};
  PREF_IDS.forEach((id) => {
    const e = document.getElementById(id);
    if (e) prefs[id] = e.type === "checkbox" ? e.checked : e.value;
  });
  try { localStorage.setItem("easyscript.prefs", JSON.stringify(prefs)); } catch (e) {}
}

// Try auto-start server, then check connection
autoStartServer().then(() => {
  setTimeout(checkBackend, 2000);
});
checkBackend();
setInterval(checkBackend, 5000);

// Scan audio tracks on init (and rescan periodically)
scanAudioTracks();
setInterval(scanAudioTracks, 10000);

// Premiere-specific event listeners
document.getElementById("loadAudioBtn").addEventListener("click", loadAudioFromTimeline);
document.getElementById("applyCutBtn").addEventListener("click", applyCutsToTimeline);
document.getElementById("exportXmlBtn").addEventListener("click", exportCutXML);
document.getElementById("makeSubtitleBtn").addEventListener("click", makeSubtitles);

// ════════════════════════════════════════════════════════════════════════
// CEP layer — every function that touches Premiere goes through the
// ExtendScript bridge (host.jsx). Everything else (transcribe / silence /
// diarize / translate / beats / waveform / settings) reaches the backend over
// fetch.
// ════════════════════════════════════════════════════════════════════════

function isDevMode() { return !(window.bridge && window.bridge.available()); }

// Wipe all analysis of the previous audio (segments, speakers, translations,
// beats, waveform) so a newly loaded clip never shows stale info.
function resetAnalysisState() {
  segments = [];
  hasTranscription = false;
  hasSpeakers = false;
  speakerMap = {};
  speakerEmbeddings = {};
  diarizeExclusive = null;
  recognisedSpeakers = {};
  translationData = {};
  audioDuration = 0;
  cutsApplied = false;
  lastAppliedCuts = null;
  try { waveform.clear(); } catch (e) {}
  try { beatUI.reset(); } catch (e) {}
  try { renderSegments([]); } catch (e) {}
  try { updateCutStats(); updateExportButtons(); updateActionButtons(); } catch (e) {}
}

async function loadAudioFromTimeline() {
  const btn = getBtn("loadAudioBtn");
  const label = "Load audio from timeline";
  if (btn) { btn.disabled = true; btn.textContent = "Scanning…"; }
  try {
    if (!window.bridge || !bridge.available()) throw new Error("Not running inside Premiere Pro.");
    const mode = document.getElementById("sourceMode")?.value || "selected";
    const trackVal = document.getElementById("trackSelect")?.value || "all";
    const trackIdx = trackVal === "all" ? -1 : parseInt(trackVal, 10);

    // {path, nested, name, start, end, inPoint, outPoint, fps, ticksPerFrame, nodeId, sequenceID}
    const clip = await bridge.getSelectedClip(mode, trackIdx);
    resetAnalysisState();  // clear previous audio's segments + waveform immediately
    stemUI.reset("");
    if (clip.ticksPerFrame > 0) seqTimebase = { tpf: clip.ticksPerFrame, fps: clip.fps, sequenceID: clip.sequenceID };
    lastAppliedFps = clip.fps || 25;

    const clipDuration = Math.max(0, (clip.end || 0) - (clip.start || 0));
    let analyzePath, analyzeDur, seqStart, seqStartTicks;

    // Render the actual timeline output when: timeline In/Out, Entire sequence,
    // or a nested sequence (no source file). Otherwise (a plain trimmed clip)
    // just trim the source file — faster, no render.
    const useRender = (mode === "inout" || mode === "entire" || clip.nested);
    if (useRender) {
      if (btn) btn.textContent = "Rendering…";
      const r = await bridge.renderRange(mode, clip.start || 0, clip.end || 0);
      console.log("[EasyScript] render:", r);
      if (!r || !r.path) throw new Error("Render failed: " + ((r && (r.error || r.log)) || "unknown"));
      seqStart = (typeof r.start === "number") ? r.start : 0;
      seqStartTicks = frameToTicks(secToFrameRound(seqStart));
      analyzeDur = (r.end > r.start) ? (r.end - r.start) : 0; // 0 → resolved from /waveform
      // Premiere's render is often quiet → normalize so silence detection works.
      analyzePath = r.path;
      try {
        const nr = await fetchBackend("/trim", {
          method: "POST",
          body: JSON.stringify({ audio_path: r.path, start: 0, end: 0, normalize: true }),
        });
        if (nr && nr.path) { analyzePath = nr.path; if (nr.audio_duration) analyzeDur = analyzeDur || nr.audio_duration; }
      } catch (e) { console.warn("[EasyScript] normalize failed, using raw render:", e); }
    } else {
      if (btn) btn.textContent = "Trimming…";
      const srcStart = clip.inPoint || 0;
      const srcEnd = srcStart + clipDuration;
      analyzePath = clip.path; analyzeDur = clipDuration; seqStart = clip.start || 0;
      seqStartTicks = Number(clip.startTicks) || frameToTicks(secToFrameRound(seqStart));
      try {
        const tr = await fetchBackend("/trim", {
          method: "POST",
          body: JSON.stringify({ audio_path: clip.path, start: srcStart, end: srcEnd }),
        });
        if (tr && tr.path) { analyzePath = tr.path; analyzeDur = tr.audio_duration || clipDuration; }
      } catch (e) {
        console.warn("[EasyScript] /trim failed, using full source:", e);
      }
    }

    currentAudioPath = analyzePath;
    document.getElementById("audioPathInput").value = analyzePath;
    // The analyzed file is 0-based and equals the on-timeline content, so
    // inPoint = 0 and sequence time = seqStart + t. srcIn/nodeId identify the
    // clip's media for clip markers.
    loadedClipInfo = {
      name: clip.name, duration: analyzeDur, mediaPath: analyzePath, sourceMode: mode,
      seqStart: seqStart, inPoint: 0, outPoint: analyzeDur,
      srcIn: useRender ? 0 : (clip.inPoint || 0),
      nodeId: useRender ? "" : (clip.nodeId || ""),
      nested: !!clip.nested,
      sequenceID: clip.sequenceID || "",
      sourcePath: useRender ? "" : (clip.path || ""),
      // Exact timeline position, and the range to render again (full quality)
      // for voice / music separation.
      seqStartTicks, renderStart: clip.start || 0, renderEnd: clip.end || 0,
    };
    waveform.setFrameRate(seqTimebase.fps);

    const modeLabel = { entire: "entire sequence", inout: "in/out", selected: "selected clip" }[mode] || mode;
    showStatus(`${clip.name}${analyzeDur > 0 ? " · " + formatTime(analyzeDur) : ""} · ${modeLabel}`, false, "LOADED");
    audioPlayback.loadAudio(analyzePath);
    stemUI.reset(analyzePath);
    updateActionButtons();
    beatUI.render();
    await loadWaveformPeaks(analyzePath, analyzeDur);
  } catch (err) {
    showStatus(err.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

async function applyCutsToTimeline() {
  if (!currentAudioPath) { showStatus("Load audio from the timeline first.", true); return; }
  const cuts = getFilteredCutPoints();
  if (cuts.length === 0) { showStatus("No cuts to apply. Run Detect silence first.", true); return; }
  if (cutsApplied) {
    showStatus("Cuts were already applied to this sequence. Load the audio again to analyse the edited timeline.", true);
    return;
  }

  const btn = getBtn("applyCutBtn");
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Cutting…"; }
  try {
    if (!window.bridge || !bridge.available()) throw new Error("Not running inside Premiere Pro.");
    await ensureSameSequence();
    const tb = await refreshTimebase();
    lastAppliedFps = tb.fps;

    // Frame-exact, inward-snapped ranges on the real frame grid (see snapCutsToFrames).
    const frameCuts = snapCutsToFrames(cuts, tb);
    if (frameCuts.length === 0) { showStatus("No cut is at least one frame long within the clip.", true); return; }
    const regionsTicks = frameCuts.map(c => [String(frameToTicks(c.f0, tb)), String(frameToTicks(c.f1, tb))]);
    console.log("[EasyScript] applyCuts:", { fps: tb.fps, tpf: tb.tpf, cuts: frameCuts.length, first: regionsTicks.slice(0, 3) });

    if (btn) btn.textContent = `Cutting ${frameCuts.length}…`;
    const t0 = performance.now();
    const r = await bridge.extractTicks(regionsTicks, loadedClipInfo && loadedClipInfo.sequenceID);
    const secs = ((performance.now() - t0) / 1000).toFixed(1);
    if (r.extracted > 0) {
      cutsApplied = true;  // timeline shifted — Tag speaker / markers / subtitles compensate
      lastAppliedCuts = frameCuts.map(c => ({ start: c.start, end: c.end }));
    }
    const removed = frameCuts.reduce((a, c) => a + (c.end - c.start), 0);
    showStatus(`Removed ${r.extracted} of ${r.total} regions (−${formatTime(removed)}) in ${secs}s` +
      (r.err ? ` · ${r.err}` : ""), r.extracted === 0, r.extracted ? "DONE" : "");
  } catch (err) {
    showStatus(`Apply cut failed: ${err.message}`, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

/**
 * Cut → new sequence via XML: export the active sequence as FCP XML, cut it in
 * the backend (every track, links kept), import the result as a new sequence.
 * Premiere does two operations whatever the number of cuts, and the original
 * sequence is left untouched.
 */
async function exportCutXML() {
  if (!currentAudioPath) { showStatus("Load audio from the timeline first.", true); return; }
  const cuts = getFilteredCutPoints();
  if (cuts.length === 0) { showStatus("No cuts to apply. Run Detect silence first.", true); return; }

  const btn = getBtn("exportXmlBtn");
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Exporting…"; }
  try {
    if (!window.bridge || !bridge.available()) throw new Error("Not running inside Premiere Pro.");
    if (cutsApplied) throw new Error("The active sequence is already cut — load the audio again before cutting it.");
    await ensureSameSequence();
    const tb = await refreshTimebase();
    const frameCuts = snapCutsToFrames(cuts, tb);
    if (frameCuts.length === 0) { showStatus("No cut is at least one frame long within the clip.", true); return; }

    const t0 = performance.now();
    const exported = await bridge.exportSequenceXML();
    if (btn) btn.textContent = `Cutting ${frameCuts.length}…`;
    const res = await fetchBackend("/xml/cut", {
      method: "POST",
      body: JSON.stringify({
        xml_path: exported.path,
        cuts_ticks: frameCuts.map(c => [frameToTicks(c.f0, tb), frameToTicks(c.f1, tb)]),
      }),
    });

    let opened = null;
    if (document.getElementById("xmlImportCheck")?.checked) {
      if (btn) btn.textContent = "Importing…";
      opened = await bridge.importSequenceXML(res.path, res.name);
      // The new, already-cut sequence is now active: subtitles, Tag speaker and
      // beat markers target it from here on.
      cutsApplied = true;
      lastAppliedCuts = frameCuts.map(c => ({ start: c.start, end: c.end }));
      loadedClipInfo.sequenceID = opened.sequenceID;
      loadedClipInfo.nodeId = "";  // imported clips are new project items
      beatUI.render();
    }
    const secs = ((performance.now() - t0) / 1000).toFixed(1);
    const warn = (res.warnings || []).join(" · ");
    showStatus(`${opened ? `Opened "${opened.name}"` : "XML saved"} — ${res.cuts} cuts, −${formatTime(res.removed_seconds)}, ` +
      `${res.clips_after} clips, ${secs}s · ${res.path}${warn ? " · " + warn : ""}`, false, "DONE");
  } catch (err) {
    showStatus(`Cut to new sequence failed: ${err.message}`, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

// Build SRT for a language (lang="" = original transcript), honoring the
// Segments "Display mode" (natural / word / punctuation / max-words) for BOTH
// the original and translated text, plus before/after-cut timing.
function generateSrtForLang(lang) {
  const mode = document.getElementById("subtitleTimingMode")?.value || "after";

  // Display-split segments for this language.
  let splitSegs;
  if (!lang) {
    splitSegs = getSplitSpeechSegments().segs;  // original text, split per display mode
  } else {
    splitSegs = [];
    segments.filter((s) => s.type === "speech").forEach((seg, i) => {
      const tt = (translationData[lang] && translationData[lang][i] && translationData[lang][i].text) || "";
      if (!tt.trim()) return;
      const synth = { type: "speech", text: tt, start: seg.start, end: seg.end, _origIndex: i };
      splitSegmentForDisplay(synth).forEach((sub) => splitSegs.push(sub));
    });
  }

  // "After cut": exactly the frames that were (or will be) removed.
  const cuts = mode === "after" ? effectiveCuts() : [];
  const fullyCut = (seg) => cuts.some((c) => c.start <= seg.start + 0.01 && c.end >= seg.end - 0.01);

  let srt = "", idx = 1;
  splitSegs.forEach((seg) => {
    const text = (seg.text || "").trim();
    if (!text) return;
    let start = seg.start, end = seg.end;
    if (mode === "after") {
      if (fullyCut(seg)) return;
      start = seg.start - removedBeforeTime(seg.start, cuts);
      end = seg.end - removedBeforeTime(seg.end, cuts);
      if (end <= start + 0.01) return;
    }
    srt += `${idx}\n${formatSrtTime(Math.max(0, start))} --> ${formatSrtTime(end)}\n${text}\n\n`;
    idx++;
  });
  return srt;
}

// Make Subtitle → open a dialog to pick which language versions to create.
async function makeSubtitles() {
  const speeches = getSpeechSegments();
  if (!speeches.length) { showStatus("No transcription. Run Transcribe first.", true); return; }
  if (!window.bridge || !bridge.available()) { showStatus("Not running inside Premiere Pro.", true); return; }

  const cont = document.getElementById("subtitleDialogLangs");
  cont.textContent = "";
  const addRow = (value, text, checked, disabled) => {
    const row = el("label", "checkbox-row");
    const cb = el("input", "sub-lang-cb");
    cb.type = "checkbox"; cb.value = value; cb.checked = checked; cb.disabled = disabled;
    row.appendChild(cb);
    row.appendChild(el("span", "checkbox-label", text));
    cont.appendChild(row);
  };
  addRow("", "Original (transcript)", true, false);
  (transLangs || []).forEach((code) => {
    const has = translationData[code] && translationData[code].some((t) => t && t.text);
    addRow(code, code.toUpperCase() + (has ? "" : " — no translation yet"), !!has, !has);
  });
  document.getElementById("subtitleDialog").classList.remove("hidden");
}

async function createSelectedSubtitles(langs) {
  const btn = getBtn("makeSubtitleBtn");
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Importing…"; }
  const baseName = loadedClipInfo ? loadedClipInfo.name.replace(/\.[^.]+$/, "") : "easyscript";
  let imported = 0, placed = 0, why = "";
  try {
    // Captions start where the analysed audio starts on the timeline.
    const startSec = (loadedClipInfo && loadedClipInfo.seqStart) || 0;
    for (const lang of langs) {
      const srt = generateSrtForLang(lang);
      if (!srt) continue;
      const path = await downloadFile(srt, `${baseName}${lang ? "." + lang : ""}.srt`, "text/srt");
      if (!path) continue;
      const r = await bridge.importSubtitle(path, startSec);
      imported++;
      if (r.captionTrack) placed++; else why = r.reason || why;
    }
    setExportInfo("DONE", placed
      ? `${placed} caption track${placed > 1 ? "s" : ""} added to the active sequence`
      : `Imported ${imported} subtitle file(s) — drag them from the Project panel onto a caption track${why ? ` (${why})` : ""}`);
  } catch (err) {
    showStatus(`Make subtitle failed: ${err.message}`, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

document.getElementById("subtitleDialogCancel")?.addEventListener("click", () => {
  document.getElementById("subtitleDialog").classList.add("hidden");
});
document.getElementById("subtitleDialogConfirm")?.addEventListener("click", () => {
  const langs = Array.prototype.slice.call(document.querySelectorAll(".sub-lang-cb:checked")).map((cb) => cb.value);
  document.getElementById("subtitleDialog").classList.add("hidden");
  if (langs.length) createSelectedSubtitles(langs);
});

async function labelSpeakerClips() {
  if (!hasSpeakers) { showStatus("Run Speakers first to detect who's talking.", true); return; }

  const btn = getBtn("labelSpeakerBtn");
  const label = btn ? btn.textContent : "";
  const mode = document.getElementById("tagSpeakerMode")?.value || "xml";
  if (btn) { btn.disabled = true; btn.textContent = mode === "xml" ? "Building…" : "Labeling…"; }
  try {
    if (!window.bridge || !bridge.available()) throw new Error("Not running inside Premiere Pro.");
    await ensureSameSequence();
    const tb = await refreshTimebase();
    const speakers = Object.keys(speakerMap);
    const LABEL_INDICES = [4, 1, 2, 6, 9, 5, 11, 13];
    const speakerColor = {};
    speakers.forEach((spk, i) => { speakerColor[spk] = LABEL_INDICES[i % LABEL_INDICES.length]; });
    const speakerName = {};
    speakers.forEach((spk) => { speakerName[spk] = speakerMap[spk] || spk; });

    // Speech segments with a speaker, clamped to the clip, sorted (analysis time).
    const outP = (loadedClipInfo && loadedClipInfo.outPoint) || Infinity;
    const spSegs = segments
      .filter((s) => s.type === "speech" && s.speaker)
      .map((s) => ({ start: Math.max(s.start, 0), end: Math.min(s.end, outP), speaker: s.speaker }))
      .filter((s) => s.end > s.start)
      .sort((a, b) => a.start - b.start);
    if (spSegs.length === 0) { showStatus("No speaker segments within the clip.", true); return; }

    // Speaker runs; a change point sits in the middle of the gap between runs.
    const runs = [];
    spSegs.forEach((s) => {
      const last = runs[runs.length - 1];
      if (last && last.speaker === s.speaker) last.end = Math.max(last.end, s.end);
      else runs.push({ ...s });
    });
    const changes = [];
    for (let i = 1; i < runs.length; i++) changes.push((runs[i - 1].end + runs[i].start) / 2);
    const toSeq = (t) => analysisToSeqTime(t);

    if (mode === "xml") {
      // Exact: rebuild the sequence with an edit at every change, clips named
      // and coloured per speaker; music / B-roll stay untouched.
      const toTicks = (t) => frameToTicks(secToFrameRound(toSeq(t), tb), tb);
      const end = (loadedClipInfo && loadedClipInfo.duration) || runs[runs.length - 1].end;
      const labels = runs.map((r, i) => ({
        start_ticks: toTicks(i === 0 ? 0 : changes[i - 1]),
        end_ticks: toTicks(i === runs.length - 1 ? end : changes[i]),
        name: speakerName[r.speaker], color: speakerColor[r.speaker],
      }));
      const t0 = performance.now();
      const exported = await bridge.exportSequenceXML();
      const res = await fetchBackend("/xml/cut", {
        method: "POST",
        body: JSON.stringify({
          xml_path: exported.path, cuts_ticks: [], splits_ticks: changes.map(toTicks), labels,
          only_media: loadedClipInfo && loadedClipInfo.sourcePath ? [loadedClipInfo.sourcePath] : [],
          name_suffix: " (speakers)", file_label: "EasyScript speakers",
        }),
      });
      if (btn) btn.textContent = "Importing…";
      const opened = await bridge.importSequenceXML(res.path, res.name);
      loadedClipInfo.sequenceID = opened.sequenceID;
      loadedClipInfo.nodeId = "";  // imported clips are new project items
      const secs = ((performance.now() - t0) / 1000).toFixed(1);
      showStatus(`Opened "${opened.name}" — ${res.splits} speaker edits, ${res.labeled} clips named, ${secs}s`, false, "DONE");
      return;
    }

    // In place: 1-frame extract at each change, then rename clips.
    const segsSeq = spSegs.map((s) => ({ start: toSeq(s.start), end: toSeq(s.end), speaker: s.speaker }));
    const res = await bridge.labelSpeaker(changes.map(toSeq), segsSeq, speakerColor, seqTimebase.fps, speakerName);
    console.log("[EasyScript] labelSpeaker result:", res);
    if ((res.renamed || 0) > 0) {
      showStatus(`${res.edits} speaker splits · renamed ${res.renamed} clips`, false, "DONE");
    } else {
      showStatus(`Made ${res.edits} speaker splits, but couldn't rename clips (API: ${res.nameApi}). Check the console.`, true);
    }
  } catch (err) {
    showStatus(`Tag speaker failed: ${err.message}`, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

async function runDiagnostic() {
  try {
    if (!window.bridge || !bridge.available()) { showStatus("Not running inside Premiere Pro.", true); return; }
    const pong = await bridge.ping();
    const info = await bridge.inspectClip();
    console.log("[EasyScript] PING:", pong);
    console.log("[EasyScript] CLIP COLOR/LABEL API:\n" + info);
    showStatus(`${pong} — clip API logged to the console (localhost:8088)`, false, "DIAG");
  } catch (e) {
    showStatus(`Diag failed: ${e.message}`, true);
  }
}

console.log("[EasyScript] CEP extension — Premiere Pro (ExtendScript bridge)");

// TEMP v2.1: dump TrackItem/ProjectItem label API to ~/.easyscript/label_api.json
setTimeout(function () {
  try {
    if (window.bridge && bridge.dumpLabelAPI) {
      bridge.dumpLabelAPI()
        .then(function () { console.log("[EasyScript] label API dumped"); })
        .catch(function () {});
    }
  } catch (e) {}
}, 2500);
