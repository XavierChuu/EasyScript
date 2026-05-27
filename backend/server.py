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

from transcriber import Transcriber, is_model_cached, MODEL_SIZES
from silence_detector import SilenceDetector
from diarizer import Diarizer
from translator import get_translator, OllamaTranslator, HyMT2Translator, NLLBTranslator
from ffmpeg_utils import run_ffmpeg, run_silent, get_ffmpeg_exe

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "easyscript_uploads")
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

AVAILABLE_MODELS = [
    {"id": "tiny", "name": "Tiny", "size": "~75MB", "speed": "Fastest", "quality": "Low"},
    {"id": "base", "name": "Base", "size": "~140MB", "speed": "Fast", "quality": "Fair"},
    {"id": "small", "name": "Small", "size": "~460MB", "speed": "Medium", "quality": "Good"},
    {"id": "medium", "name": "Medium", "size": "~1.5GB", "speed": "Slow", "quality": "Great"},
    {"id": "large-v3-turbo", "name": "Turbo", "size": "~800MB", "speed": "Fast", "quality": "Best"},
    {"id": "large-v3", "name": "Large V3", "size": "~3GB", "speed": "Slowest", "quality": "Best"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lazy-load: don't block server startup with model loading
    yield


app = FastAPI(title="EasyScript Backend", lifespan=lifespan)


def _ensure_transcriber():
    """Lazy-load transcriber on first use."""
    global transcriber
    if transcriber is None:
        model_size = os.environ.get("WHISPER_MODEL", "tiny")
        device = os.environ.get("WHISPER_DEVICE", "auto")
        transcriber = Transcriber(model_size=model_size, device=device)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
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
    # "standard" | "sensitive" | "max" — controls pyannote's
    # min_cluster_size + clustering threshold for brief-utterance detection.
    sensitivity: Optional[str] = None

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
    """Generate waveform peaks using ffmpeg raw PCM output.
    For long files (>10min), uses lower sample rate to keep memory and time reasonable.
    """
    import struct
    try:
        duration = get_audio_duration(audio_path) or 0

        # Adaptive sample rate: lower for longer files to keep data size manageable
        if duration <= 600:
            sample_rate = 8000
        elif duration <= 1800:
            sample_rate = 4000
        elif duration <= 7200:
            sample_rate = 2000
        else:
            sample_rate = 1000

        # Timeout scales with duration: minimum 30s, ~1s per minute of audio
        timeout = max(30, int(duration / 60) + 15)

        result = run_ffmpeg(
            ["-i", audio_path, "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"],
            capture_output=True, timeout=timeout,
        )
        raw = result.stdout
        if not raw:
            return []

        # Parse 16-bit signed samples
        sample_count = len(raw) // 2
        samples = struct.unpack(f"<{sample_count}h", raw[:sample_count * 2])

        chunk_size = max(1, sample_count // num_peaks)
        peaks = []
        max_val = 1

        # First pass: find global max
        for i in range(0, sample_count, chunk_size):
            chunk = samples[i:i + chunk_size]
            val = max(abs(min(chunk)), abs(max(chunk)))
            if val > max_val:
                max_val = val

        # Second pass: normalize
        for i in range(num_peaks):
            start = i * chunk_size
            end = min(start + chunk_size, sample_count)
            if start >= sample_count:
                break
            chunk = samples[start:end]
            peak = max(abs(min(chunk)), abs(max(chunk))) / max_val
            peaks.append(round(peak, 4))

        return peaks
    except subprocess.TimeoutExpired:
        print(f"[peaks] Timeout generating peaks for {audio_path}")
        return []
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

@app.post("/upload")
async def upload_audio(file: UploadFile = File(...)):
    save_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # If video file, extract audio track to WAV for processing & playback
    ext = os.path.splitext(file.filename)[1].lower()
    if ext in VIDEO_EXTENSIONS:
        wav_name = os.path.splitext(file.filename)[0] + "_audio.wav"
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
    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={"error": f"File not found: {path}"})
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
                           song_vad_threshold=None, song_min_silence_ms=None, song_beam_size=None):
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
        )

        # Format final segments
        result_segments = [
            {
                "start": seg["start"],
                "end": seg["end"],
                "text": seg["text"],
                "language": seg.get("language"),
                "speaker": seg.get("speaker"),
                "type": "speech",
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

@app.get("/settings")
def get_settings():
    settings = load_settings()
    # Mask sensitive values
    masked = {**settings}
    for key in ("hf_token", "anthropic_api_key"):
        if key in masked and masked[key]:
            masked[key] = masked[key][:4] + "..." + masked[key][-4:]
    return masked

@app.post("/settings")
def update_settings(data: dict):
    settings = load_settings()
    settings.update(data)
    save_settings(settings)
    return {"status": "ok"}


# ── Speaker Diarization (async — pyannote in background thread) ──

@app.get("/diarize/progress")
def get_diarize_progress():
    return diarize_progress

def _run_diarize_worker(audio_path, speech_segments,
                        num_speakers=None, min_speakers=None, max_speakers=None,
                        sensitivity=None):
    """Background worker for speaker diarization."""
    global diarize_progress, diarizer

    try:
        # Ensure file is accessible (macOS TCC may block ~/Documents etc.)
        audio_path = ensure_accessible(audio_path)

        settings = load_settings()
        hf_token = settings.get("hf_token", "") or os.environ.get("HF_TOKEN", "")

        if not hf_token:
            diarize_progress.update({
                "status": "error", "progress": 0.0,
                "stage": "error",
                "detail": "HuggingFace token required. Configure in Settings.",
            })
            return

        audio_duration = get_audio_duration(audio_path)
        dur_str = ""
        if audio_duration > 0:
            dm, ds = int(audio_duration // 60), int(audio_duration % 60)
            dur_str = f" ({dm}m {ds:02d}s audio)"

        # Initialize diarizer (lazy load)
        if diarizer is None or diarizer.hf_token != hf_token:
            diarize_progress.update({
                "progress": 0.05, "stage": "loading_model",
                "detail": f"Loading speaker diarization model...{dur_str} (first run downloads ~700MB)",
            })
            diarizer = Diarizer(hf_token=hf_token)

        diarize_progress.update({
            "progress": 0.10, "stage": "diarizing",
            "detail": f"Identifying speakers...{dur_str}",
        })

        def on_progress(p):
            pct = 0.10 + p * 0.80  # 10% → 90%
            diarize_progress.update({
                "progress": round(pct, 3),
                "stage": "diarizing",
                "detail": f"Identifying speakers... {round(p * 100)}%{dur_str}",
            })

        diarize_segments = diarizer.diarize(
            audio_path, on_progress=on_progress,
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            sensitivity=sensitivity,
        )

        diarize_progress.update({
            "progress": 0.92, "stage": "merging",
            "detail": "Merging speaker labels with transcription...",
        })

        # Merge with speech segments
        updated_segments, speaker_map = Diarizer.merge_speakers_into_segments(
            speech_segments, diarize_segments
        )

        num_speakers = len(speaker_map)
        diarize_progress.update({
            "status": "done", "progress": 1.0,
            "stage": "done",
            "detail": f"Done — {num_speakers} speakers identified",
            "result": {
                "segments": updated_segments,
                "speaker_map": speaker_map,
                "num_speakers": num_speakers,
                "diarize_raw": diarize_segments,
            },
        })

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
        },
        daemon=True,
    )
    thread.start()

    return {"status": "started", "message": "Diarization started. Poll /diarize/progress for updates."}


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


@app.post("/choose-folder")
def choose_folder():
    """Open native folder picker dialog. Returns selected path."""
    global export_dir
    import platform

    chosen = None

    if platform.system() == "Darwin":
        # macOS: use osascript (works from any thread)
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'set theFolder to choose folder with prompt "Choose export folder" '
                 f'default location POSIX file "{export_dir}"\n'
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


@app.post("/find-file")
def find_file(req: dict):
    """Find a media file on disk by filename. Uses macOS Spotlight (mdfind) for speed."""
    filename = req.get("filename", "").strip()
    if not filename:
        return {"error": "No filename provided", "path": ""}

    import platform
    found_path = ""

    # Method 1: macOS Spotlight search (instant)
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["mdfind", "-name", filename],
                capture_output=True, text=True, timeout=5
            )
            paths = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
            # Filter: exact filename match only (not .pek, .cfa, etc.)
            for p in paths:
                if os.path.isfile(p) and os.path.basename(p) == filename:
                    found_path = p
                    break
        except Exception as e:
            print(f"[find-file] mdfind error: {e}")

    # Method 2: Search common media directories
    if not found_path:
        search_dirs = [
            os.path.expanduser("~/Documents"),
            os.path.expanduser("~/Desktop"),
            os.path.expanduser("~/Downloads"),
            os.path.expanduser("~/Movies"),
            os.path.expanduser("~/Music"),
            "/Volumes",
        ]
        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                continue
            try:
                result = subprocess.run(
                    ["find", search_dir, "-name", filename, "-type", "f", "-maxdepth", "6"],
                    capture_output=True, text=True, timeout=10
                )
                paths = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
                if paths:
                    found_path = paths[0]
                    break
            except Exception:
                continue

    return {"path": found_path, "filename": filename}


@app.post("/resolve-nested")
def resolve_nested():
    """Resolve media paths from a nested sequence via CEP ExtendScript.
    Finds the nested sequence in the active timeline and returns the source media paths
    from inside it.
    """
    import time as _time
    import uuid

    app_dir = os.path.expanduser("~/.easyscript")
    os.makedirs(app_dir, exist_ok=True)
    cmd_file = os.path.join(app_dir, "jsx_command.json")
    result_file = os.path.join(app_dir, "jsx_result.json")

    jsx_code = (
        '(function(){'
        'var seq=app.project.activeSequence;'
        'if(!seq)return "ERROR:no seq";'
        'var paths=[];'
        # Check all video+audio track clips to find nested sequences
        'var numSeq=app.project.sequences.numSequences;'
        'function findNested(trk){'
        'for(var c=0;c<trk.clips.numItems;c++){'
        'var pi=trk.clips[c].projectItem;'
        'if(!pi)continue;'
        # Check if this projectItem is a sequence by comparing nodeId with all sequences
        'for(var s=0;s<numSeq;s++){'
        'var ss=app.project.sequences[s];'
        'if(ss.projectItem&&ss.projectItem.nodeId===pi.nodeId){'
        # Found nested sequence — get all audio media paths from it
        'for(var at=0;at<ss.audioTracks.numTracks;at++){'
        'for(var ac=0;ac<ss.audioTracks[at].clips.numItems;ac++){'
        'try{var mp=ss.audioTracks[at].clips[ac].projectItem.getMediaPath();'
        'if(mp)paths.push(mp);}catch(e){}'
        '}'
        '}'
        # Also try video tracks for media with audio
        'for(var vt=0;vt<ss.videoTracks.numTracks;vt++){'
        'for(var vc=0;vc<ss.videoTracks[vt].clips.numItems;vc++){'
        'try{var mp2=ss.videoTracks[vt].clips[vc].projectItem.getMediaPath();'
        'if(mp2)paths.push(mp2);}catch(e){}'
        '}'
        '}'
        'break;'
        '}'
        '}'
        '}'
        '}'
        'for(var t=0;t<seq.videoTracks.numTracks;t++)findNested(seq.videoTracks[t]);'
        'if(paths.length===0){'
        'for(var t=0;t<seq.audioTracks.numTracks;t++)findNested(seq.audioTracks[t]);'
        '}'
        # Deduplicate
        'var unique=[];'
        'for(var i=0;i<paths.length;i++){'
        'var dup=false;for(var j=0;j<unique.length;j++){if(unique[j]===paths[i])dup=true;}'
        'if(!dup)unique.push(paths[i]);'
        '}'
        'if(unique.length===0)return "ERROR:no media paths in nested sequence";'
        'return "PATHS|"+unique.join("|");'
        '})();'
    )

    cid = str(uuid.uuid4())[:8]
    command = {"id": cid, "action": "eval", "code": jsx_code, "timestamp": _time.time()}
    with open(cmd_file, "w") as f:
        json.dump(command, f)
    try:
        os.remove(result_file)
    except FileNotFoundError:
        pass

    # Wait for result
    for _ in range(20):
        _time.sleep(0.3)
        if os.path.exists(result_file):
            try:
                with open(result_file, "r", encoding="utf-8", errors="replace") as rf:
                    rd = json.load(rf)
                if rd.get("id") == cid:
                    result = rd.get("result", "")
                    if result.startswith("PATHS|"):
                        paths = result.split("|")[1:]
                        # Return first audio-like file, or first file
                        audio_exts = {".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg"}
                        video_exts = {".mp4", ".mov", ".mkv", ".avi", ".mxf"}
                        audio_path = ""
                        video_path = ""
                        for p in paths:
                            ext = os.path.splitext(p)[1].lower()
                            if ext in audio_exts and not audio_path:
                                audio_path = p
                            elif ext in video_exts and not video_path:
                                video_path = p
                        # Prefer dedicated audio file, otherwise use video file (has audio track)
                        chosen = audio_path or video_path or paths[0]
                        return {"status": "ok", "path": chosen, "all_paths": paths}
                    elif result.startswith("ERROR"):
                        return {"status": "error", "message": result}
                    else:
                        return {"status": "error", "message": f"Unexpected: {result}"}
            except Exception:
                pass

    return {"status": "error", "message": "CEP companion timeout"}


class SpeakerCutsRequest(BaseModel):
    speaker_changes: list  # List of {"time": float, "from_speaker": str, "to_speaker": str}
    speaker_colors: dict   # { "SPEAKER_00": 0, "SPEAKER_01": 1, ... } — label color index
    fps: float = 25.0


@app.post("/apply-speaker-labels")
def apply_speaker_labels(req: SpeakerCutsRequest):
    """Apply speaker-based cuts and color labels to the timeline via CEP.

    1. Adds razor cuts at speaker change boundaries
    2. Labels each clip segment with the speaker's assigned color
    """
    import time as _time
    import uuid

    app_dir = os.path.expanduser("~/.easyscript")
    os.makedirs(app_dir, exist_ok=True)
    cmd_file = os.path.join(app_dir, "jsx_command.json")
    result_file = os.path.join(app_dir, "jsx_result.json")

    def _send_cep(code, timeout=30):
        cid = str(uuid.uuid4())[:8]
        command = {"id": cid, "action": "eval", "code": code, "timestamp": _time.time()}
        with open(cmd_file, "w") as f:
            json.dump(command, f)
        try:
            os.remove(result_file)
        except FileNotFoundError:
            pass
        elapsed = 0
        while elapsed < timeout:
            _time.sleep(0.3)
            elapsed += 0.3
            if os.path.exists(result_file):
                try:
                    with open(result_file, "r", encoding="utf-8", errors="replace") as rf:
                        rd = json.load(rf)
                    if rd.get("id") == cid:
                        return rd.get("result", "")
                except:
                    pass
        return None

    speaker_changes = req.speaker_changes
    speaker_colors = req.speaker_colors
    fps = req.fps

    # Premiere label colors: 0=Violet, 1=Iris, 2=Caribbean, 3=Lavender,
    # 4=Cerulean, 5=Forest, 6=Rose, 7=Mango, 8=Purple, 9=Blue, 10=Teal,
    # 11=Magenta, 12=Tan, 13=Green, 14=Brown, 15=Yellow
    # We use distinct colors for speakers
    SPEAKER_LABEL_COLORS = [4, 6, 5, 7, 9, 14, 15, 8]  # Cerulean, Rose, Forest, Mango, Blue, Brown, Yellow, Purple

    # Build speaker→label color mapping
    color_map = {}
    for spk, idx in speaker_colors.items():
        color_map[spk] = SPEAKER_LABEL_COLORS[idx % len(SPEAKER_LABEL_COLORS)]

    changes_js = json.dumps(speaker_changes)
    color_map_js = json.dumps(color_map)

    # Step 1: Split at speaker change points using 1-frame extract
    # (extract removes 1 frame but effectively splits the clip at that point)
    jsx_split = (
        '(function(){'
        'var TICKS=254016000000;'
        'var fps=' + str(fps) + ';'
        'var changes=' + changes_js + ';'
        'var log=[];'
        'try{app.enableQE();}catch(e){return "ERROR:QE:"+e.message;}'
        'var qeSeq;try{qeSeq=qe.project.getActiveSequence();}catch(e){return "ERROR:"+e.message;}'
        'var seq=app.project.activeSequence;'
        'if(!seq)return "ERROR:No seq";'
        'var nV=seq.videoTracks.numTracks;var nA=seq.audioTracks.numTracks;'
        # Target all tracks
        'for(var t=0;t<nV;t++){try{seq.videoTracks[t].setTargeted(true,true);}catch(e){try{seq.videoTracks[t].setTargeted(true);}catch(e2){}}}'
        'for(var t=0;t<nA;t++){try{seq.audioTracks[t].setTargeted(true,true);}catch(e){try{seq.audioTracks[t].setTargeted(true);}catch(e2){}}}'
        # Sort change points descending (process end→start)
        'changes.sort(function(a,b){return b.time-a.time;});'
        'var ok=0;var skip=0;var frameDur=1.0/fps;'
        'for(var i=0;i<changes.length;i++){'
        'var t=Math.round(changes[i].time*fps)/fps;'  # snap to frame
        'var inTicks=Math.round(t*TICKS).toString();'
        'var outTicks=Math.round((t+frameDur)*TICKS).toString();'
        'try{'
        'seq.setInPoint(inTicks);'
        'seq.setOutPoint(outTicks);'
        'qeSeq.extract();ok++;'
        '}catch(ex){skip++;}'
        '}'
        'try{seq.setInPoint("0");}catch(ex){}'
        'log.push("splits:"+ok+" skip:"+skip);'
        'return "OK|"+ok+"|"+log.join("\\n");'
        '})();'
    )

    split_result = _send_cep(jsx_split, timeout=30)
    split_info = split_result or "timeout"

    # Step 2: Label clips by speaker color
    # Build a segment map: for each speech segment, we know the speaker.
    # We'll label clips based on their position matching speech segments.
    # Labeling is now done via UXP API in the plugin (more reliable than ExtendScript)

    return {
        "status": "ok",
        "split_result": split_info,
    }


# ── ExtendScript execution for Premiere Pro ──
# Since UXP API doesn't support razor/add-edit operations,
# we use osascript to send ExtendScript to the running Premiere Pro instance.

class JsxRequest(BaseModel):
    code: str

@app.post("/execute-jsx")
def execute_jsx(req: JsxRequest):
    """Execute ExtendScript code in the running Premiere Pro instance via osascript."""
    jsx_code = req.code
    app_dir = os.path.expanduser("~/.easyscript")
    os.makedirs(app_dir, exist_ok=True)
    jsx_path = os.path.join(app_dir, "apply_cuts.jsx")

    # Write the jsx file
    with open(jsx_path, "w", encoding="utf-8") as f:
        f.write(jsx_code)

    errors = []

    # Find the running Premiere Pro app name
    # Try to detect from running processes
    premiere_app = None
    try:
        ps_result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get name of every process whose name contains "Premiere"'],
            capture_output=True, text=True, timeout=5
        )
        if ps_result.returncode == 0 and ps_result.stdout.strip():
            premiere_app = ps_result.stdout.strip().split(",")[0].strip()
    except Exception as e:
        errors.append(f"detect app: {str(e)}")

    # Fallback app names to try
    app_names = []
    if premiere_app:
        app_names.append(premiere_app)
    app_names.extend([
        "Adobe Premiere Pro 2025",
        "Adobe Premiere Pro 2024",
        "Adobe Premiere Pro 2023",
        "Adobe Premiere Pro",
    ])
    # Remove duplicates while preserving order
    seen = set()
    app_names = [x for x in app_names if not (x in seen or seen.add(x))]

    # Method 1: osascript "do javascript" with evalFile
    for app_name in app_names:
        try:
            # Escape path for AppleScript
            escaped_path = jsx_path.replace("\\", "\\\\").replace('"', '\\"')
            apple_script = f'tell application "{app_name}" to do javascript "$.evalFile(\'{escaped_path}\')"'
            result = subprocess.run(
                ["osascript", "-e", apple_script],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                return {"status": "ok", "method": f"do javascript ({app_name})", "output": result.stdout.strip()}
            errors.append(f"do javascript [{app_name}]: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            errors.append(f"do javascript [{app_name}]: timeout")
        except Exception as e:
            errors.append(f"do javascript [{app_name}]: {str(e)}")

    # Method 2: osascript "do script"
    for app_name in app_names:
        try:
            apple_script = f'tell application "{app_name}" to do script "{jsx_path}"'
            result = subprocess.run(
                ["osascript", "-e", apple_script],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                return {"status": "ok", "method": f"do script ({app_name})", "output": result.stdout.strip()}
            errors.append(f"do script [{app_name}]: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            errors.append(f"do script [{app_name}]: timeout")
        except Exception as e:
            errors.append(f"do script [{app_name}]: {str(e)}")

    # Method 3: Use open command to open .jsx with Premiere
    for app_name in app_names:
        try:
            result = subprocess.run(
                ["open", "-a", app_name, jsx_path],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                # open returns immediately, wait a bit
                import time
                time.sleep(3)
                return {"status": "ok", "method": f"open -a ({app_name})", "output": "Script opened (async)"}
            errors.append(f"open -a [{app_name}]: {result.stderr.strip()}")
        except Exception as e:
            errors.append(f"open -a [{app_name}]: {str(e)}")

    return {"status": "error", "errors": errors, "jsx_path": jsx_path}


class SplitAtPointsRequest(BaseModel):
    points: list  # [float] — post-cut timeline positions in seconds to add edits at
    fps: float = 25.0


@app.post("/split-at-points")
def split_at_points(req: SplitAtPointsRequest):
    """Non-destructive split (Add Edit) at given timeline positions.

    Uses keyboard shortcut Cmd+Shift+D via osascript since QE DOM razor()
    silently fails in Premiere 2025/2026. This:
    1. Sets playhead position via CEP ExtendScript
    2. Sends Cmd+Shift+D (Add Edit to All Tracks) via System Events
    No content is removed — just splits clips at each point.
    """
    import time as _time
    import uuid

    if not req.points:
        return {"status": "ok", "splits": 0, "message": "No split points"}

    app_dir = os.path.expanduser("~/.easyscript")
    os.makedirs(app_dir, exist_ok=True)
    cmd_file = os.path.join(app_dir, "jsx_command.json")
    result_file = os.path.join(app_dir, "jsx_result.json")

    TICKS = 254016000000
    fps = req.fps
    points = sorted(req.points)

    def snap(sec):
        return round(sec * fps) / fps

    # Find Premiere Pro process name
    premiere_app = "Adobe Premiere Pro"
    try:
        ps_result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of every process whose name contains "Premiere"'],
            capture_output=True, text=True, timeout=5
        )
        if ps_result.returncode == 0 and ps_result.stdout.strip():
            premiere_app = ps_result.stdout.strip().split(",")[0].strip()
    except Exception:
        pass

    def send_cep_command(cmd_id, jsx_code, timeout=10):
        command = {"id": cmd_id, "action": "eval", "code": jsx_code, "timestamp": _time.time()}
        with open(cmd_file, "w") as f:
            json.dump(command, f)
        try:
            os.remove(result_file)
        except FileNotFoundError:
            pass
        for _ in range(int(timeout / 0.3)):
            _time.sleep(0.3)
            if os.path.exists(result_file):
                try:
                    with open(result_file, "r", encoding="utf-8", errors="replace") as f:
                        result_data = json.load(f)
                    if result_data.get("id") == cmd_id:
                        return result_data.get("result", "")
                except Exception:
                    pass
        return None

    log = [f"Split points: {len(points)}", f"fps: {fps}"]

    # Count clips before
    count_jsx = (
        '(function(){'
        'var seq=app.project.activeSequence;if(!seq)return "ERROR:no seq";'
        'var n=0;'
        'for(var t=0;t<seq.videoTracks.numTracks;t++)n+=seq.videoTracks[t].clips.numItems;'
        'for(var t=0;t<seq.audioTracks.numTracks;t++)n+=seq.audioTracks[t].clips.numItems;'
        'return "CLIPS:"+n;'
        '})();'
    )
    count_result = send_cep_command("scnt-" + str(uuid.uuid4())[:4], count_jsx)
    if not count_result:
        return {"status": "error", "message": "CEP companion not responding"}

    clips_before = 0
    if count_result.startswith("CLIPS:"):
        clips_before = int(count_result.split(":")[1])
    log.append(f"Clips before: {clips_before}")

    # Set playhead + Cmd+Shift+D for each split point
    edit_count = 0
    for i, pt in enumerate(points):
        snapped = snap(pt)
        ticks = str(int(round(snapped * TICKS)))

        # Set playhead via CEP
        set_pos_jsx = f'(function(){{var seq=app.project.activeSequence;if(!seq)return "ERR";seq.setPlayerPosition("{ticks}");return "OK";}})();'
        pos_result = send_cep_command(f"sp-{i}", set_pos_jsx, timeout=5)
        if pos_result != "OK":
            log.append(f"Split {i}: setPlayerPosition failed: {pos_result}")
            continue

        _time.sleep(0.05)

        # Send Cmd+Shift+D = "Add Edit to All Tracks"
        script = f'''
        tell application "System Events"
            tell process "{premiere_app}"
                set frontmost to true
                keystroke "d" using {{command down, shift down}}
            end tell
        end tell
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                edit_count += 1
            else:
                log.append(f"Split {i}: keystroke failed: {result.stderr}")
        except Exception as e:
            log.append(f"Split {i}: osascript error: {e}")

        _time.sleep(0.1)

    log.append(f"Edit commands sent: {edit_count}")

    # Wait for Premiere to process
    _time.sleep(0.5)

    # Count clips after
    count_result2 = send_cep_command("scnt2-" + str(uuid.uuid4())[:4], count_jsx)
    clips_after = 0
    if count_result2 and count_result2.startswith("CLIPS:"):
        clips_after = int(count_result2.split(":")[1])
    log.append(f"Clips after: {clips_after}")
    log.append(f"New clips: {clips_after - clips_before}")

    return {
        "status": "ok",
        "splits": clips_after - clips_before,
        "edit_count": edit_count,
        "clips_before": clips_before,
        "clips_after": clips_after,
        "log": "\n".join(log),
    }


class DiagRequest(BaseModel):
    test_time: float = 5.0  # Time in seconds to test razor

@app.post("/test-razor")
def test_razor():
    """Test ALL possible ways to razor/split a clip at a specific time without removing content."""
    import time as _time
    import uuid

    jsx_code = (
        '(function(){'
        'var TICKS=254016000000;'
        'var seq=app.project.activeSequence;if(!seq)return "ERROR:no seq";'
        'try{app.enableQE();}catch(e){return "ERROR:QE:"+e.message;}'
        'var qeSeq;try{qeSeq=qe.project.getActiveSequence();}catch(e){return "ERROR:qeSeq:"+e.message;}'
        'var log=[];'
        'var nV=seq.videoTracks.numTracks;var nA=seq.audioTracks.numTracks;'

        # Count clips before
        'var before=0;'
        'for(var t=0;t<nV;t++)before+=seq.videoTracks[t].clips.numItems;'
        'for(var t=0;t<nA;t++)before+=seq.audioTracks[t].clips.numItems;'
        'log.push("before:"+before);'

        # Get a split point in the middle of the first video clip
        'var c0=seq.videoTracks[0].clips[0];'
        'if(!c0)return "ERROR:no clips";'
        'var midSec=(c0.start.seconds+c0.end.seconds)/2;'
        'var midTicks=Math.round(midSec*TICKS);'
        'log.push("splitAt:"+midSec.toFixed(3)+"s ticks:"+midTicks);'

        # Get fps for timecode
        'var fps=25;try{var tb=seq.timebase;if(tb)fps=parseFloat(tb);}catch(e){}'
        'var frame=Math.floor(midSec*fps);'
        'var ff=frame%Math.round(fps);'
        'var ss=Math.floor(midSec)%60;'
        'var mm=Math.floor(midSec/60)%60;'
        'var hh=Math.floor(midSec/3600);'
        'var tc=(hh<10?"0":"")+hh+":"+(mm<10?"0":"")+mm+":"+(ss<10?"0":"")+ss+":"+(ff<10?"0":"")+ff;'
        'log.push("timecode:"+tc+" fps:"+fps);'

        # Enumerate qeTrack methods
        'var qeVT=qeSeq.getVideoTrackAt(0);'
        'var vtKeys=[];for(var k in qeVT)vtKeys.push(k+":"+typeof qeVT[k]);'
        'log.push("qeTrackKeys:"+vtKeys.join(","));'

        # Test 1: qeTrack.razor(ticksString)
        'try{qeVT.razor(midTicks.toString());log.push("razor(tickStr):ok");}catch(e){log.push("razor(tickStr):"+e.message);}'

        # Test 2: qeTrack.razor(timecode)
        'try{qeVT.razor(tc);log.push("razor(tc):ok");}catch(e){log.push("razor(tc):"+e.message);}'

        # Test 3: qeTrack.razor(seconds as string)
        'try{qeVT.razor(midSec.toString());log.push("razor(secStr):ok");}catch(e){log.push("razor(secStr):"+e.message);}'

        # Test 4: qeTrack.razor(ticks as number)
        'try{qeVT.razor(midTicks);log.push("razor(tickNum):ok");}catch(e){log.push("razor(tickNum):"+e.message);}'

        # Test 5: qeTrack.razorAt? splitAt?
        'try{if(typeof qeVT.razorAt==="function"){qeVT.razorAt(midTicks.toString());log.push("razorAt:ok");}else{log.push("razorAt:N/A");}}catch(e){log.push("razorAt:"+e.message);}'
        'try{if(typeof qeVT.splitAt==="function"){qeVT.splitAt(midTicks.toString());log.push("splitAt:ok");}else{log.push("splitAt:N/A");}}catch(e){log.push("splitAt:"+e.message);}'

        # Test 6: try on audio track too
        'var qeAT=qeSeq.getAudioTrackAt(0);'
        'try{qeAT.razor(midTicks.toString());log.push("audioRazor(tickStr):ok");}catch(e){log.push("audioRazor:"+e.message);}'

        # Test 7: sequence-level addEdit
        'try{if(typeof qeSeq.addEdit==="function"){qeSeq.addEdit(midTicks.toString());log.push("qeSeq.addEdit:ok");}else{log.push("qeSeq.addEdit:N/A");}}catch(e){log.push("qeSeq.addEdit:"+e.message);}'

        # Test 8: razor on all targeted tracks via sequence
        'try{if(typeof qeSeq.razor==="function"){qeSeq.razor(midTicks.toString());log.push("qeSeq.razor:ok");}else{log.push("qeSeq.razor:N/A");}}catch(e){log.push("qeSeq.razor:"+e.message);}'

        # Count clips after
        'var after=0;'
        'for(var t=0;t<nV;t++)after+=seq.videoTracks[t].clips.numItems;'
        'for(var t=0;t<nA;t++)after+=seq.audioTracks[t].clips.numItems;'
        'log.push("after:"+after+" diff:"+(after-before));'

        'return "RAZOR|"+log.join("\\n");'
        '})();'
    )

    app_dir = os.path.expanduser("~/.easyscript")
    os.makedirs(app_dir, exist_ok=True)
    cmd_file = os.path.join(app_dir, "jsx_command.json")
    result_file = os.path.join(app_dir, "jsx_result.json")

    cid = str(uuid.uuid4())[:8]
    command = {"id": cid, "action": "eval", "code": jsx_code, "timestamp": _time.time()}
    with open(cmd_file, "w") as f:
        json.dump(command, f)
    try:
        os.remove(result_file)
    except FileNotFoundError:
        pass

    for _ in range(30):
        _time.sleep(0.5)
        if os.path.exists(result_file):
            try:
                with open(result_file, "r", encoding="utf-8", errors="replace") as rf:
                    rd = json.load(rf)
                if rd.get("id") == cid:
                    return {"status": "ok", "result": rd.get("result", "")}
            except:
                pass

    return {"status": "error", "message": "CEP timeout"}


class LabelClipsRequest(BaseModel):
    speaker_ranges: list   # [{ s: float, e: float, spk: str }, ...]
    color_map: dict        # { "SPEAKER_00": 4, "SPEAKER_01": 6, ... }
    speaker_names: dict = {}  # { "SPEAKER_00": "Speaker A", ... }


@app.post("/label-clips-jsx")
def label_clips_jsx(req: LabelClipsRequest):
    """Rename clips by speaker name AND try to set label via JSX.

    Since per-clip label colors don't have a public API in Premiere ExtendScript,
    we rename clips to show the speaker name on the timeline.
    Also try projectItem.setColorLabel as a best-effort color attempt.
    """
    import time as _time
    import uuid

    ranges_js = json.dumps(req.speaker_ranges)
    names_js = json.dumps(req.speaker_names)

    jsx_code = (
        '(function(){'
        'var seq=app.project.activeSequence;if(!seq)return "ERROR:no seq";'
        'var ranges=' + ranges_js + ';'
        'var names=' + names_js + ';'
        'var nV=seq.videoTracks.numTracks;var nA=seq.audioTracks.numTracks;'

        # Helper: find speaker for a given time (midpoint of clip)
        'function getSpeaker(sec){'
        'for(var i=0;i<ranges.length;i++){'
        'if(sec>=ranges[i].s-0.15&&sec<ranges[i].e+0.15)return ranges[i].spk;'
        '}'
        'return "";'
        '}'

        # Helper: format seconds to MM:SS
        'function fmtTime(s){var m=Math.floor(s/60);var ss=Math.floor(s%60);'
        'return(m<10?"0":"")+m+":"+(ss<10?"0":"")+ss;}'

        # Rename clips by speaker
        'var renamed=0;'
        'for(var t=0;t<nV;t++){'
        'for(var c=0;c<seq.videoTracks[t].clips.numItems;c++){'
        'var cl=seq.videoTracks[t].clips[c];'
        'var mid=(cl.start.seconds+cl.end.seconds)/2;'
        'var spk=getSpeaker(mid);'
        'if(spk&&names[spk]){'
        'try{cl.name=names[spk]+" · "+fmtTime(cl.start.seconds);renamed++;}catch(e){}'
        '}'
        '}}'
        'for(var t=0;t<nA;t++){'
        'for(var c=0;c<seq.audioTracks[t].clips.numItems;c++){'
        'var cl=seq.audioTracks[t].clips[c];'
        'var mid=(cl.start.seconds+cl.end.seconds)/2;'
        'var spk=getSpeaker(mid);'
        'if(spk&&names[spk]){'
        'try{cl.name=names[spk]+" · "+fmtTime(cl.start.seconds);renamed++;}catch(e){}'
        '}'
        '}}'

        'return "COLORED|"+renamed+"|clip.name|renamed:"+renamed;'
        '})();'
    )

    app_dir = os.path.expanduser("~/.easyscript")
    os.makedirs(app_dir, exist_ok=True)
    cmd_file = os.path.join(app_dir, "jsx_command.json")
    result_file = os.path.join(app_dir, "jsx_result.json")

    cid = str(uuid.uuid4())[:8]
    command = {"id": cid, "action": "eval", "code": jsx_code, "timestamp": _time.time()}
    with open(cmd_file, "w") as f:
        json.dump(command, f)
    try:
        os.remove(result_file)
    except FileNotFoundError:
        pass

    for _ in range(30):
        _time.sleep(0.5)
        if os.path.exists(result_file):
            try:
                with open(result_file, "r", encoding="utf-8", errors="replace") as rf:
                    rd = json.load(rf)
                if rd.get("id") == cid:
                    return {"status": "ok", "result": rd.get("result", "")}
            except:
                pass

    return {"status": "error", "message": "CEP timeout"}


@app.post("/diag-jsx")
def diag_jsx(req: DiagRequest):
    """Run diagnostic ExtendScript to discover what APIs actually work for splitting clips."""
    import time as _time
    import uuid

    test_time = req.test_time

    # Simplified diagnostic that tests extract and enumerate qeSeq
    # Single-line diagnostic for evalScript compatibility
    jsx_code = (
        '(function(){'
        'var log=[];var TICKS=254016000000;var testSec=' + str(test_time) + ';'
        'try{app.enableQE();log.push("QE:ok");}catch(e){return "ERROR:QE:"+e.message;}'
        'var seq=app.project.activeSequence;if(!seq)return "ERROR:no sequence";'
        'var qeSeq;try{qeSeq=qe.project.getActiveSequence();}catch(e){return "ERROR:qeSeq:"+e.message;}'
        'log.push("seq:"+seq.name);log.push("ver:"+app.version);'
        'var nV=seq.videoTracks.numTracks;var nA=seq.audioTracks.numTracks;'
        'log.push("V:"+nV+" A:"+nA);'
        # Count clips
        'var totalClips=0;'
        'for(var t=0;t<nV;t++)totalClips+=seq.videoTracks[t].clips.numItems;'
        'for(var t=0;t<nA;t++)totalClips+=seq.audioTracks[t].clips.numItems;'
        'log.push("Clips:"+totalClips);'
        # Target all tracks
        'for(var t=0;t<nV;t++){try{seq.videoTracks[t].setTargeted(true,true);}catch(e){}}'
        'for(var t=0;t<nA;t++){try{seq.audioTracks[t].setTargeted(true,true);}catch(e){}}'
        # Test extract with in/out points
        'var inT=Math.round(testSec*TICKS).toString();'
        'var outT=Math.round((testSec+1.0)*TICKS).toString();'
        'try{seq.setInPoint(inT);log.push("setIn:ok");}catch(e){log.push("setIn:"+e.message);}'
        'try{seq.setOutPoint(outT);log.push("setOut:ok");}catch(e){log.push("setOut:"+e.message);}'
        'var clipsBefore=totalClips;'
        'try{qeSeq.extract();log.push("extract:ok");}catch(e){log.push("extract:"+e.message);}'
        'var clipsAfter=0;'
        'for(var t=0;t<nV;t++)clipsAfter+=seq.videoTracks[t].clips.numItems;'
        'for(var t=0;t<nA;t++)clipsAfter+=seq.audioTracks[t].clips.numItems;'
        'log.push("ClipsAfter:"+clipsAfter);'
        # Check clip.move() and undo APIs
        'log.push("clip.move:"+(typeof seq.videoTracks[0].clips[0].move));'
        'log.push("clip.start.ticks:"+seq.videoTracks[0].clips[0].start.ticks);'
        # Check for undo group APIs
        'log.push("beginUndoGroup:"+(typeof app.beginUndoGroup));'
        'log.push("endUndoGroup:"+(typeof app.endUndoGroup));'
        # Enumerate qeSeq keys
        'var qeAll=[];try{for(var k in qeSeq)qeAll.push(k);}catch(e){}'
        'log.push("qeSeq:"+qeAll.join(","));'
        # Enumerate app keys for undo
        'var appKeys=[];try{for(var k in app){if(k.indexOf("ndo")>=0||k.indexOf("roup")>=0)appKeys.push(k);}}catch(e){}'
        'log.push("appUndo:"+appKeys.join(","));'
        'try{seq.setInPoint("0");}catch(e){}'
        'return "DIAG|"+log.join("\\n");'
        '})();'
    )

    app_dir = os.path.expanduser("~/.easyscript")
    os.makedirs(app_dir, exist_ok=True)
    cmd_file = os.path.join(app_dir, "jsx_command.json")
    result_file = os.path.join(app_dir, "jsx_result.json")

    cmd_id = "diag-" + str(uuid.uuid4())[:4]
    command = {"id": cmd_id, "action": "eval", "code": jsx_code, "timestamp": _time.time()}

    with open(cmd_file, "w") as f:
        json.dump(command, f)

    try:
        os.remove(result_file)
    except FileNotFoundError:
        pass

    for _ in range(60):  # 30 second timeout
        _time.sleep(0.5)
        if os.path.exists(result_file):
            try:
                with open(result_file, "r", encoding="utf-8", errors="replace") as f:
                    result_data = json.load(f)
                if result_data.get("id") == cmd_id:
                    return {"status": "ok", "result": result_data.get("result", "")}
            except:
                pass

    return {"status": "error", "message": "CEP companion timeout"}


@app.post("/diag-label")
def diag_label():
    """Diagnostic: enumerate ALL properties/methods on TrackItem and try setting label color."""
    import time as _time
    import uuid

    jsx_code = (
        '(function(){'
        'var seq=app.project.activeSequence;if(!seq)return "ERROR:no seq";'
        'var clip=seq.videoTracks[0].clips[0];if(!clip)return "ERROR:no clip";'
        'var log=[];'

        # Enumerate ALL keys on clip
        'var allKeys=[];for(var k in clip){allKeys.push(k+":"+typeof clip[k]);}log.push("CLIP_KEYS:"+allKeys.join(","));'

        # Enumerate projectItem keys
        'try{var pi=clip.projectItem;var piKeys=[];for(var k in pi){piKeys.push(k+":"+typeof pi[k]);}log.push("PI_KEYS:"+piKeys.join(","));}catch(e){log.push("PI_ERR:"+e.message);}'

        # Try QE clip keys
        'try{app.enableQE();var qeSeq=qe.project.getActiveSequence();'
        'var qeVT=qeSeq.getVideoTrackAt(0);var qeClip=qeVT.getItemAt(0);'
        'var qeKeys=[];for(var k in qeClip){qeKeys.push(k+":"+typeof qeClip[k]);}log.push("QE_CLIP_KEYS:"+qeKeys.join(","));'
        '}catch(e){log.push("QE_ERR:"+e.message);}'

        # Try setting label via different methods
        'var tests=[];'
        'try{clip.setColorLabel(4);tests.push("setColorLabel:OK");}catch(e){tests.push("setColorLabel:"+e.message);}'
        'try{clip.colorLabel=4;tests.push("colorLabel=4:OK");}catch(e){tests.push("colorLabel=:"+e.message);}'
        'try{clip.label=4;tests.push("label=4:OK");}catch(e){tests.push("label=:"+e.message);}'
        'try{clip.projectItem.setColorLabel(4);tests.push("pi.setColorLabel:OK");}catch(e){tests.push("pi.setColorLabel:"+e.message);}'
        # QE methods
        'try{var qeC=qeSeq.getVideoTrackAt(0).getItemAt(0);qeC.setColorLabel(4);tests.push("qe.setColorLabel:OK");}catch(e){tests.push("qe.setColorLabel:"+e.message);}'
        'try{var qeC=qeSeq.getVideoTrackAt(0).getItemAt(0);qeC.SetLabel(4);tests.push("qe.SetLabel:OK");}catch(e){tests.push("qe.SetLabel:"+e.message);}'
        'try{var qeC=qeSeq.getVideoTrackAt(0).getItemAt(0);qeC.setLabel(4);tests.push("qe.setLabel:OK");}catch(e){tests.push("qe.setLabel:"+e.message);}'

        'log.push("TESTS:"+tests.join("|"));'
        'return "DIAG|"+log.join("\\n");'
        '})();'
    )

    app_dir = os.path.expanduser("~/.easyscript")
    os.makedirs(app_dir, exist_ok=True)
    cmd_file = os.path.join(app_dir, "jsx_command.json")
    result_file = os.path.join(app_dir, "jsx_result.json")

    cmd_id = "diag-label-" + str(uuid.uuid4())[:4]
    command = {"id": cmd_id, "action": "eval", "code": jsx_code, "timestamp": _time.time()}
    with open(cmd_file, "w") as f:
        json.dump(command, f)
    try:
        os.remove(result_file)
    except FileNotFoundError:
        pass

    for _ in range(20):
        _time.sleep(0.5)
        if os.path.exists(result_file):
            try:
                with open(result_file, "r", encoding="utf-8", errors="replace") as rf:
                    rd = json.load(rf)
                if rd.get("id") == cmd_id:
                    return {"status": "ok", "result": rd.get("result", "")}
            except:
                pass

    return {"status": "error", "message": "timeout"}


class ApplyCutsRequest(BaseModel):
    boundaries: list  # List of time positions (in seconds) to add edit
    silence_regions: list  # List of [start, end] pairs to remove

@app.post("/apply-cuts")
def apply_cuts(req: ApplyCutsRequest):
    """
    Send cut data to the CEP companion extension via file-based IPC.
    The CEP extension polls ~/.easyscript/jsx_command.json and executes ExtendScript.
    Results are written to ~/.easyscript/jsx_result.json.
    """
    import time as _time
    import uuid

    boundaries = req.boundaries
    silence_regions = req.silence_regions

    if not boundaries:
        return {"status": "error", "errors": ["No boundaries provided"]}

    app_dir = os.path.expanduser("~/.easyscript")
    os.makedirs(app_dir, exist_ok=True)
    cmd_file = os.path.join(app_dir, "jsx_command.json")
    result_file = os.path.join(app_dir, "jsx_result.json")

    def _send_cep(code, timeout=30):
        """Send code to CEP and wait for result."""
        cid = str(uuid.uuid4())[:8]
        command = {"id": cid, "action": "eval", "code": code, "timestamp": _time.time()}
        with open(cmd_file, "w") as f:
            json.dump(command, f)
        try:
            os.remove(result_file)
        except FileNotFoundError:
            pass
        elapsed = 0
        while elapsed < timeout:
            _time.sleep(0.3)
            elapsed += 0.3
            if os.path.exists(result_file):
                try:
                    with open(result_file, "r", encoding="utf-8", errors="replace") as f:
                        rd = json.load(f)
                    if rd.get("id") == cid:
                        return rd.get("result", "")
                except:
                    pass
        return None

    # ── Step 1: Extract silence regions ──
    jsx_extract = _generate_apply_cuts_jsx(boundaries, silence_regions)
    extract_result = _send_cep(jsx_extract, timeout=30)

    if not extract_result:
        return {
            "status": "error",
            "method": "timeout",
            "errors": [
                "CEP companion not responding. Make sure:",
                "1. Open Window > Extensions > Pro Cut Helper",
                "2. Panel shows 'Polling for commands...'",
            ]
        }

    if extract_result.startswith("ERROR"):
        return {"status": "error", "method": "CEP companion", "errors": [extract_result]}

    # Parse extract result
    extract_log = ""
    extract_count = 0
    if extract_result.startswith("OK|"):
        parts = extract_result.split("|", 3)
        extract_count = int(parts[1]) if len(parts) > 1 else 0
        extract_log = parts[3] if len(parts) > 3 else ""

    # ── Step 2: Close remaining gaps (safety net — extract should already ripple-delete) ──
    # Parse gap count from extract result
    gaps_remaining = 0
    if extract_result and extract_result.startswith("OK|"):
        parts = extract_result.split("|", 3)
        gaps_remaining = int(parts[2]) if len(parts) > 2 else 0

    gap_result = None
    if gaps_remaining > 0:
        jsx_gaps = (
            '(function(){'
            'var seq=app.project.activeSequence;'
            'if(!seq)return "ERROR:no seq";'
            'var nV=seq.videoTracks.numTracks;var nA=seq.audioTracks.numTracks;'
            'var closed=0;var err=0;'
            # Close gaps on video tracks — use parseFloat for numeric comparison
            'for(var t=0;t<nV;t++){'
            'var trk=seq.videoTracks[t];'
            # Multiple passes — moving clips can reveal new gaps
            'for(var pass=0;pass<3;pass++){'
            'for(var c=1;c<trk.clips.numItems;c++){'
            'try{'
            'var pe=parseFloat(trk.clips[c-1].end.ticks);'
            'var cs=parseFloat(trk.clips[c].start.ticks);'
            'if(cs-pe>1000){trk.clips[c].move(pe.toString());closed++;}'
            '}catch(e){err++;}'
            '}'
            '}'
            '}'
            # Close gaps on audio tracks
            'for(var t=0;t<nA;t++){'
            'var trk=seq.audioTracks[t];'
            'for(var pass=0;pass<3;pass++){'
            'for(var c=1;c<trk.clips.numItems;c++){'
            'try{'
            'var pe=parseFloat(trk.clips[c-1].end.ticks);'
            'var cs=parseFloat(trk.clips[c].start.ticks);'
            'if(cs-pe>1000){trk.clips[c].move(pe.toString());closed++;}'
            '}catch(e){err++;}'
            '}'
            '}'
            '}'
            # Verify remaining gaps
            'var still=0;'
            'for(var t=0;t<nV;t++){'
            'var trk=seq.videoTracks[t];'
            'for(var c=1;c<trk.clips.numItems;c++){'
            'var pe=parseFloat(trk.clips[c-1].end.ticks);'
            'var cs=parseFloat(trk.clips[c].start.ticks);'
            'if(cs-pe>1000)still++;'
            '}'
            '}'
            'return "GAPS|"+closed+"|"+err+"|"+still;'
            '})();'
        )

        gap_result = _send_cep(jsx_gaps, timeout=15)
    gap_info = ""
    if gap_result and gap_result.startswith("GAPS|"):
        gparts = gap_result.split("|")
        gap_info = f"GapsClosed:{gparts[1]} err:{gparts[2]} remaining:{gparts[3]}"
    elif gaps_remaining == 0:
        gap_info = "NoGaps (extract ripple-deleted cleanly)"

    # Combine results
    full_log = extract_log + ("\n" + gap_info if gap_info else "")
    full_output = f"OK|{extract_count}|{gaps_remaining}|{full_log}"

    return {
        "status": "ok",
        "method": "CEP companion",
        "output": full_output,
        "editCount": extract_count,
        "removedCount": extract_count,
        "log": full_log,
        "gapResult": gap_result or (gap_info if gap_info else "skipped")
    }


@app.get("/cep-status")
def cep_status():
    """Check if CEP companion is responsive by sending a ping."""
    import time as _time
    import uuid

    app_dir = os.path.expanduser("~/.easyscript")
    os.makedirs(app_dir, exist_ok=True)
    cmd_file = os.path.join(app_dir, "jsx_command.json")
    result_file = os.path.join(app_dir, "jsx_result.json")

    cmd_id = "ping-" + str(uuid.uuid4())[:4]
    command = {"id": cmd_id, "action": "ping", "timestamp": _time.time()}

    with open(cmd_file, "w") as f:
        json.dump(command, f)

    try:
        os.remove(result_file)
    except FileNotFoundError:
        pass

    # Wait up to 5 seconds
    for _ in range(10):
        _time.sleep(0.5)
        if os.path.exists(result_file):
            try:
                with open(result_file, "r", encoding="utf-8", errors="replace") as f:
                    result_data = json.load(f)
                if result_data.get("id") == cmd_id:
                    return {"status": "ok", "result": result_data.get("result", "")}
            except:
                pass

    return {"status": "error", "message": "CEP companion not responding"}


def _generate_apply_cuts_jsx(boundaries, silence_regions):
    """Generate ExtendScript code to remove silence using extract approach.

    Strategy: Use setInPoint/setOutPoint + qeSeq.extract() for each silence region.
    Process from LAST to FIRST silence region to preserve earlier timings.
    extract() = ripple delete (removes content AND closes gap).

    All cut points snapped to frame boundaries so video & audio stay aligned.
    """
    silence_js = json.dumps(silence_regions)

    extract_code = (
        '(function(){'
        'var TICKS=254016000000;'
        'var sr=' + silence_js + ';'
        'var log=[];'
        'try{app.enableQE();}catch(e){return "ERROR:QE:"+e.message;}'
        'var qeSeq;try{qeSeq=qe.project.getActiveSequence();}catch(e){return "ERROR:"+e.message;}'
        'if(!qeSeq)return "ERROR:No QE seq";'
        'var seq=app.project.activeSequence;'
        'if(!seq)return "ERROR:No seq";'
        'var nV=seq.videoTracks.numTracks;var nA=seq.audioTracks.numTracks;'

        # ── Detect frame rate ──
        # Method 1: sequence timebase property (most reliable)
        'var fps=0;'
        'try{var tb=seq.timebase;if(tb){fps=parseFloat(tb);}}catch(ex){}'
        # Method 2: calculate from first video clip ticks vs seconds
        'if(fps<=0||fps>120){'
        'try{'
        'var c0=seq.videoTracks[0].clips[0];'
        'var durSec=c0.end.seconds-c0.start.seconds;'
        'var durTicks=parseFloat(c0.end.ticks)-parseFloat(c0.start.ticks);'
        'if(durSec>0&&durTicks>0){'
        'var frameTicks=TICKS/24;'
        'var tryFps=[23.976,24,25,29.97,30,50,59.94,60];'
        'for(var fi=0;fi<tryFps.length;fi++){'
        'var ft=TICKS/tryFps[fi];'
        'var nFrames=Math.round(durTicks/ft);'
        'if(Math.abs(nFrames*ft-durTicks)<ft*0.01){fps=tryFps[fi];break;}'
        '}'
        '}'
        '}catch(ex){}}'
        # Method 3: fallback to 25fps (PAL) — common for video editing
        'if(fps<=0||fps>120)fps=25;'
        'var frameDur=1.0/fps;'
        'log.push("fps:"+fps.toFixed(3));'

        # Snap to nearest frame boundary
        'function snap(sec){return Math.round(sec*fps)/fps;}'

        # ── Target ALL tracks ──
        'for(var t=0;t<nV;t++){try{seq.videoTracks[t].setTargeted(true,true);}catch(e){try{seq.videoTracks[t].setTargeted(true);}catch(e2){}}}'
        'for(var t=0;t<nA;t++){try{seq.audioTracks[t].setTargeted(true,true);}catch(e){try{seq.audioTracks[t].setTargeted(true);}catch(e2){}}}'

        # ── Sort descending (process end→start to preserve timecodes) ──
        'var sorted=sr.slice(0);sorted.sort(function(a,b){return b[0]-a[0];});'
        'var ok=0;var er=0;var skip=0;'
        'for(var i=0;i<sorted.length;i++){'
        'var s=snap(sorted[i][0]);var e2=snap(sorted[i][1]);'
        # Skip if snapped region is less than 1 frame
        'if(e2-s<frameDur*0.9){skip++;continue;}'
        'try{'
        'seq.setInPoint(Math.round(s*TICKS).toString());'
        'seq.setOutPoint(Math.round(e2*TICKS).toString());'
        'qeSeq.extract();ok++;'
        '}catch(ex){er++;log.push("ex:"+ex.message);}'
        '}'
        # Clear in point
        'try{seq.setInPoint("0");}catch(ex){}'
        'log.push("Tracks V:"+nV+" A:"+nA);'
        'log.push("Extracted:"+ok+"/"+sorted.length+" skip:"+skip+" err:"+er);'

        # ── Verify V/A alignment ──
        'try{'
        'var nClips=Math.min(4,seq.videoTracks[0].clips.numItems);'
        'var aClips=seq.audioTracks[0].clips.numItems;'
        'log.push("Clips V:"+nClips+" A:"+aClips);'
        'var maxD=0;'
        'for(var c=0;c<Math.min(nClips,aClips);c++){'
        'var vs=seq.videoTracks[0].clips[c].start.seconds;'
        'var as2=seq.audioTracks[0].clips[c].start.seconds;'
        'var d=Math.abs(vs-as2);if(d>maxD)maxD=d;'
        'log.push("c"+c+" V:"+vs.toFixed(4)+" A:"+as2.toFixed(4)+" d:"+d.toFixed(4));'
        '}'
        'log.push("maxDrift:"+maxD.toFixed(4)+"s");'
        '}catch(ex){log.push("align:"+ex.message);}'

        # ── Check for remaining gaps ──
        'var gaps=0;'
        'try{'
        'for(var c=1;c<seq.videoTracks[0].clips.numItems;c++){'
        'var pe=parseFloat(seq.videoTracks[0].clips[c-1].end.ticks);'
        'var cs=parseFloat(seq.videoTracks[0].clips[c].start.ticks);'
        'if(Math.abs(cs-pe)>1000)gaps++;'
        '}'
        'log.push("gaps:"+gaps);'
        '}catch(ex){}'

        'return "OK|"+ok+"|"+gaps+"|"+log.join("\\n");'
        '})();'
    )
    return extract_code


@app.post("/apply-cuts-keyboard")
def apply_cuts_keyboard(req: ApplyCutsRequest):
    """
    Apply cuts using keyboard shortcut simulation via osascript.
    This approach:
    1. Uses CEP to set player position at each boundary
    2. Uses osascript/System Events to send Cmd+Shift+D (Add Edit to All Tracks)
    3. Then uses CEP to remove silence clips

    This is the fallback when QE DOM razor() silently fails.
    """
    import time as _time
    import uuid

    boundaries = sorted(req.boundaries)
    silence_regions = req.silence_regions

    if not boundaries:
        return {"status": "error", "errors": ["No boundaries provided"]}

    app_dir = os.path.expanduser("~/.easyscript")
    os.makedirs(app_dir, exist_ok=True)
    cmd_file = os.path.join(app_dir, "jsx_command.json")
    result_file = os.path.join(app_dir, "jsx_result.json")

    # Find Premiere Pro process name
    premiere_app = "Adobe Premiere Pro"
    try:
        ps_result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of every process whose name contains "Premiere"'],
            capture_output=True, text=True, timeout=5
        )
        if ps_result.returncode == 0 and ps_result.stdout.strip():
            premiere_app = ps_result.stdout.strip().split(",")[0].strip()
    except:
        pass

    log = [f"App: {premiere_app}", f"Boundaries: {len(boundaries)}", f"Silence: {len(silence_regions)}"]

    def send_cep_command(cmd_id, jsx_code, timeout=10):
        """Send command to CEP and wait for result."""
        command = {"id": cmd_id, "action": "eval", "code": jsx_code, "timestamp": _time.time()}
        with open(cmd_file, "w") as f:
            json.dump(command, f)
        try:
            os.remove(result_file)
        except FileNotFoundError:
            pass

        for _ in range(int(timeout / 0.3)):
            _time.sleep(0.3)
            if os.path.exists(result_file):
                try:
                    with open(result_file, "r", encoding="utf-8", errors="replace") as f:
                        result_data = json.load(f)
                    if result_data.get("id") == cmd_id:
                        return result_data.get("result", "")
                except:
                    pass
        return None

    def send_keyboard_shortcut(key, modifiers="command down, shift down"):
        """Send a keyboard shortcut to Premiere Pro via osascript."""
        script = f'''
        tell application "System Events"
            tell process "{premiere_app}"
                set frontmost to true
                keystroke "{key}" using {{{modifiers}}}
            end tell
        end tell
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except:
            return False

    # Step 1: Count clips before
    count_jsx = """
    (function() {
        var seq = app.project.activeSequence;
        if (!seq) return "ERROR:no seq";
        var n = 0;
        for (var t = 0; t < seq.videoTracks.numTracks; t++) n += seq.videoTracks[t].clips.numItems;
        for (var t = 0; t < seq.audioTracks.numTracks; t++) n += seq.audioTracks[t].clips.numItems;
        return "CLIPS:" + n;
    })();
    """
    count_result = send_cep_command("cnt-" + str(uuid.uuid4())[:4], count_jsx)
    if not count_result:
        return {"status": "error", "errors": [
            "CEP companion not responding. Please:",
            "1. Open Premiere Pro",
            "2. Go to Window > Extensions > Pro Cut Helper",
            "3. The panel should show 'Polling for commands...'"
        ]}

    clips_before = 0
    if count_result.startswith("CLIPS:"):
        clips_before = int(count_result.split(":")[1])
    log.append(f"Clips before: {clips_before}")

    # Step 2: For each boundary, set playhead + send keyboard shortcut
    edit_count = 0
    TICKS = 254016000000

    for i, boundary in enumerate(boundaries):
        ticks = str(int(round(boundary * TICKS)))

        # Set playhead position via CEP
        set_pos_jsx = f"""
        (function() {{
            var seq = app.project.activeSequence;
            if (!seq) return "ERR";
            seq.setPlayerPosition("{ticks}");
            return "OK";
        }})();
        """
        pos_result = send_cep_command(f"pos-{i}", set_pos_jsx, timeout=5)
        if pos_result != "OK":
            log.append(f"Boundary {i}: setPlayerPosition failed: {pos_result}")
            continue

        # Small delay to let Premiere update
        _time.sleep(0.05)

        # Send Cmd+Shift+D = "Add Edit to All Tracks"
        if send_keyboard_shortcut("d"):
            edit_count += 1
        else:
            log.append(f"Boundary {i}: keyboard shortcut failed")

        # Small delay between cuts
        _time.sleep(0.05)

    log.append(f"Edit commands sent: {edit_count}")

    # Wait for Premiere to finish processing
    _time.sleep(0.5)

    # Step 3: Count clips after
    count_result2 = send_cep_command("cnt2-" + str(uuid.uuid4())[:4], count_jsx)
    clips_after = 0
    if count_result2 and count_result2.startswith("CLIPS:"):
        clips_after = int(count_result2.split(":")[1])
    log.append(f"Clips after: {clips_after}")

    if clips_after <= clips_before:
        log.append("KEYBOARD CUTS DID NOT WORK - aborting remove")
        return {
            "status": "ok",
            "method": "keyboard",
            "output": f"OK|{edit_count}|0|" + "\n".join(log),
            "editCount": edit_count,
            "removedCount": 0,
            "log": "\n".join(log)
        }

    # Step 4: Remove silence clips via CEP
    silence_js = json.dumps(silence_regions)
    remove_jsx = f"""
    (function() {{
        var seq = app.project.activeSequence;
        if (!seq) return "ERROR:no seq";
        var silenceRegions = {silence_js};
        var TOL = 0.2;
        var removed = 0;

        function isSilence(cs, ce) {{
            for (var r = 0; r < silenceRegions.length; r++) {{
                var ss = silenceRegions[r][0];
                var se = silenceRegions[r][1];
                if (Math.abs(cs - ss) < TOL && Math.abs(ce - se) < TOL) return true;
                if (cs >= ss - TOL && ce <= se + TOL) return true;
            }}
            return false;
        }}

        for (var t = seq.videoTracks.numTracks - 1; t >= 0; t--) {{
            var track = seq.videoTracks[t];
            for (var c = track.clips.numItems - 1; c >= 0; c--) {{
                try {{
                    var clip = track.clips[c];
                    if (isSilence(clip.start.seconds, clip.end.seconds)) {{
                        clip.remove(false, true);
                        removed++;
                    }}
                }} catch(e) {{}}
            }}
        }}
        for (var t = seq.audioTracks.numTracks - 1; t >= 0; t--) {{
            var track = seq.audioTracks[t];
            for (var c = track.clips.numItems - 1; c >= 0; c--) {{
                try {{
                    var clip = track.clips[c];
                    if (isSilence(clip.start.seconds, clip.end.seconds)) {{
                        clip.remove(false, true);
                        removed++;
                    }}
                }} catch(e) {{}}
            }}
        }}
        return "REMOVED:" + removed;
    }})();
    """
    remove_result = send_cep_command("rm-" + str(uuid.uuid4())[:4], remove_jsx, timeout=15)
    removed_count = 0
    if remove_result and remove_result.startswith("REMOVED:"):
        removed_count = int(remove_result.split(":")[1])
    log.append(f"Removed: {removed_count}")

    return {
        "status": "ok",
        "method": "keyboard",
        "output": f"OK|{edit_count}|{removed_count}|" + "\n".join(log),
        "editCount": edit_count,
        "removedCount": removed_count,
        "log": "\n".join(log)
    }


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
        "tiny": 0.3, "base": 0.4, "small": 0.5,
        "medium": 0.8, "large-v3-turbo": 0.4, "large-v3": 1.0,
    }

    SENTENCE_MAX_WORDS = 10
    SENTENCE_MIN_WORDS_FOR_SOFT_PUNCT = 4
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
    HIGH_CONFIDENCE_THRESHOLD = 0.80
    # Min seconds between word end and buffer end to consider the word "settled"
    # (i.e. Whisper has enough following audio that it's unlikely to revise it).
    CONFIDENT_COMMIT_TAIL_SECONDS = 0.3

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
                        if self._buffer_duration >= 0.6:
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
        """Decode current buffer → list of words with RELATIVE timestamps."""
        if self._buffer_duration < 0.4:
            return []
        self._ensure_transcriber()
        tmp_path = os.path.join(UPLOAD_DIR, f"_live_{id(self)}.wav")
        try:
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.SAMPLE_RATE)
                wf.writeframes(bytes(self._speech_buffer))
            words = self._transcriber.transcribe_buffer(
                tmp_path,
                language=self.language,
                initial_prompt=self._initial_prompt(),
            )
            return words
        except Exception as e:
            print(f"[live] inference error: {e}")
            return []
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

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

        # Scan committed buffer; cut at each closing boundary
        bucket: list[dict] = []
        for w in self._pending_sentence:
            bucket.append(w)
            last_char = w["word"].strip()[-1:] if w["word"].strip() else ""
            if last_char in self.HARD_PUNCT:
                emit(bucket)
                bucket = []
            elif last_char in self.SOFT_PUNCT and len(bucket) >= self.SENTENCE_MIN_WORDS_FOR_SOFT_PUNCT:
                emit(bucket)
                bucket = []
            elif len(bucket) >= self.SENTENCE_MAX_WORDS:
                emit(bucket)
                bucket = []

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

        # Emit hypothesis tail as partial text
        tail_text = " ".join(w["word"].strip() for w in self._prev_hypothesis).strip()
        if tail_text:
            events.append({
                "type": "partial",
                "text": tail_text,
                "start": round(self._buffer_start_time, 2),
                "duration": round(self._buffer_duration, 1),
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
        self._last_draft_text = ""
        self._last_partial_time = _time_module.time()

        events.extend(self._flush_sentence(force=True))

        # Always send empty partial to clear the live line
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
