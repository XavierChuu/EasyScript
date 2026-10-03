import os
import json
import shutil
import tempfile
import subprocess
import threading
from contextlib import asynccontextmanager
from typing import Optional

import asyncio
import struct
import time as _time_module
import wave

from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import re
import uuid

from transcriber import Transcriber, is_model_cached, MODEL_SIZES
from silence_detector import SilenceDetector
from diarizer import Diarizer
import speakers as speaker_attr
import voices as voice_lib
from translator import get_translator, OllamaTranslator, HyMT2Translator, NLLBTranslator
from ffmpeg_utils import run_ffmpeg, run_silent, get_ffmpeg_exe
import security
import waveform as waveform_data
import beat_tracker
import xml_cut

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "easyscript_uploads")
WAVEFORM_CACHE_DIR = os.path.join(UPLOAD_DIR, "_waveform")
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".easyscript", "settings.json")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)

# Progress states include result when done
autocut_progress = {"status": "idle", "progress": 0.0}
transcribe_progress = {"status": "idle", "progress": 0.0}
diarize_progress = {"status": "idle", "progress": 0.0}
translate_progress = {"status": "idle", "progress": 0.0}

# Cancel flags — background workers check these to stop early
autocut_cancel = False
transcribe_cancel = False

transcriber = None
diarizer = None

# Benchmarked on 22 min of Vietnamese (FLEURS, long-form, RTX 3060):
# turbo 11.3 % WER at 50x real time, large-v3 11.2 % at 18x, PhoWhisper-large
# 15.7 % (lower-case, no punctuation) — so Turbo is the default.
AVAILABLE_MODELS = [
    {"id": "large-v3-turbo", "name": "Turbo", "size": "~1.6GB", "speed": "Fast", "quality": "Best",
     "default": True, "note": "Recommended — large-v3 accuracy, ~3x faster"},
    {"id": "large-v3", "name": "Large V3", "size": "~3GB", "speed": "Slow", "quality": "Best"},
    {"id": "medium", "name": "Medium", "size": "~1.5GB", "speed": "Medium", "quality": "Great"},
    {"id": "small", "name": "Small", "size": "~460MB", "speed": "Fast", "quality": "Good"},
    {"id": "base", "name": "Base", "size": "~140MB", "speed": "Faster", "quality": "Fair"},
    {"id": "tiny", "name": "Tiny", "size": "~75MB", "speed": "Fastest", "quality": "Low"},
]
DEFAULT_MODEL = "large-v3-turbo"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Publish the access token for the panel before the first request is
    # served (the panel polls /health, then reads the token file).
    # Models stay lazy: don't block startup with model loading.
    try:
        security.write_token_file(int(os.environ.get("PORT", "9876")))
    except Exception as e:
        print(f"[security] could not write token file: {e}")
    yield


app = FastAPI(title="EasyScript Backend", lifespan=lifespan)


def _ensure_transcriber():
    """Lazy-load transcriber on first use."""
    global transcriber
    if transcriber is None:
        model_size = os.environ.get("WHISPER_MODEL", DEFAULT_MODEL)
        device = os.environ.get("WHISPER_DEVICE", "auto")
        transcriber = Transcriber(model_size=model_size, device=device)

# Order matters: the middleware added last runs first. CORS is outermost so it
# answers preflights and decorates the auth layer's 401s; the auth layer then
# rejects every request without the session token (see security.py).
app.add_middleware(security.LocalAccessMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=security.CORS_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-EasyScript-Token"],
)


# ── Request Models ──

class AutoCutRequest(BaseModel):
    audio_path: str
    min_silence_ms: int = 500
    silence_thresh_db: int = -30

class TranscribeRequest(BaseModel):
    audio_path: str
    model: str | None = None
    language: str | None = None
    start_from: float = 0.0  # Resume from this time (seconds)
    song_mode: bool = False  # Separate vocals with Demucs before transcription
    # Song-mode tuning (only used when song_mode=True; ignored otherwise)
    song_vad_threshold: float | None = None     # VAD threshold 0.10–0.90 (default 0.40)
    song_min_silence_ms: int | None = None      # Phrase gap 200–2000ms (default 700)
    song_beam_size: int | None = None           # Beam search width 1–5 (default 1)
    vocabulary: str | None = None               # names / terms to bias recognition

class SwitchModelRequest(BaseModel):
    model: str

class DiarizeRequest(BaseModel):
    audio_path: str
    segments: list[dict] = []  # Speech segments to merge speakers into
    # Optional speaker-count hints — passed to pyannote to skip its
    # cluster-size search. num_speakers wins if both are provided.
    num_speakers: Optional[int] = None
    min_speakers: Optional[int] = None
    max_speakers: Optional[int] = None
    # "fewer" | "standard" | "sensitive" | "max" — VBx clustering presets
    # (see diarizer.SENSITIVITY).
    sensitivity: Optional[str] = None
    # "accurate" | "balanced" | "fast" — segmentation hop; None = balanced.
    speed: Optional[str] = None
    # Name speakers that match a voice saved in the voice library.
    match_voices: bool = True
    # False: never split a segment (translations are indexed per segment).
    split: bool = True

class TranslateRequest(BaseModel):
    segments: list[dict]  # [{ text: "...", start: ..., end: ... }]
    source_lang: str
    target_lang: str
    provider: str = "ollama"  # "ollama", "claude", "hymt2", or "nllb"
    model: Optional[str] = None
    hymt2_model_size: Optional[str] = None
    nllb_model_size: Optional[str] = None

class TranslateOneRequest(BaseModel):
    text: str
    source_lang: str
    target_lang: str
    provider: str = "ollama"
    model: Optional[str] = None
    hymt2_model_size: Optional[str] = None
    nllb_model_size: Optional[str] = None

class SaveFileRequest(BaseModel):
    filename: str
    content: str


# ── Utility ──

def ensure_accessible(audio_path: str) -> str:
    """Copy file to UPLOAD_DIR if it's outside temp and not accessible.

    On macOS, TCC (Transparency, Consent, Control) may block Python from
    reading files in ~/Documents, ~/Desktop etc. even from Terminal.
    We try: 1) direct access, 2) shutil copy, 3) ffmpeg copy (ffmpeg
    often has separate TCC permissions).
    Returns the (possibly new) path that's guaranteed readable.
    """
    # Already in our upload dir — fine
    if audio_path.startswith(UPLOAD_DIR):
        return audio_path

    # Quick readability check
    if os.access(audio_path, os.R_OK):
        try:
            # Double-check by actually opening
            with open(audio_path, "rb") as f:
                f.read(1)
            return audio_path
        except OSError:
            pass  # TCC block — fall through

    basename = os.path.basename(audio_path)
    dest = os.path.join(UPLOAD_DIR, basename)

    # Try 1: shutil copy
    try:
        shutil.copy2(audio_path, dest)
        print(f"[easyscript] Copied inaccessible file to {dest}")
        return dest
    except OSError:
        pass

    # Try 2: ffmpeg copy (ffmpeg may have separate TCC permissions)
    try:
        result = run_ffmpeg(
            ["-y", "-i", audio_path, "-c", "copy", dest],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and os.path.isfile(dest):
            print(f"[easyscript] Copied via ffmpeg to {dest}")
            return dest
    except Exception:
        pass

    print(f"[easyscript] WARNING: Cannot access {audio_path} — grant Terminal 'Files and Folders' or 'Full Disk Access' in System Settings → Privacy & Security")
    return audio_path


def get_audio_duration(audio_path):
    """Get audio duration in seconds (uses bundled ffmpeg, no ffprobe needed)."""
    from ffmpeg_utils import get_audio_duration as _get_dur
    return _get_dur(audio_path)

def generate_peaks(audio_path, num_peaks=800):
    """Normalized 0..1 peak list (the old /peaks format), built from the
    numpy waveform overview — one decode, cached, instead of a Python loop
    over every sample."""
    try:
        peaks, _duration = waveform_data.legacy_peaks(audio_path, WAVEFORM_CACHE_DIR, num_peaks)
        return peaks
    except Exception as e:
        print(f"[peaks] Error: {e}")
        return []


# ── Health & Models ──

@app.get("/health")
def health():
    # Resolve ffmpeg via bundled imageio-ffmpeg (works in bundle); fall back
    # to whatever's on PATH.
    ffmpeg_path = get_ffmpeg_exe()
    ffmpeg_ok = bool(ffmpeg_path) and os.path.isfile(ffmpeg_path)

    return {
        "status": "ok",
        "model": transcriber.model_size if transcriber else None,
        "backend": transcriber.backend if transcriber else None,
        "device": transcriber.device_name if transcriber else None,
        "ffmpeg": ffmpeg_ok,
        "ffmpeg_path": ffmpeg_path,
    }

@app.get("/models")
def list_models():
    current = transcriber.model_size if transcriber else None
    backend = transcriber.backend if transcriber else "faster-whisper"
    models = []
    for m in AVAILABLE_MODELS:
        models.append({
            **m,
            "active": m["id"] == current,
            "cached": is_model_cached(m["id"], backend),
        })
    return {
        "models": models,
        "current": current,
        "backend": backend,
        "device": transcriber.device_name if transcriber else None,
    }

@app.post("/models/switch")
def switch_model(req: SwitchModelRequest):
    global transcriber
    valid_ids = [m["id"] for m in AVAILABLE_MODELS]
    if req.model not in valid_ids:
        return {"error": f"Invalid model. Choose from: {valid_ids}"}
    if transcriber and transcriber.model_size == req.model:
        return {"status": "ok", "model": req.model, "message": "Already loaded"}
    try:
        device = os.environ.get("WHISPER_DEVICE", "auto")
        transcriber = Transcriber(model_size=req.model, device=device)
        return {"status": "ok", "model": req.model}
    except Exception as e:
        return {"error": str(e)}


# ── Upload & Serve Audio ──

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".webm", ".flv", ".wmv", ".m4v"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aif", ".aiff",
                    ".caf", ".wma", ".m4b", ".amr"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | {".mts", ".m2ts", ".ts", ".3gp", ".mpg", ".mpeg"}


def _safe_upload_name(filename):
    """Client-supplied names must never steer the write path: an absolute name
    makes os.path.join drop UPLOAD_DIR, and '..' climbs out of it. Keep only a
    sanitized basename, prefixed so concurrent uploads can't collide."""
    base = os.path.basename((filename or "").replace("\\", "/")).strip()
    base = re.sub(r"[^\w.\- ]+", "_", base).strip(" .") or "upload"
    stem, ext = os.path.splitext(base)
    return f"{uuid.uuid4().hex[:8]}_{stem[:100]}{ext[:10]}"


@app.post("/upload")
async def upload_audio(file: UploadFile = File(...)):
    safe_name = _safe_upload_name(file.filename)
    save_path = os.path.join(UPLOAD_DIR, safe_name)
    if os.path.dirname(os.path.abspath(save_path)) != os.path.abspath(UPLOAD_DIR):
        return JSONResponse(status_code=400, content={"error": "Invalid filename"})
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # If video file, extract audio track to WAV for processing & playback
    ext = os.path.splitext(safe_name)[1].lower()
    if ext in VIDEO_EXTENSIONS:
        wav_name = os.path.splitext(safe_name)[0] + "_audio.wav"
        wav_path = os.path.join(UPLOAD_DIR, wav_name)
        try:
            run_ffmpeg(
                ["-y", "-i", save_path, "-vn", "-acodec", "pcm_s16le",
                 "-ar", "16000", "-ac", "1", wav_path],
                capture_output=True, timeout=300,
            )
            if os.path.isfile(wav_path) and os.path.getsize(wav_path) > 0:
                save_path = wav_path
                print(f"[easyscript] Extracted audio from video: {wav_path}")
            else:
                print(f"[easyscript] Warning: ffmpeg extracted empty audio from {file.filename}")
        except Exception as e:
            print(f"[easyscript] Warning: failed to extract audio from video: {e}")

    return {"path": save_path, "filename": file.filename, "size": os.path.getsize(save_path)}

@app.get("/audio")
def serve_audio(path: str):
    # Token-gated like everything else; additionally only media files are
    # served, so even a leaked token can't read documents or keys.
    if not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in MEDIA_EXTENSIONS:
        return JSONResponse(status_code=404, content={"error": "Media file not found"})
    import mimetypes
    mime, _ = mimetypes.guess_type(path)
    return FileResponse(path, media_type=mime or "audio/mpeg")


class PeaksRequest(BaseModel):
    audio_path: str
    num_peaks: int = 800

@app.post("/peaks")
def peaks_only(req: PeaksRequest):
    """Generate waveform peaks + duration without running silence detection.
    Lets the frontend display a waveform immediately after audio selection."""
    if not os.path.isfile(req.audio_path):
        return JSONResponse(status_code=400, content={"error": f"File not found: {req.audio_path}"})
    audio_path = ensure_accessible(req.audio_path)
    duration = get_audio_duration(audio_path) or 0
    pks = generate_peaks(audio_path, num_peaks=req.num_peaks)
    return {"peaks": pks, "audio_duration": round(duration, 1)}


class TrimRequest(BaseModel):
    audio_path: str
    start: float = 0.0
    end: float = 0.0
    normalize: bool = False


def _peak_normalize(path):
    """Peak-normalize a WAV so its loudest point sits near -1 dBFS, preserving
    dynamics. Premiere's rendered mixdown is often quiet (mono downmix / master
    gain), which breaks the dB-based silence threshold; this restores a usable
    level. Returns the (possibly new) path."""
    import subprocess as _sp, re as _re
    try:
        proc = _sp.run([get_ffmpeg_exe(), "-i", path, "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
        m = _re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stderr or "")
        if not m:
            return path
        maxv = float(m.group(1))
        gain = (-maxv) - 1.0  # bring peak to -1 dBFS
        if gain <= 0.5:
            return path  # already loud enough
        norm = path[:-4] + "_norm.wav" if path.endswith(".wav") else path + "_norm.wav"
        run_ffmpeg(["-y", "-i", path, "-af", f"volume={gain:.1f}dB", norm])
        if os.path.isfile(norm):
            try: os.remove(path)
            except OSError: pass
            return norm
    except Exception as e:
        print(f"[trim] normalize failed: {e}")
    return path


@app.post("/trim")
def trim_audio(req: TrimRequest):
    """Extract [start, end] of a source file to a temp 16k mono WAV. Used by the
    Premiere extension so analysis runs on EXACTLY the selected/trimmed clip
    (respecting in/out points) instead of the whole original source. When
    `normalize` is set (e.g. for a Premiere render), the result is peak-normalized."""
    import time as _t
    if not os.path.isfile(req.audio_path):
        return JSONResponse(status_code=400, content={"error": f"File not found: {req.audio_path}"})
    src = ensure_accessible(req.audio_path)
    start = max(0.0, float(req.start or 0.0))
    dur = (float(req.end) - float(req.start)) if (req.end and req.end > req.start) else 0.0
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    out = os.path.join(UPLOAD_DIR, f"_trim_{int(_t.time() * 1000)}.wav")
    args = ["-y", "-ss", f"{start:.3f}"]
    if dur > 0:
        args += ["-t", f"{dur:.3f}"]
    args += ["-i", src, "-vn", "-ac", "1", "-ar", "16000", out]
    try:
        run_ffmpeg(args)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"trim failed: {e}"})
    if not os.path.isfile(out):
        return JSONResponse(status_code=500, content={"error": "trim produced no file"})
    if req.normalize:
        out = _peak_normalize(out)
    real_dur = get_audio_duration(out) or dur
    return {"path": out, "audio_duration": round(real_dur, 3)}


# ── Background jobs (id-addressed) ──
# Newer endpoints use per-job state instead of one global progress dict per
# feature, so a stale job can never report into a new one, and Cancel works.

_jobs = {}
_jobs_lock = threading.Lock()


class JobCancelled(Exception):
    pass


def _new_job(kind):
    job = {"id": uuid.uuid4().hex[:12], "kind": kind, "status": "processing",
           "progress": 0.0, "stage": "starting", "detail": "", "created": _time_module.time()}
    with _jobs_lock:
        cutoff = _time_module.time() - 3600
        for jid in [j for j, v in _jobs.items() if v["created"] < cutoff and v["status"] != "processing"]:
            _jobs.pop(jid, None)
        _jobs[job["id"]] = job
    return job


def _job_progress(job):
    def report(p, detail=""):
        if job.get("cancel"):
            raise JobCancelled()
        job.update({"progress": round(float(p), 3), "stage": job["kind"], "detail": detail})
    return report


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Unknown job"})
    return {k: v for k, v in job.items() if k != "cancel"}


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Unknown job"})
    job["cancel"] = True
    return {"status": "cancelling"}


# ── Waveform (multi-resolution peaks for the canvas renderer) ──

class WaveformRequest(BaseModel):
    audio_path: str
    bins_per_sec: Optional[int] = None


class WaveformSliceRequest(BaseModel):
    audio_path: str
    start: float
    end: float
    bins: int = 1000
    peak: Optional[int] = None


@app.post("/waveform")
def waveform_overview(req: WaveformRequest):
    if not os.path.isfile(req.audio_path):
        return JSONResponse(status_code=400, content={"error": "File not found"})
    try:
        return waveform_data.overview(ensure_accessible(req.audio_path), WAVEFORM_CACHE_DIR,
                                      bins_per_sec=req.bins_per_sec)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"waveform failed: {e}"})


@app.post("/waveform/slice")
def waveform_slice(req: WaveformSliceRequest):
    if not os.path.isfile(req.audio_path):
        return JSONResponse(status_code=400, content={"error": "File not found"})
    try:
        return waveform_data.slice_(ensure_accessible(req.audio_path), WAVEFORM_CACHE_DIR,
                                    req.start, req.end, req.bins, req.peak)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"waveform slice failed: {e}"})


# ── Beat detection (for beat markers) ──

class BeatsRequest(BaseModel):
    audio_path: str
    bpm: Optional[float] = None        # fixed tempo; None = estimate (may drift)
    min_bpm: float = 60.0
    max_bpm: float = 200.0
    tightness: float = 100.0           # higher = steadier grid, lower = follows onsets


def _run_beats_worker(job, req):
    try:
        report = _job_progress(job)
        report(0.02, "Preparing audio…")
        src, _sr = waveform_data.pcm_source(ensure_accessible(req.audio_path), WAVEFORM_CACHE_DIR)
        result = beat_tracker.detect(
            src, min_bpm=req.min_bpm, max_bpm=req.max_bpm, bpm=req.bpm,
            tightness=req.tightness, progress=lambda p, d: report(p, d))
        job.update({"status": "done", "progress": 1.0, "stage": "done",
                    "detail": f"{len(result['beats'])} beats · {result['bpm']} BPM",
                    "result": result})
    except JobCancelled:
        job.update({"status": "error", "stage": "error", "detail": "Cancelled"})
    except Exception as e:
        job.update({"status": "error", "stage": "error", "detail": f"Beat detection failed: {e}"})


@app.post("/beats")
def detect_beats(req: BeatsRequest):
    if not os.path.isfile(req.audio_path):
        return JSONResponse(status_code=400, content={"error": "File not found"})
    if req.bpm is not None and not (20 <= req.bpm <= 400):
        return JSONResponse(status_code=400, content={"error": "BPM must be between 20 and 400"})
    job = _new_job("beats")
    threading.Thread(target=_run_beats_worker, args=(job, req), daemon=True).start()
    return {"job_id": job["id"]}


# ── Cut via XML (rebuild the sequence instead of editing it N times) ──

class XmlCutRequest(BaseModel):
    xml_path: str                      # FCP XML exported by Premiere (host.jsx)
    cuts_ticks: list = []              # [[start_ticks, end_ticks], ...] sequence time
    name_suffix: str = " (EasyScript cut)"
    splits_ticks: list = []            # extra edit points (Tag speaker), nothing removed
    labels: list = []                  # [{start_ticks, end_ticks, name, color}] clip names/colours
    only_media: list = []              # splits/labels only on clips of these files (+ linked)
    file_label: str = "EasyScript cut"


@app.post("/xml/cut")
def xml_cut_sequence(req: XmlCutRequest):
    src = req.xml_path
    if not os.path.isfile(src) or not src.lower().endswith(".xml"):
        return JSONResponse(status_code=400, content={"error": "Exported sequence XML not found"})
    try:
        cuts = [[int(a), int(b)] for a, b in req.cuts_ticks]
        splits = [int(t) for t in req.splits_ticks]
        labels = [{"start_ticks": int(x["start_ticks"]), "end_ticks": int(x["end_ticks"]),
                   "name": str(x.get("name") or "")[:120], "color": x.get("color")}
                  for x in req.labels]
    except (TypeError, ValueError, KeyError):
        return JSONResponse(status_code=400, content={"error": "Invalid cuts / splits / labels"})
    stamp = _time_module.strftime("%Y%m%d-%H%M%S")
    try:
        import xml.etree.ElementTree as _ET
        seq_name = (_ET.parse(src).getroot().findtext(".//sequence/name") or "Sequence")
    except Exception:
        seq_name = "Sequence"
    tag = xml_cut.safe_filename(req.file_label, "EasyScript cut")
    dst = os.path.join(export_dir, f"{xml_cut.safe_filename(seq_name)} - {tag} {stamp}.xml")
    try:
        result = xml_cut.cut_sequence_xml(src, dst, cuts, name_suffix=req.name_suffix,
                                          splits_ticks=splits, labels=labels,
                                          only_media=[str(m) for m in req.only_media if m])
    except xml_cut.XmlCutError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"XML cut failed: {e}"})
    return result


# ── Auto Cut (async — silence detection + peaks in background thread) ──

@app.get("/autocut/progress")
def get_autocut_progress():
    return autocut_progress

def _run_autocut_worker(audio_path, min_silence_ms, silence_thresh_db):
    """Background worker for silence detection + peak generation."""
    global autocut_progress

    try:
        # Ensure file is accessible (macOS TCC may block ~/Documents etc.)
        audio_path = ensure_accessible(audio_path)

        # Get duration
        audio_duration = get_audio_duration(audio_path)
        dur_str = ""
        if audio_duration > 0:
            dm, ds = int(audio_duration // 60), int(audio_duration % 60)
            dur_str = f" ({dm}m {ds:02d}s)"

        autocut_progress.update({
            "progress": 0.10, "stage": "silence",
            "detail": f"Detecting silence & breaths...{dur_str}",
            "audio_duration": round(audio_duration, 1),
        })

        def on_silence_progress(p):
            pct = 0.10 + p * 0.60  # 10% → 70%
            autocut_progress.update({
                "progress": round(pct, 3),
                "stage": "silence",
                "detail": f"Detecting silence & breaths... {round(p * 100)}%",
                "audio_duration": round(audio_duration, 1),
            })

        silence_segments = SilenceDetector.detect(
            audio_path,
            min_silence_ms=min_silence_ms,
            silence_thresh_db=silence_thresh_db,
            on_progress=on_silence_progress,
        )

        autocut_progress.update({
            "progress": 0.75, "stage": "peaks",
            "detail": f"Generating waveform... ({len(silence_segments)} segments found)",
        })

        peaks = generate_peaks(audio_path, num_peaks=800)

        # Store result in progress so frontend can fetch it
        autocut_progress.update({
            "status": "done", "progress": 1.0,
            "stage": "done",
            "detail": f"Done — {len(silence_segments)} segments",
            "result": {
                "segments": silence_segments,
                "peaks": peaks,
                "audio_duration": round(audio_duration, 1),
                "count": len(silence_segments),
            },
        })

    except Exception as e:
        autocut_progress.update({
            "status": "error", "progress": 0.0,
            "stage": "error", "detail": str(e),
        })

@app.post("/autocut")
def autocut(req: AutoCutRequest):
    global autocut_progress, autocut_cancel

    if not os.path.isfile(req.audio_path):
        return JSONResponse(status_code=400, content={"error": f"File not found: {req.audio_path}"})

    # Cancel any previous run
    autocut_cancel = True

    autocut_progress = {
        "status": "processing", "progress": 0.05,
        "stage": "loading_audio", "detail": "Loading audio file..."
    }
    autocut_cancel = False

    thread = threading.Thread(
        target=_run_autocut_worker,
        args=(req.audio_path, req.min_silence_ms, req.silence_thresh_db),
        daemon=True,
    )
    thread.start()

    return {"status": "started", "message": "Processing started. Poll /autocut/progress for updates."}


# ── Transcribe (async — speech to text in background thread) ──

@app.get("/transcribe/progress")
def get_transcribe_progress():
    return transcribe_progress

def _separate_vocals(audio_path):
    """Run Demucs to extract vocals. Returns vocals WAV path, or None on failure.

    Uses _demucs_runner which monkey-patches torchaudio.load to soundfile
    (bypassing torchcodec). In dev mode: subprocess via system Python. In
    bundled mode (PyInstaller): multiprocessing.Process (subprocess to the
    frozen exe with a script arg doesn't work).
    """
    import sys
    basename = os.path.splitext(os.path.basename(audio_path))[0]
    output_dir = os.path.join(tempfile.gettempdir(), "easyscript_demucs")
    os.makedirs(output_dir, exist_ok=True)

    vocals_path = os.path.join(output_dir, "htdemucs", basename, "vocals.wav")
    if os.path.isfile(vocals_path):
        return vocals_path

    # Detect if running in PyInstaller bundle (sys.executable is not Python).
    bundled = getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None) is not None

    try:
        if bundled:
            # In bundled mode, invoke the runner in-process (sys.executable
            # points to the GUI exe, so subprocess(sys.executable, ...) would
            # relaunch EasyScript instead of running demucs).
            from _demucs_runner import run_demucs_main
            run_demucs_main(output_dir, audio_path)
        else:
            runner = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "_demucs_runner.py")
            result = subprocess.run(
                [sys.executable, runner, output_dir, audio_path],
                capture_output=True, text=True, timeout=900,
            )
            if result.returncode != 0:
                print(f"[demucs] returncode={result.returncode}")
                print(f"[demucs] stderr (tail):\n{result.stderr[-800:]}")
                return None

        if os.path.isfile(vocals_path):
            return vocals_path
        # Fallback: find any vocals.wav under matching basename folder
        for root, _dirs, files in os.walk(output_dir):
            for f in files:
                if f == "vocals.wav" and basename in root:
                    return os.path.join(root, f)
        return None
    except Exception as e:
        print(f"[demucs] Exception: {e}")
        return None


def _run_transcribe_worker(audio_path, model, language, start_from, song_mode=False,
                           song_vad_threshold=None, song_min_silence_ms=None, song_beam_size=None,
                           vocabulary=None):
    """Background worker for whisper transcription with chunked processing."""
    global transcriber, transcribe_progress

    try:
        # Ensure file is accessible (macOS TCC may block ~/Documents etc.)
        audio_path = ensure_accessible(audio_path)

        # Song mode: isolate vocals with Demucs first, then transcribe the
        # clean vocal track. This is the industry-standard approach for music
        # lyrics transcription (used by WhisperX and similar tools).
        if song_mode:
            transcribe_progress.update({
                "progress": 0.02, "stage": "isolating_vocals",
                "detail": "Isolating vocals from music (Demucs, ~30-90s)...",
            })
            vocals_path = _separate_vocals(audio_path)
            if vocals_path:
                audio_path = vocals_path
                transcribe_progress.update({
                    "progress": 0.30, "stage": "transcribing",
                    "detail": "Vocals isolated. Transcribing lyrics...",
                })
            else:
                transcribe_progress.update({
                    "progress": 0.05, "stage": "transcribing",
                    "detail": "Demucs unavailable or failed - transcribing original audio.",
                })

        _ensure_transcriber()
        # Switch model if needed
        target_model = model or transcriber.model_size
        if model and model != transcriber.model_size:
            # Check if model needs downloading
            cached = is_model_cached(model, transcriber.backend)
            size_str = MODEL_SIZES.get(model, "")

            if not cached:
                transcribe_progress.update({
                    "progress": 0.01, "stage": "downloading",
                    "detail": f"Downloading model {model} ({size_str})... This is a one-time download.",
                })
            else:
                transcribe_progress.update({
                    "progress": 0.02, "stage": "loading_model",
                    "detail": f"Loading model {model}...",
                })

            device = os.environ.get("WHISPER_DEVICE", "auto")
            transcriber = Transcriber(model_size=model, device=device)

            if not cached:
                transcribe_progress.update({
                    "progress": 0.04, "stage": "loading_model",
                    "detail": f"Model {model} downloaded. Loading...",
                })

        audio_duration = get_audio_duration(audio_path)
        dur_str = ""
        if audio_duration > 0:
            dm, ds = int(audio_duration // 60), int(audio_duration % 60)
            dur_str = f" ({dm}m {ds:02d}s audio)"

        resume_str = ""
        if start_from > 0:
            rm, rs = int(start_from // 60), int(start_from % 60)
            resume_str = f" (resuming from {rm}:{rs:02d})"

        transcribe_progress.update({
            "progress": 0.05, "stage": "transcribing",
            "detail": f"Transcribing speech...{dur_str}{resume_str}",
            "audio_duration": round(audio_duration, 1),
        })

        def on_progress(p):
            pct = 0.05 + p * 0.90  # 5% → 95%
            transcribe_progress.update({
                "progress": round(pct, 3),
                "stage": "transcribing",
                "detail": f"Transcribing... {round(p * 100)}%{dur_str}",
                "audio_duration": round(audio_duration, 1),
            })

        def on_chunk_done(segments_so_far, chunk_num, total_chunks):
            """Stream partial results after each chunk completes."""
            partial_segments = [
                {
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": seg["text"],
                    "language": seg.get("language"),
                    "speaker": seg.get("speaker"),
                    "type": "speech",
                }
                for seg in segments_so_far
            ]
            transcribe_progress.update({
                "partial_segments": partial_segments,
                "partial_count": len(partial_segments),
                "chunk": chunk_num,
                "total_chunks": total_chunks,
                "detail": f"Chunk {chunk_num}/{total_chunks} done — {len(partial_segments)} segments so far{dur_str}",
            })

        speech_segments = transcriber.transcribe(
            audio_path,
            language=language,
            on_progress=on_progress,
            start_from=start_from,
            on_chunk_done=on_chunk_done,
            song_mode=song_mode,
            song_vad_threshold=song_vad_threshold,
            song_min_silence_ms=song_min_silence_ms,
            song_beam_size=song_beam_size,
            vocabulary=(vocabulary or "").strip()[:800] or None,
            cache_dir=WAVEFORM_CACHE_DIR,
        )

        # Final segments keep their word timestamps (subtitle timing, word
        # display, word-level speaker attribution).
        result_segments = [
            {
                "start": seg["start"],
                "end": seg["end"],
                "text": seg["text"],
                "language": seg.get("language"),
                "speaker": seg.get("speaker"),
                "type": "speech",
                "words": [{"word": w["word"], "start": w["start"], "end": w["end"],
                           "p": w.get("probability")} for w in seg.get("words") or []],
            }
            for seg in speech_segments
        ]

        transcribe_progress.update({
            "status": "done", "progress": 1.0,
            "stage": "done",
            "detail": f"Done — {len(result_segments)} speech segments",
            "result": {
                "segments": result_segments,
                "count": len(result_segments),
                "model": transcriber.model_size,
                "audio_duration": round(audio_duration, 1),
            },
            "partial_segments": None,  # Clear partial
        })

    except Exception as e:
        transcribe_progress.update({
            "status": "error", "progress": 0.0,
            "stage": "error", "detail": str(e),
        })

@app.post("/transcribe")
def transcribe_audio(req: TranscribeRequest):
    global transcribe_progress, transcribe_cancel

    if not os.path.isfile(req.audio_path):
        return JSONResponse(status_code=400, content={"error": f"File not found: {req.audio_path}"})

    # Cancel any previous run
    transcribe_cancel = True

    transcribe_progress = {
        "status": "processing", "progress": 0.02,
        "stage": "preparing", "detail": "Preparing transcription..."
    }
    transcribe_cancel = False

    thread = threading.Thread(
        target=_run_transcribe_worker,
        args=(req.audio_path, req.model, req.language, req.start_from, req.song_mode),
        kwargs={
            "song_vad_threshold": req.song_vad_threshold,
            "song_min_silence_ms": req.song_min_silence_ms,
            "song_beam_size": req.song_beam_size,
            "vocabulary": req.vocabulary,
        },
        daemon=True,
    )
    thread.start()

    return {"status": "started", "message": "Transcription started. Poll /transcribe/progress for updates."}


# ── Settings (persistent config) ──

def load_settings():
    try:
        with open(SETTINGS_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_settings(data):
    with open(SETTINGS_PATH, "w") as f:
        json.dump(data, f, indent=2)

SECRET_SETTINGS = ("hf_token", "anthropic_api_key")


def _mask_secret(value):
    return value[:4] + "..." + value[-4:] if value else value


@app.get("/settings")
def get_settings():
    settings = load_settings()
    # Mask sensitive values
    masked = {**settings}
    for key in SECRET_SETTINGS:
        if key in masked and masked[key]:
            masked[key] = _mask_secret(masked[key])
    return masked

@app.post("/settings")
def update_settings(data: dict):
    settings = load_settings()
    for key in SECRET_SETTINGS:
        # The panel shows the masked value from GET /settings; saving the form
        # unchanged must not overwrite the real secret with its mask.
        if key in data and settings.get(key) and data[key] == _mask_secret(settings[key]):
            data = {k: v for k, v in data.items() if k != key}
    settings.update(data)
    save_settings(settings)
    return {"status": "ok"}


# ── Speaker Diarization (async — pyannote in background thread) ──

@app.get("/diarize/progress")
def get_diarize_progress():
    return diarize_progress

def _label_speakers(segments, embeddings, match_voices=True):
    """Display labels in order of first appearance; saved voices name their match."""
    order = speaker_attr.speaker_order(segments) or list(embeddings)
    for spk in embeddings:
        if spk not in order:
            order.append(spk)
    matches = voice_lib.match({k: embeddings[k] for k in order if k in embeddings}) if match_voices else {}
    labels = speaker_attr.default_labels(order, {k: v["name"] for k, v in matches.items()})
    return labels, matches


def _attach_labels(segments, speaker_map):
    for seg in segments:
        spk = seg.get("speaker")
        if spk:
            seg["speakerLabel"] = speaker_map.get(spk, spk)
    return segments


def _run_diarize_worker(audio_path, speech_segments, num_speakers=None, min_speakers=None,
                        max_speakers=None, sensitivity=None, speed=None, match_voices=True,
                        split=True):
    """Background worker for speaker diarization (community-1, no token needed)."""
    global diarize_progress, diarizer

    try:
        audio_path = ensure_accessible(audio_path)
        audio_duration = get_audio_duration(audio_path)
        dur_str = ""
        if audio_duration > 0:
            dm, ds = int(audio_duration // 60), int(audio_duration % 60)
            dur_str = f" ({dm}m {ds:02d}s audio)"

        hf_token = load_settings().get("hf_token", "") or os.environ.get("HF_TOKEN", "")
        if diarizer is None:
            diarize_progress.update({
                "progress": 0.03, "stage": "loading_model",
                "detail": f"Loading speaker model...{dur_str} (first run downloads ~35 MB)",
            })
            diarizer = Diarizer(hf_token=hf_token)
            diarizer._ensure_pipeline()

        device = diarizer.engine or "CPU"

        def on_progress(p):
            diarize_progress.update({
                "progress": round(0.05 + p * 0.87, 3), "stage": "diarizing",
                "detail": f"Identifying speakers ({device})... {round(p * 100)}%{dur_str}",
            })

        on_progress(0.0)
        result = diarizer.diarize(
            audio_path, on_progress=on_progress,
            num_speakers=num_speakers, min_speakers=min_speakers, max_speakers=max_speakers,
            sensitivity=sensitivity, speed=speed,
            cancelled=lambda: diarize_progress.get("cancel", False),
        )

        diarize_progress.update({"progress": 0.94, "stage": "merging",
                                 "detail": "Assigning speakers to words..."})
        updated = speaker_attr.assign_speakers(speech_segments or [], result["exclusive"], split=split)
        speaker_map, matches = _label_speakers(updated, result["embeddings"], match_voices)
        _attach_labels(updated, speaker_map)

        n = len(result["embeddings"]) or len({t["speaker"] for t in result["turns"]})
        named = f", {len(matches)} recognised" if matches else ""
        diarize_progress.update({
            "status": "done", "progress": 1.0, "stage": "done",
            "detail": f"Done — {n} speakers identified{named}",
            "result": {
                "segments": updated,
                "speaker_map": speaker_map,
                "num_speakers": n,
                "diarize_raw": result["turns"],
                "exclusive": result["exclusive"],
                "speaker_embeddings": result["embeddings"],
                "voice_matches": matches,
            },
        })
    except InterruptedError:
        diarize_progress.update({"status": "cancelled", "progress": 0.0, "stage": "cancelled",
                                 "detail": "Cancelled"})
    except Exception as e:
        diarize_progress.update({
            "status": "error", "progress": 0.0,
            "stage": "error", "detail": str(e),
        })


@app.post("/diarize")
def diarize_audio(req: DiarizeRequest):
    global diarize_progress

    if not os.path.isfile(req.audio_path):
        return JSONResponse(status_code=400, content={"error": f"File not found: {req.audio_path}"})

    diarize_progress = {
        "status": "processing", "progress": 0.02,
        "stage": "preparing", "detail": "Preparing diarization..."
    }

    thread = threading.Thread(
        target=_run_diarize_worker,
        args=(req.audio_path, req.segments),
        kwargs={
            "num_speakers": req.num_speakers,
            "min_speakers": req.min_speakers,
            "max_speakers": req.max_speakers,
            "sensitivity": req.sensitivity,
            "speed": req.speed,
            "match_voices": req.match_voices,
            "split": req.split,
        },
        daemon=True,
    )
    thread.start()

    return {"status": "started", "message": "Diarization started. Poll /diarize/progress for updates."}


@app.post("/diarize/cancel")
def diarize_cancel():
    diarize_progress["cancel"] = True
    return {"status": "cancelling"}


class AssignSpeakersRequest(BaseModel):
    segments: list[dict]
    exclusive: list[dict]                  # diarize result "exclusive"
    speaker_embeddings: dict = {}
    speaker_map: dict = {}                 # keep labels the user already chose
    match_voices: bool = True
    split: bool = True


@app.post("/speakers/assign")
def assign_speakers(req: AssignSpeakersRequest):
    """Word-level speaker attribution for segments transcribed after (or in
    parallel with) diarization."""
    updated = speaker_attr.assign_speakers(req.segments, req.exclusive, split=req.split)
    speaker_map, matches = _label_speakers(updated, req.speaker_embeddings, req.match_voices)
    for k, v in (req.speaker_map or {}).items():
        if k in speaker_map and k not in matches:
            speaker_map[k] = v
    _attach_labels(updated, speaker_map)
    return {"segments": updated, "speaker_map": speaker_map, "voice_matches": matches}


# ── Voice library (remember named speakers across videos) ──

class SaveVoiceRequest(BaseModel):
    name: str
    embedding: list[float]


@app.get("/voices")
def get_voices():
    return {"voices": voice_lib.list_voices()}


@app.post("/voices")
def save_voice(req: SaveVoiceRequest):
    try:
        return voice_lib.save_voice(req.name, req.embedding)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


class DeleteVoiceRequest(BaseModel):
    name: str


@app.post("/voices/delete")
def delete_voice(req: DeleteVoiceRequest):
    return {"deleted": voice_lib.delete_voice(req.name)}


# ── Translation (async — translate in background thread) ──

@app.get("/translate/progress")
def get_translate_progress():
    return translate_progress

# Module-level cache for translators that load big models — we want to keep
# the model resident across requests (cold loads are expensive).
_nllb_instances: dict[str, NLLBTranslator] = {}

def _get_nllb_translator(model_size: str) -> NLLBTranslator:
    """Return a cached NLLBTranslator for the given size (keeps weights in RAM/VRAM)."""
    if model_size not in _nllb_instances:
        _nllb_instances[model_size] = NLLBTranslator(model_size=model_size)
    return _nllb_instances[model_size]


def _build_translator_kwargs(provider, settings, model=None,
                              hymt2_model_size=None, nllb_model_size=None):
    """DRY: build provider-specific kwargs for get_translator() from settings."""
    kwargs = {}
    if provider == "claude":
        kwargs["api_key"] = settings.get("anthropic_api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
    elif provider == "hymt2":
        kwargs["model_size"] = hymt2_model_size or settings.get("hymt2_model_size", "1.8B")
    elif provider == "nllb":
        kwargs["model_size"] = nllb_model_size or settings.get("nllb_model_size", "600M")
    else:  # ollama
        kwargs["base_url"] = settings.get("ollama_url", "http://localhost:11434")
        resolved_model = model or settings.get("ollama_model", "") or os.environ.get("OLLAMA_MODEL", "")
        if resolved_model:
            kwargs["model"] = resolved_model
    return kwargs


def _make_translator(provider, settings, model=None,
                     hymt2_model_size=None, nllb_model_size=None):
    """Get translator instance, using cached weights for NLLB."""
    if provider == "nllb":
        size = nllb_model_size or settings.get("nllb_model_size", "600M")
        return _get_nllb_translator(size)
    kwargs = _build_translator_kwargs(
        provider, settings, model=model,
        hymt2_model_size=hymt2_model_size, nllb_model_size=nllb_model_size,
    )
    return get_translator(provider=provider, **kwargs)


def _run_translate_worker(segments, source_lang, target_lang, provider, model,
                          hymt2_model_size=None, nllb_model_size=None):
    """Background worker for translation with partial result streaming."""
    global translate_progress

    try:
        settings = load_settings()

        translate_progress.update({
            "progress": 0.05, "stage": "translating",
            "detail": f"Translating {len(segments)} segments to {target_lang}...",
            "partial_segments": None,
        })

        translator = _make_translator(
            provider, settings, model=model,
            hymt2_model_size=hymt2_model_size, nllb_model_size=nllb_model_size,
        )

        # Collect partial results as batches complete
        all_translated = [{"text": ""}] * len(segments)

        def on_progress(p):
            pct = 0.05 + p * 0.90  # 5% → 95%
            done_count = int(p * len(segments))
            translate_progress.update({
                "progress": round(pct, 3),
                "stage": "translating",
                "detail": f"Translating... {done_count}/{len(segments)} segments",
            })

        def on_batch_done(results_so_far, batch_end):
            """Push partial results after each batch completes."""
            for i, t in enumerate(results_so_far):
                if i < len(all_translated):
                    all_translated[i] = t
            translate_progress.update({
                "partial_segments": list(all_translated),
                "partial_count": batch_end,
                "target_lang": target_lang,
            })

        translated = translator.translate(
            segments, source_lang, target_lang,
            on_progress=on_progress,
            on_batch_done=on_batch_done,
        )

        # Update all_translated with final results
        for i, t in enumerate(translated):
            if i < len(all_translated):
                all_translated[i] = t

        translate_progress.update({
            "status": "done", "progress": 1.0,
            "stage": "done",
            "detail": f"Done — {len(translated)} segments translated",
            "result": {
                "segments": translated,
                "count": len(translated),
                "target_lang": target_lang,
                "provider": provider,
            },
            "partial_segments": None,
        })

    except Exception as e:
        translate_progress.update({
            "status": "error", "progress": 0.0,
            "stage": "error", "detail": str(e),
        })

@app.post("/translate")
def translate_text(req: TranslateRequest):
    global translate_progress

    if not req.segments:
        return JSONResponse(status_code=400, content={"error": "No segments to translate"})

    translate_progress = {
        "status": "processing", "progress": 0.02,
        "stage": "preparing", "detail": "Preparing translation..."
    }

    thread = threading.Thread(
        target=_run_translate_worker,
        args=(req.segments, req.source_lang, req.target_lang, req.provider, req.model),
        kwargs={
            "hymt2_model_size": req.hymt2_model_size,
            "nllb_model_size": req.nllb_model_size,
        },
        daemon=True,
    )
    thread.start()

    return {"status": "started", "message": "Translation started. Poll /translate/progress for updates."}


@app.post("/translate/stream")
def translate_stream(req: TranslateOneRequest):
    """Stream translation tokens for a single text (live mode).

    Returns text/plain chunked body. Ollama and NLLB both produce real token
    streams; Claude/Hy-MT2 fall back to a non-streamed single chunk.
    """
    settings = load_settings()

    if req.provider == "ollama":
        base_url = settings.get("ollama_url", "http://localhost:11434")
        resolved_model = req.model or settings.get("ollama_model", "") or os.environ.get("OLLAMA_MODEL", "")
        translator = OllamaTranslator(
            base_url=base_url,
            model=resolved_model if resolved_model else None,
        )
        def gen_ollama():
            try:
                for tok in translator.stream_translate_one(req.text, req.source_lang, req.target_lang):
                    yield tok
            except Exception as e:
                yield f"\n[error: {e}]"
        return StreamingResponse(gen_ollama(), media_type="text/plain; charset=utf-8")

    if req.provider == "nllb":
        size = req.nllb_model_size or settings.get("nllb_model_size", "600M")
        translator = _get_nllb_translator(size)
        def gen_nllb():
            try:
                for tok in translator.stream_translate_one(req.text, req.source_lang, req.target_lang):
                    yield tok
            except Exception as e:
                yield f"\n[error: {e}]"
        return StreamingResponse(gen_nllb(), media_type="text/plain; charset=utf-8")

    # Fallback for Claude / Hy-MT2: non-streamed, wrap result in single chunk
    try:
        translator = _make_translator(
            req.provider, settings, model=req.model,
            hymt2_model_size=req.hymt2_model_size, nllb_model_size=req.nllb_model_size,
        )
        result = translator.translate([{"text": req.text}], req.source_lang, req.target_lang)
        text = result[0]["text"] if result else ""
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return StreamingResponse(iter([text]), media_type="text/plain; charset=utf-8")


@app.post("/translate/prewarm")
def translate_prewarm(req: TranslateOneRequest):
    """Pre-load translator model so first real translation isn't a cold start.

    Returns immediately on best-effort failure (model not pulled, Ollama down).
    """
    settings = load_settings()
    if req.provider == "ollama":
        base_url = settings.get("ollama_url", "http://localhost:11434")
        resolved_model = req.model or settings.get("ollama_model", "") or os.environ.get("OLLAMA_MODEL", "")
        translator = OllamaTranslator(
            base_url=base_url,
            model=resolved_model if resolved_model else None,
        )
        ok = translator.warmup()
        return {"ok": True, "warmed": ok, "model": translator.model}
    if req.provider == "nllb":
        size = req.nllb_model_size or settings.get("nllb_model_size", "600M")
        translator = _get_nllb_translator(size)
        ok = translator.warmup()
        return {"ok": True, "warmed": ok, "model": translator.model_id, "device": translator._device}
    return {"ok": True, "warmed": False}


@app.post("/translate/one")
def translate_one(req: TranslateOneRequest):
    """Translate a single segment synchronously (for per-row re-translate)."""
    settings = load_settings()
    try:
        translator = _make_translator(
            req.provider, settings, model=req.model,
            hymt2_model_size=req.hymt2_model_size, nllb_model_size=req.nllb_model_size,
        )
        result = translator.translate(
            [{"text": req.text}], req.source_lang, req.target_lang,
        )
        return {"text": result[0]["text"] if result else ""}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── Demucs check ──

@app.get("/demucs/check")
def demucs_check():
    try:
        import importlib
        spec = importlib.util.find_spec("demucs")
        return {"available": spec is not None}
    except Exception:
        return {"available": False}


# ── Ollama status check ──

@app.get("/ollama/status")
def ollama_status():
    settings = load_settings()
    base_url = settings.get("ollama_url", "http://localhost:11434")
    return OllamaTranslator.check_available(base_url)


# ── Hy-MT2 status & download ──

hymt2_download_progress = {"status": "idle", "progress": 0.0, "detail": ""}

@app.get("/hymt2/status")
def hymt2_status(model_size: str = "1.8B"):
    downloaded = HyMT2Translator.is_downloaded(model_size)
    model_id = HyMT2Translator.MODELS.get(model_size, model_size)
    return {
        "model_size": model_size,
        "model_id": model_id,
        "downloaded": downloaded,
        "download_progress": hymt2_download_progress,
    }

def _run_hymt2_download(model_size: str):
    global hymt2_download_progress
    hymt2_download_progress = {"status": "downloading", "progress": 0.1, "detail": f"Downloading Hy-MT2 {model_size}... (~3GB, may take several minutes)"}
    try:
        import sys
        model_id = HyMT2Translator.MODELS.get(model_size, HyMT2Translator.MODELS["1.8B"])
        cache_dir = HyMT2Translator.CACHE_DIR
        os.makedirs(cache_dir, exist_ok=True)

        # Detect if running in PyInstaller bundle. In that case sys.executable
        # is the bundled binary (Windows .exe or macOS .app), not Python — so
        # `subprocess.run([sys.executable, "-c", ...])` would re-launch the
        # GUI app instead of running Python. Download in-process instead.
        bundled = getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None) is not None

        if bundled:
            from huggingface_hub import snapshot_download
            snapshot_download(
                repo_id=model_id,
                cache_dir=cache_dir,
                ignore_patterns=["*.bin"],
            )
            hymt2_download_progress = {"status": "done", "progress": 1.0, "detail": f"Hy-MT2 {model_size} downloaded successfully!"}
        else:
            # Dev mode: use subprocess for isolation
            result = subprocess.run(
                [sys.executable, "-c",
                 f"from huggingface_hub import snapshot_download; "
                 f"snapshot_download(repo_id='{model_id}', cache_dir=r'{cache_dir}', ignore_patterns=['*.bin']);"
                 f"print('done')"],
                capture_output=True, text=True, timeout=3600,
            )
            if result.returncode == 0:
                hymt2_download_progress = {"status": "done", "progress": 1.0, "detail": f"Hy-MT2 {model_size} downloaded successfully!"}
            else:
                hymt2_download_progress = {"status": "error", "progress": 0.0, "detail": result.stderr[-500:] or "Download failed"}
    except subprocess.TimeoutExpired:
        hymt2_download_progress = {"status": "error", "progress": 0.0, "detail": "Timeout sau 1 giờ"}
    except Exception as e:
        hymt2_download_progress = {"status": "error", "progress": 0.0, "detail": str(e)}

class HyMT2DownloadRequest(BaseModel):
    model_size: str = "1.8B"

@app.post("/hymt2/download")
def hymt2_download(req: HyMT2DownloadRequest):
    model_size = req.model_size
    if model_size not in HyMT2Translator.MODELS:
        return JSONResponse(status_code=400, content={"error": f"Invalid model_size. Choose from: {list(HyMT2Translator.MODELS)}"})
    if hymt2_download_progress.get("status") == "downloading":
        return {"status": "already_downloading"}
    thread = threading.Thread(target=_run_hymt2_download, args=(model_size,), daemon=True)
    thread.start()
    return {"status": "started", "model_size": model_size}


# ── NLLB-200 status & download ──

nllb_download_progress = {"status": "idle", "progress": 0.0, "detail": ""}

@app.get("/nllb/status")
def nllb_status(model_size: str = "600M"):
    downloaded = NLLBTranslator.is_downloaded(model_size)
    model_id = NLLBTranslator.MODELS.get(model_size, model_size)
    return {
        "model_size": model_size,
        "model_id": model_id,
        "downloaded": downloaded,
        "download_progress": nllb_download_progress,
    }


def _run_nllb_download(model_size: str):
    global nllb_download_progress
    size_label = "~2.4GB" if model_size == "600M" else "~5GB"
    nllb_download_progress = {
        "status": "downloading", "progress": 0.1,
        "detail": f"Downloading NLLB-200 {model_size} ({size_label})...",
    }
    try:
        import sys
        model_id = NLLBTranslator.MODELS.get(model_size, NLLBTranslator.MODELS["600M"])
        cache_dir = NLLBTranslator.CACHE_DIR
        os.makedirs(cache_dir, exist_ok=True)
        bundled = getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None) is not None

        def _is_benign_symlink_error(stderr_or_msg: str) -> bool:
            return ("WinError 1314" in stderr_or_msg
                    or "privilege is not held" in stderr_or_msg
                    or "symlink" in stderr_or_msg.lower())

        if bundled:
            from huggingface_hub import snapshot_download
            try:
                snapshot_download(
                    repo_id=model_id,
                    cache_dir=cache_dir,
                    ignore_patterns=["*.bin"],
                )
            except OSError as oe:
                # On Windows without Developer Mode / admin, HF Hub fails when
                # creating symlinks from snapshots → blobs. Model weights are
                # usually already downloaded; verify via is_downloaded.
                if _is_benign_symlink_error(str(oe)) and NLLBTranslator.is_downloaded(model_size):
                    pass  # benign — model files are present
                else:
                    raise
            nllb_download_progress = {"status": "done", "progress": 1.0,
                                       "detail": f"NLLB-200 {model_size} ready"}
        else:
            result = subprocess.run(
                [sys.executable, "-c",
                 f"from huggingface_hub import snapshot_download; "
                 f"snapshot_download(repo_id='{model_id}', cache_dir=r'{cache_dir}', ignore_patterns=['*.bin']);"
                 f"print('done')"],
                capture_output=True, text=True, timeout=3600,
            )
            if result.returncode == 0:
                nllb_download_progress = {"status": "done", "progress": 1.0,
                                           "detail": f"NLLB-200 {model_size} ready"}
            elif _is_benign_symlink_error(result.stderr or "") and NLLBTranslator.is_downloaded(model_size):
                # Windows symlink permission failure but weights are present
                nllb_download_progress = {"status": "done", "progress": 1.0,
                                           "detail": f"NLLB-200 {model_size} ready (symlinks skipped)"}
            else:
                nllb_download_progress = {"status": "error", "progress": 0.0,
                                           "detail": result.stderr[-500:] or "Download failed"}
    except subprocess.TimeoutExpired:
        nllb_download_progress = {"status": "error", "progress": 0.0,
                                   "detail": "Timeout after 1 hour"}
    except Exception as e:
        nllb_download_progress = {"status": "error", "progress": 0.0, "detail": str(e)}


class NLLBDownloadRequest(BaseModel):
    model_size: str = "600M"

@app.post("/nllb/download")
def nllb_download(req: NLLBDownloadRequest):
    model_size = req.model_size
    if model_size not in NLLBTranslator.MODELS:
        return JSONResponse(status_code=400, content={"error": f"Invalid model_size. Choose from: {list(NLLBTranslator.MODELS)}"})
    if nllb_download_progress.get("status") == "downloading":
        return {"status": "already_downloading"}
    thread = threading.Thread(target=_run_nllb_download, args=(model_size,), daemon=True)
    thread.start()
    return {"status": "started", "model_size": model_size}


# ── File save endpoint ──

# Export directory — user can change via /choose-folder
# Load saved export dir, fallback to Downloads
_saved_export = load_settings().get("export_dir", "")
export_dir = _saved_export if _saved_export and os.path.isdir(_saved_export) else os.path.join(os.path.expanduser("~"), "Downloads")


@app.get("/export-dir")
def get_export_dir():
    """Return current export directory."""
    return {"path": export_dir}


class ExportDirRequest(BaseModel):
    path: str


@app.post("/export-dir")
def set_export_dir(req: ExportDirRequest):
    """Set the export directory (the panel picks it with CEP's native dialog)."""
    global export_dir
    chosen = os.path.abspath(os.path.expanduser(req.path or ""))
    if not os.path.isdir(chosen):
        return JSONResponse(status_code=400, content={"error": f"Folder not found: {req.path}"})
    export_dir = chosen
    settings = load_settings()
    settings["export_dir"] = export_dir
    save_settings(settings)
    return {"path": export_dir}


@app.post("/choose-folder")
def choose_folder():
    """Open native folder picker dialog. Returns selected path."""
    global export_dir
    import platform

    chosen = None

    if platform.system() == "Darwin":
        # macOS: use osascript (works from any thread). The path is embedded in
        # an AppleScript string literal, so escape it.
        default_loc = export_dir.replace("\\", "\\\\").replace('"', '\\"')
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'set theFolder to choose folder with prompt "Choose export folder" '
                 f'default location POSIX file "{default_loc}"\n'
                 'return POSIX path of theFolder'],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                chosen = result.stdout.strip().rstrip("/")
        except Exception:
            pass
    else:
        # Windows/Linux: use tkinter
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            chosen = filedialog.askdirectory(
                title="Choose export folder",
                initialdir=export_dir,
            )
            root.destroy()
        except Exception:
            pass

    if chosen and os.path.isdir(chosen):
        export_dir = chosen
        # Persist in settings
        settings = load_settings()
        settings["export_dir"] = export_dir
        save_settings(settings)
        return {"path": export_dir}

    return {"path": export_dir, "cancelled": True}


@app.post("/save-file")
def save_file(req: SaveFileRequest):
    """Save exported file to chosen export directory."""
    safe_name = os.path.basename(req.filename)
    if not safe_name:
        return JSONResponse(status_code=400, content={"error": "Invalid filename"})

    # Avoid overwriting: add suffix if file exists
    base, ext = os.path.splitext(safe_name)
    dest = os.path.join(export_dir, safe_name)
    counter = 1
    while os.path.exists(dest):
        dest = os.path.join(export_dir, f"{base}_{counter}{ext}")
        counter += 1

    try:
        with open(dest, "w", encoding="utf-8") as f:
            f.write(req.content)
        return {"path": dest, "filename": os.path.basename(dest)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── Legacy analyze endpoint (kept for compatibility) ──

@app.get("/analyze/progress")
def get_analyze_progress():
    return autocut_progress


# Removed (security): the legacy Premiere bridge endpoints — /find-file,
# /resolve-nested, /apply-speaker-labels, /execute-jsx, /split-at-points,
# /test-razor, /label-clips-jsx, /diag-jsx, /diag-label, /apply-cuts,
# /cep-status, /apply-cuts-keyboard. They drove Premiere through a file-IPC
# companion panel that evals arbitrary code, or through osascript (/execute-jsx
# ran any ExtendScript it was sent). The CEP panel talks to Premiere directly
# via host.jsx; nothing current calls these.


# ── Live Transcription (WebSocket) — LocalAgreement-2 + Sliding Window ──
#
# Architecture (modeled after YouTube Live Captions / Whisper-Streaming):
#
#   Browser ──[~42ms PCM chunks]──► WebSocket ──► sliding audio window
#                                                      │
#                                              webrtcvad (pause/resume only)
#                                                      │
#                                    Every ~0.5-1s ──► Whisper(window) ──► word list
#                                                      │
#                                    LocalAgreement-2: commit words that appear
#                                    in TWO consecutive hypotheses at same position
#                                                      │
#                              ┌─── commit words → trim window from start
#                              │                     keep ~0.8s overlap for context
#                              │
#                              └─── flush sentence when:
#                                     • ≥6 committed words, OR
#                                     • hard punctuation (.!?), OR
#                                     • silence ≥ 500ms
#
#   Frontend receives:
#     { type: "partial", text: "..." }         ← hypothesis tail (flickers, light text)
#     { type: "final_segment", segment: {} }   ← committed sentence (locked, bold)
#


class LiveStreamProcessor:
    """Real-time transcription using LocalAgreement-2 + sliding window.

    Key ideas (YouTube-style):
    - The audio buffer is a SLIDING WINDOW (max ~12s), not a growing utterance.
    - On each partial cycle, Whisper re-decodes the window → produces a word list.
    - LocalAgreement-2: a word is COMMITTED only when two consecutive hypotheses
      agree on it at the same position (with small timestamp tolerance).
    - After committing, the audio buffer is TRIMMED from the start (keeping a
      small overlap), so the next decode is fast and latency stays constant.
    - Committed words feed a sentence buffer; we emit final_segment when the
      sentence is "closed" (punctuation, length cap, or silence).
    """

    FRAME_MS = 30
    SAMPLE_RATE = 16000
    BYTES_PER_SAMPLE = 2
    FRAME_BYTES = SAMPLE_RATE * BYTES_PER_SAMPLE * FRAME_MS // 1000  # 960

    SILENCE_THRESHOLD_MS = 350    # silence to flush pending sentence (lower → end-of-sentence detected sooner)
    MIN_SPEECH_MS = 200           # minimum speech to start a new utterance
    MAX_WINDOW_S = 12.0           # sliding window cap; force-commit if exceeded
    TRIM_OVERLAP_S = 0.8          # audio kept after last committed word

    # Partials are cheap now (window stays short) — keep cadence tight.
    # On a fast GPU (RTX 30/40-series), Whisper Turbo decodes a 6s window in
    # ~200ms; we can afford a 0.4s cycle and still leave headroom.
    PARTIAL_INTERVALS = {
        "tiny": 0.25, "base": 0.3, "small": 0.4,
        "medium": 0.7, "large-v3-turbo": 0.3, "large-v3": 0.9,
    }

    # Line grouping: put up to SENTENCES_PER_LINE complete sentences on one line
    # so the caption reads naturally instead of breaking every sentence.
    SENTENCES_PER_LINE = 2
    # Safety cap on words per line (only triggers for speech without punctuation,
    # or two very long sentences) so a line never runs away.
    SENTENCE_MAX_WORDS = 28
    SENTENCE_MIN_WORDS_FOR_SOFT_PUNCT = 12   # (kept for reference; soft punct no longer breaks)
    # On a short pause (finalize), only end the line if it's already a real
    # clause (≥ this many words). Shorter fragments stay pending and merge with
    # the next utterance, so micro-pauses don't chop the text into tiny lines.
    MIN_LINE_WORDS_ON_PAUSE = 8
    HARD_PUNCT = ".!?。？！"
    SOFT_PUNCT = ",;，；:"
    INITIAL_PROMPT_TAIL_CHARS = 200

    # Emit a "draft_segment" event whenever the committed prefix changes (even
    # just one word). The frontend uses these for two purposes:
    #   1. Display the committed-but-unflushed text immediately (so users see
    #      stable words right after they're committed, not only at sentence end)
    #   2. Pre-translate when text grows past its own threshold (frontend-gated)
    # De-dup is by text content (_last_draft_text), so cadence is naturally
    # ~one emit per newly committed word.
    DRAFT_MIN_WORDS = 1

    # ── Confidence-based early commit ──────────────────────────────────────
    # LocalAgreement-2 normally needs two consecutive Whisper passes to agree
    # on a word before we commit it (≥1 partial cycle of latency floor). For
    # words Whisper is confident about AND that sit before the buffer end (so
    # they have right-context), skip the second pass and commit on first
    # detection. Trade-off: rare possibility of wrong commit when Whisper is
    # confident but wrong on first pass.
    HIGH_CONFIDENCE_THRESHOLD = 0.72
    # Min seconds between word end and buffer end to consider the word "settled"
    # (i.e. Whisper has enough following audio that it's unlikely to revise it).
    CONFIDENT_COMMIT_TAIL_SECONDS = 0.15

    def __init__(self, model: str = "base", language: str | None = None,
                 time_offset: float = 0.0, vad_aggressiveness: int = 2):
        import webrtcvad
        self.vad = webrtcvad.Vad(vad_aggressiveness)
        self.model = model
        self.language = language
        self.time_offset = time_offset
        self._partial_interval = self.PARTIAL_INTERVALS.get(model, 0.8)

        # Audio
        self._incoming = bytearray()
        self._speech_buffer = bytearray()
        self._buffer_start_time = 0.0  # absolute timeline of buffer[0]
        self._is_speaking = False
        self._silence_frames = 0
        self._speech_frames = 0
        self._total_received = 0
        self._pending_silence_flush = False

        # LocalAgreement state (timestamps RELATIVE to buffer)
        self._prev_hypothesis: list[dict] = []

        # Committed state (timestamps ABSOLUTE on session timeline)
        self._pending_sentence: list[dict] = []  # committed words awaiting sentence flush
        self._committed_session_text = ""        # rolling tail for initial_prompt
        self._finalized: list[dict] = []

        # Last draft text emitted (for de-dup so the frontend isn't spammed)
        self._last_draft_text = ""

        self._last_partial_time = 0.0

        self._transcriber = None
        self.running = False
        self._lock = threading.Lock()

    def _ensure_transcriber(self):
        if self._transcriber is None or self._transcriber.model_size != self.model:
            device = os.environ.get("WHISPER_DEVICE", "auto")
            self._transcriber = Transcriber(model_size=self.model, device=device)

    @property
    def current_time(self) -> float:
        return self.time_offset + self._total_received / (self.SAMPLE_RATE * self.BYTES_PER_SAMPLE)

    @property
    def _buffer_duration(self) -> float:
        return len(self._speech_buffer) / (self.SAMPLE_RATE * self.BYTES_PER_SAMPLE)

    # Phrases Whisper "imagines" on silence/noise (memorized from YouTube training data).
    # Substring match, case-insensitive, on the joined word list.
    _HALLUCINATION_PHRASES = (
        # Vietnamese YouTube intros/outros
        "ghiền mì gõ",
        "đăng ký kênh",
        "subscribe cho kênh",
        "nhấn chuông thông báo",
        "cảm ơn các bạn đã xem",
        "hẹn gặp lại các bạn",
        "đừng quên like",
        "đừng quên đăng ký",
        # English Whisper hallucinations
        "thanks for watching",
        "thank you for watching",
        "subscribe to my channel",
        "see you next time",
        "see you in the next video",
        "don't forget to subscribe",
        "like and subscribe",
        # Music / bracket tokens
        "[music]", "[applause]", "[laughter]", "♪", "♫",
        # Japanese / Korean common hallucinations
        "ご視聴ありがとうございました",
        "字幕",
        "다음 영상에서",
    )

    AVG_PROB_THRESHOLD = 0.35   # below this, suspect hallucination
    MIN_PROB_FRACTION = 0.5     # at least this fraction of words must clear AVG_PROB_THRESHOLD

    @classmethod
    def _is_hallucination_words(cls, words: list[dict]) -> bool:
        """Detect Whisper hallucinations: repetition loops, low confidence, blacklist phrases."""
        from collections import Counter
        if not words:
            return False
        # Probability-based: avg word prob too low → likely silent audio
        probs = [float(w.get("probability") or 0.0) for w in words]
        if probs:
            avg = sum(probs) / len(probs)
            if avg < cls.AVG_PROB_THRESHOLD:
                return True
        # Blacklist phrases (substring match)
        joined = " ".join(w["word"].strip() for w in words if w.get("word")).lower()
        for phrase in cls._HALLUCINATION_PHRASES:
            if phrase in joined:
                return True
        # Repetition loops
        toks = [w["word"].lower().strip(".,!?;:、，。") for w in words if w.get("word")]
        if len(toks) >= 5:
            counts = Counter(toks)
            _, top = counts.most_common(1)[0]
            if top / len(toks) > 0.55:
                return True
            if len(toks) >= 6:
                bigrams = [f"{toks[i]} {toks[i+1]}" for i in range(len(toks) - 1)]
                bc = Counter(bigrams).most_common(1)[0][1]
                if bc / len(bigrams) > 0.45:
                    return True
        return False

    # ── Audio ingestion (non-blocking) ──

    def ingest_audio(self, pcm_bytes: bytes) -> str | None:
        """Buffer audio + run VAD. Returns 'partial', 'finalize', or None.

        Never blocks on transcription — that runs in an executor.
        """
        with self._lock:
            self._incoming.extend(pcm_bytes)
            self._total_received += len(pcm_bytes)

        action_needed = None

        while len(self._incoming) >= self.FRAME_BYTES:
            frame = bytes(self._incoming[:self.FRAME_BYTES])
            del self._incoming[:self.FRAME_BYTES]

            try:
                is_speech = self.vad.is_speech(frame, self.SAMPLE_RATE)
            except Exception:
                is_speech = True

            if is_speech:
                self._silence_frames = 0
                self._speech_frames += 1

                if not self._is_speaking and self._speech_frames >= (self.MIN_SPEECH_MS // self.FRAME_MS):
                    self._is_speaking = True
                    # Buffer starts roughly when speech started
                    if not self._speech_buffer:
                        self._buffer_start_time = self.current_time - (
                            self._speech_frames * self.FRAME_MS / 1000.0
                        )

                if self._is_speaking:
                    self._speech_buffer.extend(frame)
                    if self._buffer_duration >= self.MAX_WINDOW_S:
                        action_needed = "finalize"
                    elif _time_module.time() - self._last_partial_time >= self._partial_interval:
                        # Lower gate → first words appear sooner after speech starts.
                        if self._buffer_duration >= 0.4:
                            action_needed = "partial"
            else:
                self._speech_frames = 0
                self._silence_frames += 1

                if self._is_speaking:
                    # Keep a bit of trailing silence in the buffer for context
                    self._speech_buffer.extend(frame)
                    silence_ms = self._silence_frames * self.FRAME_MS
                    if silence_ms >= self.SILENCE_THRESHOLD_MS:
                        action_needed = "finalize"
                        self._pending_silence_flush = True

        return action_needed

    # ── Inference + commit (runs in executor) ──

    def do_transcription(self, action: str) -> list[dict]:
        try:
            if action == "partial":
                return self._do_partial()
            elif action == "finalize":
                return self._do_finalize()
        except Exception as e:
            print(f"[live] do_transcription({action}) error: {e}")
        return []

    def _initial_prompt(self) -> str | None:
        tail = self._committed_session_text.strip()
        if not tail:
            return None
        return tail[-self.INITIAL_PROMPT_TAIL_CHARS:]

    def _run_inference(self) -> list[dict]:
        """Decode current buffer → list of words with RELATIVE timestamps.

        Feeds the PCM samples directly to the model as a float32 numpy array —
        no WAV file is written to disk each cycle (saves I/O + an extra audio
        decode in mlx/faster-whisper every partial).
        """
        if self._buffer_duration < 0.3:
            return []
        self._ensure_transcriber()
        try:
            import numpy as np
            pcm = (
                np.frombuffer(bytes(self._speech_buffer), dtype=np.int16)
                .astype(np.float32) / 32768.0
            )
            words = self._transcriber.transcribe_buffer(
                pcm,
                language=self.language,
                initial_prompt=self._initial_prompt(),
            )
            return words
        except Exception as e:
            print(f"[live] inference error: {e}")
            return []

    @staticmethod
    def _word_key(w: dict) -> str:
        return (w.get("word") or "").strip().lower().strip(".,!?;:、，。？！；：")

    def _agreement_prefix_len(self, prev: list[dict], curr: list[dict]) -> int:
        """Number of leading words in `curr` that agree with `prev` by token+timestamp."""
        n = 0
        for p, c in zip(prev, curr):
            if self._word_key(p) != self._word_key(c):
                break
            # Timestamps should be within ~400ms — generous for VAD drift
            if abs(p["start"] - c["start"]) > 0.5:
                break
            n += 1
        return n

    def _trim_buffer_to(self, time_relative: float):
        """Cut audio from buffer start to `time_relative` (seconds), aligned to frame."""
        if time_relative <= 0:
            return
        cut_bytes = int(time_relative * self.SAMPLE_RATE * self.BYTES_PER_SAMPLE)
        cut_bytes -= cut_bytes % self.FRAME_BYTES
        if cut_bytes <= 0 or cut_bytes >= len(self._speech_buffer):
            return
        cut_seconds = cut_bytes / (self.SAMPLE_RATE * self.BYTES_PER_SAMPLE)
        del self._speech_buffer[:cut_bytes]
        self._buffer_start_time += cut_seconds
        # Shift any remaining hypothesis words so they stay relative to new buffer start
        for w in self._prev_hypothesis:
            w["start"] = max(0.0, w["start"] - cut_seconds)
            w["end"] = max(0.0, w["end"] - cut_seconds)

    def _absolutize(self, w: dict) -> dict:
        return {
            "word": w["word"],
            "start": round(self._buffer_start_time + w["start"], 3),
            "end": round(self._buffer_start_time + w["end"], 3),
            "probability": w.get("probability", 0.0),
        }

    @staticmethod
    def _compose_text(words: list[dict]) -> str:
        """Join word fragments into a clean sentence."""
        return "".join(
            (w["word"] if w["word"].startswith((" ", "'", "’", ",", ".", "!", "?", ":", ";"))
             else " " + w["word"])
            for w in words
        ).strip()

    def _maybe_emit_draft(self) -> dict | None:
        """Emit a draft_segment event with the current committed-but-unflushed text,
        so the frontend can start translating before the sentence closes.

        Only emits when ≥DRAFT_MIN_WORDS committed AND text has changed since
        the last draft (de-dup).
        """
        if len(self._pending_sentence) < self.DRAFT_MIN_WORDS:
            return None
        text = self._compose_text(self._pending_sentence)
        if not text or text == self._last_draft_text:
            return None
        self._last_draft_text = text
        return {
            "type": "draft_segment",
            "text": text,
            "start": round(self._pending_sentence[0]["start"], 2),
            "end": round(self._pending_sentence[-1]["end"], 2),
            # Index of the upcoming final_segment so frontend can pair them
            "next_index": len(self._finalized),
            # Per-word tokens + absolute start timecodes so the frontend can
            # reveal each word at the speaker's actual rhythm.
            "words": [
                {"w": w["word"], "t": round(w["start"], 3)}
                for w in self._pending_sentence
            ],
        }

    def _flush_sentence(self, force: bool = False) -> list[dict]:
        """Emit final_segment(s) from _pending_sentence according to closing rules."""
        events: list[dict] = []
        if not self._pending_sentence:
            return events

        def emit(words: list[dict]):
            if not words:
                return
            text = self._compose_text(words)
            if not text:
                return
            segment = {
                "start": round(words[0]["start"], 2),
                "end": round(words[-1]["end"], 2),
                "text": text,
                "language": self.language,
                "type": "speech",
            }
            self._finalized.append(segment)
            # Keep rolling tail for next initial_prompt
            self._committed_session_text = (self._committed_session_text + " " + text)[
                -self.INITIAL_PROMPT_TAIL_CHARS * 2:
            ]
            # Sentence closed — reset draft so the next sentence starts fresh
            self._last_draft_text = ""
            events.append({
                "type": "final_segment",
                "segment": segment,
                "index": len(self._finalized) - 1,
            })

        # Scan committed buffer; group SENTENCES_PER_LINE complete sentences
        # onto one line. A line closes when we've collected that many hard-punct
        # sentences, OR the word cap is hit (safety so a line never runs away).
        bucket: list[dict] = []
        sentence_count = 0
        for w in self._pending_sentence:
            bucket.append(w)
            last_char = w["word"].strip()[-1:] if w["word"].strip() else ""
            if last_char in self.HARD_PUNCT:
                sentence_count += 1
                if sentence_count >= self.SENTENCES_PER_LINE:
                    emit(bucket)
                    bucket = []
                    sentence_count = 0
            elif len(bucket) >= self.SENTENCE_MAX_WORDS:
                # Runaway line (e.g. speech with no punctuation) — cap it.
                emit(bucket)
                bucket = []
                sentence_count = 0

        if force:
            emit(bucket)
            bucket = []

        self._pending_sentence = bucket
        return events

    def _do_partial(self) -> list[dict]:
        """One partial cycle: re-decode window → LocalAgreement → commit → trim."""
        self._last_partial_time = _time_module.time()
        words = self._run_inference()
        if not words:
            return []
        if self._is_hallucination_words(words):
            return []

        events: list[dict] = []

        # LocalAgreement-2: commit prefix that matches previous hypothesis
        commit_n = self._agreement_prefix_len(self._prev_hypothesis, words)

        # Extend the commit prefix with high-confidence words that already have
        # enough right-context — saves one full partial cycle of latency on
        # words Whisper is sure about. Only continues from where LocalAgreement
        # left off (we never skip past an uncertain word).
        buffer_dur = self._buffer_duration
        while commit_n < len(words):
            w = words[commit_n]
            prob = float(w.get("probability") or 0.0)
            word_end = float(w.get("end") or 0.0)
            tail = buffer_dur - word_end
            if prob >= self.HIGH_CONFIDENCE_THRESHOLD and tail >= self.CONFIDENT_COMMIT_TAIL_SECONDS:
                commit_n += 1
            else:
                break

        if commit_n > 0:
            committed = [self._absolutize(w) for w in words[:commit_n]]
            self._pending_sentence.extend(committed)

            # Trim audio up to last committed word end (minus small overlap)
            last_end_rel = words[commit_n - 1]["end"]
            trim_to = max(0.0, last_end_rel - self.TRIM_OVERLAP_S)
            self._trim_buffer_to(trim_to)

            # Recompute hypothesis tail (relative to NEW buffer after trim)
            shifted_tail = []
            for w in words[commit_n:]:
                shifted_tail.append({
                    "word": w["word"],
                    "start": max(0.0, w["start"] - trim_to),
                    "end": max(0.0, w["end"] - trim_to),
                    "probability": w.get("probability", 0.0),
                })
            self._prev_hypothesis = shifted_tail

            events.extend(self._flush_sentence(force=False))
        else:
            self._prev_hypothesis = words

        # Force-trim if window has grown past MAX_WINDOW_S without commits
        if self._buffer_duration >= self.MAX_WINDOW_S and not commit_n:
            # Force-commit current hypothesis prefix (best effort) to keep latency bounded
            if words:
                committed = [self._absolutize(w) for w in words]
                self._pending_sentence.extend(committed)
                self._committed_session_text = (
                    self._committed_session_text
                    + " "
                    + " ".join(w["word"].strip() for w in committed)
                )[-self.INITIAL_PROMPT_TAIL_CHARS * 2:]
                self._speech_buffer.clear()
                self._buffer_start_time = self.current_time
                self._prev_hypothesis = []
                events.extend(self._flush_sentence(force=False))

        # Emit draft of current committed-but-unflushed sentence so frontend
        # can start translating before sentence boundary fires. De-duplicated
        # by _last_draft_text inside _maybe_emit_draft.
        draft_event = self._maybe_emit_draft()
        if draft_event:
            events.append(draft_event)

        # Emit hypothesis tail as partial text + per-word timecodes (absolute),
        # so the frontend can reveal each tail word at the speaker's rhythm.
        tail_text = " ".join(w["word"].strip() for w in self._prev_hypothesis).strip()
        if tail_text:
            events.append({
                "type": "partial",
                "text": tail_text,
                "start": round(self._buffer_start_time, 2),
                "duration": round(self._buffer_duration, 1),
                "words": [
                    {"w": w["word"], "t": round(self._buffer_start_time + w["start"], 3)}
                    for w in self._prev_hypothesis
                ],
            })
        elif not events:
            # Send empty partial so frontend clears stale hypothesis
            events.append({"type": "partial", "text": "",
                           "start": round(self._buffer_start_time, 2), "duration": 0.0})

        return events

    def _do_finalize(self) -> list[dict]:
        """Utterance closed (silence or max-window): commit ALL remaining words + flush."""
        events: list[dict] = []
        words = self._run_inference()

        if words and not self._is_hallucination_words(words):
            committed = [self._absolutize(w) for w in words]
            self._pending_sentence.extend(committed)

        # Reset audio + hypothesis state
        self._speech_buffer.clear()
        self._buffer_start_time = self.current_time
        self._prev_hypothesis = []
        self._is_speaking = False
        self._silence_frames = 0
        self._speech_frames = 0
        self._pending_silence_flush = False
        self._last_partial_time = _time_module.time()

        # Complete sentences (hard punctuation, soft-punct-at-length, or the
        # word cap) always break inside _flush_sentence. The `force` flag only
        # decides what to do with the TRAILING incomplete clause:
        #   - If it's already a real clause (≥ MIN_LINE_WORDS_ON_PAUSE), end the
        #     line here — a pause after a full clause is a natural boundary.
        #   - If it's just a few words, keep it pending so it merges with the
        #     next utterance instead of becoming a tiny standalone line.
        substantial = len(self._pending_sentence) >= self.MIN_LINE_WORDS_ON_PAUSE
        events.extend(self._flush_sentence(force=substantial))

        if self._pending_sentence:
            # An unfinished clause carries across the pause — keep it on screen
            # as the in-progress draft instead of clearing the line.
            self._last_draft_text = ""
            draft = self._maybe_emit_draft()
            if draft:
                events.append(draft)
        else:
            # Nothing pending — clear the live line.
            self._last_draft_text = ""
            events.append({"type": "partial", "text": "",
                           "start": round(self.current_time, 2), "duration": 0.0})
        return events

    def flush(self) -> list[dict]:
        """Called on stop: drain any in-flight buffer + pending sentence."""
        events: list[dict] = []
        if self._is_speaking and self._buffer_duration >= 0.4:
            events.extend(self._do_finalize())
        else:
            events.extend(self._flush_sentence(force=True))
        return events

    @property
    def segments(self) -> list[dict]:
        return list(self._finalized)


@app.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    """WebSocket endpoint for real-time audio transcription.

    Protocol:
    → Client sends JSON: {"action": "start", "model": "base", "language": "auto"}
    → Client sends binary PCM frames (16-bit mono 16kHz, ~100ms chunks)
    ← Server sends: {"type": "partial", "text": "..."} (updating live text)
    ← Server sends: {"type": "final_segment", "segment": {...}} (locked segment)
    → Client sends JSON: {"action": "stop"}
    ← Server sends: {"type": "stopped", "segments": [...]}
    """
    await ws.accept()
    processor = None
    transcription_lock = asyncio.Lock()
    pending_action = [None]  # mutable container for latest action

    async def run_transcription_if_needed(proc, ws_conn):
        """Run transcription in background without blocking audio ingestion."""
        if transcription_lock.locked():
            return  # Already transcribing, skip — next cycle will pick it up
        async with transcription_lock:
            action = pending_action[0]
            pending_action[0] = None
            if not action or not proc.running:
                return
            try:
                loop = asyncio.get_event_loop()
                events = await loop.run_in_executor(
                    None, proc.do_transcription, action
                )
                for ev in events:
                    await ws_conn.send_json(ev)
            except Exception as e:
                print(f"[ws/live] Transcription error: {e}")

    try:
        while True:
            message = await ws.receive()

            if message["type"] == "websocket.disconnect":
                break

            # JSON commands
            if "text" in message:
                data = json.loads(message["text"])
                action = data.get("action", "")

                if action == "start":
                    model = data.get("model", "base")
                    language = data.get("language") or None
                    if language == "auto":
                        language = None
                    time_offset = float(data.get("time_offset", 0))
                    processor = LiveStreamProcessor(
                        model=model, language=language,
                        time_offset=time_offset, vad_aggressiveness=2,
                    )
                    processor.running = True

                    # Pre-load model (blocks until ready, before receiving audio)
                    await asyncio.get_event_loop().run_in_executor(
                        None, processor._ensure_transcriber
                    )

                    await ws.send_json({
                        "type": "status",
                        "status": "started",
                        "model": model,
                        "language": language or "auto",
                    })

                elif action == "stop":
                    if processor:
                        processor.running = False
                        # Flush remaining buffer
                        flush_events = await asyncio.get_event_loop().run_in_executor(
                            None, processor.flush
                        )
                        for ev in flush_events:
                            await ws.send_json(ev)

                        await ws.send_json({
                            "type": "stopped",
                            "segments": processor.segments,
                            "duration": round(processor.current_time, 1),
                        })
                        processor = None
                    break

            # Binary audio data
            elif "bytes" in message:
                if processor and processor.running:
                    # Fast path: buffer audio + VAD (instant, no transcription)
                    action = processor.ingest_audio(message["bytes"])

                    if action:
                        # Store latest action, fire-and-forget transcription
                        pending_action[0] = action
                        asyncio.create_task(run_transcription_if_needed(processor, ws))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[ws/live] Error: {e}")
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except:
            pass
    finally:
        if processor:
            processor.running = False


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 9876))
    uvicorn.run(app, host="127.0.0.1", port=port)
