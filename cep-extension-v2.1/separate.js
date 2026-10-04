/**
 * EasyScript — voice / music separation.
 *
 * "Separate voice / music…" → Voice only | Music only. The backend (/separate
 * job, Mel-Band RoFormer) writes both stems in one run from a full-quality
 * copy of the loaded range — the source file's own audio for a single clip, a
 * 48 kHz stereo render otherwise — never from the 16 kHz analysis file.
 * The chosen stem then replaces the analysed audio (waveform, playback,
 * transcribe, beats); Original / Voice / Music switch instantly afterwards.
 * Stems cover exactly the loaded range, so every existing analysis (segments,
 * cuts, beats) keeps its timing.
 *
 * "Import to timeline" copies the stem to the export folder and lays it at the
 * range's original sequence time on the first audio track that is empty there
 * (adding a track when none is), so nothing is overwritten.
 *
 * Uses index.js globals (currentAudioPath, loadedClipInfo, fetchBackend,
 * progressTracker, showStatus, loadWaveformPeaks, audioPlayback, timebase
 * helpers) — only from event handlers, i.e. after index.js has run.
 */
(function (global) {
  "use strict";

  var LABEL = { original: "Original", vocals: "Voice", music: "Music" };
  var state = {
    original: "",     // analysed audio of the current load
    stems: null,      // {vocals, music, duration, device}
    active: "original",
    busy: false,
    placed: {},       // stem → {nodeId, seqStart} of its clip once imported to the timeline
  };

  function $(id) { return document.getElementById(id); }
  function inPremiere() { return !!(global.bridge && bridge.available()); }

  function render() {
    var box = $("stemBox");
    if (!box) return;
    var loaded = !!state.original;
    box.classList.toggle("hidden", !loaded);
    if (!loaded) return;
    var has = !!state.stems;
    $("separateBtn").classList.toggle("hidden", has);
    $("separateBtn").disabled = state.busy || !backendConnected;
    if (has) $("stemChoice").classList.add("hidden");
    ["stemVoiceBtn", "stemMusicBtn"].forEach(function (id) { $(id).disabled = state.busy; });
    $("stemBar").classList.toggle("hidden", !has);
    document.querySelectorAll('input[name="stemView"]').forEach(function (r) {
      r.checked = r.value === state.active;
      r.disabled = state.busy;
    });
    var imp = $("stemImportBtn");
    var stem = state.active !== "original";
    imp.disabled = state.busy || !stem || !inPremiere();
    imp.textContent = stem ? "Import " + LABEL[state.active].toLowerCase() + " to timeline" : "Import to timeline";
    imp.title = !inPremiere() ? "Available inside Premiere Pro"
      : !stem ? "Pick Voice or Music first"
      : "Place it at its original time on a free audio track (a new track if every track is busy there)";
  }

  /** New audio was loaded: forget the previous stems. */
  function reset(originalPath) {
    state.original = originalPath || "";
    state.stems = null;
    state.active = "original";
    state.placed = {};
    var choice = $("stemChoice");
    if (choice) choice.classList.add("hidden");
    render();
  }

  // ── Full-quality source of the loaded range ──

  async function sourceRequest() {
    var info = loadedClipInfo;
    if (!info) return { audio_path: state.original };   // browser dev mode: the file itself
    if (info.sourcePath) {
      // A single trimmed clip: its source media, exactly the used part.
      return { audio_path: info.sourcePath, start: info.srcIn || 0, end: (info.srcIn || 0) + (info.duration || 0) };
    }
    // Sequence / In-Out / nested clip: render the same range again, 48 kHz stereo.
    await ensureSameSequence();
    var r = await bridge.renderRange(info.sourceMode, info.renderStart || 0, info.renderEnd || 0, "hq");
    if (!r || !r.path) throw new Error("Render failed: " + ((r && (r.error || r.log)) || "unknown"));
    var start = typeof r.start === "number" ? r.start : 0;
    var tpf = seqTimebase.tpf / TICKS;
    if (info.duration > 0 && r.end > r.start &&
        (Math.abs(start - (info.seqStart || 0)) > tpf || Math.abs((r.end - r.start) - info.duration) > tpf)) {
      throw new Error("The In/Out range changed since the audio was loaded — load it again first.");
    }
    return { audio_path: r.path };
  }

  // ── Separation ──

  async function confirmDownload() {
    try {
      var st = await fetchBackend("/separate/status");
      if (st.downloaded) return true;
      return global.confirm("Separation needs a one-time download of the AI model (about " +
        st.model_mb + " MB). Download it now?");
    } catch (e) {
      return true;   // status unavailable — the job reports any real problem
    }
  }

  async function separate(which) {
    if (state.busy || !state.original) return;
    if (state.stems) { activate(which); return; }
    if (!(await confirmDownload())) return;
    state.busy = true;
    render();
    progressTracker.show();
    progressTracker.update(0.01, "separate", "Preparing full-quality audio…");
    try {
      var body = await sourceRequest();
      var job = await fetchBackend("/separate", { method: "POST", body: JSON.stringify(body) });
      var result = await progressTracker.pollUntilDone("/jobs/" + job.job_id, null, function () {
        fetchBackend("/jobs/" + job.job_id + "/cancel", { method: "POST" }).catch(function () {});
      });
      state.stems = result;
      progressTracker.update(1, "done", "Done — voice and music separated" + (result.device ? " · " + result.device : ""));
      state.busy = false;
      await activate(which);
    } catch (err) {
      if (err.message !== "__CANCELLED__") {
        progressTracker.update(0, "error", "Error: " + err.message);
        showStatus("Separation failed: " + err.message, true);
      }
    } finally {
      state.busy = false;
      progressTracker.stopPolling();
      progressTracker.hide();
      render();
    }
  }

  /** Make the original or a stem the analysed / played audio. */
  async function activate(which) {
    var path = which === "original" ? state.original : (state.stems && state.stems[which]);
    if (!path) return;
    state.active = which;
    render();
    currentAudioPath = path;
    $("audioPathInput").value = path;
    audioPlayback.loadAudio(path);
    await loadWaveformPeaks(path, (loadedClipInfo && loadedClipInfo.duration) || (state.stems && state.stems.duration) || 0);
    showStatus(which === "original" ? "Using the original audio"
      : "Using " + (which === "vocals" ? "the voice only (music removed)" : "the music only (voice removed)") +
        " — transcribe it, detect beats, or import it to the timeline", false, LABEL[which].toUpperCase());
  }

  // ── Import to timeline ──

  async function importToTimeline() {
    var which = state.active;
    if (which === "original" || !state.stems || !inPremiere()) return;
    var info = loadedClipInfo || {};
    var btn = $("stemImportBtn");
    state.busy = true;
    render();
    btn.classList.add("processing");
    try {
      await ensureSameSequence();
      var tb = await refreshTimebase();
      var name = (info.name || "Audio").replace(/\.[^.]+$/, "") + " - " + LABEL[which];
      var copy = await fetchBackend("/separate/export", {
        method: "POST", body: JSON.stringify({ path: state.stems[which], name: name }),
      });
      var startTicks = info.seqStartTicks != null ? info.seqStartTicks
        : frameToTicks(secToFrameRound(info.seqStart || 0, tb), tb);
      var durTicks = Math.round((state.stems.duration || info.duration || 0) * TICKS);
      var r = await bridge.placeStem(copy.path, startTicks, durTicks);
      state.placed[which] = { nodeId: r.nodeId, seqStart: (Number(r.startTicks) || 0) / TICKS };
      var at = formatTime((Number(r.startTicks) || 0) / TICKS);
      showStatus("Imported “" + r.name + "” to A" + (r.track + 1) + " at " + at +
        (r.addedTrack ? " (new track — the others had audio there)" : ""), false, "DONE");
    } catch (err) {
      showStatus("Import failed: " + err.message, true);
    } finally {
      btn.classList.remove("processing");
      state.busy = false;
      render();
    }
  }

  function init() {
    $("separateBtn").addEventListener("click", function () { $("stemChoice").classList.toggle("hidden"); });
    $("stemVoiceBtn").addEventListener("click", function () { separate("vocals"); });
    $("stemMusicBtn").addEventListener("click", function () { separate("music"); });
    document.querySelectorAll('input[name="stemView"]').forEach(function (r) {
      r.addEventListener("change", function () { if (r.checked) activate(r.value); });
    });
    $("stemImportBtn").addEventListener("click", importToTimeline);
    render();
  }

  /** Timeline clip of the audio the panel is using — the imported stem when
   *  Voice / Music is active and was imported, else the loaded clip. Clip
   *  markers fall back to it when nothing is selected in the timeline. */
  function markerClip() {
    var placed = state.active !== "original" && state.placed[state.active];
    if (placed) return placed;
    var info = loadedClipInfo || {};
    return { nodeId: info.nodeId || "", seqStart: info.seqStart || 0 };
  }

  global.stemUI = { init: init, reset: reset, render: render, markerClip: markerClip, state: state };
})(window);
