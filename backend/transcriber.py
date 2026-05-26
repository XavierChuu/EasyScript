import tempfile
import platform
import os
import sys

from ffmpeg_utils import run_silent, run_ffmpeg

# Add NVIDIA CUDA/cuDNN DLL directories on Windows so faster-whisper can
# load cublas64_12.dll / cudnn_*.dll. We search two places:
#  - dev venv: site-packages/nvidia/*/bin (pip-installed cuBLAS/cuDNN)
#  - bundle:   _MEIPASS/nvidia/*/bin (PyInstaller-collected dynamic libs)
if platform.system() == "Windows":
    from pathlib import Path

    def _add_nvidia_dll_paths():
        roots = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            # PyInstaller flattens DLLs into _MEIPASS root → make it searchable.
            try:
                os.add_dll_directory(meipass)
            except Exception:
                pass
            roots.append(Path(meipass))
        try:
            import site
            for sd in site.getsitepackages():
                roots.append(Path(sd))
        except Exception:
            pass

        seen = set()
        for root in roots:
            nvidia_path = root / "nvidia"
            if not nvidia_path.exists():
                continue
            for sub in nvidia_path.iterdir():
                bin_dir = sub / "bin"
                if not bin_dir.exists():
                    continue
                key = str(bin_dir).lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    os.add_dll_directory(str(bin_dir))
                except Exception:
                    pass

    _add_nvidia_dll_paths()



# ── Model repo mappings for mlx-whisper (HuggingFace MLX community) ──
MLX_MODEL_REPOS = {
    "tiny":           "mlx-community/whisper-tiny-mlx",
    "base":           "mlx-community/whisper-base-mlx-q4",
    "small":          "mlx-community/whisper-small-mlx",
    "medium":         "mlx-community/whisper-medium-mlx",
    "large-v3":       "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}

# faster-whisper (CTranslate2) model repos
FW_MODEL_REPOS = {
    "tiny":           "Systran/faster-whisper-tiny",
    "base":           "Systran/faster-whisper-base",
    "small":          "Systran/faster-whisper-small",
    "medium":         "Systran/faster-whisper-medium",
    "large-v3":       "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

# Approximate model sizes for download progress
MODEL_SIZES = {
    "tiny": "~75MB", "base": "~140MB", "small": "~460MB",
    "medium": "~1.5GB", "large-v3-turbo": "~800MB", "large-v3": "~3GB",
}


def is_model_cached(model_size, backend="mlx"):
    """Check if a model is already downloaded in HuggingFace cache."""
    try:
        from huggingface_hub import try_to_load_from_cache
        repos = MLX_MODEL_REPOS if backend == "mlx" else FW_MODEL_REPOS
        repo = repos.get(model_size)
        if not repo:
            return False
        result = try_to_load_from_cache(repo, "config.json")
        return result is not None and os.path.exists(str(result))
    except Exception:
        return False


def detect_best_backend():
    """
    Detect the best available backend for whisper transcription.
    Returns: ("mlx", device_name) | ("faster-whisper", device_name)
    """
    # 1. Apple Silicon → try mlx-whisper (Metal GPU)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            import mlx.core as mx
            if mx.metal.is_available():
                import mlx_whisper  # noqa: F401
                chip = _get_apple_chip()
                return "mlx", f"Metal GPU ({chip})"
            else:
                print("[Transcriber] MLX available but Metal not detected")
        except ImportError as e:
            print(f"[Transcriber] MLX not available: {e}")
        except Exception as e:
            print(f"[Transcriber] MLX error: {e}")

    # 2. NVIDIA GPU → faster-whisper with CUDA
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            gpu_name = _get_nvidia_gpu_name()
            return "faster-whisper", f"CUDA ({gpu_name})"
    except Exception:
        pass

    # 3. Fallback: faster-whisper on CPU
    cpu = platform.processor() or platform.machine()
    return "faster-whisper", f"CPU ({cpu})"


def _get_apple_chip():
    try:
        result = run_silent(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or "Apple Silicon"
    except Exception:
        return "Apple Silicon"


def _get_nvidia_gpu_name():
    try:
        result = run_silent(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip().split("\n")[0] or "NVIDIA GPU"
    except Exception:
        return "NVIDIA GPU"


def _is_hallucination(text):
    """No-op: blacklist filtering removed — rely on probability thresholds only."""
    return False


class Transcriber:
    CHUNK_DURATION = 600  # 10 minutes per chunk
    OVERLAP = 5.0  # 5s overlap to avoid cutting words at boundaries

    def __init__(self, model_size="large-v3", device="auto"):
        self.model_size = model_size
        self.backend, self.device_name = detect_best_backend()
        self.model = None

        if self.backend == "mlx":
            self._init_mlx(model_size)
        else:
            self._init_faster_whisper(model_size, device)

        print(f"[Transcriber] Backend: {self.backend} | Device: {self.device_name} | Model: {model_size}")

    def _init_mlx(self, model_size):
        """Initialize mlx-whisper (Apple Metal GPU)."""
        repo = MLX_MODEL_REPOS.get(model_size)
        if not repo:
            raise ValueError(f"No MLX model for '{model_size}'. Available: {list(MLX_MODEL_REPOS.keys())}")
        self._mlx_repo = repo
        # mlx-whisper loads model lazily on first transcribe call

    def _init_faster_whisper(self, model_size, device):
        """Initialize faster-whisper (CUDA or CPU).

        If `device="auto"` (default) and CUDA is reported available, we try
        loading the model on CUDA first. If that fails for any reason — most
        commonly because the bundled NVIDIA DLLs (cublas/cudnn) can't be
        loaded on this machine — we silently fall back to CPU and update
        `device_name` so the UI reflects what's actually being used.
        """
        from faster_whisper import WhisperModel

        want_cuda = False
        if device == "auto":
            try:
                import ctranslate2
                want_cuda = ctranslate2.get_cuda_device_count() > 0
            except Exception:
                want_cuda = False
            device = "cuda" if want_cuda else "cpu"
        elif device == "cuda":
            want_cuda = True

        def _load(dev):
            ct = "float16" if dev == "cuda" else "int8"
            return WhisperModel(
                model_size,
                device=dev,
                compute_type=ct,
                cpu_threads=os.cpu_count() or 4,
            )

        if device == "cuda":
            try:
                self.model = _load("cuda")
                return
            except Exception as e:
                print(f"[Transcriber] CUDA init failed ({e}); falling back to CPU.")
                cpu = platform.processor() or platform.machine()
                self.device_name = f"CPU ({cpu})"

        self.model = _load("cpu")

    # ── Public API ──

    def transcribe(self, audio_path, language=None, on_progress=None,
                   start_from=0.0, on_chunk_done=None, song_mode=False,
                   song_vad_threshold=None, song_min_silence_ms=None, song_beam_size=None):
        """Transcribe audio with chunked processing for long files."""
        song_opts = {
            "vad_threshold": song_vad_threshold,
            "min_silence_ms": song_min_silence_ms,
            "beam_size": song_beam_size,
        }
        duration = self._get_duration(audio_path) or 0
        if duration <= 0:
            if on_progress:
                on_progress(1.0)
            return []

        start_from = max(0, min(start_from, duration - 1))
        remaining = duration - start_from

        # Short files: process in one go
        if remaining <= self.CHUNK_DURATION:
            if start_from > 0:
                return self._transcribe_range(
                    audio_path, start_from, duration, duration,
                    language, on_progress, on_chunk_done, song_mode, song_opts
                )
            else:
                return self._transcribe_single(
                    audio_path, language, on_progress, on_chunk_done, song_mode, song_opts
                )

        # Long files: split into 10-min chunks with overlap
        all_segments = []
        chunk_starts = []
        pos = start_from
        while pos < duration:
            chunk_starts.append(pos)
            pos += self.CHUNK_DURATION
        num_chunks = len(chunk_starts)

        for i, chunk_start in enumerate(chunk_starts):
            chunk_end = min(chunk_start + self.CHUNK_DURATION + self.OVERLAP, duration)

            def make_chunk_progress(chunk_idx):
                def _progress(p):
                    if on_progress:
                        base = start_from / duration if duration > 0 else 0
                        chunk_frac = (chunk_idx + p) / num_chunks
                        overall = base + (1 - base) * chunk_frac
                        on_progress(min(overall, 0.99))
                return _progress

            chunk_segments = self._transcribe_range(
                audio_path, chunk_start, chunk_end, duration,
                language,
                on_progress=make_chunk_progress(i),
                on_chunk_done=None,
                song_mode=song_mode,
                song_opts=song_opts,
            )

            for seg in chunk_segments:
                if all_segments and seg["start"] < all_segments[-1]["end"] - 0.1:
                    continue
                all_segments.append(seg)

            if on_chunk_done:
                on_chunk_done(list(all_segments), i + 1, num_chunks)

        if on_progress:
            on_progress(1.0)

        return all_segments

    # ── Backend-specific transcription ──

    def _transcribe_single(self, audio_path, language, on_progress, on_chunk_done, song_mode=False, song_opts=None):
        """Transcribe entire file (short files, no extraction)."""
        if self.backend == "mlx":
            results = self._mlx_transcribe(audio_path, language, 0, on_progress, song_mode)
        else:
            results = self._fw_transcribe(audio_path, language, 0, on_progress, song_mode, song_opts)

        if on_chunk_done:
            on_chunk_done(results, 1, 1)
        return results

    def _transcribe_range(self, audio_path, start_sec, end_sec, total_duration,
                          language, on_progress, on_chunk_done, song_mode=False, song_opts=None):
        """Extract time range via ffmpeg, then transcribe."""
        chunk_duration = end_sec - start_sec
        tmp_path = None

        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.close(tmp_fd)

            run_ffmpeg(
                ["-y", "-ss", str(start_sec), "-t", str(chunk_duration),
                 "-i", audio_path, "-ac", "1", "-ar", "16000", tmp_path],
                capture_output=True, timeout=60,
            )

            if self.backend == "mlx":
                results = self._mlx_transcribe(tmp_path, language, start_sec, on_progress, song_mode)
            else:
                results = self._fw_transcribe(tmp_path, language, start_sec, on_progress, song_mode, song_opts)

            if on_chunk_done:
                on_chunk_done(results, 1, 1)
            return results

        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ── mlx-whisper backend (Apple Metal GPU) ──

    def _mlx_transcribe(self, audio_path, language, time_offset, on_progress, song_mode=False):
        """Transcribe using mlx-whisper on Metal GPU."""
        import mlx_whisper

        opts = {
            "path_or_hf_repo": self._mlx_repo,
            "word_timestamps": True,
            "condition_on_previous_text": False,
        }
        if language:
            opts["language"] = language
        if song_mode:
            if language == "vi":
                opts["initial_prompt"] = "Lời bài hát tiếng Việt:"
                opts["no_speech_threshold"] = 0.15
            else:
                opts["initial_prompt"] = "Song lyrics:"
                opts["no_speech_threshold"] = 0.25

        result = mlx_whisper.transcribe(audio_path, **opts)

        detected_lang = result.get("language", language or "")
        segments_raw = result.get("segments", [])
        duration = max((s["end"] for s in segments_raw), default=1) if segments_raw else 1

        results = []
        for seg in segments_raw:
            text = seg.get("text", "").strip()
            if not text or _is_hallucination(text):
                if on_progress and duration > 0:
                    on_progress(min(seg["end"] / duration, 1.0))
                continue

            words = []
            for w in seg.get("words", []):
                words.append({
                    "word": w.get("word", "").strip(),
                    "start": round(time_offset + w["start"], 3),
                    "end": round(time_offset + w["end"], 3),
                    "probability": round(w.get("probability", 0), 3),
                })

            results.append({
                "start": round(time_offset + seg["start"], 3),
                "end": round(time_offset + seg["end"], 3),
                "text": text,
                "language": detected_lang,
                "words": words,
            })

            if on_progress and duration > 0:
                on_progress(min(seg["end"] / duration, 1.0))

        return results

    # ── faster-whisper backend (CUDA / CPU) ──

    def _fw_transcribe(self, audio_path, language, time_offset, on_progress, song_mode=False, song_opts=None):
        """Transcribe using faster-whisper (CTranslate2).

        For song_mode, audio is assumed to already be vocal-isolated (e.g. by
        Demucs). User-tunable knobs (song_opts): vad_threshold, min_silence_ms,
        beam_size. None falls back to song-mode defaults below.
        """
        fw_opts = dict(
            language=language,
            beam_size=1,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        if song_mode:
            song_opts = song_opts or {}
            vad_thresh = song_opts.get("vad_threshold")
            min_silence_ms = song_opts.get("min_silence_ms")
            beam_size = song_opts.get("beam_size")

            fw_opts["vad_parameters"] = {
                "threshold": float(vad_thresh) if vad_thresh is not None else 0.4,
                "min_silence_duration_ms": int(min_silence_ms) if min_silence_ms is not None else 700,
            }
            fw_opts["no_speech_threshold"] = 0.4
            if beam_size is not None:
                fw_opts["beam_size"] = max(1, min(int(beam_size), 5))
            if language == "vi":
                fw_opts["initial_prompt"] = "Đây là lời bài hát tiếng Việt."
            else:
                fw_opts["initial_prompt"] = "Song lyrics."
        else:
            fw_opts["vad_parameters"] = {"min_silence_duration_ms": 300}
        segments_iter, info = self.model.transcribe(audio_path, **fw_opts)

        detected_lang = info.language
        duration = info.duration or 1
        results = []

        for seg in segments_iter:
            # Skip hallucinated segments: silence/noise that Whisper fills with
            # training-data phrases (e.g. YouTube subscribe prompts in Vietnamese).
            # Song mode uses looser thresholds because singing legitimately has
            # lower confidence than spoken speech.
            no_speech = getattr(seg, 'no_speech_prob', 0.0)
            avg_logprob = getattr(seg, 'avg_logprob', 0.0)
            if song_mode:
                # Isolated vocals: mild filter (avoid pure-noise segments)
                hallucinated = no_speech > 0.8 or avg_logprob < -1.5
            else:
                hallucinated = no_speech > 0.6 or avg_logprob < -1.0
            if hallucinated:
                if on_progress and duration > 0:
                    on_progress(min(seg.end / duration, 1.0))
                continue

            text = seg.text.strip()
            if not text or _is_hallucination(text):
                if on_progress and duration > 0:
                    on_progress(min(seg.end / duration, 1.0))
                continue

            results.append({
                "start": round(time_offset + seg.start, 3),
                "end": round(time_offset + seg.end, 3),
                "text": text,
                "language": detected_lang,
                "words": [
                    {
                        "word": w.word.strip(),
                        "start": round(time_offset + w.start, 3),
                        "end": round(time_offset + w.end, 3),
                        "probability": round(w.probability, 3),
                    }
                    for w in (seg.words or [])
                ],
            })

            if on_progress and duration > 0:
                on_progress(min(seg.end / duration, 1.0))

        return results

    # ── Utility ──

    @staticmethod
    def _get_duration(audio_path):
        try:
            from ffmpeg_utils import get_audio_duration
            d = get_audio_duration(audio_path)
            return d if d > 0 else None
        except Exception:
            return None
