// BACKEND_URL: overridden by pywebview at runtime, fallback for UXP plugin mode
let BACKEND_URL = window.BACKEND_URL || "http://localhost:9876";
let segments = [];
let backendConnected = false;
let currentAudioPath = "";
let hasTranscription = false; // Whether speech-to-text has been run
let audioDuration = 0; // Duration from autocut/transcribe

// Segment display settings
let segLineBreakMode = "natural"; // "natural" | "word" | "punctuation" | "maxWords"
let segMaxWords = 5;              // max words per line (for "maxWords" mode)
let segTextView = false;          // true = text-only view (no badges, timestamps, etc.)

// ── Backend communication ──

async function fetchBackend(endpoint, options = {}) {
  let res;
  try {
    res = await fetch(`${BACKEND_URL}${endpoint}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (e) {
    throw new Error("Cannot connect to backend. Is the server running on port 9876?");
  }
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || data.detail || `Backend error: ${res.status}`);
  }
  return data;
}

let backendDevice = "";

async function checkBackend() {
  try {
    const data = await fetchBackend("/health");
    setConnected(data.status === "ok");
    if (data.status === "ok") {
      backendDevice = data.device || "";
      updateModelInfo(data.model);
      // Warn if ffmpeg is not found
      if (data.ffmpeg === false) {
        const info = document.getElementById("audioInfo");
        if (info) {
          info.classList.remove("hidden");
          info.innerHTML = '<span style="color:#ff6b6b">⚠ ffmpeg not found — audio detection will not work. Install: brew install ffmpeg</span>';
        }
      }
    }
  } catch {
    setConnected(false);
  }
}

function updateModelInfo(loadedModel) {
  const info = document.getElementById("modelInfo");
  const selected = document.getElementById("modelSelect").value;
  if (!info) return;

  const deviceTag = backendDevice ? ` · ${backendDevice}` : "";

  // Check cached models from backend
  fetchBackend("/models").then(data => {
    const model = (data.models || []).find(m => m.id === selected);
    const isCached = model ? model.cached : false;
    const isLoaded = loadedModel === selected;
    const size = model ? model.size : "";

    if (isLoaded) {
      info.innerHTML = `<span class="model-tag tag-loaded">READY</span> ${selected}${deviceTag}`;
    } else if (isCached) {
      info.innerHTML = `<span class="model-tag tag-loaded">CACHED</span> ${selected} — will switch on run${deviceTag}`;
    } else {
      info.innerHTML = `<span class="model-tag tag-download">DOWNLOAD</span> ${selected} (${size}) — first run will download${deviceTag}`;
    }
  }).catch(() => {
    info.innerHTML = `${selected}${deviceTag}`;
  });
}

document.getElementById("modelSelect")?.addEventListener("change", () => {
  updateModelInfo(null);
});

// (Speaker params now live in the collapsible Settings section and are
// always visible; no checkbox-gated show/hide needed.)

function setConnected(connected) {
  backendConnected = connected;
  const badge = document.getElementById("statusBadge");
  const text = document.getElementById("statusText");

  badge.className = connected ? "status-badge online" : "status-badge offline";
  text.textContent = connected ? "Connected" : "Offline";
  updateActionButtons();
}

// ── Premiere Pro UXP API helpers (async) ──

let ppro = null;
try { ppro = require("premierepro"); } catch {}

async function getActiveProject() {
  if (!ppro) return null;
  try { return await ppro.Project.getActiveProject(); }
  catch (e) { console.error("[EasyScript] getActiveProject:", e); return null; }
}

async function getActiveSequence() {
  const project = await getActiveProject();
  if (!project) return null;
  try { return await project.getActiveSequence(); }
  catch (e) { console.error("[EasyScript] getActiveSequence:", e); return null; }
}

async function getAudioTracks() {
  const seq = await getActiveSequence();
  if (!seq) return [];
  try {
    const count = await seq.getAudioTrackCount();
    const tracks = [];
    for (let i = 0; i < count; i++) {
      const track = await seq.getAudioTrack(i);
      const clips = await track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false);
      const name = await track.getName?.() || `Audio ${i + 1}`;
      tracks.push({ index: i, name, track, clipCount: clips ? clips.length : 0 });
    }
    return tracks;
  } catch (e) {
    console.error("[EasyScript] getAudioTracks:", e);
    return [];
  }
}

async function populateTrackSelect() {
  const select = document.getElementById("trackSelect");
  select.innerHTML = '<option value="">Loading tracks...</option>';
  const tracks = await getAudioTracks();
  select.innerHTML = '<option value="">Select audio track...</option>';
  const tracksWithClips = tracks.filter((t) => t.clipCount > 0);
  tracksWithClips.forEach((t) => {
    const opt = document.createElement("option");
    opt.value = t.index;
    opt.textContent = `${t.name} (${t.clipCount} clip${t.clipCount !== 1 ? "s" : ""})`;
    select.appendChild(opt);
  });
  if (tracksWithClips.length === 0) {
    select.innerHTML = '<option value="">No audio tracks with clips</option>';
  } else if (tracksWithClips.length === 1) {
    select.value = tracksWithClips[0].index;
  }
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

    const stageIcons = {
      preparing: "⏳", downloading: "⬇️", loading_model: "📦", loading_audio: "📂",
      silence: "🔇", transcribing: "🎤", diarizing: "🗣️", merging: "🔀",
      peaks: "📊", done: "✅", error: "❌",
    };
    const icon = stageIcons[stage] || "";
    stageEl.textContent = `${icon} ${detail || stage}`;

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
    this._seenProcessing = false; // Must see "processing" before accepting "done"
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

  const btn = document.getElementById("autoCutBtn");
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

    audioDuration = result.audio_duration || 0;
    const peaks = result.peaks || waveform.generateMockPeaks(audioDuration, 800);
    waveform.loadPeaks(peaks, audioDuration);

    renderSegments(segments);
    updateSegmentCount(segments);

    const cuts = getFilteredCutPoints();
    waveform.updateMarkers(cuts);
    updateCutStats();
    updateExportButtons();

    currentAudioPath = audioPath;
    audioPlayback.loadAudio(audioPath);
    showAudioInfo(result);

  } catch (err) {
    if (err.message !== "__CANCELLED__") {
      progressTracker.update(0, "error", `Error: ${err.message}`);
      alert(`Detect Silence failed: ${err.message}`);
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

async function runTranscribe(resumeFromPlayhead = false, songMode = false, songParams = null) {
  const audioPath = document.getElementById("audioPathInput").value.trim();
  if (!audioPath) return;

  const btn = document.getElementById("transcribeBtn");
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
    const body = {
      audio_path: audioPath,
      model: selectedModel,
      language: selectedLang,
      start_from: startFrom,
      song_mode: songMode,
    };
    if (songMode && songParams) {
      body.song_vad_threshold = songParams.vad_threshold;
      body.song_min_silence_ms = songParams.min_silence_ms;
      body.song_beam_size = songParams.beam_size;
    }
    await fetchBackend("/transcribe", {
      method: "POST",
      body: JSON.stringify(body),
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
        applyDiarizeResult(diarizeResult);
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
      alert(`Transcribe failed: ${err.message}`);
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
  let text = `Duration: ${dm}m ${String(ds).padStart(2, "0")}s`;
  if (result.segments) {
    const silCount = result.segments.filter(s => s.type === "silence").length;
    const breathCount = result.segments.filter(s => s.type === "breath").length;
    text += ` — ${silCount} silence, ${breathCount} breath segments`;
  }
  info.textContent = text;
  info.classList.remove("hidden");
}

function updateExportButtons() {
  const hasCuts = getFilteredCutPoints().length > 0;
  const hasSpeech = hasTranscription && segments.some(s => s.type === "speech" && s.text);

  // XML export: needs cuts from autocut
  document.getElementById("exportXmlBtn").disabled = !hasCuts;

  // SRT export: needs transcription
  document.getElementById("exportSrtOrigBtn").disabled = !hasSpeech;
  document.getElementById("exportSrtCutBtn").disabled = !hasSpeech || !hasCuts;

  // Show/hide transcribe hint
  const hint = document.getElementById("transcribeHint");
  if (hasSpeech) {
    hint.classList.add("hidden");
  } else {
    hint.classList.remove("hidden");
  }
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
    const playBtn = document.getElementById("playBtn");
    const stopBtn = document.getElementById("stopBtn");

    playBtn.addEventListener("click", () => this.togglePlay());
    stopBtn.addEventListener("click", () => this.stop());

    const skipCheck = document.getElementById("skipSilenceCheck");
    if (skipCheck) {
      skipCheck.addEventListener("change", () => { this.skipSilence = skipCheck.checked; });
    }

    this.audio.addEventListener("timeupdate", () => {
      if (this.playing && this.skipSilence && this.maybeSkipSilence(this.audio.currentTime)) {
        return; // jumped past a silence region; next timeupdate continues
      }
      waveform.setPlayhead(this.audio.currentTime);
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
  },

  loadAudio(audioPath) {
    if (isDevMode()) {
      this.audio.src = `${BACKEND_URL}/audio?path=${encodeURIComponent(audioPath)}`;
    } else {
      this.audio.src = audioPath;
    }
    this.audio.load();
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
  },

  // When "Skip silence" is on, jump over any detected silence/breath cut region
  // the playhead enters, so review only plays the kept (speech) parts.
  // Returns true if a jump happened.
  maybeSkipSilence(time) {
    const cuts = getFilteredCutPoints();
    if (!cuts || cuts.length === 0) return false;
    for (const cut of cuts) {
      // Small epsilon so we trigger right as we cross into the region.
      if (time >= cut.start - 0.02 && time < cut.end - 0.05) {
        const target = Math.min(cut.end + 0.01, this.audio.duration || cut.end);
        if (target > this.audio.currentTime) {
          this.audio.currentTime = target;
          waveform.setPlayhead(target);
          this.updateTimeUI();
          this.autoFocusSegment(target);
        }
        return true;
      }
    }
    return false;
  },

  animatePlayhead() {
    if (!this.playing) return;
    waveform.setPlayhead(this.audio.currentTime);
    this.updateTimeUI();
    this.animFrame = requestAnimationFrame(() => this.animatePlayhead());
  },

  updatePlayIcon() {
    const icon = document.getElementById("playIcon");
    icon.innerHTML = this.playing ? "&#9646;&#9646;" : "&#9654;";
  },

  updateTimeUI() {
    document.getElementById("currentTime").textContent = formatTime(this.audio.currentTime);
  },

  autoFocusSegment(time) {
    const list = document.getElementById("segmentList");

    // ── Text-view mode: highlight spans ──
    if (segTextView) {
      const spans = list.querySelectorAll(".text-view-span");
      spans.forEach(sp => sp.classList.remove("text-view-active"));
      for (const sp of spans) {
        const s = parseFloat(sp.dataset.start);
        const e = parseFloat(sp.dataset.end);
        if (time >= s && time < e) {
          sp.classList.add("text-view-active");
          const listRect = list.getBoundingClientRect();
          const spRect = sp.getBoundingClientRect();
          if (spRect.top < listRect.top || spRect.bottom > listRect.bottom) {
            sp.scrollIntoView({ block: "center", behavior: "smooth" });
          }
        }
      }
      return;
    }

    // ── Normal / split mode: highlight items by data-start/data-end ──
    const items = list.querySelectorAll(".segment-item");
    items.forEach(item => item.classList.remove("segment-active"));

    let firstMatch = null;
    for (const item of items) {
      const s = parseFloat(item.dataset.start);
      const e = parseFloat(item.dataset.end);
      if (time >= s && time < e) {
        item.classList.add("segment-active");
        if (!firstMatch) firstMatch = item;
      }
    }
    if (firstMatch) {
      const listRect = list.getBoundingClientRect();
      const itemRect = firstMatch.getBoundingClientRect();
      if (itemRect.top < listRect.top || itemRect.bottom > listRect.bottom) {
        firstMatch.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    }
  },
};

// ── Zoom State ──

const zoomState = {
  level: 1,
  levels: [1, 2, 4, 8, 16, 32, 64],

  init() {
    document.getElementById("zoomInBtn").addEventListener("click", () => this.zoomIn());
    document.getElementById("zoomOutBtn").addEventListener("click", () => this.zoomOut());
    document.getElementById("zoomFitBtn").addEventListener("click", () => this.zoomFit());
  },

  zoomIn() {
    const idx = this.levels.indexOf(this.level);
    if (idx < this.levels.length - 1) this.setZoom(this.levels[idx + 1]);
  },

  zoomOut() {
    const idx = this.levels.indexOf(this.level);
    if (idx > 0) this.setZoom(this.levels[idx - 1]);
  },

  zoomFit() { this.setZoom(1); },

  setZoom(level) {
    const scroll = document.getElementById("waveformScroll");
    const wrap = document.getElementById("waveformWrap");
    const overview = document.getElementById("waveformOverview");

    // ── Preserve the current viewing position when zooming ──
    // Anchor on the playhead if it is inside the current viewport; otherwise
    // anchor on the center of what the user is currently looking at.
    const oldScrollW = wrap.scrollWidth || scroll.clientWidth || 1;
    let anchorRatio = (scroll.scrollLeft + scroll.clientWidth / 2) / oldScrollW;
    if (waveform.duration > 0) {
      const phRatio = waveform.playheadPos / waveform.duration;
      const phX = phRatio * oldScrollW;
      if (phX >= scroll.scrollLeft && phX <= scroll.scrollLeft + scroll.clientWidth) {
        anchorRatio = phRatio;
      }
    }
    anchorRatio = Math.max(0, Math.min(1, anchorRatio));

    this.level = level;
    document.getElementById("zoomLevel").textContent = `${level}x`;
    const containerWidth = scroll.clientWidth;
    wrap.style.width = level === 1 ? "100%" : `${containerWidth * level}px`;
    if (level > 1) { overview.classList.remove("hidden"); }
    else { overview.classList.add("hidden"); }
    waveform.draw();

    // Restore the anchor point, centered in the viewport.
    const newScrollW = wrap.scrollWidth || containerWidth;
    if (level > 1) {
      scroll.scrollLeft = anchorRatio * newScrollW - scroll.clientWidth / 2;
    }

    overviewMinimap.draw();
    overviewMinimap.updateViewport();
  },
};

// ── Overview Minimap ──

const overviewMinimap = {
  canvas: null, ctx: null, dragging: false,

  init() {
    this.canvas = document.getElementById("overviewCanvas");
    this.ctx = this.canvas.getContext("2d");
    const overviewEl = document.getElementById("waveformOverview");
    const scroll = document.getElementById("waveformScroll");
    overviewEl.addEventListener("mousedown", (e) => { this.dragging = true; this.handleClick(e); });
    document.addEventListener("mousemove", (e) => { if (this.dragging) this.handleClick(e); });
    document.addEventListener("mouseup", () => { this.dragging = false; });
    scroll.addEventListener("scroll", () => this.updateViewport());
  },

  handleClick(e) {
    const rect = document.getElementById("waveformOverview").getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const scroll = document.getElementById("waveformScroll");
    const wrap = document.getElementById("waveformWrap");
    scroll.scrollLeft = ratio * wrap.scrollWidth - scroll.clientWidth / 2;
  },

  updateViewport() {
    const scroll = document.getElementById("waveformScroll");
    const wrap = document.getElementById("waveformWrap");
    const viewport = document.getElementById("overviewViewport");
    const overview = document.getElementById("waveformOverview");
    if (zoomState.level <= 1) return;
    const overviewW = overview.clientWidth;
    viewport.style.left = `${(scroll.scrollLeft / wrap.scrollWidth) * overviewW}px`;
    viewport.style.width = `${(scroll.clientWidth / wrap.scrollWidth) * overviewW}px`;
  },

  draw() {
    if (!this.canvas || !this.ctx || waveform.peaks.length === 0 || zoomState.level <= 1) return;
    const el = document.getElementById("waveformOverview");
    const rect = el.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = rect.width, h = rect.height;
    this.canvas.width = w * dpr; this.canvas.height = h * dpr;
    this.ctx.scale(dpr, dpr);
    const ctx = this.ctx;
    ctx.fillStyle = waveform.colors.bg; ctx.fillRect(0, 0, w, h);
    const step = 1, totalBars = Math.floor(w / step), mid = h / 2;
    for (let i = 0; i < totalBars; i++) {
      const peakIdx = Math.floor((i / totalBars) * waveform.peaks.length);
      const val = waveform.peaks[peakIdx] || 0;
      const barH = Math.max(0.5, val * (h * 0.4));
      const x = i * step;
      const time = (i / totalBars) * waveform.duration;
      const seg = segments.find((s) => time >= s.start && time < s.end);
      const type = seg ? seg.type : "speech";
      let col = waveform.colors[type] || waveform.colors.speech;
      if (type === "speech" && hasSpeakers && seg && seg.speaker) {
        col = SPEAKER_WAVE_COLORS[getSpeakerColorIndex(seg.speaker)] || col;
      }
      ctx.fillStyle = col;
      ctx.globalAlpha = 0.6;
      ctx.fillRect(x, mid - barH, 1, barH);
      ctx.fillRect(x, mid, 1, barH);
    }
    ctx.globalAlpha = 1;
  },
};

// ── Waveform renderer ──

const waveform = {
  canvas: null, ctx: null, wrap: null,
  peaks: [], duration: 0, playheadPos: 0, cutMarkers: [],
  colors: {
    speech: "#3ddc84", silence: "#f0a830", breath: "#f06050", bg: "#1a1a1a",
    markerLine: "rgba(255,255,255,0.9)", markerFill: "rgba(255,255,255,0.08)",
    grid: "rgba(255,255,255,0.04)", gridText: "rgba(255,255,255,0.2)",
  },

  init() {
    this.canvas = document.getElementById("waveformCanvas");
    this.ctx = this.canvas.getContext("2d");
    this.wrap = document.getElementById("waveformWrap");
    this.setupEvents();
  },

  setupEvents() {
    this.wrap.addEventListener("click", async (e) => {
      const rect = this.wrap.getBoundingClientRect();
      const ratio = (e.clientX - rect.left) / rect.width;
      const time = ratio * this.duration;
      this.setPlayhead(time);
      audioPlayback.seekTo(time);
      if (!isDevMode()) {
        try {
          const seq = await getActiveSequence();
          if (seq) await seq.setPlayerPosition(Math.round(time * 254016000000).toString());
        } catch (e) { console.error("[EasyScript] waveform seek:", e); }
      }
    });
    this.wrap.addEventListener("mousemove", (e) => {
      const rect = this.wrap.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const time = (x / rect.width) * this.duration;
      const tooltip = document.getElementById("waveformTooltip");
      tooltip.classList.remove("hidden");
      tooltip.textContent = formatTime(time);
      tooltip.style.left = `${x + 10 > rect.width - 50 ? x - 56 : x + 10}px`;
    });
    this.wrap.addEventListener("mouseleave", () => {
      document.getElementById("waveformTooltip").classList.add("hidden");
    });
    new ResizeObserver(() => this.draw()).observe(this.wrap);
  },

  loadPeaks(peaks, duration) {
    this.peaks = peaks; this.duration = duration;
    this.cutMarkers = [];
    this.updateTimeDisplay();
    document.getElementById("totalTime").textContent = formatTime(duration);
    zoomState.zoomFit();
    this.draw();
  },

  setPlayhead(time) {
    this.playheadPos = time;
    const ratio = this.duration > 0 ? time / this.duration : 0;
    document.getElementById("waveformPlayhead").style.left = `${ratio * 100}%`;
    this.updateTimeDisplay();
    if (zoomState.level > 1) {
      const scroll = document.getElementById("waveformScroll");
      const wrap = document.getElementById("waveformWrap");
      const playheadX = ratio * wrap.scrollWidth;
      const viewLeft = scroll.scrollLeft;
      const viewRight = viewLeft + scroll.clientWidth;
      const margin = scroll.clientWidth * 0.15;
      if (playheadX < viewLeft + margin || playheadX > viewRight - margin) {
        scroll.scrollLeft = playheadX - scroll.clientWidth / 2;
      }
      overviewMinimap.updateViewport();
    }
  },

  updateTimeDisplay() {
    const el = document.getElementById("waveformTime");
    if (el) el.textContent = `${formatTime(this.playheadPos)} / ${formatTime(this.duration)}`;
  },

  updateMarkers(cuts) { this.cutMarkers = cuts; this.draw(); },

  draw() {
    if (!this.canvas || !this.ctx || this.peaks.length === 0) return;
    const rect = this.wrap.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = rect.width, h = rect.height;
    this.canvas.width = w * dpr; this.canvas.height = h * dpr;
    this.ctx.scale(dpr, dpr);
    const ctx = this.ctx;
    ctx.fillStyle = this.colors.bg; ctx.fillRect(0, 0, w, h);
    this.drawTimeGrid(ctx, w, h);
    this.drawSegmentRegions(ctx, w, h);
    this.drawWaveformBars(ctx, w, h);
    this.drawCutMarkers(ctx, w, h);
    ctx.strokeStyle = "rgba(255,255,255,0.08)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, h/2); ctx.lineTo(w, h/2); ctx.stroke();
  },

  drawTimeGrid(ctx, w, h) {
    if (this.duration <= 0) return;
    const targetCount = Math.floor(w / 60);
    const rawInterval = this.duration / targetCount;
    const niceIntervals = [0.5, 1, 2, 5, 10, 15, 30, 60];
    const interval = niceIntervals.find((i) => i >= rawInterval) || 60;
    ctx.fillStyle = this.colors.gridText;
    ctx.font = "9px SF Mono, Menlo, Consolas, monospace";
    ctx.textAlign = "center";
    for (let t = interval; t < this.duration; t += interval) {
      const x = (t / this.duration) * w;
      ctx.strokeStyle = this.colors.grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      ctx.fillText(formatTime(t), x, h - 3);
    }
  },

  drawSegmentRegions(ctx, w, h) {
    segments.forEach((seg) => {
      if (seg.type === "speech") return;
      const x1 = (seg.start / this.duration) * w;
      const x2 = (seg.end / this.duration) * w;
      const regionW = x2 - x1;
      if (regionW < 0.5) return; // Skip sub-pixel regions

      if (seg.type === "silence") {
        ctx.fillStyle = "rgba(240, 168, 48, 0.18)"; // amber, stronger
      } else {
        ctx.fillStyle = "rgba(240, 96, 80, 0.22)"; // red/breath, stronger
      }
      ctx.fillRect(x1, 0, regionW, h);

      // Top color bar for visibility
      ctx.fillStyle = seg.type === "silence" ? this.colors.silence : this.colors.breath;
      ctx.globalAlpha = 0.6;
      ctx.fillRect(x1, 0, regionW, 3);
      ctx.globalAlpha = 1;
    });
  },

  drawWaveformBars(ctx, w, h) {
    const barWidth = 2, gap = 1, step = barWidth + gap;
    const totalBars = Math.floor(w / step), mid = h / 2;

    // Build a lookup for faster segment matching
    const segLookup = this._buildSegLookup(totalBars);

    for (let i = 0; i < totalBars; i++) {
      const peakIdx = Math.floor((i / totalBars) * this.peaks.length);
      const val = this.peaks[peakIdx] || 0;
      const barH = Math.max(1, val * (h * 0.42));
      const x = i * step;
      const seg = segLookup[i];
      const type = seg ? seg.type : "speech";
      let color = this.colors[type] || this.colors.speech;
      // After diarization, tint speech bars by their speaker.
      if (type === "speech" && hasSpeakers && seg && seg.speaker) {
        color = SPEAKER_WAVE_COLORS[getSpeakerColorIndex(seg.speaker)] || color;
      }
      ctx.fillStyle = color;
      ctx.globalAlpha = type === "speech" ? 0.85 : 0.5;
      ctx.fillRect(x, mid - barH, barWidth, barH);
      ctx.fillRect(x, mid, barWidth, barH);
    }
    ctx.globalAlpha = 1;
  },

  // Pre-compute the segment per bar for O(n) instead of O(n*m)
  _buildSegLookup(totalBars) {
    const lookup = new Array(totalBars);
    if (segments.length === 0 || this.duration <= 0) return lookup;

    let segIdx = 0;
    for (let i = 0; i < totalBars; i++) {
      const time = (i / totalBars) * this.duration;
      // Advance segment index
      while (segIdx < segments.length && segments[segIdx].end <= time) segIdx++;
      if (segIdx < segments.length && segments[segIdx].start <= time) {
        lookup[i] = segments[segIdx];
      }
    }
    return lookup;
  },

  drawCutMarkers(ctx, w, h) {
    const count = this.cutMarkers.length;
    // Adapt detail level based on marker density
    const minPixelWidth = 3; // Minimum pixel width to draw individual markers

    this.cutMarkers.forEach((cut) => {
      const x1 = (cut.start / this.duration) * w;
      const x2 = (cut.end / this.duration) * w;
      const markerW = x2 - x1;

      if (markerW < 0.5) return; // Skip sub-pixel

      // Shaded region (always draw)
      ctx.fillStyle = "rgba(255, 255, 255, 0.06)";
      ctx.fillRect(x1, 0, markerW, h);

      // Boundary lines — only when markers are wide enough
      if (markerW >= minPixelWidth) {
        ctx.strokeStyle = "rgba(255, 255, 255, 0.5)";
        ctx.lineWidth = 0.5;
        ctx.setLineDash([2, 3]);
        ctx.beginPath();
        ctx.moveTo(x1, 0); ctx.lineTo(x1, h);
        if (markerW > 6) { // Only draw end line if wide enough
          ctx.moveTo(x2, 0); ctx.lineTo(x2, h);
        }
        ctx.stroke();
        ctx.setLineDash([]);
      }

      // Scissors icon — only when zoomed in enough (marker > 20px wide)
      if (markerW > 20) {
        ctx.fillStyle = "rgba(255, 255, 255, 0.7)";
        ctx.font = "8px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("✂", (x1 + x2) / 2, 10);
      }
    });
  },

  generateMockPeaks(duration, sampleCount = 500) {
    const peaks = [];
    for (let i = 0; i < sampleCount; i++) {
      const t = (i / sampleCount) * duration;
      const seg = segments.find((s) => t >= s.start && t < s.end);
      let base;
      if (!seg || seg.type === "silence") { base = 0.02 + Math.random() * 0.04; }
      else if (seg.type === "breath") { base = 0.08 + Math.random() * 0.12; }
      else { base = 0.25 + Math.random() * 0.65; }
      const prev = peaks[i - 1] || base;
      peaks.push(prev * 0.3 + base * 0.7);
    }
    return peaks;
  },
};

// ── Segment rendering ──

function formatTime(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  const ms = Math.round((seconds % 1) * 100);
  return `${m}:${String(s).padStart(2, "0")}.${String(ms).padStart(2, "0")}`;
}

function updateSegmentCount(segs) {
  const el = document.getElementById("segmentCount");
  if (el) el.textContent = `${segs.length} found`;
}

let currentFilter = "all";
let speakerMap = {};  // { "SPEAKER_00": "Speaker A", ... }
let hasSpeakers = false;

function getSpeakerColorIndex(speakerId) {
  const speakers = Object.keys(speakerMap);
  const idx = speakers.indexOf(speakerId);
  return idx >= 0 ? idx % 6 : 0;
}

// Solid speaker colors for the waveform canvas — matches the speaker-tag
// palette (data-color 0..5) used in the Segments/Translation views.
const SPEAKER_WAVE_COLORS = [
  "#4dcafa", // code blue
  "#e96b34", // vapi orange
  "#9977ff", // electric violet
  "#62f6b5", // vapi mint
  "#ffdd03", // vivid yellow
  "#de94e2", // neon pink
];

/**
 * Split a speech segment's text based on display settings.
 * Returns an array of sub-segments (each with start/end/text/speaker etc.)
 */
function splitSegmentForDisplay(seg) {
  if (seg.type !== "speech" || !seg.text) return [seg];

  const text = seg.text.trim();
  if (!text) return [seg];
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

function reRenderCurrentSegments() {
  if (currentFilter === "translation") {
    renderTranslationSegments();
  } else {
    const filtered = currentFilter === "all"
      ? segments
      : segments.filter(s => s.type === currentFilter);
    renderSegments(filtered);
  }
}

function renderSegments(segs) {
  const list = document.getElementById("segmentList");
  list.innerHTML = "";
  list.classList.toggle("text-view", segTextView);

  if (currentFilter === "translation") {
    renderTranslationSegments();
    return;
  }

  if (segs.length === 0) {
    list.innerHTML = '<div class="segment-empty">No segments match filter</div>';
    return;
  }

  // ── Text-only view ──
  if (segTextView) {
    const speechSegs = segs.filter(s => s.type === "speech" && s.text);
    if (speechSegs.length === 0) {
      list.innerHTML = '<div class="segment-empty">No speech segments</div>';
      return;
    }

    // Check if any segment has speaker info
    const hasSpeakers = speechSegs.some(s => s.speaker && speakerMap[s.speaker]);

    if (hasSpeakers) {
      // Group consecutive segments by speaker, each group on its own line
      let currentSpeaker = null;
      let currentBlock = null;

      speechSegs.forEach((seg) => {
        const speaker = seg.speaker || "";
        if (speaker !== currentSpeaker) {
          // New speaker → new line
          currentSpeaker = speaker;
          currentBlock = document.createElement("div");
          currentBlock.className = "text-view-block text-view-speaker-block";

          // Speaker label
          if (speaker && speakerMap[speaker]) {
            const colorIdx = getSpeakerColorIndex(speaker);
            const label = document.createElement("span");
            label.className = "text-view-speaker-label";
            label.dataset.color = colorIdx;
            label.textContent = speakerMap[speaker];
            currentBlock.appendChild(label);
          }

          list.appendChild(currentBlock);
        }

        const span = document.createElement("span");
        span.className = "text-view-span";
        span.dataset.start = seg.start;
        span.dataset.end = seg.end;
        span.textContent = seg.text.trim();
        span.title = `${formatTime(seg.start)} – ${formatTime(seg.end)}`;
        span.addEventListener("click", () => seekToSegment(seg));
        currentBlock.appendChild(span);
        currentBlock.appendChild(document.createTextNode(" "));
      });
    } else {
      // No speakers → flowing paragraph
      const block = document.createElement("div");
      block.className = "text-view-block";
      speechSegs.forEach((seg) => {
        const span = document.createElement("span");
        span.className = "text-view-span";
        span.dataset.start = seg.start;
        span.dataset.end = seg.end;
        span.textContent = seg.text.trim();
        span.title = `${formatTime(seg.start)} – ${formatTime(seg.end)}`;
        span.addEventListener("click", () => seekToSegment(seg));
        block.appendChild(span);
        block.appendChild(document.createTextNode(" "));
      });
      list.appendChild(block);
    }
    return;
  }

  // ── Normal view ──
  // Apply display splitting based on mode
  const needsSplit = segLineBreakMode !== "natural";
  let displaySegs = segs;
  if (needsSplit) {
    displaySegs = [];
    segs.forEach((seg, i) => {
      const tagged = { ...seg, _origIndex: i };
      const splits = splitSegmentForDisplay(tagged);
      displaySegs.push(...splits);
    });
  }

  displaySegs.forEach((seg, i) => {
    const origIndex = seg._origIndex != null ? seg._origIndex : i;
    const item = document.createElement("div");
    item.className = "segment-item";
    item.dataset.index = origIndex;
    item.dataset.start = seg.start;
    item.dataset.end = seg.end;

    // Build speaker tag if available
    let speakerHtml = "";
    if (seg.speaker && speakerMap[seg.speaker]) {
      const colorIdx = getSpeakerColorIndex(seg.speaker);
      const label = speakerMap[seg.speaker] || seg.speaker;
      speakerHtml = `<span class="speaker-tag" data-speaker="${seg.speaker}" data-color="${colorIdx}" title="Click to rename">${label}</span>`;
    }

    // Build text element — editable for speech segments (only if not display-split)
    let textHtml = "";
    if (seg.type === "speech") {
      if (seg._displaySplit) {
        textHtml = `<div class="segment-text">${seg.text || ""}</div>`;
      } else {
        textHtml = `<div class="segment-text" contenteditable="true" data-seg-index="${origIndex}">${seg.text || ""}</div>`;
      }
    } else if (seg.text) {
      textHtml = `<div class="segment-text">${seg.text}</div>`;
    }

    item.innerHTML = `
      <div class="segment-row">
        <span class="segment-badge ${seg.type}">${seg.type}</span>
        ${speakerHtml}
        <span class="segment-time">${formatTime(seg.start)} – ${formatTime(seg.end)}</span>
      </div>
      ${textHtml}
    `;

    // Click row (not text) to seek
    item.querySelector(".segment-row").addEventListener("click", () => seekToSegment(seg));

    // Speaker tag click → inline rename
    const tagEl = item.querySelector(".speaker-tag");
    if (tagEl) {
      tagEl.addEventListener("click", (e) => {
        e.stopPropagation();
        startSpeakerRename(tagEl);
      });
    }

    // Save edits on blur (only for non-split items)
    const textEl = item.querySelector(".segment-text[contenteditable]");
    if (textEl) {
      textEl.addEventListener("blur", () => {
        const idx = parseInt(textEl.dataset.segIndex);
        const origSeg = segments[idx];
        if (origSeg) origSeg.text = textEl.textContent.trim();
      });
      textEl.addEventListener("click", (e) => e.stopPropagation());
    }

    list.appendChild(item);
  });
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

  const finishRename = () => {
    const newLabel = input.value.trim() || currentLabel;
    speakerMap[speakerId] = newLabel;

    // Update ALL segments with this speaker
    segments.forEach(seg => {
      if (seg.speaker === speakerId) {
        seg.speakerLabel = newLabel;
      }
    });

    // Re-render
    const filtered = currentFilter === "all" ? segments : segments.filter(s => s.type === currentFilter);
    renderSegments(filtered);
  };

  input.addEventListener("blur", finishRename);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    if (e.key === "Escape") { input.value = currentLabel; input.blur(); }
  });
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
    const hasSpeakers = speechSegs.some(s => s.speaker && speakerMap[s.speaker]);

    // Helper to build a paragraph of spans from a slice of segments
    function buildSpans(segs, useTranslation) {
      const frag = document.createDocumentFragment();
      segs.forEach((segRef) => {
        const { seg, idx } = segRef;
        const text = useTranslation
          ? (langData[idx] ? langData[idx].text : "")
          : seg.text.trim();
        if (!text) return;
        const span = document.createElement("span");
        span.className = "text-view-span";
        span.dataset.start = seg.start;
        span.dataset.end = seg.end;
        span.textContent = text.trim();
        span.title = `${formatTime(seg.start)} – ${formatTime(seg.end)}`;
        span.addEventListener("click", () => seekToSegment(seg));
        frag.appendChild(span);
        frag.appendChild(document.createTextNode(" "));
      });
      return frag;
    }

    if (hasSpeakers) {
      // Group consecutive segments by speaker — each group has its own block
      // showing speaker label + original + translation
      let currentSpeaker = null;
      let currentGroup = [];

      const flushGroup = () => {
        if (currentGroup.length === 0) return;
        const block = document.createElement("div");
        block.className = "text-view-block text-view-speaker-block text-view-trans";

        // Speaker label
        const spk = currentGroup[0].seg.speaker;
        if (spk && speakerMap[spk]) {
          const colorIdx = getSpeakerColorIndex(spk);
          const label = document.createElement("span");
          label.className = "text-view-speaker-label";
          label.dataset.color = colorIdx;
          label.textContent = speakerMap[spk];
          block.appendChild(label);
        }

        // Original
        const origP = document.createElement("p");
        origP.className = "text-view-original";
        origP.appendChild(buildSpans(currentGroup, false));
        block.appendChild(origP);

        // Translation
        const transP = document.createElement("p");
        transP.className = "text-view-translation";
        transP.appendChild(buildSpans(currentGroup, true));
        block.appendChild(transP);

        list.appendChild(block);
        currentGroup = [];
      };

      speechSegs.forEach((seg, i) => {
        const speaker = seg.speaker || "";
        if (speaker !== currentSpeaker) {
          flushGroup();
          currentSpeaker = speaker;
        }
        currentGroup.push({ seg, idx: i });
      });
      flushGroup();
    } else {
      // No speakers → single block
      const block = document.createElement("div");
      block.className = "text-view-block text-view-trans";
      const allRefs = speechSegs.map((seg, i) => ({ seg, idx: i }));
      const origP = document.createElement("p");
      origP.className = "text-view-original";
      origP.appendChild(buildSpans(allRefs, false));
      block.appendChild(origP);
      const transP = document.createElement("p");
      transP.className = "text-view-translation";
      transP.appendChild(buildSpans(allRefs, true));
      block.appendChild(transP);
      list.appendChild(block);
    }
    return;
  }

  // ── Normal view ──
  speechSegs.forEach((seg, i) => {
    const item = document.createElement("div");
    item.className = "segment-item";
    item.dataset.start = seg.start;
    item.dataset.end = seg.end;

    const transText = langData[i] ? langData[i].text : "";

    // Build speaker tag if available (matches Speech tab behavior)
    let speakerHtml = "";
    if (seg.speaker && speakerMap[seg.speaker]) {
      const colorIdx = getSpeakerColorIndex(seg.speaker);
      const label = speakerMap[seg.speaker] || seg.speaker;
      speakerHtml = `<span class="speaker-tag" data-speaker="${seg.speaker}" data-color="${colorIdx}" title="Click to rename">${label}</span>`;
    }

    item.innerHTML = `
      <div class="segment-row">
        <span class="segment-badge speech">speech</span>
        ${speakerHtml}
        <span class="segment-time">${formatTime(seg.start)} – ${formatTime(seg.end)}</span>
      </div>
      <div class="segment-text trans-original">${seg.text}</div>
      <div class="trans-text-row">
        <div class="segment-text translation-text" contenteditable="true" data-trans-index="${i}"
             data-trans-lang="${activeTransLang}"
             placeholder="Translation (${activeTransLang.toUpperCase()})...">${transText}</div>
        <button class="trans-retranslate" data-idx="${i}" data-lang="${activeTransLang}" title="Re-translate this segment">↻</button>
      </div>
    `;

    item.querySelector(".segment-row").addEventListener("click", () => seekToSegment(seg));

    // Speaker tag click → inline rename (same as Speech tab)
    const tagEl = item.querySelector(".speaker-tag");
    if (tagEl) {
      tagEl.addEventListener("click", (e) => {
        e.stopPropagation();
        startSpeakerRename(tagEl);
      });
    }

    const transEl = item.querySelector(".translation-text");
    transEl.addEventListener("blur", () => {
      const idx = parseInt(transEl.dataset.transIndex);
      const lang = transEl.dataset.transLang;
      if (translationData[lang] && translationData[lang][idx]) {
        translationData[lang][idx].text = transEl.textContent.trim();
      }
    });
    transEl.addEventListener("click", (e) => e.stopPropagation());

    // Per-row translate icon
    const retranslateBtn = item.querySelector(".trans-retranslate");
    retranslateBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const idx = parseInt(retranslateBtn.dataset.idx);
      const lang = retranslateBtn.dataset.lang;
      translateSingleSegment(idx, lang);
    });

    list.appendChild(item);
  });
}

async function seekToSegment(seg) {
  audioPlayback.seekTo(seg.start);
  if (!isDevMode()) {
    try {
      const seq = await getActiveSequence();
      if (seq) await seq.setPlayerPosition(Math.round(seg.start * 254016000000).toString());
    } catch (e) { console.error("[EasyScript] seekToSegment:", e); }
  }
}

// ── Filter tabs ──

document.querySelectorAll(".filter-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".filter-tab").forEach((t) => t.classList.remove("active"));
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
  const btn = document.getElementById("translateBtn");
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

document.getElementById("translateDialogCancel").addEventListener("click", () => {
  document.getElementById("translateDialog").classList.add("hidden");
});

document.getElementById("translateDialogConfirm").addEventListener("click", () => {
  const dialog = document.getElementById("translateDialog");
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

  clearSearchHighlights();
  if (!query) return;

  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const regex = new RegExp(`(${escaped})`, "gi");

  // Choose target elements based on active tab
  let textEls;
  if (currentFilter === "translation") {
    textEls = document.querySelectorAll(".translation-text[contenteditable='true']");
  } else {
    textEls = document.querySelectorAll(".segment-text[contenteditable='true']:not(.translation-text)");
  }

  textEls.forEach(el => {
    const text = el.textContent;
    const allMatches = [...text.matchAll(new RegExp(escaped, "gi"))];
    if (allMatches.length > 0) {
      allMatches.forEach(m => {
        searchMatches.push({ el, index: m.index, length: m[0].length });
      });
      el.innerHTML = text.replace(regex, '<mark class="search-highlight">$1</mark>');
    }
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

  const match = searchMatches[searchMatchIndex];
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const regex = new RegExp(escaped, "i");

  if (currentFilter === "translation") {
    // Replace in translationData
    const idx = parseInt(match.el.dataset.transIndex);
    const lang = match.el.dataset.transLang;
    if (!isNaN(idx) && translationData[lang] && translationData[lang][idx]) {
      translationData[lang][idx].text = translationData[lang][idx].text.replace(regex, replacement);
    }
    renderSegments(segments);
  } else {
    const segIndex = match.el.dataset.segIndex;
    if (segIndex !== undefined) {
      const seg = segments[parseInt(segIndex)];
      if (seg) seg.text = seg.text.replace(regex, replacement);
    }
    const filtered = currentFilter === "all" ? segments : segments.filter(s => s.type === currentFilter);
    renderSegments(filtered);
  }
  runSearch();
}

function replaceAll() {
  const query = document.getElementById("searchInput").value.trim();
  const replacement = document.getElementById("replaceInput").value;
  if (!query) return;

  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const regex = new RegExp(escaped, "gi");

  if (currentFilter === "translation" && activeTransLang) {
    const langData = translationData[activeTransLang];
    if (langData) {
      langData.forEach(t => { if (t.text) t.text = t.text.replace(regex, replacement); });
    }
    renderSegments(segments);
  } else {
    segments.forEach(seg => {
      if (seg.type === "speech" && seg.text) seg.text = seg.text.replace(regex, replacement);
    });
    const filtered = currentFilter === "all" ? segments : segments.filter(s => s.type === currentFilter);
    renderSegments(filtered);
  }
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

  return segments
    .filter((s) => s.type !== "speech" && (s.end - s.start) >= minSilence)
    .map((s) => ({
      start: Math.max(0, s.start + paddingBefore),
      end: s.end - paddingAfter,
      type: s.type,
    }))
    .filter((s) => s.end > s.start);
}

async function previewCuts() {
  const cuts = getFilteredCutPoints();
  waveform.updateMarkers(cuts);
  updateCutStats();
  updateExportButtons();

  if (!isDevMode()) {
    try {
      const seq = await getActiveSequence();
      const project = await getActiveProject();
      if (seq && project && cuts.length > 0) {
        const markers = await seq.getMarkers();
        for (const cut of cuts) {
          const startTicks = Math.round(cut.start * 254016000000).toString();
          const durTicks = Math.round((cut.end - cut.start) * 254016000000).toString();
          const action = markers.createAddMarkerAction(
            cut.type === "breath" ? "Breath" : "Silence",
            "Comment", startTicks, durTicks, `${cut.type} — cut point`
          );
          await project.executeTransaction(action);
        }
      }
    } catch (e) { console.error("[EasyScript] previewCuts markers:", e); }
  }
}

// ── FCP XML Export ──

function getKeptSegments() {
  const cuts = getFilteredCutPoints();
  if (cuts.length === 0 && !hasSpeakers) return [{ start: 0, end: waveform.duration }];

  const sortedCuts = [...cuts].sort((a, b) => a.start - b.start);

  // Step 1: Build kept ranges by removing silence/breath cuts
  let rawKept = [];
  let cursor = 0;
  for (const cut of sortedCuts) {
    if (cut.start > cursor) rawKept.push({ start: cursor, end: cut.start });
    cursor = Math.max(cursor, cut.end);
  }
  if (cursor < waveform.duration) rawKept.push({ start: cursor, end: waveform.duration });
  if (rawKept.length === 0) rawKept = [{ start: 0, end: waveform.duration }];

  // Step 2: If speakers detected, cut ONLY where the speaker changes.
  // A single speaker talking with natural pauses between sentences should
  // stay as ONE clip — we do NOT cut at every silence gap within one
  // speaker's turn. Cuts happen exclusively at speaker-change boundaries.
  if (!hasSpeakers) return rawKept;

  const speechSegs = segments
    .filter(s => s.type === "speech" && s.speaker)
    .sort((a, b) => a.start - b.start);
  if (speechSegs.length === 0) return rawKept;

  // Group consecutive same-speaker segments into speaker turns.
  const turns = [];
  let cur = { speaker: speechSegs[0].speaker, start: speechSegs[0].start, end: speechSegs[0].end };
  for (let i = 1; i < speechSegs.length; i++) {
    const s = speechSegs[i];
    if (s.speaker === cur.speaker) {
      cur.end = Math.max(cur.end, s.end);
    } else {
      turns.push(cur);
      cur = { speaker: s.speaker, start: s.start, end: s.end };
    }
  }
  turns.push(cur);

  // One clip per speaker turn. Cut points sit at the midpoint between the end
  // of one turn and the start of the next (i.e. exactly where the speaker
  // changes), and the clips cover the full timeline with no internal cuts.
  const result = [];
  for (let i = 0; i < turns.length; i++) {
    const start = i === 0 ? 0 : (turns[i - 1].end + turns[i].start) / 2;
    const end = i === turns.length - 1
      ? waveform.duration
      : (turns[i].end + turns[i + 1].start) / 2;
    result.push({ start, end, speaker: turns[i].speaker });
  }

  return result;
}

function generateFCPXML(enableMulticam = false) {
  const audioPath = document.getElementById("audioPathInput").value.trim();
  const audioFileName = audioPath.split("/").pop().split("\\").pop();
  const baseName = audioFileName.replace(/\.[^.]+$/, "");
  const duration = waveform.duration;
  const kept = getKeptSegments();
  const cuts = getFilteredCutPoints();
  const timebase = 30;
  const ntsc = false;

  function secToFrames(sec) { return Math.round(sec * timebase); }

  const totalDurFrames = secToFrames(duration);
  const totalOutFrames = kept.reduce((sum, s) => sum + secToFrames(s.end - s.start), 0);
  const rateBlock = `<rate><timebase>${timebase}</timebase><ntsc>${ntsc ? "TRUE" : "FALSE"}</ntsc></rate>`;

  // Speaker → camera mapping
  const uniqueSpeakers = Object.keys(speakerMap);
  const speakerToCamera = {};
  uniqueSpeakers.forEach((spk, i) => { speakerToCamera[spk] = i + 1; });
  const useMulticam = enableMulticam && uniqueSpeakers.length > 1;
  const labelColors = ["Iris", "Caribbean", "Lavender", "Lemon", "Rose", "Mango", "Cerulean", "Forest"];

  // ── [Source] sequence — user replaces media here ──
  // When multicam: one video track per camera/speaker
  // When normal: single video track
  let sourceVideoTracks = "";
  if (useMulticam) {
    uniqueSpeakers.forEach((spk, i) => {
      const label = speakerMap[spk] || `Speaker ${String.fromCharCode(65 + i)}`;
      const color = labelColors[i % labelColors.length];
      sourceVideoTracks += `
          <track>
            <clipitem id="source-cam-${i + 1}">
              <name>${label} — Camera ${i + 1}</name>
              <duration>${totalDurFrames}</duration>${rateBlock}
              <start>0</start><end>${totalDurFrames}</end><in>0</in><out>${totalDurFrames}</out>
              <file id="file-1">
                <name>${audioFileName}</name><pathurl>file://localhost${audioPath}</pathurl>
                <duration>${totalDurFrames}</duration>${rateBlock}
                <media>
                  <video><samplecharacteristics><width>1920</width><height>1080</height></samplecharacteristics></video>
                  <audio><channelcount>2</channelcount><samplecharacteristics><depth>16</depth><samplerate>48000</samplerate></samplecharacteristics></audio>
                </media>
              </file>
              <labels><label2>${color}</label2></labels>
            </clipitem>
          </track>`;
    });
  } else {
    sourceVideoTracks = `
          <track>
            <clipitem id="source-video">
              <name>${audioFileName}</name><duration>${totalDurFrames}</duration>${rateBlock}
              <start>0</start><end>${totalDurFrames}</end><in>0</in><out>${totalDurFrames}</out>
              <file id="file-1">
                <name>${audioFileName}</name><pathurl>file://localhost${audioPath}</pathurl>
                <duration>${totalDurFrames}</duration>${rateBlock}
                <media>
                  <video><samplecharacteristics><width>1920</width><height>1080</height></samplecharacteristics></video>
                  <audio><channelcount>2</channelcount><samplecharacteristics><depth>16</depth><samplerate>48000</samplerate></samplecharacteristics></audio>
                </media>
              </file>
            </clipitem>
          </track>`;
  }

  const speakerNote = useMulticam
    ? ` — ${uniqueSpeakers.length} cameras — Replace each track with camera footage`
    : " — Replace media here";

  const sourceSeq = `
    <sequence id="source-seq">
      <name>[Source] ${baseName}${speakerNote}</name>
      <duration>${totalDurFrames}</duration>
      ${rateBlock}
      <media>
        <video>
          <format><samplecharacteristics><width>1920</width><height>1080</height><pixelaspectratio>Square</pixelaspectratio></samplecharacteristics></format>${sourceVideoTracks}
        </video>
        <audio>
          <numOutputChannels>2</numOutputChannels>
          <format><samplecharacteristics><depth>16</depth><samplerate>48000</samplerate></samplecharacteristics></format>
          <track>
            <clipitem id="source-audio">
              <name>${audioFileName}</name><duration>${totalDurFrames}</duration>${rateBlock}
              <start>0</start><end>${totalDurFrames}</end><in>0</in><out>${totalDurFrames}</out>
              <file id="file-1"/><sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>
            </clipitem>
          </track>
        </audio>
      </media>
      <timecode>${rateBlock}<string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat></timecode>
    </sequence>`;

  // ── [EasyScript] edit sequence — all clips on 1 video track ──
  let videoClips = "", audioClips = "", timelinePos = 0;
  const removedSec = cuts.reduce((sum, c) => sum + (c.end - c.start), 0);

  kept.forEach((seg, i) => {
    const inFrame = secToFrames(seg.start), outFrame = secToFrames(seg.end);
    const segDuration = outFrame - inFrame;

    // Determine speaker → camera for this clip
    let clipName = `[Source] ${baseName}`;
    let labelXml = "";
    let videoSourceTrack = "";
    let camIdx = 1;
    if (useMulticam) {
      const midTime = (seg.start + seg.end) / 2;
      const matchSeg = segments.find(s => s.type === "speech" && s.speaker && midTime >= s.start && midTime < s.end);
      const speaker = matchSeg ? matchSeg.speaker : uniqueSpeakers[0];
      camIdx = speakerToCamera[speaker] || 1;
      const label = speakerMap[speaker] || `Speaker ${String.fromCharCode(64 + camIdx)}`;
      const color = labelColors[(camIdx - 1) % labelColors.length];
      clipName = `${label} (Cam ${camIdx})`;
      labelXml = `
              <labels><label2>${color}</label2></labels>`;
      // Select video from the correct camera track in [Source]
      videoSourceTrack = `
              <sourcetrack><mediatype>video</mediatype><trackindex>${camIdx}</trackindex></sourcetrack>`;
    }

    // Build <link> elements to bind video + audio as one linked clip
    const linkBlock = `
              <link><linkclipref>edit-v-${i + 1}</linkclipref><mediatype>video</mediatype><trackindex>1</trackindex><clipindex>${i + 1}</clipindex></link>
              <link><linkclipref>edit-a-${i + 1}</linkclipref><mediatype>audio</mediatype><trackindex>1</trackindex><clipindex>${i + 1}</clipindex></link>`;

    videoClips += `
            <clipitem id="edit-v-${i + 1}">
              <name>${clipName}</name>
              <duration>${totalDurFrames}</duration>${rateBlock}
              <start>${timelinePos}</start><end>${timelinePos + segDuration}</end>
              <in>${inFrame}</in><out>${outFrame}</out>
              <sequence id="source-seq"/>${videoSourceTrack}${labelXml}${linkBlock}
            </clipitem>`;

    audioClips += `
            <clipitem id="edit-a-${i + 1}">
              <name>${clipName}</name>
              <duration>${totalDurFrames}</duration>${rateBlock}
              <start>${timelinePos}</start><end>${timelinePos + segDuration}</end>
              <in>${inFrame}</in><out>${outFrame}</out>
              <sequence id="source-seq"/>${labelXml}
              <sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>${linkBlock}
            </clipitem>`;

    timelinePos += segDuration;
  });

  const mcNote = useMulticam ? `, ${uniqueSpeakers.length} speakers` : "";
  const editSeq = `
    <sequence id="edit-seq">
      <name>[EasyScript] ${baseName} — ${kept.length} clips${mcNote}, -${formatTime(removedSec)}</name>
      <duration>${totalOutFrames}</duration>${rateBlock}
      <media>
        <video>
          <format><samplecharacteristics><width>1920</width><height>1080</height><pixelaspectratio>Square</pixelaspectratio></samplecharacteristics></format>
          <track>${videoClips}
          </track>
        </video>
        <audio>
          <numOutputChannels>2</numOutputChannels>
          <format><samplecharacteristics><depth>16</depth><samplerate>48000</samplerate></samplecharacteristics></format>
          <track>${audioClips}
          </track>
        </audio>
      </media>
      <timecode>${rateBlock}<string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat></timecode>
    </sequence>`;

  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="5">
  <project>
    <name>EasyScript — ${baseName}</name>
    <children>${sourceSeq}${editSeq}
    </children>
  </project>
</xmeml>`;
}

function exportXML() {
  const cuts = getFilteredCutPoints();
  if (cuts.length === 0) {
    alert("No cuts to export. Run Detect Silence first, then adjust settings.");
    return;
  }
  // Show export dialog instead of directly exporting
  showExportDialog();
}

function showExportDialog() {
  const dialog = document.getElementById("exportDialog");
  const subtitlesCheck = document.getElementById("exportOptSubtitles");
  const multicamCheck = document.getElementById("exportOptMulticam");
  const multicamHint = document.getElementById("exportMulticamHint");
  const subLangOptions = document.getElementById("exportSubLangOptions");

  // Enable subtitles checkbox only if transcription exists
  subtitlesCheck.disabled = !hasTranscription;
  subtitlesCheck.checked = hasTranscription;

  // Enable multicam only if speakers detected
  const numSpeakers = Object.keys(speakerMap).length;
  multicamCheck.disabled = numSpeakers <= 1;
  multicamCheck.checked = numSpeakers > 1;
  multicamHint.textContent = numSpeakers > 1
    ? `${numSpeakers} speakers detected`
    : "Run Speakers first to enable multicam";

  // Build language checkboxes for subtitle export
  subLangOptions.innerHTML = "";
  if (hasTranscription) {
    // Original language
    const srcLang = getSourceLang() || "orig";
    const origLabel = document.createElement("label");
    origLabel.innerHTML = `<input type="checkbox" checked value="original" /> Original (${srcLang})`;
    subLangOptions.appendChild(origLabel);

    // Translation languages
    transLangs.forEach(code => {
      const hasData = translationData[code] && translationData[code].some(t => t.text);
      const label = document.createElement("label");
      label.innerHTML = `<input type="checkbox" ${hasData ? "checked" : "disabled"} value="${code}" /> ${code.toUpperCase()}${!hasData ? " (not translated)" : ""}`;
      subLangOptions.appendChild(label);
    });
  }

  // Toggle sub-options visibility
  subtitlesCheck.onchange = () => {
    subLangOptions.classList.toggle("hidden", !subtitlesCheck.checked);
  };
  subLangOptions.classList.toggle("hidden", !subtitlesCheck.checked);

  dialog.classList.remove("hidden");
}

async function doExportXML() {
  const dialog = document.getElementById("exportDialog");
  const includeSubtitles = document.getElementById("exportOptSubtitles").checked;
  const enableMulticam = document.getElementById("exportOptMulticam").checked;
  dialog.classList.add("hidden");

  const cuts = getFilteredCutPoints();
  const kept = getKeptSegments();
  const removedDuration = cuts.reduce((sum, c) => sum + (c.end - c.start), 0);
  const name = getAudioBaseName();
  const savedFiles = [];

  // Generate and download XML
  const xml = generateFCPXML(enableMulticam);
  const xmlPath = await downloadFile(xml, `${name}_easyscript.xml`, "application/xml");
  if (xmlPath) savedFiles.push(xmlPath);

  // Optionally export SRT files
  if (includeSubtitles) {
    const subLangOptions = document.getElementById("exportSubLangOptions");
    const checkedLangs = [...subLangOptions.querySelectorAll("input:checked")].map(cb => cb.value);

    if (checkedLangs.includes("original")) {
      const srt = generateSrtAfterCuts();
      const p = await downloadFile(srt, `${name}_cut.srt`, "text/srt");
      if (p) savedFiles.push(p);
    }

    // Export translation SRTs
    for (const code of checkedLangs.filter(c => c !== "original")) {
      const srt = generateTranslationSrt(code);
      if (srt) {
        const p = await downloadFile(srt, `${name}_cut_${code}.srt`, "text/srt");
        if (p) savedFiles.push(p);
      }
    }
  }

  const info = document.getElementById("exportInfo");
  let extraInfo = "";
  if (includeSubtitles) extraInfo += ` + ${savedFiles.length - 1} SRT`;
  if (enableMulticam) extraInfo += " + Multi-track";
  const folderEl = document.getElementById("exportFolderPath");
  const folderName = folderEl ? folderEl.textContent : "~/Downloads";
  const folder = savedFiles.length > 0 ? ` → ${folderName}` : "";
  info.innerHTML = `<span class="model-tag tag-loaded">SAVED</span> ${kept.length} clips, removed ${formatTime(removedDuration)}${extraInfo}${folder}`;
}

function generateTranslationSrt(langCode) {
  const langData = translationData[langCode];
  if (!langData) return null;

  const { segs, parentMap } = getSplitSpeechSegments();

  // Build split translation text: split each translation proportionally
  // based on how many sub-segments its parent was split into
  const transTexts = [];
  if (segLineBreakMode === "natural") {
    segs.forEach((_, i) => {
      transTexts.push(langData[i] ? langData[i].text || "" : "");
    });
  } else {
    // Group sub-segments by parent index
    const groups = new Map();
    segs.forEach((sub, i) => {
      const pi = parentMap.get(i);
      if (!groups.has(pi)) groups.set(pi, []);
      groups.get(pi).push(i);
    });
    // Split each parent's translation text proportionally by word count
    for (const [pi, subIndices] of groups) {
      const fullTrans = langData[pi] ? langData[pi].text || "" : "";
      if (!fullTrans || subIndices.length <= 1) {
        subIndices.forEach(() => transTexts.push(fullTrans));
      } else {
        const words = fullTrans.split(/\s+/).filter(w => w);
        const origWords = segs[subIndices[0]]
          ? subIndices.reduce((sum, si) => sum + (segs[si].text || "").split(/\s+/).filter(w => w).length, 0)
          : words.length;
        let wordIdx = 0;
        subIndices.forEach(si => {
          const subWordCount = (segs[si].text || "").split(/\s+/).filter(w => w).length;
          const ratio = origWords > 0 ? subWordCount / origWords : 1 / subIndices.length;
          const take = Math.max(1, Math.round(ratio * words.length));
          transTexts.push(words.slice(wordIdx, wordIdx + take).join(" "));
          wordIdx += take;
        });
        // Ensure remaining words go to last sub-segment
        if (wordIdx < words.length) {
          transTexts[transTexts.length - 1] += " " + words.slice(wordIdx).join(" ");
        }
      }
    }
  }

  const cuts = getFilteredCutPoints().sort((a, b) => a.start - b.start);
  function getRemovedBefore(time) {
    let removed = 0;
    for (const cut of cuts) {
      if (cut.end <= time) removed += cut.end - cut.start;
      else if (cut.start < time) removed += time - cut.start;
    }
    return removed;
  }

  let srt = "", idx = 1;
  segs.forEach((seg, i) => {
    const transText = transTexts[i] || "";
    if (!transText) return;
    const newStart = seg.start - getRemovedBefore(seg.start);
    const newEnd = seg.end - getRemovedBefore(seg.end);
    if (newEnd <= newStart) return;
    srt += `${idx}\n${formatSrtTime(Math.max(0, newStart))} --> ${formatSrtTime(newEnd)}\n${transText}\n\n`;
    idx++;
  });
  return srt;
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
 * Returns { segs: [...], parentMap: Map(subIndex → origSpeechIndex) }
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

function generateSrtOriginal() {
  const { segs } = getSplitSpeechSegments();
  let srt = "";
  segs.forEach((seg, i) => {
    srt += `${i+1}\n${formatSrtTime(seg.start)} --> ${formatSrtTime(seg.end)}\n${seg.text.trim()}\n\n`;
  });
  return srt;
}

function generateSrtAfterCuts() {
  const { segs } = getSplitSpeechSegments();
  const cuts = getFilteredCutPoints().sort((a, b) => a.start - b.start);
  function getRemovedBefore(time) {
    let removed = 0;
    for (const cut of cuts) {
      if (cut.end <= time) removed += cut.end - cut.start;
      else if (cut.start < time) removed += time - cut.start;
    }
    return removed;
  }
  function isFullyCut(seg) {
    for (const cut of cuts) {
      if (cut.start <= seg.start && cut.end >= seg.end) return true;
    }
    return false;
  }
  let srt = "", idx = 1;
  segs.forEach((seg) => {
    if (isFullyCut(seg)) return;
    const newStart = seg.start - getRemovedBefore(seg.start);
    const newEnd = seg.end - getRemovedBefore(seg.end);
    if (newEnd <= newStart) return;
    srt += `${idx}\n${formatSrtTime(Math.max(0, newStart))} --> ${formatSrtTime(newEnd)}\n${seg.text.trim()}\n\n`;
    idx++;
  });
  return srt;
}

async function downloadFile(content, filename, mimeType) {
  // Try backend save endpoint (works in both UXP and dev mode)
  try {
    const res = await fetch(`${BACKEND_URL}/save-file`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename, content }),
    });
    const data = await res.json();
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
    console.error("[EasyScript] Download failed:", e);
    alert(`Could not save file: ${filename}`);
  }
}

function getAudioBaseName() {
  return document.getElementById("audioPathInput").value
    .split("/").pop().split("\\").pop().replace(/\.[^.]+$/, "");
}

async function exportSrtOriginal() {
  const speeches = getSpeechSegments();
  if (speeches.length === 0) { alert("No speech segments. Run Transcribe first."); return; }
  const srt = generateSrtOriginal();
  const path = await downloadFile(srt, `${getAudioBaseName()}_original.srt`, "text/srt");
  const folder = path ? " → ~/Downloads" : "";
  document.getElementById("exportInfo").innerHTML =
    `<span class="model-tag tag-loaded">SRT</span> ${speeches.length} subtitles (original timecode)${folder}`;
}

async function exportSrtAfterCuts() {
  const speeches = getSpeechSegments();
  const cuts = getFilteredCutPoints();
  if (speeches.length === 0) { alert("No speech segments. Run Transcribe first."); return; }
  if (cuts.length === 0) { alert("No cuts defined. Run Detect Silence first."); return; }
  const srt = generateSrtAfterCuts();
  const path = await downloadFile(srt, `${getAudioBaseName()}_cut.srt`, "text/srt");
  const lineCount = (srt.match(/^\d+$/gm) || []).length;
  const removedDuration = cuts.reduce((sum, c) => sum + (c.end - c.start), 0);
  const folder = path ? " → ~/Downloads" : "";
  document.getElementById("exportInfo").innerHTML =
    `<span class="model-tag tag-loaded">SRT</span> ${lineCount} subtitles (adjusted, -${formatTime(removedDuration)})${folder}`;
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
function splitAndAssignSpeakers(diarizeRaw) {
  // A single Whisper segment = one complete sentence/phrase. Whisper breaks at
  // natural pauses, so its sentence boundaries are far more trustworthy than
  // pyannote's frame-level turn boundaries (which often slip a fraction of a
  // second into a sentence). Splitting a sentence mid-way produces the bug
  // where "Tôi" → Speaker A and "là một bác sĩ" → Speaker B.
  //
  // Therefore we NEVER split a Whisper segment. Each sentence is attributed
  // whole to the speaker who occupies the most of it (dominant overlap).
  for (const seg of segments) {
    if (seg.type !== "speech") continue;

    // Sum overlap duration per speaker across the whole sentence.
    const overlapBySpeaker = {};
    for (const d of diarizeRaw) {
      const ov = Math.min(d.end, seg.end) - Math.max(d.start, seg.start);
      if (ov > 0) {
        overlapBySpeaker[d.speaker] = (overlapBySpeaker[d.speaker] || 0) + ov;
      }
    }

    let dominant = null, best = 0;
    for (const spk in overlapBySpeaker) {
      if (overlapBySpeaker[spk] > best) { best = overlapBySpeaker[spk]; dominant = spk; }
    }

    seg.speaker = dominant || "UNKNOWN";
    seg.speakerLabel = dominant ? (speakerMap[dominant] || "Unknown") : "Unknown";
  }

  segments = segments.sort((a, b) => a.start - b.start);
}

function round3(n) { return Math.round(n * 1000) / 1000; }

/**
 * Start diarization on backend only (POST + return immediately).
 * Does NOT poll or show progress — caller handles that.
 */
async function startDiarizeBackend(audioPath) {
  const speechSegs = segments.filter(s => s.type === "speech");
  // Optional known-speaker hint — speeds pyannote up noticeably (skips
  // cluster-size search). 0 / empty = auto-detect.
  const knownInput = parseInt(document.getElementById("knownSpeakersInput")?.value ?? "0", 10);
  const numSpeakers = Number.isFinite(knownInput) && knownInput > 0 ? knownInput : null;
  const sensitivity = document.getElementById("speakerSensitivitySelect")?.value || "standard";
  await fetchBackend("/diarize", {
    method: "POST",
    body: JSON.stringify({
      audio_path: audioPath,
      segments: speechSegs.map(s => ({
        start: s.start, end: s.end, text: s.text,
        language: s.language, type: s.type,
      })),
      num_speakers: numSpeakers,
      sensitivity: sensitivity,
    }),
  });
}

/**
 * Apply diarization result to current segments.
 * If no speech segments exist yet (standalone mode), create segments from diarize_raw.
 */
function applyDiarizeResult(result) {
  if (result.speaker_map) {
    speakerMap = result.speaker_map;
    hasSpeakers = true;
  }

  const hasSpeechSegs = segments.some(s => s.type === "speech");

  if (hasSpeechSegs) {
    // Has transcription — split speech segments at speaker boundaries
    if (result.diarize_raw && result.diarize_raw.length > 0) {
      splitAndAssignSpeakers(result.diarize_raw);
    } else if (result.segments) {
      result.segments.forEach(updSeg => {
        const match = segments.find(s =>
          s.type === "speech" && Math.abs(s.start - updSeg.start) < 0.5
        );
        if (match) {
          match.speaker = updSeg.speaker;
          match.speakerLabel = updSeg.speakerLabel;
        }
      });
    }
  } else if (result.diarize_raw && result.diarize_raw.length > 0) {
    // No transcription yet — create speech segments from raw diarization
    // Group consecutive diarize segments by speaker
    const dRaw = result.diarize_raw;
    const grouped = [];
    let cur = { speaker: dRaw[0].speaker, start: dRaw[0].start, end: dRaw[0].end };

    for (let i = 1; i < dRaw.length; i++) {
      if (dRaw[i].speaker === cur.speaker && dRaw[i].start - cur.end < 0.5) {
        // Same speaker, small gap — merge
        cur.end = dRaw[i].end;
      } else {
        grouped.push(cur);
        cur = { speaker: dRaw[i].speaker, start: dRaw[i].start, end: dRaw[i].end };
      }
    }
    grouped.push(cur);

    // Create speech segments with speaker labels
    const newSegs = grouped.map(g => ({
      type: "speech",
      start: round3(g.start),
      end: round3(g.end),
      text: "",
      speaker: g.speaker,
      speakerLabel: speakerMap[g.speaker] || "Unknown",
    }));

    // Keep existing non-speech segments (silence/breath), add new speaker segments
    const nonSpeech = segments.filter(s => s.type !== "speech");
    segments = [...nonSpeech, ...newSegs].sort((a, b) => a.start - b.start);
  }

  renderSegments(segments);
  updateSegmentCount(segments);
  if (waveform.peaks.length > 0) waveform.draw();
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

  const btn = document.getElementById("diarizeBtn");
  btn.disabled = true;
  btn.classList.add("processing");
  progressTracker.show();

  try {
    await startDiarizeBackend(audioPath);
    const result = await progressTracker.pollUntilDone("/diarize/progress");

    progressTracker.update(1, "done", `Done — ${result.num_speakers} speakers identified`);
    applyDiarizeResult(result);

  } catch (err) {
    if (err.message !== "__CANCELLED__") {
      progressTracker.update(0, "error", `Error: ${err.message}`);
      alert(`Diarization failed: ${err.message}`);
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
    alert("No speech segments to translate.");
    return;
  }

  const btn = document.getElementById("translateBtn");
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
      const ollamaModel = document.getElementById("ollamaModelSelect")?.value || "";
      const hymt2Size = document.getElementById("hymt2ModelSize")?.value || "1.8B";

      await fetchBackend("/translate", {
        method: "POST",
        body: JSON.stringify({
          segments: speechSegs.map(s => ({ text: s.text, start: s.start, end: s.end })),
          source_lang: sourceLang,
          target_lang: targetLang,
          provider: provider,
          model: provider === "ollama" ? (ollamaModel || undefined) : undefined,
          hymt2_model_size: provider === "hymt2" ? hymt2Size : undefined,
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
    alert(`Translation failed: ${err.message}`);
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
  const ollamaModel = document.getElementById("ollamaModelSelect")?.value || "";
  const hymt2Size = document.getElementById("hymt2ModelSize")?.value || "1.8B";

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
        model: provider === "ollama" ? (ollamaModel || undefined) : undefined,
        hymt2_model_size: provider === "hymt2" ? hymt2Size : undefined,
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
    alert(`Translation failed: ${err.message}`);
  } finally {
    if (icon) {
      icon.textContent = "↻";
      icon.style.pointerEvents = "";
    }
  }
}

// ── Settings ──

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

// ── Cut Settings Toggle (inside Waveform header) ──

function initCutControlsToggle() {
  const btn = document.getElementById("cutSettingsBtn");
  const panel = document.getElementById("cutSettingsPanel");
  if (!btn || !panel) return;

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = !panel.classList.contains("hidden");
    panel.classList.toggle("hidden");
    btn.classList.toggle("active", !isOpen);
  });
}

// ── Settings ──

function initSettings() {
  // Settings popup overlay open/close
  const settingsOverlay = document.getElementById("settingsOverlay");
  const openSettings = () => settingsOverlay.classList.remove("hidden");
  const closeSettings = () => settingsOverlay.classList.add("hidden");
  document.getElementById("settingsBtn").addEventListener("click", openSettings);
  document.getElementById("settingsCloseBtn").addEventListener("click", closeSettings);
  // Click on the dim backdrop (outside the panel) closes the popup
  settingsOverlay.addEventListener("click", (e) => {
    if (e.target === settingsOverlay) closeSettings();
  });
  // Escape key closes the popup
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !settingsOverlay.classList.contains("hidden")) closeSettings();
  });


  // Toggle export section
  document.getElementById("exportToggle").addEventListener("click", () => {
    const body = document.getElementById("exportBody");
    const chevron = document.getElementById("exportChevron");
    const isOpen = !body.classList.contains("hidden");
    body.classList.toggle("hidden");
    chevron.innerHTML = isOpen ? "&#9654;" : "&#9660;";
  });

  // Provider toggle — show/hide relevant settings sections
  function updateProviderUI(provider) {
    const isOllama = provider === "ollama";
    const isHyMT2 = provider === "hymt2";
    const isNLLB = provider === "nllb";
    const isClaude = provider === "claude";
    document.getElementById("ollamaSettings")?.classList.toggle("hidden", !isOllama);
    document.getElementById("hymt2Settings")?.classList.toggle("hidden", !isHyMT2);
    document.getElementById("nllbSettings")?.classList.toggle("hidden", !isNLLB);
    document.getElementById("claudeKeyRow")?.classList.toggle("hidden", !isClaude);
    if (isHyMT2) refreshHyMT2Status();
    if (isNLLB) refreshNLLBStatus();
  }

  const providerSelect = document.getElementById("translationProvider");
  if (providerSelect) {
    providerSelect.addEventListener("change", () => updateProviderUI(providerSelect.value));
    updateProviderUI(providerSelect.value);
  }

  // Ollama refresh
  document.getElementById("ollamaUrlInput")?.addEventListener("change", () => refreshOllamaModels());
  document.getElementById("refreshOllamaModelsBtn")?.addEventListener("click", () => refreshOllamaModels());

  // Hy-MT2 size change → refresh status
  document.getElementById("hymt2ModelSize")?.addEventListener("change", refreshHyMT2Status);
  // NLLB size change → refresh status
  document.getElementById("nllbModelSize")?.addEventListener("change", refreshNLLBStatus);

  // NLLB download button — same flow as Hy-MT2
  document.getElementById("nllbDownloadBtn")?.addEventListener("click", async () => {
    const btn = document.getElementById("nllbDownloadBtn");
    const statusText = document.getElementById("nllbStatusText");
    const size = document.getElementById("nllbModelSize")?.value || "600M";
    btn.disabled = true;
    btn.textContent = "Downloading...";
    statusText.textContent = "Downloading model — this may take several minutes...";
    statusText.style.color = "";
    try {
      await fetchBackend("/nllb/download", {
        method: "POST",
        body: JSON.stringify({ model_size: size }),
      });
      let polling = true;
      while (polling) {
        await new Promise(r => setTimeout(r, 3000));
        const data = await fetchBackend(`/nllb/status?model_size=${size}`);
        const dlp = data.download_progress || {};
        if (data.downloaded || dlp.status === "done") {
          statusText.textContent = `✓ ${data.model_id} ready`;
          statusText.style.color = "var(--accent)";
          polling = false;
        } else if (dlp.status === "error") {
          statusText.textContent = `Error: ${dlp.detail || "download failed"}`;
          polling = false;
        } else if (dlp.detail) {
          statusText.textContent = dlp.detail;
        }
      }
    } catch (err) {
      statusText.textContent = `Error: ${err.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = "Re-download";
    }
  });

  // Hy-MT2 download button
  document.getElementById("hymt2DownloadBtn")?.addEventListener("click", async () => {
    const btn = document.getElementById("hymt2DownloadBtn");
    const statusText = document.getElementById("hymt2StatusText");
    const size = document.getElementById("hymt2ModelSize")?.value || "1.8B";
    btn.disabled = true;
    btn.textContent = "Downloading...";
    statusText.textContent = "Downloading model — this may take several minutes...";
    statusText.style.color = "";
    try {
      await fetchBackend("/hymt2/download", {
        method: "POST",
        body: JSON.stringify({ model_size: size }),
      });
      // Poll until download completes
      let polling = true;
      while (polling) {
        await new Promise(r => setTimeout(r, 3000));
        const data = await fetchBackend(`/hymt2/status?model_size=${size}`);
        const dlp = data.download_progress || {};
        if (data.downloaded || dlp.status === "done") {
          statusText.textContent = `✓ ${data.model_id} ready`;
          statusText.style.color = "var(--accent)";
          polling = false;
        } else if (dlp.status === "error") {
          statusText.textContent = `Error: ${dlp.detail || "download failed"}`;
          polling = false;
        } else if (dlp.detail) {
          statusText.textContent = dlp.detail;
        }
      }
    } catch (err) {
      statusText.textContent = `Error: ${err.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = "Re-download";
    }
  });

  // Save settings
  document.getElementById("saveSettingsBtn").addEventListener("click", async () => {
    const settings = {
      hf_token: document.getElementById("hfTokenInput").value.trim(),
      translation_provider: document.getElementById("translationProvider")?.value || "ollama",
      ollama_url: document.getElementById("ollamaUrlInput")?.value.trim() || "http://localhost:11434",
      ollama_model: document.getElementById("ollamaModelSelect")?.value || "",
      hymt2_model_size: document.getElementById("hymt2ModelSize")?.value || "1.8B",
      nllb_model_size: document.getElementById("nllbModelSize")?.value || "600M",
      anthropic_api_key: document.getElementById("claudeApiKeyInput")?.value.trim() || "",
    };

    try {
      await fetchBackend("/settings", {
        method: "POST",
        body: JSON.stringify(settings),
      });
      alert("Settings saved!");
    } catch (err) {
      alert(`Failed to save settings: ${err.message}`);
    }
  });

  // Load saved settings
  loadSavedSettings();
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
      const sizeEl = document.getElementById("hymt2ModelSize");
      if (sizeEl) sizeEl.value = settings.hymt2_model_size;
    }
    if (settings.nllb_model_size) {
      const sizeEl = document.getElementById("nllbModelSize");
      if (sizeEl) sizeEl.value = settings.nllb_model_size;
    }
    // Populate Ollama models, then restore saved selection
    await refreshOllamaModels(settings.ollama_model);
  } catch {
    // Settings not available yet — try populating Ollama models anyway
    await refreshOllamaModels();
  }
}

async function refreshOllamaModels(savedModel) {
  const select = document.getElementById("ollamaModelSelect");
  if (!select) return;

  try {
    const data = await fetchBackend("/ollama/status");
    const models = data.models || [];
    select.innerHTML = "";
    if (models.length === 0) {
      select.innerHTML = '<option value="">No models found</option>';
      return;
    }
    models.forEach(name => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    });
    if (savedModel && models.includes(savedModel)) {
      select.value = savedModel;
    } else {
      select.value = models[0];
    }
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
    if (data.downloaded) {
      statusText.textContent = `✓ ${data.model_id} ready`;
      statusText.style.color = "var(--accent)";
      downloadBtn.textContent = "Re-download";
    } else {
      statusText.textContent = `Not downloaded — ${data.model_id}`;
      statusText.style.color = "";
      downloadBtn.textContent = "Download";
    }
  } catch {
    statusText.textContent = "Status check failed";
    statusText.style.color = "";
  }
}

async function refreshNLLBStatus() {
  const statusText = document.getElementById("nllbStatusText");
  const downloadBtn = document.getElementById("nllbDownloadBtn");
  if (!statusText || !downloadBtn) return;

  const size = document.getElementById("nllbModelSize")?.value || "600M";
  try {
    const data = await fetchBackend(`/nllb/status?model_size=${size}`);
    if (data.downloaded) {
      statusText.textContent = `✓ ${data.model_id} ready`;
      statusText.style.color = "var(--accent)";
      downloadBtn.textContent = "Re-download";
    } else {
      statusText.textContent = `Not downloaded — ${data.model_id}`;
      statusText.style.color = "";
      downloadBtn.textContent = "Download";
    }
  } catch {
    statusText.textContent = "Status check failed";
    statusText.style.color = "";
  }
}

// ── Event bindings ──

document.getElementById("progressCancelBtn").addEventListener("click", () => progressTracker.cancel());
document.getElementById("autoCutBtn").addEventListener("click", runAutoCut);
// Show transcribe mode dialog, then run with selected mode
function setupTranscribeModeDialogOnce() {
  if (setupTranscribeModeDialogOnce._done) return;
  setupTranscribeModeDialogOnce._done = true;

  const panel = document.getElementById("songParamsPanel");
  const syncPanelVisibility = () => {
    const isSong = document.getElementById("modeSong").checked;
    panel.classList.toggle("hidden", !isSong);
  };
  document.getElementById("modeAudio").addEventListener("change", syncPanelVisibility);
  document.getElementById("modeSong").addEventListener("change", syncPanelVisibility);

  const vadEl = document.getElementById("songVadThresh");
  const vadVal = document.getElementById("songVadThreshVal");
  vadEl.addEventListener("input", () => { vadVal.textContent = (vadEl.value / 100).toFixed(2); });

  const silEl = document.getElementById("songMinSilence");
  const silVal = document.getElementById("songMinSilenceVal");
  silEl.addEventListener("input", () => { silVal.textContent = `${silEl.value}ms`; });

  const beamEl = document.getElementById("songBeamSize");
  const beamVal = document.getElementById("songBeamSizeVal");
  beamEl.addEventListener("input", () => { beamVal.textContent = beamEl.value; });
}

function readSongParams() {
  return {
    vad_threshold: parseInt(document.getElementById("songVadThresh").value, 10) / 100,
    min_silence_ms: parseInt(document.getElementById("songMinSilence").value, 10),
    beam_size: parseInt(document.getElementById("songBeamSize").value, 10),
  };
}

function showTranscribeModeDialog(resumeFromPlayhead = false) {
  setupTranscribeModeDialogOnce();
  const dialog = document.getElementById("transcribeModeDialog");
  document.getElementById("modeAudio").checked = true;
  document.getElementById("songParamsPanel").classList.add("hidden");
  dialog.classList.remove("hidden");

  document.getElementById("transcribeModeConfirm").onclick = () => {
    dialog.classList.add("hidden");
    const songMode = document.getElementById("modeSong").checked;
    const songParams = songMode ? readSongParams() : null;
    runTranscribe(resumeFromPlayhead, songMode, songParams);
  };
  document.getElementById("transcribeModeCancel").onclick = () => {
    dialog.classList.add("hidden");
  };
}

document.getElementById("transcribeBtn").addEventListener("click", () => showTranscribeModeDialog(false));
// Right-click transcribe = resume from playhead
document.getElementById("transcribeBtn").addEventListener("contextmenu", (e) => {
  e.preventDefault();
  const playheadTime = audioPlayback.audio ? audioPlayback.audio.currentTime : 0;
  showTranscribeModeDialog(playheadTime > 1);
});
document.getElementById("diarizeBtn").addEventListener("click", runDiarize);
document.getElementById("exportXmlBtn").addEventListener("click", exportXML);
document.getElementById("exportSrtOrigBtn").addEventListener("click", exportSrtOriginal);
document.getElementById("exportSrtCutBtn").addEventListener("click", exportSrtAfterCuts);

// Export dialog buttons
document.getElementById("exportDialogCancel").addEventListener("click", () => {
  document.getElementById("exportDialog").classList.add("hidden");
});
document.getElementById("exportDialogConfirm").addEventListener("click", doExportXML);

// Export folder picker
async function chooseExportFolder() {
  try {
    const res = await fetchBackend("/choose-folder", { method: "POST" });
    if (res.path) {
      updateExportFolderDisplay(res.path);
    }
  } catch (e) {
    console.warn("[EasyScript] Choose folder failed:", e);
  }
}

function updateExportFolderDisplay(path) {
  const el = document.getElementById("exportFolderPath");
  if (!el) return;
  // Shorten path for display: ~/Documents/... or C:\Users\...
  const home = path.includes("/Users/") ? path.replace(/^\/Users\/[^/]+/, "~") : path;
  el.textContent = home;
  el.title = path;
}

// Load saved export dir on startup
fetchBackend("/export-dir").then(data => {
  if (data && data.path) updateExportFolderDisplay(data.path);
}).catch(() => {});

document.getElementById("chooseFolderBtn").addEventListener("click", chooseExportFolder);
document.getElementById("exportFolderPath").addEventListener("click", chooseExportFolder);

// ── Dev mode ──

function isDevMode() { return !ppro; }

async function loadWaveformForPath(audioPath) {
  if (!audioPath) return;
  const info = document.getElementById("audioInfo");
  info.textContent = "Generating waveform…";
  info.classList.remove("hidden");
  try {
    const res = await fetch(`${BACKEND_URL}/peaks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audio_path: audioPath }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    // Fresh file selected — reset any prior session state
    segments = [];
    hasTranscription = false;

    audioDuration = data.audio_duration || 0;
    const peaks = (data.peaks && data.peaks.length)
      ? data.peaks
      : waveform.generateMockPeaks(audioDuration, 800);
    waveform.loadPeaks(peaks, audioDuration);
    waveform.updateMarkers([]);

    currentAudioPath = audioPath;
    audioPlayback.loadAudio(audioPath);

    renderSegments(segments);
    updateSegmentCount(segments);
    updateCutStats();
    updateExportButtons();
    showAudioInfo({ audio_duration: audioDuration });
  } catch (err) {
    console.error("[peaks] Failed to load waveform:", err);
    info.textContent = `Waveform unavailable: ${err.message}`;
  }
}

function setupFileBrowse() {
  const pathInput = document.getElementById("audioPathInput");
  pathInput.addEventListener("input", () => updateActionButtons());

  document.getElementById("filePicker").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    pathInput.value = `Uploading ${file.name}...`;
    document.getElementById("autoCutBtn").disabled = true;
    document.getElementById("transcribeBtn").disabled = true;
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await fetch(`${BACKEND_URL}/upload`, { method: "POST", body: formData });
      if (!res.ok) throw new Error("Upload failed");
      const data = await res.json();
      pathInput.value = data.path;
      pathInput.dispatchEvent(new Event("input"));
      // Auto-load waveform so user can Transcribe / Speakers without running Detect Silence first
      loadWaveformForPath(data.path);
    } catch (err) {
      pathInput.value = "";
      alert(`Upload failed: ${err.message}`);
    }
  });
}

function loadDevMode() {
  console.log("[EasyScript] Dev mode — browser environment");
  document.getElementById("trackSelect").classList.add("hidden");
}

// Update action buttons to include diarize state
function updateActionButtons() {
  const hasInput = !!document.getElementById("audioPathInput")?.value.trim();
  document.getElementById("autoCutBtn").disabled = !backendConnected || !hasInput;
  document.getElementById("transcribeBtn").disabled = !backendConnected || !hasInput;
  document.getElementById("diarizeBtn").disabled = !backendConnected || !hasInput;
}

// ══════════════════════════════════════
// LIVE TAB — UI Logic
// ══════════════════════════════════════

let activeMainTab = "editor"; // "editor" | "live"

function initMainTabs() {
  const tabs = document.querySelectorAll(".main-tab");
  tabs.forEach(tab => {
    tab.addEventListener("click", () => {
      const target = tab.dataset.tab;
      if (target === activeMainTab) return;
      activeMainTab = target;

      // Update tab buttons
      tabs.forEach(t => t.classList.toggle("active", t.dataset.tab === target));

      // Toggle panels
      document.getElementById("editorPanel").classList.toggle("hidden", target !== "editor");
      document.getElementById("livePanel").classList.toggle("hidden", target !== "live");
    });
  });
}

function initLiveTab() {
  // ── State ──
  let liveSource = "mic";  // "mic" | "system"
  let liveRunning = false;
  let livePaused = false;     // paused = stopped but can continue
  let liveStartTime = 0;
  let liveElapsedBefore = 0;  // accumulated seconds from previous sessions
  let liveTimerInterval = null;
  let liveWs = null;           // WebSocket connection
  let liveMediaStream = null;  // MediaStream from getUserMedia/getDisplayMedia
  let liveAudioCtx = null;     // AudioContext
  let liveWorklet = null;      // ScriptProcessorNode
  let liveRecordedChunks = []; // Int16Array PCM chunks captured during the session
  let liveRecBlobUrl = null;   // object URL of the built WAV for playback
  let liveRecActiveIdx = -1;   // segment currently highlighted during playback
  const LIVE_REC_SAMPLE_RATE = 16000;
  let liveSegments = [];       // Transcribed segments
  let liveFilter = "speech";   // "speech" | "translation"
  let liveTransLangs = [];     // max 2 translation languages
  let liveActiveTransLang = "";
  let liveTextView = false;
  // ── Live in-progress line state ──
  // The user sees ONE growing string in the live-partial row. It's the
  // concatenation of the committed draft (from backend) and the latest
  // hypothesis tail. We typewriter-animate the COMBINED text, not the parts
  // separately, so that when the backend promotes words from hypothesis →
  // draft the visible text doesn't briefly retract and re-type.
  let liveDraftText = "";        // committed-but-unflushed text from backend
  let liveDraftNextIndex = -1;   // segment index this draft will become
  let liveHypothesisText = "";   // latest hypothesis tail from backend
  let liveDisplayedText = "";    // what the user currently sees (typewriter output)
  let liveDisplayedTarget = "";  // draft + " " + hypothesis (animation goal)
  let liveDisplayTimer = null;
  // Backend emits draft_segment + partial as separate WS events in the same
  // cycle. If we recompute the target between them, we see a stale-hypothesis
  // moment that briefly duplicates the just-committed word. Debounce so both
  // events update state before a single recompute fires.
  let liveRecomputeTimer = null;
  const RECOMPUTE_DEBOUNCE_MS = 10;
  const MAX_LIVE_TRANS_LANGS = 2;
  // Adaptive typewriter cadence: reveal new words spread across the actual
  // backend partial interval so the on-screen pace tracks the speaker. Fixed
  // 80ms/word felt jerky — too fast for slow speech, dumps in chunks for fast
  // speech. We measure interval-between-recomputes and divide by the new
  // word count, clamped to a readable range.
  const CADENCE_MIN_MS = 120;     // very fast speech cap
  const CADENCE_MAX_MS = 350;     // slow speech cap (readable floor)
  const CADENCE_DEFAULT_MS = 200; // before we have a measurement
  let liveCadenceMs = CADENCE_DEFAULT_MS;
  let liveLastRecomputeAt = 0;

  // ── Timecode-paced word reveal (live box) ──────────────────────────────
  // Each word from the backend carries its absolute spoken start time. We
  // reveal words one-by-one paced by the GAP between their timecodes, so the
  // on-screen rhythm matches the speaker (hesitation → pause, fast → fast).
  // Append-only: a revealed word is never rewritten (no flicker); corrections
  // surface only in the list when the line graduates.
  let liveShownWords = [];    // word tokens already revealed (current line)
  let liveWordQueue = [];     // [{w, t}] waiting to be revealed
  let liveDraftWords = [];    // committed words for the current line (from draft_segment)
  let liveHypWords = [];      // hypothesis tail words (from partial)
  let liveRevealTimer = null;
  let liveIngestTimer = null; // debounce to coalesce draft+partial of one cycle
  let liveLastWordT = null;   // timecode of the last revealed word
  const WORD_MIN_MS = 45;        // min visible time per word
  const WORD_MAX_GAP_MS = 900;   // cap a spoken pause so the box never freezes long
  const WORD_CATCHUP_BACKLOG = 4;// queue longer than this → speed up to catch realtime
  const WORD_CATCHUP_MS = 70;    // capped per-word delay while catching up

  // Display mode for the in-progress line:
  //   false → committed-only (LocalAgreement-2 commits). +1–2s latency,
  //           but words never retract — feels like YouTube's no-flicker
  //           captions.
  //   true  → include the hypothesis tail. Lower latency, but Whisper may
  //           revise the tail between cycles → visible flicker.
  const LIVE_SHOW_HYPOTHESIS = true;

  // Show committed (draft) words one-by-one with the typewriter instead of
  // snapping them in instantly. The reveal speed adapts to the backlog (see
  // revealCadence): comfortable when keeping up, faster when behind — so each
  // word is still visible while catching up to the live voice.
  const LIVE_INSTANT_COMMIT = false;

  // Word-reveal cadence (ms per word). Default = comfortable reading pace when
  // transcription keeps up; when a backlog builds (slow transcription), the
  // pace speeds up toward REVEAL_FAST_MS so the line catches up to realtime,
  // while still animating each word. The whole backlog targets ~CATCHUP_BUDGET.
  const REVEAL_DEFAULT_MS = 190;
  const REVEAL_FAST_MS = 45;
  const REVEAL_CATCHUP_BUDGET_MS = 650;

  // When final_segment fires while the typewriter is still mid-sentence, we
  // queue the push and let typewriter finish revealing the full final text
  // first — avoids an abrupt snap-to-end visual.
  let livePendingFinal = null; // { seg, draftText, draftIdx }

  // A finished line currently shown on the highlighted live box but not yet
  // moved into the list below. It graduates when the next line appears, so
  // every line is seen on the realtime box first (never "dumped" to the list).
  let livePendingGraduation = null; // { seg, index, draftText, draftIdx }

  // Translation state
  let liveTranslations = {};  // { langCode: { segIndex: "translated text" } }
  let liveTransQueue = [];     // queue of segments awaiting translation
  // Pre-translation cache for draft sentences. When a draft_segment matches
  // the eventual final_segment text, we can reuse the cached translation and
  // avoid waiting for a fresh round-trip.
  let liveDraftTranslations = {}; // { langCode: { nextIndex: "translation" } }
  let liveDraftSourceTexts = {};  // { nextIndex: "source text being pre-translated" }
  let liveDraftAborters = {};     // { "langCode:nextIndex": AbortController }

  // Elements
  const startBtn = document.getElementById("liveStartBtn");
  const pauseBtn = document.getElementById("livePauseBtn");
  const stopBtn = document.getElementById("liveStopBtn");
  const continueBtn = document.getElementById("liveContinueBtn");
  const newBtn = document.getElementById("liveNewBtn");
  const timerEl = document.getElementById("liveTimer");
  const setupSection = document.getElementById("liveSetupSection");
  const runningBar = document.getElementById("liveRunningBar");
  const livePanel = document.getElementById("livePanel");
  const srcMic = document.getElementById("liveSrcMic");
  const srcSystem = document.getElementById("liveSrcSystem");
  const micDetail = document.getElementById("liveMicDetail");
  const systemDetail = document.getElementById("liveSystemDetail");

  // ── Mic device list ──
  async function populateMicList() {
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const mics = devices.filter(d => d.kind === "audioinput");
      const sel = document.getElementById("liveMicSelect");
      if (!sel) return;
      sel.innerHTML = "";
      mics.forEach((mic, i) => {
        const opt = document.createElement("option");
        opt.value = mic.deviceId;
        opt.textContent = mic.label || `Microphone ${i + 1}`;
        sel.appendChild(opt);
      });
    } catch (e) {
      console.warn("[live] Cannot enumerate devices:", e);
    }
  }
  navigator.mediaDevices.getUserMedia({ audio: true })
    .then(stream => { stream.getTracks().forEach(t => t.stop()); populateMicList(); })
    .catch(() => {});

  // ── Source toggle ──
  if (srcMic && srcSystem) {
    srcMic.addEventListener("click", () => {
      if (liveRunning) return;
      liveSource = "mic";
      srcMic.classList.add("active");
      srcSystem.classList.remove("active");
      micDetail.classList.remove("hidden");
      systemDetail.classList.add("hidden");
    });
    srcSystem.addEventListener("click", () => {
      if (liveRunning) return;
      liveSource = "system";
      srcSystem.classList.add("active");
      srcMic.classList.remove("active");
      systemDetail.classList.remove("hidden");
      micDetail.classList.add("hidden");
    });
  }

  // ── Status helper ──
  function setLiveStatus(msg, type = "info") {
    const el = document.getElementById("liveStatus");
    if (!el) return;
    el.textContent = msg;
    if (type === "error") {
      el.className = "live-status-inline live-status-error";
    } else {
      el.className = "live-status-inline";
    }
  }

  // ── Split text by sentence punctuation ──
  function splitBySentence(text) {
    // Split by sentence-ending punctuation, keep the punctuation attached
    const sentences = text.match(/[^.!?。？！]+[.!?。？！]+|[^.!?。？！]+$/g);
    if (!sentences) return [text.trim()];
    return sentences.map(s => s.trim()).filter(s => s.length > 0);
  }

  // ── Render live segments (newest first) ──
  function renderLiveSegments() {
    const list = document.getElementById("liveSegmentList");
    const countEl = document.getElementById("liveSegmentCount");
    if (!list) return;

    const hasContent = liveSegments.length > 0 || liveDisplayedText || liveRunning;

    if (!hasContent) {
      list.innerHTML = '<div class="segment-empty">Select an input source and press Start</div>';
      if (countEl) countEl.textContent = "0 segments";
      return;
    }

    if (countEl) countEl.textContent = `${liveSegments.length} segments`;

    const isTransView = liveFilter === "translation" && liveActiveTransLang;

    if (isTransView) {
      // ── TRANSLATION VIEW ──
      // Only show segments up to the latest one with a completed translation.
      // This prevents jumping ahead when transcription is faster than translation.
      const transData = liveTranslations[liveActiveTransLang] || {};

      // Find the highest consecutive translated index
      // (show all segments that have translations, don't skip gaps)
      let maxTranslatedIdx = -1;
      for (let i = 0; i < liveSegments.length; i++) {
        if (transData[i]) maxTranslatedIdx = i;
        else break; // stop at first gap — don't show segments past untranslated ones
      }

      // Also show ONE "translating..." segment after the last translated one
      const showUpTo = Math.min(maxTranslatedIdx + 1, liveSegments.length - 1);

      // Order: newest-first while LIVE; chronological (oldest-first) for review.
      const order = [];
      for (let i = 0; i <= showUpTo; i++) order.push(i);
      if (liveRunning) order.reverse();

      if (liveTextView) {
        let html = '';
        for (const i of order) {
          const seg = liveSegments[i];
          const trans = transData[i];
          html += `<div class="live-text-line live-speech-text" data-seg-idx="${i}">${seg.text || ""}</div>`;
          if (trans) {
            html += `<div class="live-text-line segment-translation live-trans-text" data-seg-idx="${i}">${trans}</div>`;
          } else {
            html += `<div class="live-text-line segment-translation translating">Translating...</div>`;
          }
        }
        list.innerHTML = html || '<div class="segment-empty">Waiting for translation...</div>';
      } else {
        let html = '';
        for (const i of order) {
          const seg = liveSegments[i];
          const startFmt = fmtTime(seg.start);
          const endFmt = fmtTime(seg.end);
          const trans = transData[i];

          let transHtml = '';
          if (trans) {
            transHtml = `<div class="segment-translation live-trans-text" data-seg-idx="${i}">${trans}</div>`;
          } else {
            transHtml = `<div class="segment-translation translating">Translating...</div>`;
          }

          html += `<div class="segment-item" data-start="${seg.start}" data-end="${seg.end}">
            <div class="segment-header">
              <span class="segment-time">${startFmt} → ${endFmt}</span>
            </div>
            <div class="segment-text live-speech-text" data-seg-idx="${i}">${seg.text || ""}</div>
            ${transHtml}
          </div>`;
        }
        list.innerHTML = html || '<div class="segment-empty">Waiting for translation...</div>';
      }
    } else {
      // ── SPEECH VIEW ──
      // Fast transcription display, newest first, no translation.
      // The "in-progress" line is the append-only live buffer (liveDisplayedText):
      // it only grows, never rewrites, so we render it in ONE uniform style —
      // no draft/hypothesis split that could cause a style flicker.
      const total = liveDisplayedText || "";
      const hasInProgress = !!total;
      const inProgressInner = total ? `<span class="live-draft">${total}</span>` : "";

      // Always keep the live caption box at the top (highlighted) while running,
      // so the user has a stable focal point — even during brief pauses when
      // there's no in-progress text yet.
      const showLiveBox = hasInProgress || liveRunning;
      const liveInner = hasInProgress
        ? inProgressInner
        : `<span class="live-waiting">Listening…</span>`;

      // Order: newest-first while LIVE; chronological (oldest-first) for review.
      const order = [];
      for (let i = 0; i < liveSegments.length; i++) order.push(i);
      if (liveRunning) order.reverse();

      if (liveTextView) {
        let html = '';
        if (showLiveBox) {
          html += `<div class="live-text-line live-partial">${liveInner}</div>`;
        }
        for (const i of order) {
          html += `<div class="live-text-line live-speech-text" data-seg-idx="${i}">${liveSegments[i].text || ""}</div>`;
        }
        list.innerHTML = html;
      } else {
        let html = '';
        if (showLiveBox) {
          html += `<div class="segment-item live-partial-item">
            <div class="segment-header">
              <span class="segment-time live-partial-label">● live</span>
            </div>
            <div class="segment-text live-partial">${liveInner}</div>
          </div>`;
        }
        for (const i of order) {
          const seg = liveSegments[i];
          const startFmt = fmtTime(seg.start);
          const endFmt = fmtTime(seg.end);
          html += `<div class="segment-item" data-start="${seg.start}" data-end="${seg.end}">
            <div class="segment-header">
              <span class="segment-time">${startFmt} → ${endFmt}</span>
            </div>
            <div class="segment-text live-speech-text" data-seg-idx="${i}">${seg.text || ""}</div>
          </div>`;
        }
        list.innerHTML = html;
      }
    }

    // Scroll to top (newest is at top)
    list.scrollTop = 0;

    const txtBtn = document.getElementById("liveExportTxtBtn");
    const srtBtn = document.getElementById("liveExportSrtBtn");
    if (txtBtn) txtBtn.disabled = liveSegments.length === 0;
    if (srtBtn) srtBtn.disabled = liveSegments.length === 0;
  }

  function fmtTime(sec) {
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  // ── Downsample float32 audio from native rate to 16kHz ──
  function downsampleTo16k(float32, fromRate) {
    if (fromRate === 16000) {
      const int16 = new Int16Array(float32.length);
      for (let i = 0; i < float32.length; i++) {
        const s = Math.max(-1, Math.min(1, float32[i]));
        int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }
      return int16;
    }
    const ratio = fromRate / 16000;
    const newLen = Math.floor(float32.length / ratio);
    const int16 = new Int16Array(newLen);
    for (let i = 0; i < newLen; i++) {
      const srcIdx = Math.floor(i * ratio);
      const s = Math.max(-1, Math.min(1, float32[srcIdx]));
      int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return int16;
  }

  // ── Get media stream (called directly in click handler for getDisplayMedia) ──
  async function getMediaStream() {
    if (liveSource === "mic") {
      const deviceId = document.getElementById("liveMicSelect")?.value || "default";
      return navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId: deviceId !== "default" ? { exact: deviceId } : undefined,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        }
      });
    } else {
      // System audio capture — uses getDisplayMedia
      // pywebview (WebKit) does not support getDisplayMedia
      if (window.pywebview || !navigator.mediaDevices.getDisplayMedia) {
        // Auto-open in default browser for System Audio support
        if (window.pywebview && window.pywebview.api) {
          try { window.pywebview.api.open_browser(); } catch(e) {}
        } else {
          // Fallback: try window.open with current BACKEND_URL
          try { window.open(BACKEND_URL + "/plugin/index.html", "_blank"); } catch(e) {}
        }
        throw new Error(
          "System Audio requires a browser.\n" +
          "Opening EasyScript in your default browser...\n" +
          "Use the browser window to capture tab audio."
        );
      }

      let stream;
      try {
        stream = await navigator.mediaDevices.getDisplayMedia({
          video: true,
          audio: true,
        });
      } catch (e) {
        if (e.name === "NotAllowedError") {
          throw new Error("Permission denied or cancelled.");
        }
        throw new Error("Screen sharing failed: " + e.message);
      }

      // Check for audio
      let audioTracks = stream.getAudioTracks();
      console.log(`[live] getDisplayMedia: ${audioTracks.length} audio tracks, ${stream.getVideoTracks().length} video tracks`);

      if (audioTracks.length > 0) {
        // Got audio — success!
        console.log(`[live] Audio track: ${audioTracks[0].label}`);
      } else {
        // No audio from getDisplayMedia — common on macOS
        // Try to get system audio via getUserMedia as fallback
        // This works if user has a virtual audio device (BlackHole, etc.)
        console.log("[live] No audio from getDisplayMedia, trying system audio fallback...");

        // Check if there's a "system audio" or "loopback" device available
        try {
          const devices = await navigator.mediaDevices.enumerateDevices();
          const systemAudio = devices.find(d =>
            d.kind === "audioinput" &&
            (d.label.toLowerCase().includes("blackhole") ||
             d.label.toLowerCase().includes("loopback") ||
             d.label.toLowerCase().includes("soundflower") ||
             d.label.toLowerCase().includes("system") ||
             d.label.toLowerCase().includes("virtual"))
          );

          if (systemAudio) {
            console.log(`[live] Found virtual audio device: ${systemAudio.label}`);
            const audioStream = await navigator.mediaDevices.getUserMedia({
              audio: { deviceId: { exact: systemAudio.deviceId } }
            });
            // Combine: video from getDisplayMedia + audio from virtual device
            audioStream.getAudioTracks().forEach(t => stream.addTrack(t));
            audioTracks = stream.getAudioTracks();
          }
        } catch (e2) {
          console.log("[live] Virtual audio fallback failed:", e2);
        }
      }

      if (audioTracks.length === 0) {
        // Still no audio — stop and show guidance
        stream.getTracks().forEach(t => t.stop());
        throw new Error(
          "Could not capture audio. Try:\n" +
          "• Use 'Microphone' mode instead\n" +
          "• Install BlackHole (free) for system audio capture\n" +
          "• Or download the video/audio and use the Editor tab"
        );
      }

      // Monitor tracks — auto-stop when sharing ends
      stream.getTracks().forEach(t => {
        t.addEventListener("ended", () => {
          if (liveRunning) doPause();
        });
      });

      return stream;
    }
  }

  // ── Connect WebSocket and start audio pipeline ──
  function startAudioPipeline(stream) {
    liveMediaStream = stream;

    // Use native sample rate — we downsample to 16kHz manually
    liveAudioCtx = new AudioContext();
    const nativeRate = liveAudioCtx.sampleRate;
    console.log(`[live] AudioContext sampleRate: ${nativeRate}`);

    const source = liveAudioCtx.createMediaStreamSource(stream);

    // Smaller buffer = more frequent sends = more responsive VAD on backend
    const bufferSize = 2048;  // ~42ms at 48kHz
    liveWorklet = liveAudioCtx.createScriptProcessor(bufferSize, 1, 1);

    liveWorklet.onaudioprocess = (e) => {
      if (!liveWs || liveWs.readyState !== WebSocket.OPEN) return;
      const float32 = e.inputBuffer.getChannelData(0);
      // Send ALL audio to backend — backend has VAD for speech detection
      // (frontend silence filtering caused missed speech starts)
      const int16 = downsampleTo16k(float32, nativeRate);
      liveWs.send(int16.buffer);
      // Keep a copy for post-session playback (16kHz mono PCM). Slice to detach
      // from the underlying buffer so it isn't mutated by the next callback.
      liveRecordedChunks.push(int16.slice());
    };

    source.connect(liveWorklet);
    liveWorklet.connect(liveAudioCtx.destination);

    // Open WebSocket
    const wsUrl = BACKEND_URL.replace(/^http/, "ws") + "/ws/live";
    liveWs = new WebSocket(wsUrl);

    liveWs.onopen = () => {
      const model = document.getElementById("liveModelSelect")?.value || "base";
      const language = document.getElementById("liveLanguageSelect")?.value || "";
      liveWs.send(JSON.stringify({
        action: "start",
        model: model,
        language: language || "auto",
        time_offset: liveElapsedBefore,
      }));
      setLiveStatus("Connected — listening...");
    };

    liveWs.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === "status") {
        setLiveStatus(`Listening — model: ${data.model}, lang: ${data.language}`);

      } else if (data.type === "partial") {
        // Hypothesis tail updated — feed its per-word timecodes into the live
        // word reveal so the unsettled tail appears at the speaker's rhythm too.
        if (LIVE_SHOW_HYPOTHESIS) {
          liveHypWords = Array.isArray(data.words) ? data.words : [];
          refreshLiveTarget();
        }
        setLiveStatus(`Listening... ${liveSegments.length} segments`);

      } else if (data.type === "draft_segment") {
        // In-progress committed words for the upcoming line. A draft for a NEW
        // line (next_index past the one we're holding on the live box) means
        // the previous finished line should now graduate down into the list.
        const draftText = data.text || "";
        const nextIdx = typeof data.next_index === "number" ? data.next_index : -1;
        if (livePendingGraduation && nextIdx > livePendingGraduation.index) {
          graduatePending();
          resetLiveLine();   // new line → restart the append-only live buffer
        }
        liveDraftText = draftText;          // kept for translation pairing
        liveDraftNextIndex = nextIdx;
        liveDraftWords = Array.isArray(data.words) ? data.words : [];
        refreshLiveTarget();                // feed committed words (timecode-paced)
        const draftWordCount = draftText ? draftText.trim().split(/\s+/).length : 0;
        if (nextIdx >= 0 && draftWordCount >= 4) {
          liveTransLangs.forEach(langCode => {
            preTranslateDraft(nextIdx, draftText, langCode);
          });
        }

      } else if (data.type === "final_segment") {
        // A line is complete. It stays on the highlighted live box (every line
        // passes through there) and only graduates into the list when the NEXT
        // line appears. We flush any words still queued so the line shows in
        // full, then freeze the box on it.
        const seg = data.segment;
        if (seg && seg.text) {
          if (livePendingGraduation) { graduatePending(); resetLiveLine(); }
          flushLiveWords();                 // reveal remaining queued words now
          const idx = typeof data.index === "number" ? data.index : liveSegments.length;
          livePendingGraduation = {
            seg,
            index: idx,
            draftText: liveDraftText,
            draftIdx: liveDraftNextIndex,
          };
          // Freeze: clear word sources so a partial for the next line can't feed
          // onto this held line before it graduates.
          liveDraftWords = [];
          liveHypWords = [];
          setLiveStatus(`${liveSegments.length + 1} segments`);
        }

      } else if (data.type === "stopped") {
        // Session ended — merge any remaining
        const finalSegs = data.segments || [];
        const prevSegs = liveSegments.filter(s => s._prev);
        liveSegments = [...prevSegs, ...finalSegs];
        clearLiveDisplay();
        setLiveStatus(`Paused — ${liveSegments.length} segments`);

      } else if (data.type === "error") {
        setLiveStatus(`Error: ${data.message}`, "error");
      }
    };

    liveWs.onerror = () => setLiveStatus("WebSocket error — check backend", "error");

    liveWs.onclose = () => {
      if (liveRunning) {
        setLiveStatus("Connection lost", "error");
        doPause(true);
      }
    };
  }

  // ── Stop capture (cleanup streams) ──
  function stopCapture(sendStop = true) {
    if (sendStop && liveWs && liveWs.readyState === WebSocket.OPEN) {
      try { liveWs.send(JSON.stringify({ action: "stop" })); } catch (e) {}
    }
    setTimeout(() => {
      if (liveWs) { try { liveWs.close(); } catch (e) {} liveWs = null; }
    }, 1500);
    if (liveWorklet) { try { liveWorklet.disconnect(); } catch (e) {} liveWorklet = null; }
    if (liveAudioCtx) { try { liveAudioCtx.close(); } catch (e) {} liveAudioCtx = null; }
    if (liveMediaStream) {
      liveMediaStream.getTracks().forEach(t => t.stop());
      liveMediaStream = null;
    }
  }

  // ── UI state transitions ──
  function showRunningUI() {
    // Focus mode: hide setup/export/settings, show running bar with Pause + Stop
    setupSection.classList.add("hidden");
    runningBar.classList.remove("hidden");
    pauseBtn.classList.remove("hidden");
    stopBtn.classList.remove("hidden");
    continueBtn.classList.add("hidden");
    newBtn.classList.add("hidden");
    document.getElementById("liveExportToggle")?.closest(".section")?.classList.add("hidden");
    document.getElementById("liveSettingsSection")?.classList.add("hidden");
    document.getElementById("liveRecordingSection")?.classList.add("hidden");
    livePanel.classList.add("live-focus");
  }

  function showPausedUI() {
    // Still in focus mode, show Stop + Continue + New (hide Pause)
    setupSection.classList.add("hidden");
    runningBar.classList.remove("hidden");
    pauseBtn.classList.add("hidden");
    stopBtn.classList.remove("hidden");
    continueBtn.classList.remove("hidden");
    newBtn.classList.remove("hidden");
    // Show export when paused
    document.getElementById("liveExportToggle")?.closest(".section")?.classList.remove("hidden");
    livePanel.classList.add("live-focus");
  }

  function showStoppedUI() {
    // Full UI — exit focus mode, show everything, data preserved
    setupSection.classList.remove("hidden");
    runningBar.classList.add("hidden");
    document.getElementById("liveExportToggle")?.closest(".section")?.classList.remove("hidden");
    document.getElementById("liveSettingsSection")?.classList.remove("hidden");
    livePanel.classList.remove("live-focus");
  }

  // ── Typewriter on the combined live line ──
  // We animate the COMBINED text (draft + hypothesis), not the parts in
  // isolation. When the backend promotes a word from hypothesis → draft, the
  // combined target text doesn't change, so the typewriter just keeps going
  // and there's no visible re-type. Only when Whisper actually revises the
  // hypothesis (the words change, not just where they're stored) do we apply
  // a single edit at the divergence point.
  function setLiveHypothesis(text) {
    liveHypothesisText = text || "";
    scheduleRecompute();
  }

  function setLiveDraft(text, nextIndex) {
    liveDraftText = text || "";
    liveDraftNextIndex = (typeof nextIndex === "number") ? nextIndex : liveDraftNextIndex;
    scheduleRecompute();
  }

  function scheduleRecompute() {
    if (liveRecomputeTimer !== null) return;
    liveRecomputeTimer = setTimeout(() => {
      liveRecomputeTimer = null;
      recomputeDisplayedTarget();
    }, RECOMPUTE_DEBOUNCE_MS);
  }

  function clearLiveDisplay() {
    if (liveRecomputeTimer !== null) {
      clearTimeout(liveRecomputeTimer);
      liveRecomputeTimer = null;
    }
    liveDraftText = "";
    liveDraftNextIndex = -1;
    liveHypothesisText = "";
    liveDisplayedText = "";
    liveDisplayedTarget = "";
    liveLastRecomputeAt = 0;
    liveCadenceMs = CADENCE_DEFAULT_MS;
    livePendingFinal = null;
    livePendingGraduation = null;
    // Reset timecode word-reveal state too.
    if (liveRevealTimer) { clearTimeout(liveRevealTimer); liveRevealTimer = null; }
    if (liveIngestTimer) { clearTimeout(liveIngestTimer); liveIngestTimer = null; }
    liveShownWords = [];
    liveWordQueue = [];
    liveDraftWords = [];
    liveHypWords = [];
    liveLastWordT = null;
    stopDisplayAnim();
    renderLiveSegments();
  }

  function recomputeDisplayedTarget() {
    // While a finished line is held awaiting graduation, the live box is FROZEN
    // on that line — ignore stray drafts/partials for the next line until it
    // actually graduates (which resets the buffer). Prevents any mixing/flicker.
    if (livePendingGraduation) return;

    // Smart-join draft + hypothesis with exactly one space between, trimming
    // any whitespace at the seam so we don't get double spaces.
    let target = "";
    if (liveDraftText && liveHypothesisText) {
      target = liveDraftText.replace(/\s+$/, "") + " " + liveHypothesisText.replace(/^\s+/, "");
    } else {
      target = liveDraftText || liveHypothesisText || "";
    }
    liveDisplayedTarget = target;

    if (!target) {
      liveDisplayedText = "";
      stopDisplayAnim();
      renderLiveSegments();
      return;
    }

    // ── Append-only live line (instant, zero flicker) ──────────────────────
    // The live box grows MONOTONICALLY: words already on screen are never
    // rewritten or retracted, so there is no flicker. We only extend with words
    // beyond what's already shown, and we render them immediately (no typewriter
    // delay). If Whisper later revises an earlier word, the live box keeps the
    // original — the correction surfaces in the list when the line graduates
    // (that uses the backend's final, corrected text).
    const shownWords = liveDisplayedText ? liveDisplayedText.split(/\s+/) : [];
    const targetWords = target.split(/\s+/);
    if (targetWords.length > shownWords.length) {
      liveDisplayedText = shownWords
        .concat(targetWords.slice(shownWords.length))
        .join(" ");
    }
    stopDisplayAnim();
    renderLiveSegments();
  }

  // Reset the live box for a brand-new line so the append-only buffer starts
  // fresh (called when the previous line graduates into the list).
  function resetLiveLine() {
    if (liveRevealTimer) { clearTimeout(liveRevealTimer); liveRevealTimer = null; }
    if (liveIngestTimer) { clearTimeout(liveIngestTimer); liveIngestTimer = null; }
    liveShownWords = [];
    liveWordQueue = [];
    liveDraftWords = [];
    liveHypWords = [];
    liveLastWordT = null;
    liveDisplayedText = "";
    liveDisplayedTarget = "";
    liveDraftText = "";
    liveHypothesisText = "";
    liveDraftNextIndex = -1;
  }

  // Join word tokens into a line, with no space before punctuation.
  function composeLiveWords(words) {
    let out = "";
    for (const s of words) {
      if (!s) continue;
      if (out && !/^[,.!?;:…)»”'’%]/.test(s)) out += " ";
      out += s;
    }
    return out;
  }

  // Feed the latest full word list for the current line. Append-only: only
  // words past what we already know (shown + queued) are added — earlier words
  // are never touched, even if Whisper revised them.
  function ingestLiveWords(wordList) {
    if (livePendingGraduation) return; // frozen on the held line until it graduates
    const known = liveShownWords.length + liveWordQueue.length;
    if (wordList.length > known) {
      for (let i = known; i < wordList.length; i++) liveWordQueue.push(wordList[i]);
      scheduleWordReveal();
    }
  }

  // draft_segment and partial arrive as two separate WS messages in the same
  // backend cycle. Coalesce them with a tiny debounce so we ingest a consistent
  // (same-cycle) committed+hypothesis word list — avoids a stale-hypothesis mix.
  function refreshLiveTarget() {
    if (liveIngestTimer) return;
    liveIngestTimer = setTimeout(() => {
      liveIngestTimer = null;
      ingestLiveWords(liveDraftWords.concat(liveHypWords));
    }, 12);
  }

  // Reveal the next queued word after a delay derived from its timecode gap to
  // the previous word (the speaker's rhythm), with catch-up when behind.
  function scheduleWordReveal() {
    if (liveRevealTimer || liveWordQueue.length === 0) return;
    const word = liveWordQueue[0];
    let gapMs = (liveLastWordT != null) ? Math.max(0, (word.t - liveLastWordT) * 1000) : 0;
    if (liveWordQueue.length > WORD_CATCHUP_BACKLOG) gapMs = Math.min(gapMs, WORD_CATCHUP_MS);
    const delay = Math.max(WORD_MIN_MS, Math.min(gapMs, WORD_MAX_GAP_MS));
    liveRevealTimer = setTimeout(() => {
      liveRevealTimer = null;
      const w = liveWordQueue.shift();
      if (!w) return;
      liveShownWords.push(w.w);
      liveLastWordT = w.t;
      liveDisplayedText = composeLiveWords(liveShownWords);
      renderLiveSegments();
      scheduleWordReveal();
    }, delay);
  }

  // Reveal all remaining queued words instantly (e.g. when a line finalizes, so
  // the full line is shown before it graduates into the list).
  function flushLiveWords() {
    if (liveRevealTimer) { clearTimeout(liveRevealTimer); liveRevealTimer = null; }
    if (liveIngestTimer) { clearTimeout(liveIngestTimer); liveIngestTimer = null; }
    while (liveWordQueue.length) {
      const w = liveWordQueue.shift();
      liveShownWords.push(w.w);
      liveLastWordT = w.t;
    }
    liveDisplayedText = composeLiveWords(liveShownWords);
    renderLiveSegments();
  }

  // Words still waiting to be revealed on the live box.
  function remainingRevealWords() {
    if (liveDisplayedTarget === liveDisplayedText) return 0;
    if (!liveDisplayedTarget.startsWith(liveDisplayedText)) return 1;
    const rem = liveDisplayedTarget.slice(liveDisplayedText.length).trim();
    return rem ? rem.split(/\s+/).length : 0;
  }

  // Per-word delay: comfortable default when keeping up; speeds up as the
  // backlog grows so the line catches up to the live voice, with a fast floor.
  function revealCadence(remaining) {
    if (remaining <= 1) return REVEAL_DEFAULT_MS;
    const paced = Math.round(REVEAL_CATCHUP_BUDGET_MS / remaining);
    return Math.max(REVEAL_FAST_MS, Math.min(REVEAL_DEFAULT_MS, paced));
  }

  function startDisplayAnim() {
    // Self-scheduling: each word's delay is recomputed from the current backlog
    // (setTimeout, not setInterval), so a mid-reveal backlog spike speeds it up.
    stopDisplayAnim();
    scheduleNextReveal();
  }

  function scheduleNextReveal() {
    const remaining = remainingRevealWords();
    if (remaining <= 0) { liveDisplayTimer = null; return; }
    const delay = revealCadence(remaining);
    liveDisplayTimer = setTimeout(() => {
      liveDisplayTimer = null;
      advanceDisplayAnim();
      if (liveDisplayedText !== liveDisplayedTarget) scheduleNextReveal();
    }, delay);
  }

  function stopDisplayAnim() {
    if (liveDisplayTimer) {
      clearTimeout(liveDisplayTimer);
      liveDisplayTimer = null;
    }
  }

  function advanceDisplayAnim() {
    if (liveDisplayedText === liveDisplayedTarget) {
      stopDisplayAnim();
      if (livePendingFinal) {
        const pending = livePendingFinal;
        pushFinalSegment(pending.seg, pending.draftText, pending.draftIdx);
      }
      return;
    }
    if (liveDisplayedTarget.startsWith(liveDisplayedText)) {
      const remainder = liveDisplayedTarget.slice(liveDisplayedText.length);
      const m = remainder.match(/^(\s*\S+)/);
      liveDisplayedText = m ? liveDisplayedText + m[1] : liveDisplayedTarget;
    } else {
      // Defensive — recomputeDisplayedTarget should have rebased us already
      liveDisplayedText = liveDisplayedTarget;
    }
    renderLiveSegments();
  }

  function pushFinalSegment(seg, draftText, draftIdx) {
    const segIdx = liveSegments.length;
    liveSegments.push(seg);
    livePendingFinal = null;
    clearLiveDisplay();
    setLiveStatus(`${liveSegments.length} segments`);
    liveTransLangs.forEach(langCode => {
      const cached = useDraftTranslation(segIdx, seg.text, langCode, draftIdx, draftText);
      if (!cached) {
        translateLiveSegment(segIdx, seg.text, langCode);
      }
    });
  }

  // Show a finished line on the live (realtime) box, without moving it into the
  // list yet. Appends any final words not yet shown (append-only), then FREEZES
  // the box (clears draft/hypothesis) so a partial for the NEXT line can't get
  // appended onto this held line before it graduates.
  function showFinalOnLiveBox(text) {
    const t = text || "";
    const shownWords = liveDisplayedText ? liveDisplayedText.split(/\s+/) : [];
    const tWords = t ? t.split(/\s+/) : [];
    if (tWords.length > shownWords.length) {
      liveDisplayedText = shownWords.concat(tWords.slice(shownWords.length)).join(" ");
    }
    liveDisplayedTarget = liveDisplayedText;
    liveDraftText = "";
    liveHypothesisText = "";
    liveDraftNextIndex = -1;
    if (liveRecomputeTimer !== null) { clearTimeout(liveRecomputeTimer); liveRecomputeTimer = null; }
    stopDisplayAnim();
    renderLiveSegments();
  }

  // Move the line currently held on the live box down into the finalized list
  // (and kick its translation). Called when the next line begins.
  function graduatePending() {
    if (!livePendingGraduation) return;
    const p = livePendingGraduation;
    livePendingGraduation = null;
    const segIdx = liveSegments.length;
    liveSegments.push(p.seg);
    liveTransLangs.forEach(langCode => {
      const cached = useDraftTranslation(segIdx, p.seg.text, langCode, p.draftIdx, p.draftText);
      if (!cached) {
        translateLiveSegment(segIdx, p.seg.text, langCode);
      }
    });
    // Note: don't clear the live box here — the caller sets the next line's
    // draft/final right after, which replaces the box contents.
  }

  // ── Pre-translate draft sentences for faster final translation ──
  // Backend emits draft_segment when the committed prefix grows; we kick off a
  // translation immediately so the result is ready (or in flight) by the time
  // the final_segment arrives.
  async function preTranslateDraft(nextIndex, text, langCode) {
    if (!text || nextIndex < 0) return;
    if (!liveDraftTranslations[langCode]) liveDraftTranslations[langCode] = {};
    // Same source already pre-translated (or in flight) — skip
    if (liveDraftSourceTexts[nextIndex] === text && (liveDraftTranslations[langCode][nextIndex] || liveDraftTranslations[langCode][nextIndex] === "")) {
      return;
    }
    // Abort previous in-flight pre-translation for this slot
    const aborterKey = `${langCode}:${nextIndex}`;
    if (liveDraftAborters[aborterKey]) {
      try { liveDraftAborters[aborterKey].abort(); } catch (_) {}
    }
    const ctrl = new AbortController();
    liveDraftAborters[aborterKey] = ctrl;

    liveDraftSourceTexts[nextIndex] = text;
    liveDraftTranslations[langCode][nextIndex] = ""; // reserve

    const provider = document.getElementById("translationProvider")?.value || "ollama";
    try {
      const res = await fetch(BACKEND_URL + "/translate/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: text,
          source_lang: "auto",
          target_lang: langCode,
          provider: provider,
        }),
        signal: ctrl.signal,
      });
      if (!res.body) {
        const fallback = await res.text();
        if (liveDraftSourceTexts[nextIndex] === text) {
          liveDraftTranslations[langCode][nextIndex] = (fallback || "").trim();
        }
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let acc = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        acc += decoder.decode(value, { stream: true });
        // If draft text changed while streaming, the request was already aborted —
        // but guard once more in case the abort raced with a chunk arrival.
        if (liveDraftSourceTexts[nextIndex] !== text) return;
      }
      acc += decoder.decode();
      if (liveDraftSourceTexts[nextIndex] === text) {
        liveDraftTranslations[langCode][nextIndex] = acc.trim();
      }
    } catch (e) {
      if (e?.name === "AbortError") return;
      console.warn(`[live] Pre-translation failed for draft #${nextIndex} → ${langCode}:`, e);
    } finally {
      if (liveDraftAborters[aborterKey] === ctrl) {
        delete liveDraftAborters[aborterKey];
      }
    }
  }

  // Returns true if a cached draft translation was applied; false if caller
  // should fall back to a fresh translation request.
  function useDraftTranslation(segIdx, finalText, langCode, draftIdx, draftText) {
    if (draftIdx !== segIdx || !draftText) return false;
    // Compare normalized (strip trailing/leading whitespace + sentence punct)
    const norm = (s) => (s || "").trim().replace(/[\s.!?。！？,;，；:]+$/u, "").toLowerCase();
    if (norm(finalText) !== norm(draftText)) return false;
    const cached = liveDraftTranslations[langCode]?.[segIdx];
    if (typeof cached !== "string" || !cached) return false;
    if (!liveTranslations[langCode]) liveTranslations[langCode] = {};
    liveTranslations[langCode][segIdx] = cached;
    renderLiveSegments();
    return true;
  }

  // ── Realtime translation for live segments ──
  async function translateLiveSegment(segIdx, text, langCode) {
    // Skip if already translated (non-empty string)
    if (liveTranslations[langCode]?.[segIdx]) return;

    if (!liveTranslations[langCode]) liveTranslations[langCode] = {};
    // Reserve slot so UI shows "translating..." instead of nothing
    liveTranslations[langCode][segIdx] = "";

    const provider = document.getElementById("translationProvider")?.value || "ollama";

    try {
      const res = await fetch(BACKEND_URL + "/translate/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: text,
          source_lang: "auto",
          target_lang: langCode,
          provider: provider,
        }),
      });

      if (!res.body) {
        // Browser doesn't support body streaming — read full response
        const fallback = await res.text();
        liveTranslations[langCode][segIdx] = fallback;
        renderLiveSegments();
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let acc = "";
      let renderScheduled = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        acc += decoder.decode(value, { stream: true });
        liveTranslations[langCode][segIdx] = acc;
        // Throttle re-renders to ~60ms — feels smooth without thrashing layout
        if (!renderScheduled) {
          renderScheduled = true;
          setTimeout(() => {
            renderScheduled = false;
            renderLiveSegments();
          }, 60);
        }
      }
      // Flush decoder + final render
      acc += decoder.decode();
      liveTranslations[langCode][segIdx] = acc.trim();
      renderLiveSegments();
    } catch (e) {
      console.warn(`[live] Translation failed for seg ${segIdx} → ${langCode}:`, e);
      // Allow retry by clearing the empty placeholder
      if (liveTranslations[langCode][segIdx] === "") {
        delete liveTranslations[langCode][segIdx];
      }
    }
  }

  async function prewarmTranslator() {
    const provider = document.getElementById("translationProvider")?.value || "ollama";
    if (provider !== "ollama") return;
    if (!liveTransLangs.length) return;
    try {
      // Single warmup call — same model serves all target languages
      await fetch(BACKEND_URL + "/translate/prewarm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: "warmup",
          source_lang: "auto",
          target_lang: liveTransLangs[0] || "en",
          provider: provider,
        }),
      });
    } catch (e) {
      console.warn("[live] Translator prewarm failed:", e);
    }
  }

  // ── Live recording (capture → WAV → playback) ──────────────────────────
  function resetRecording() {
    liveRecordedChunks = [];
    liveRecActiveIdx = -1;
    if (liveRecBlobUrl) { URL.revokeObjectURL(liveRecBlobUrl); liveRecBlobUrl = null; }
    const audio = document.getElementById("liveRecAudio");
    if (audio) { try { audio.pause(); } catch (_) {} audio.removeAttribute("src"); audio.load(); }
    const sec = document.getElementById("liveRecordingSection");
    if (sec) sec.classList.add("hidden");
  }

  function buildLiveWavBlob() {
    let length = 0;
    for (const c of liveRecordedChunks) length += c.length;
    if (length === 0) return null;
    const pcm = new Int16Array(length);
    let p = 0;
    for (const c of liveRecordedChunks) { pcm.set(c, p); p += c.length; }

    const sr = LIVE_REC_SAMPLE_RATE;
    const buffer = new ArrayBuffer(44 + pcm.length * 2);
    const view = new DataView(buffer);
    const ws = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    ws(0, "RIFF");
    view.setUint32(4, 36 + pcm.length * 2, true);
    ws(8, "WAVE");
    ws(12, "fmt ");
    view.setUint32(16, 16, true);   // PCM chunk size
    view.setUint16(20, 1, true);    // PCM format
    view.setUint16(22, 1, true);    // mono
    view.setUint32(24, sr, true);
    view.setUint32(28, sr * 2, true); // byte rate
    view.setUint16(32, 2, true);    // block align
    view.setUint16(34, 16, true);   // bits per sample
    ws(36, "data");
    view.setUint32(40, pcm.length * 2, true);
    new Int16Array(buffer, 44).set(pcm);
    return new Blob([buffer], { type: "audio/wav" });
  }

  // Build the WAV from captured PCM and wire up the playback section.
  function finalizeRecording() {
    const blob = buildLiveWavBlob();
    const sec = document.getElementById("liveRecordingSection");
    if (!blob || !sec) { if (sec) sec.classList.add("hidden"); return; }
    if (liveRecBlobUrl) URL.revokeObjectURL(liveRecBlobUrl);
    liveRecActiveIdx = -1;
    liveRecBlobUrl = URL.createObjectURL(blob);
    const audio = document.getElementById("liveRecAudio");
    const dl = document.getElementById("liveRecDownload");
    if (dl) dl.href = liveRecBlobUrl;
    if (audio) { audio.src = liveRecBlobUrl; audio.load(); }
    sec.classList.remove("hidden");
  }

  function initRecordingPlayer() {
    const audio = document.getElementById("liveRecAudio");
    const playBtn = document.getElementById("liveRecPlayBtn");
    const playIcon = document.getElementById("liveRecPlayIcon");
    const seek = document.getElementById("liveRecSeek");
    const cur = document.getElementById("liveRecCurrent");
    const tot = document.getElementById("liveRecTotal");
    if (!audio || !playBtn || !seek) return;

    const setIcon = () => { playIcon.innerHTML = audio.paused ? "&#9654;" : "&#9646;&#9646;"; };

    playBtn.addEventListener("click", () => {
      if (audio.paused) audio.play(); else audio.pause();
    });
    audio.addEventListener("play", setIcon);
    audio.addEventListener("pause", setIcon);
    audio.addEventListener("ended", setIcon);
    audio.addEventListener("loadedmetadata", () => {
      const d = isFinite(audio.duration) ? audio.duration : 0;
      seek.max = d || 100;
      if (tot) tot.textContent = fmtTime(d);
      if (cur) cur.textContent = "0:00";
      seek.value = 0;
    });
    audio.addEventListener("timeupdate", () => {
      if (cur) cur.textContent = fmtTime(audio.currentTime);
      if (!seek.matches(":active")) seek.value = audio.currentTime;
      highlightRecordingSegment(audio.currentTime);
    });
    seek.addEventListener("input", () => { audio.currentTime = parseFloat(seek.value) || 0; });
  }

  // During playback, highlight the segment matching the current timecode and
  // keep it focused (scrolled into view) so you can follow along.
  function highlightRecordingSegment(time) {
    // Segment containing `time`, else the last one that has started.
    let idx = -1;
    for (let i = 0; i < liveSegments.length; i++) {
      const s = liveSegments[i];
      const end = (typeof s.end === "number") ? s.end : (s.start + 30);
      if (time >= s.start && time < end) { idx = i; break; }
    }
    if (idx === -1) {
      for (let i = liveSegments.length - 1; i >= 0; i--) {
        if (liveSegments[i].start <= time) { idx = i; break; }
      }
    }
    if (idx === liveRecActiveIdx) return;
    liveRecActiveIdx = idx;

    const list = document.getElementById("liveSegmentList");
    if (!list) return;
    list.querySelectorAll(".rec-active").forEach(el => el.classList.remove("rec-active"));
    if (idx < 0) return;
    const inner = list.querySelector(`[data-seg-idx="${idx}"]`);
    if (!inner) return;
    const target = inner.closest(".segment-item") || inner;
    target.classList.add("rec-active");
    // Keep it centered within the list container (not the whole page).
    const r = target.getBoundingClientRect();
    const lr = list.getBoundingClientRect();
    if (r.top < lr.top || r.bottom > lr.bottom) {
      list.scrollTop += (r.top - lr.top) - (lr.height - r.height) / 2;
    }
  }

  // Seek the recording to a segment's timecode and play it.
  function playRecordingAt(timeSec) {
    const audio = document.getElementById("liveRecAudio");
    if (!audio || !audio.src) return;
    audio.currentTime = Math.max(0, timeSec || 0);
    audio.play().catch(() => {});
  }

  function startTimer() {
    liveStartTime = Date.now();
    liveTimerInterval = setInterval(() => {
      const elapsed = liveElapsedBefore + Math.floor((Date.now() - liveStartTime) / 1000);
      const m = Math.floor(elapsed / 60);
      const s = elapsed % 60;
      document.getElementById("liveElapsed").textContent = `${m}:${String(s).padStart(2, "0")}`;
    }, 1000);
  }

  function stopTimer() {
    if (liveTimerInterval) { clearInterval(liveTimerInterval); liveTimerInterval = null; }
    // Accumulate elapsed time
    if (liveStartTime > 0) {
      liveElapsedBefore += Math.floor((Date.now() - liveStartTime) / 1000);
      liveStartTime = 0;
    }
  }

  // ── Start (or restart — clears old data) ──
  async function doStart() {
    if (liveRunning) return;

    // Clear previous session data when starting fresh
    liveSegments = [];
    liveTranslations = {};
    liveDraftTranslations = {};
    liveDraftSourceTexts = {};
    liveDraftAborters = {};
    clearLiveDisplay();
    liveElapsedBefore = 0;
    resetRecording();
    renderLiveSegments();

    setLiveStatus("Connecting...");

    // Fire-and-forget translator warm-up in parallel with audio setup
    prewarmTranslator();

    try {
      // Get media stream DIRECTLY in click handler (required for getDisplayMedia)
      const stream = await getMediaStream();
      startAudioPipeline(stream);
    } catch (e) {
      console.error("[live] Start error:", e);
      setLiveStatus(`Error: ${e.message}`, "error");
      return;
    }

    liveRunning = true;
    livePaused = false;
    showRunningUI();
    startTimer();
  }

  // ── Continue (resume from pause without clearing data) ──
  async function doContinue() {
    if (liveRunning) return;

    setLiveStatus("Reconnecting...");
    document.getElementById("liveRecordingSection")?.classList.add("hidden");

    try {
      const stream = await getMediaStream();
      startAudioPipeline(stream);
    } catch (e) {
      console.error("[live] Continue error:", e);
      setLiveStatus(`Error: ${e.message}`, "error");
      return;
    }

    liveRunning = true;
    livePaused = false;
    showRunningUI();
    renderLiveSegments();   // back to newest-first (live) order
    startTimer();
  }

  // ── Pause (stay in focus mode, keep data, can continue) ──
  function doPause(connectionLost = false) {
    liveRunning = false;
    livePaused = true;
    stopTimer();

    if (!connectionLost) {
      stopCapture(true);
    } else {
      stopCapture(false);
    }

    finalizeRecording();
    showPausedUI();
    renderLiveSegments();   // flip to chronological (review) order

    // Mark existing segments as from previous session
    liveSegments.forEach(s => s._prev = true);

    if (!connectionLost) {
      setLiveStatus(`Paused — ${liveSegments.length} segments`);
    }
  }

  // ── Stop (exit focus mode, show full UI, data preserved for export) ──
  function doStop() {
    liveRunning = false;
    livePaused = false;
    stopTimer();
    stopCapture(true);
    finalizeRecording();
    showStoppedUI();
    renderLiveSegments();   // flip to chronological (review) order
    setLiveStatus(`Stopped — ${liveSegments.length} segments`);
  }

  // ── New (reset everything, back to initial state) ──
  function doNew() {
    liveRunning = false;
    livePaused = false;
    liveSegments = [];
    liveTranslations = {};
    liveDraftTranslations = {};
    liveDraftSourceTexts = {};
    liveDraftAborters = {};
    clearLiveDisplay();
    liveElapsedBefore = 0;
    stopCapture(false);
    resetRecording();
    showStoppedUI();
    renderLiveSegments();
    const el = document.getElementById("liveStatus");
    if (el) el.textContent = "";
  }

  // ── Button handlers ──
  if (startBtn) startBtn.addEventListener("click", doStart);
  if (pauseBtn) pauseBtn.addEventListener("click", () => doPause());
  if (stopBtn) stopBtn.addEventListener("click", doStop);
  if (continueBtn) continueBtn.addEventListener("click", doContinue);
  if (newBtn) newBtn.addEventListener("click", doNew);

  // Recording playback: build the player once, and let clicking a transcribed
  // line jump the recorded audio to that line's timecode.
  initRecordingPlayer();
  const liveListEl = document.getElementById("liveSegmentList");
  if (liveListEl) {
    liveListEl.addEventListener("click", (e) => {
      // Only when a recording exists (after Stop/Pause) and not while running.
      if (liveRunning || !liveRecBlobUrl) return;
      const item = e.target.closest(".segment-item[data-start]");
      if (!item) return;
      playRecordingAt(parseFloat(item.dataset.start) || 0);
    });
  }

  // ── Live filter tabs (Speech / Translation) ──
  const liveFilterTabs = document.querySelectorAll("[data-live-filter]");
  liveFilterTabs.forEach(tab => {
    tab.addEventListener("click", () => {
      liveFilter = tab.dataset.liveFilter;
      liveFilterTabs.forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      const transTabs = document.getElementById("liveTransTabs");
      if (transTabs) transTabs.classList.toggle("hidden", liveFilter !== "translation");
      renderLiveSegments();
    });
  });

  // ── Translation language tabs (max 2) ──
  function renderLiveTransTabs() {
    const list = document.getElementById("liveTransTabList");
    const addBtn = document.getElementById("liveTransAddBtn");
    if (!list) return;
    list.innerHTML = "";
    liveTransLangs.forEach(code => {
      const tab = document.createElement("button");
      tab.className = "trans-tab" + (code === liveActiveTransLang ? " active" : "");
      tab.textContent = code.toUpperCase();
      tab.addEventListener("click", () => {
        liveActiveTransLang = code;
        renderLiveTransTabs();
      });
      const rm = document.createElement("span");
      rm.className = "trans-tab-remove";
      rm.textContent = "×";
      rm.addEventListener("click", (e) => {
        e.stopPropagation();
        liveTransLangs = liveTransLangs.filter(c => c !== code);
        if (liveActiveTransLang === code) liveActiveTransLang = liveTransLangs[0] || "";
        renderLiveTransTabs();
      });
      tab.appendChild(rm);
      list.appendChild(tab);
    });
    if (addBtn) addBtn.classList.toggle("hidden", liveTransLangs.length >= MAX_LIVE_TRANS_LANGS);
    renderLiveSegments();
  }

  const liveAddBtn = document.getElementById("liveTransAddBtn");
  const livePicker = document.getElementById("liveLangPicker");
  const livePickerList = document.getElementById("liveLangPickerList");

  if (liveAddBtn && livePicker && livePickerList) {
    liveAddBtn.addEventListener("click", () => {
      if (liveTransLangs.length >= MAX_LIVE_TRANS_LANGS) return;
      livePickerList.innerHTML = "";
      const langs = TRANSLATION_LANGUAGES || [
        { code: "vi", name: "Vietnamese" }, { code: "en", name: "English" },
        { code: "zh", name: "Chinese" }, { code: "ja", name: "Japanese" },
        { code: "ko", name: "Korean" }, { code: "fr", name: "French" },
        { code: "de", name: "German" }, { code: "es", name: "Spanish" },
      ];
      langs.forEach(lang => {
        if (liveTransLangs.includes(lang.code)) return;
        const item = document.createElement("div");
        item.className = "lang-picker-item";
        item.textContent = `${lang.name} (${lang.code})`;
        item.addEventListener("click", () => {
          liveTransLangs.push(lang.code);
          liveActiveTransLang = lang.code;
          livePicker.classList.add("hidden");
          renderLiveTransTabs();
          // Translate all existing segments for the new language
          liveSegments.forEach((seg, idx) => {
            translateLiveSegment(idx, seg.text, lang.code);
          });
        });
        livePickerList.appendChild(item);
      });
      livePicker.classList.toggle("hidden");
    });
  }

  // ── Export ──
  const exportToggle = document.getElementById("liveExportToggle");
  if (exportToggle) {
    exportToggle.addEventListener("click", () => {
      const body = document.getElementById("liveExportBody");
      const chevron = document.getElementById("liveExportChevron");
      const isOpen = !body.classList.contains("hidden");
      body.classList.toggle("hidden");
      chevron.innerHTML = isOpen ? "&#9654;" : "&#9660;";
    });
  }

  const exportTxtBtn = document.getElementById("liveExportTxtBtn");
  if (exportTxtBtn) {
    exportTxtBtn.addEventListener("click", async () => {
      if (!liveSegments.length) return;
      const text = liveSegments.map(s => s.text).join("\n");
      const filename = `live_transcript_${new Date().toISOString().slice(0, 10)}.txt`;
      try {
        const res = await fetch(BACKEND_URL + "/save-file", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename, content: text }),
        });
        const data = await res.json();
        const info = document.getElementById("liveExportInfo");
        if (info) info.textContent = data.path ? `Saved: ${data.path}` : `Error: ${data.error}`;
      } catch (e) { console.error("[live] Export error:", e); }
    });
  }

  const exportSrtBtn = document.getElementById("liveExportSrtBtn");
  if (exportSrtBtn) {
    exportSrtBtn.addEventListener("click", async () => {
      if (!liveSegments.length) return;
      let srt = "";
      liveSegments.forEach((seg, i) => {
        const start = srtTime(seg.start);
        const end = srtTime(seg.end);
        srt += `${i + 1}\n${start} --> ${end}\n${seg.text}\n\n`;
      });
      const filename = `live_transcript_${new Date().toISOString().slice(0, 10)}.srt`;
      try {
        const res = await fetch(BACKEND_URL + "/save-file", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename, content: srt }),
        });
        const data = await res.json();
        const info = document.getElementById("liveExportInfo");
        if (info) info.textContent = data.path ? `Saved: ${data.path}` : `Error: ${data.error}`;
      } catch (e) { console.error("[live] Export error:", e); }
    });
  }

  function srtTime(sec) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    const ms = Math.round((sec % 1) * 1000);
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")},${String(ms).padStart(3, "0")}`;
  }

  // ── Settings toggle ──
  const settingsToggle = document.getElementById("liveSettingsToggle");
  if (settingsToggle) {
    settingsToggle.addEventListener("click", () => {
      const body = document.getElementById("liveSettingsBody");
      const chevron = document.getElementById("liveSettingsChevron");
      const isOpen = !body.classList.contains("hidden");
      body.classList.toggle("hidden");
      chevron.innerHTML = isOpen ? "&#9654;" : "&#9660;";
      // Refresh model lists when panel opens so newly-pulled / downloaded
      // models show up without needing a manual click.
      if (!isOpen) {
        const provider = document.getElementById("liveTransProvider")?.value || "ollama";
        if (provider === "ollama") {
          const savedModel = document.getElementById("liveOllamaModelSelect")?.value;
          refreshLiveOllamaModels(savedModel);
        } else if (provider === "hymt2") {
          refreshLiveHyMT2Status();
        } else if (provider === "nllb") {
          refreshLiveNLLBStatus();
        }
      }
    });
  }

  // (Segment settings removed — live mode always splits by sentence punctuation)

  // ── Text view toggle ──
  const textViewBtn = document.getElementById("liveTextViewToggle");
  if (textViewBtn) {
    textViewBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      liveTextView = !liveTextView;
      textViewBtn.classList.toggle("active", liveTextView);
      renderLiveSegments();
    });
  }

  // ── Live Search & Replace ──

  let liveSearchMatches = [];
  let liveSearchMatchIndex = -1;

  function liveGetSearchTargets() {
    if (liveFilter === "translation" && liveActiveTransLang) {
      return document.querySelectorAll("#liveSegmentList .live-trans-text");
    }
    return document.querySelectorAll("#liveSegmentList .live-speech-text");
  }

  function liveClearHighlights() {
    liveSearchMatches = [];
    liveSearchMatchIndex = -1;
    const countEl = document.getElementById("liveSearchCount");
    if (countEl) countEl.textContent = "";
    document.querySelectorAll("#liveSegmentList .search-highlight").forEach(el => {
      el.replaceWith(document.createTextNode(el.textContent));
      el.parentNode?.normalize();
    });
  }

  function liveHighlightActive() {
    document.querySelectorAll("#liveSegmentList .search-highlight.active")
      .forEach(el => el.classList.remove("active"));
    if (liveSearchMatchIndex < 0 || liveSearchMatchIndex >= liveSearchMatches.length) return;
    const allMarks = document.querySelectorAll("#liveSegmentList .search-highlight");
    if (allMarks[liveSearchMatchIndex]) {
      allMarks[liveSearchMatchIndex].classList.add("active");
      allMarks[liveSearchMatchIndex].scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  function liveUpdateCount() {
    const countEl = document.getElementById("liveSearchCount");
    if (!countEl) return;
    const q = document.getElementById("liveSearchInput")?.value.trim();
    countEl.textContent = liveSearchMatches.length === 0
      ? (q ? "0" : "")
      : `${liveSearchMatchIndex + 1}/${liveSearchMatches.length}`;
  }

  function liveRunSearch() {
    const query = document.getElementById("liveSearchInput")?.value.trim();
    liveClearHighlights();
    if (!query) return;

    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const regex = new RegExp(`(${escaped})`, "gi");

    liveGetSearchTargets().forEach(el => {
      const text = el.textContent;
      const hits = [...text.matchAll(new RegExp(escaped, "gi"))];
      if (hits.length > 0) {
        hits.forEach(m => liveSearchMatches.push({ el }));
        el.innerHTML = text.replace(regex, '<mark class="search-highlight">$1</mark>');
      }
    });

    if (liveSearchMatches.length > 0) {
      liveSearchMatchIndex = 0;
      liveHighlightActive();
    }
    liveUpdateCount();
  }

  function liveNavigateSearch(dir) {
    if (liveSearchMatches.length === 0) return;
    liveSearchMatchIndex = (liveSearchMatchIndex + dir + liveSearchMatches.length) % liveSearchMatches.length;
    liveHighlightActive();
    liveUpdateCount();
  }

  function liveReplaceCurrent() {
    const query = document.getElementById("liveSearchInput")?.value.trim();
    const replacement = document.getElementById("liveReplaceInput")?.value ?? "";
    if (!query || liveSearchMatches.length === 0 || liveSearchMatchIndex < 0) return;

    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const regex = new RegExp(escaped, "i");
    const match = liveSearchMatches[liveSearchMatchIndex];
    const el = match.el;

    if (liveFilter === "translation" && liveActiveTransLang) {
      const idx = parseInt(el.dataset.segIdx ?? "-1");
      if (idx >= 0 && liveTranslations[liveActiveTransLang]?.[idx]) {
        liveTranslations[liveActiveTransLang][idx] =
          liveTranslations[liveActiveTransLang][idx].replace(regex, replacement);
      }
    } else {
      const idx = parseInt(el.dataset.segIdx ?? "-1");
      if (idx >= 0 && liveSegments[idx]) {
        liveSegments[idx].text = (liveSegments[idx].text || "").replace(regex, replacement);
      }
    }
    renderLiveSegments();
    liveRunSearch();
  }

  function liveReplaceAll() {
    const query = document.getElementById("liveSearchInput")?.value.trim();
    const replacement = document.getElementById("liveReplaceInput")?.value ?? "";
    if (!query) return;

    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const regex = new RegExp(escaped, "gi");

    if (liveFilter === "translation" && liveActiveTransLang) {
      const transData = liveTranslations[liveActiveTransLang] || {};
      Object.keys(transData).forEach(k => {
        if (transData[k]) transData[k] = transData[k].replace(regex, replacement);
      });
    } else {
      liveSegments.forEach(seg => {
        if (seg.text) seg.text = seg.text.replace(regex, replacement);
      });
    }
    renderLiveSegments();
    liveRunSearch();
  }

  // ── Search toggle & button wiring ──
  const searchBtn = document.getElementById("liveSearchToggleBtn");
  const searchPanel = document.getElementById("liveSearchPanel");
  if (searchBtn && searchPanel) {
    searchBtn.addEventListener("click", () => {
      const opening = searchPanel.classList.contains("hidden");
      searchPanel.classList.toggle("hidden");
      if (opening) {
        document.getElementById("liveSearchInput")?.focus();
      } else {
        liveClearHighlights();
      }
    });

    document.getElementById("liveSearchInput")?.addEventListener("input", liveRunSearch);
    document.getElementById("liveSearchInput")?.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); liveNavigateSearch(e.shiftKey ? -1 : 1); }
      if (e.key === "Escape") { searchPanel.classList.add("hidden"); liveClearHighlights(); }
    });
    document.getElementById("liveSearchPrevBtn")?.addEventListener("click", () => liveNavigateSearch(-1));
    document.getElementById("liveSearchNextBtn")?.addEventListener("click", () => liveNavigateSearch(1));
    document.getElementById("liveReplaceOneBtn")?.addEventListener("click", liveReplaceCurrent);
    document.getElementById("liveReplaceAllBtn")?.addEventListener("click", liveReplaceAll);
  }

  // ── Live whisper model download/cache info (mirrors Editor's modelInfo) ──
  async function updateLiveModelInfo() {
    const info = document.getElementById("liveModelInfo");
    const sel = document.getElementById("liveModelSelect");
    if (!info || !sel) return;
    const selected = sel.value;
    const deviceTag = backendDevice ? ` · ${backendDevice}` : "";
    try {
      const data = await fetchBackend("/models");
      const model = (data.models || []).find(m => m.id === selected);
      const isCached = model ? model.cached : false;
      const isLoaded = data.current === selected;
      const size = model ? model.size : "";
      if (isLoaded) {
        info.innerHTML = `<span class="model-tag tag-loaded">READY</span> ${selected}${deviceTag}`;
      } else if (isCached) {
        info.innerHTML = `<span class="model-tag tag-loaded">CACHED</span> ${selected} — will switch on start${deviceTag}`;
      } else {
        info.innerHTML = `<span class="model-tag tag-download">DOWNLOAD</span> ${selected} (${size}) — first run will download${deviceTag}`;
      }
    } catch {
      info.innerHTML = `${selected}${deviceTag}`;
    }
  }
  document.getElementById("liveModelSelect")?.addEventListener("change", updateLiveModelInfo);
  // Initial render — and again whenever backend status refreshes.
  updateLiveModelInfo();
  setInterval(updateLiveModelInfo, 5000);

  // ── Live Ollama model list refresh ──
  async function refreshLiveOllamaModels(savedModel) {
    const select = document.getElementById("liveOllamaModelSelect");
    if (!select) return;
    try {
      const data = await fetchBackend("/ollama/status");
      const models = data.models || [];
      select.innerHTML = "";
      if (models.length === 0) {
        select.innerHTML = '<option value="">No models found</option>';
        return;
      }
      models.forEach(name => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        select.appendChild(opt);
      });
      if (savedModel && models.includes(savedModel)) {
        select.value = savedModel;
      } else {
        select.value = models[0];
      }
    } catch {
      select.innerHTML = '<option value="">Ollama not reachable</option>';
    }
  }
  document.getElementById("liveRefreshOllamaModelsBtn")?.addEventListener("click", () => refreshLiveOllamaModels());
  document.getElementById("liveOllamaUrlInput")?.addEventListener("change", () => refreshLiveOllamaModels());

  // ── Live Hy-MT2 status refresh ──
  async function refreshLiveHyMT2Status() {
    const statusText = document.getElementById("liveHymt2StatusText");
    const downloadBtn = document.getElementById("liveHymt2DownloadBtn");
    if (!statusText || !downloadBtn) return;
    const size = document.getElementById("liveHymt2ModelSize")?.value || "1.8B";
    try {
      const data = await fetchBackend(`/hymt2/status?model_size=${size}`);
      if (data.downloaded) {
        statusText.textContent = `✓ ${data.model_id} ready`;
        statusText.style.color = "var(--accent)";
        downloadBtn.textContent = "Re-download";
      } else {
        statusText.textContent = `Not downloaded — ${data.model_id}`;
        statusText.style.color = "";
        downloadBtn.textContent = "Download";
      }
    } catch {
      statusText.textContent = "Status check failed";
      statusText.style.color = "";
    }
  }

  // ── Live NLLB-200 status refresh ──
  async function refreshLiveNLLBStatus() {
    const statusText = document.getElementById("liveNllbStatusText");
    const downloadBtn = document.getElementById("liveNllbDownloadBtn");
    if (!statusText || !downloadBtn) return;
    const size = document.getElementById("liveNllbModelSize")?.value || "600M";
    try {
      const data = await fetchBackend(`/nllb/status?model_size=${size}`);
      if (data.downloaded) {
        statusText.textContent = `✓ ${data.model_id} ready`;
        statusText.style.color = "var(--accent)";
        downloadBtn.textContent = "Re-download";
      } else {
        statusText.textContent = `Not downloaded — ${data.model_id}`;
        statusText.style.color = "";
        downloadBtn.textContent = "Download";
      }
    } catch {
      statusText.textContent = "Status check failed";
      statusText.style.color = "";
    }
  }

  // ── Live settings provider toggle ──
  function updateLiveProviderUI(provider) {
    document.getElementById("liveOllamaSettings")?.classList.toggle("hidden", provider !== "ollama");
    document.getElementById("liveHymt2Settings")?.classList.toggle("hidden", provider !== "hymt2");
    document.getElementById("liveNllbSettings")?.classList.toggle("hidden", provider !== "nllb");
    document.getElementById("liveClaudeKeyRow")?.classList.toggle("hidden", provider !== "claude");
    if (provider === "hymt2") refreshLiveHyMT2Status();
    if (provider === "nllb") refreshLiveNLLBStatus();
  }

  const liveProviderSelect = document.getElementById("liveTransProvider");
  if (liveProviderSelect) {
    liveProviderSelect.addEventListener("change", () => updateLiveProviderUI(liveProviderSelect.value));
    updateLiveProviderUI(liveProviderSelect.value);
  }

  document.getElementById("liveHymt2ModelSize")?.addEventListener("change", refreshLiveHyMT2Status);
  document.getElementById("liveNllbModelSize")?.addEventListener("change", refreshLiveNLLBStatus);

  // ── Live NLLB-200 download (mirrors Live Hy-MT2 flow) ──
  document.getElementById("liveNllbDownloadBtn")?.addEventListener("click", async () => {
    const btn = document.getElementById("liveNllbDownloadBtn");
    const statusText = document.getElementById("liveNllbStatusText");
    const size = document.getElementById("liveNllbModelSize")?.value || "600M";
    btn.disabled = true;
    btn.textContent = "Downloading...";
    statusText.textContent = "Downloading model — this may take several minutes...";
    statusText.style.color = "";
    try {
      await fetchBackend("/nllb/download", {
        method: "POST",
        body: JSON.stringify({ model_size: size }),
      });
      let polling = true;
      while (polling) {
        await new Promise(r => setTimeout(r, 3000));
        const data = await fetchBackend(`/nllb/status?model_size=${size}`);
        const dlp = data.download_progress || {};
        if (data.downloaded || dlp.status === "done") {
          statusText.textContent = `✓ ${data.model_id} ready`;
          statusText.style.color = "var(--accent)";
          polling = false;
        } else if (dlp.status === "error") {
          statusText.textContent = `Error: ${dlp.detail || "download failed"}`;
          polling = false;
        } else if (dlp.detail) {
          statusText.textContent = dlp.detail;
        }
      }
    } catch (err) {
      statusText.textContent = `Error: ${err.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = "Re-download";
    }
  });

  // ── Live Hy-MT2 download (mirrors editor's hymt2DownloadBtn flow) ──
  document.getElementById("liveHymt2DownloadBtn")?.addEventListener("click", async () => {
    const btn = document.getElementById("liveHymt2DownloadBtn");
    const statusText = document.getElementById("liveHymt2StatusText");
    const size = document.getElementById("liveHymt2ModelSize")?.value || "1.8B";
    btn.disabled = true;
    btn.textContent = "Downloading...";
    statusText.textContent = "Downloading model — this may take several minutes...";
    statusText.style.color = "";
    try {
      await fetchBackend("/hymt2/download", {
        method: "POST",
        body: JSON.stringify({ model_size: size }),
      });
      let polling = true;
      while (polling) {
        await new Promise(r => setTimeout(r, 3000));
        const data = await fetchBackend(`/hymt2/status?model_size=${size}`);
        const dlp = data.download_progress || {};
        if (data.downloaded || dlp.status === "done") {
          statusText.textContent = `✓ ${data.model_id} ready`;
          statusText.style.color = "var(--accent)";
          polling = false;
        } else if (dlp.status === "error") {
          statusText.textContent = `Error: ${dlp.detail || "download failed"}`;
          polling = false;
        } else if (dlp.detail) {
          statusText.textContent = dlp.detail;
        }
      }
    } catch (err) {
      statusText.textContent = `Error: ${err.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = "Re-download";
    }
  });

  // ── Live save settings ──
  document.getElementById("liveSaveSettingsBtn")?.addEventListener("click", async () => {
    const settings = {
      hf_token: document.getElementById("liveHfTokenInput")?.value.trim() || "",
      translation_provider: document.getElementById("liveTransProvider")?.value || "ollama",
      ollama_url: document.getElementById("liveOllamaUrlInput")?.value.trim() || "http://localhost:11434",
      ollama_model: document.getElementById("liveOllamaModelSelect")?.value || "",
      hymt2_model_size: document.getElementById("liveHymt2ModelSize")?.value || "1.8B",
      nllb_model_size: document.getElementById("liveNllbModelSize")?.value || "600M",
      anthropic_api_key: document.getElementById("liveClaudeApiKeyInput")?.value.trim() || "",
    };
    try {
      await fetchBackend("/settings", { method: "POST", body: JSON.stringify(settings) });
      const btn = document.getElementById("liveSaveSettingsBtn");
      if (btn) { btn.textContent = "Saved!"; setTimeout(() => { btn.textContent = "Save Settings"; }, 1500); }
    } catch (e) {
      console.error("Save settings error:", e);
    }
  });

  // ── Load saved settings into Live inputs ──
  // Settings are shared with Editor — but Live has its own inputs, so we
  // populate them from the same /settings endpoint on tab init.
  async function loadLiveSavedSettings() {
    try {
      const s = await fetchBackend("/settings");
      if (s.hf_token) {
        const el = document.getElementById("liveHfTokenInput");
        if (el) el.value = s.hf_token;
      }
      if (s.translation_provider) {
        const sel = document.getElementById("liveTransProvider");
        if (sel) { sel.value = s.translation_provider; sel.dispatchEvent(new Event("change")); }
      }
      if (s.ollama_url) {
        const el = document.getElementById("liveOllamaUrlInput");
        if (el) el.value = s.ollama_url;
      }
      if (s.anthropic_api_key) {
        const el = document.getElementById("liveClaudeApiKeyInput");
        if (el) el.value = s.anthropic_api_key;
      }
      if (s.hymt2_model_size) {
        const el = document.getElementById("liveHymt2ModelSize");
        if (el) el.value = s.hymt2_model_size;
      }
      if (s.nllb_model_size) {
        const el = document.getElementById("liveNllbModelSize");
        if (el) el.value = s.nllb_model_size;
      }
      await refreshLiveOllamaModels(s.ollama_model);
    } catch {
      await refreshLiveOllamaModels();
    }
  }
  loadLiveSavedSettings();
}

// ── Init ──

waveform.init();
audioPlayback.init();
zoomState.init();
overviewMinimap.init();
setupFileBrowse();
initSettings();
initSegmentSettings();
initCutControlsToggle();
initMainTabs();
initLiveTab();
checkBackend();
setInterval(checkBackend, 5000);

if (isDevMode()) {
  loadDevMode();
} else {
  console.log("[EasyScript] Premiere Pro mode");
  populateTrackSelect().catch((e) => console.error("[EasyScript] populateTrackSelect:", e));
}
