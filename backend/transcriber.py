import subprocess
import tempfile
import platform
import os


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
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() or "Apple Silicon"
    except Exception:
        return "Apple Silicon"


def _get_nvidia_gpu_name():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip().split("\n")[0] or "NVIDIA GPU"
    except Exception:
        return "NVIDIA GPU"


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
        """Initialize faster-whisper (CUDA or CPU)."""
        from faster_whisper import WhisperModel

        if device == "auto":
            try:
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"

        compute_type = "float16" if device == "cuda" else "int8"
        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            cpu_threads=os.cpu_count() or 4,
        )

    # ── Public API ──

    def transcribe(self, audio_path, language=None, on_progress=None,
                   start_from=0.0, on_chunk_done=None):
        """Transcribe audio with chunked processing for long files."""
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
                    language, on_progress, on_chunk_done
                )
            else:
                return self._transcribe_single(
                    audio_path, language, on_progress, on_chunk_done
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

    def _transcribe_single(self, audio_path, language, on_progress, on_chunk_done):
        """Transcribe entire file (short files, no extraction)."""
        if self.backend == "mlx":
            results = self._mlx_transcribe(audio_path, language, 0, on_progress)
        else:
            results = self._fw_transcribe(audio_path, language, 0, on_progress)

        if on_chunk_done:
            on_chunk_done(results, 1, 1)
        return results

    def _transcribe_range(self, audio_path, start_sec, end_sec, total_duration,
                          language, on_progress, on_chunk_done):
        """Extract time range via ffmpeg, then transcribe."""
        chunk_duration = end_sec - start_sec
        tmp_path = None

        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.close(tmp_fd)

            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-t", str(chunk_duration),
                "-i", audio_path,
                "-ac", "1", "-ar", "16000",
                tmp_path,
            ]
            subprocess.run(cmd, capture_output=True, timeout=60)

            if self.backend == "mlx":
                results = self._mlx_transcribe(tmp_path, language, start_sec, on_progress)
            else:
                results = self._fw_transcribe(tmp_path, language, start_sec, on_progress)

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

    def _mlx_transcribe(self, audio_path, language, time_offset, on_progress):
        """Transcribe using mlx-whisper on Metal GPU."""
        import mlx_whisper

        opts = {
            "path_or_hf_repo": self._mlx_repo,
            "word_timestamps": True,
            "condition_on_previous_text": False,
        }
        if language:
            opts["language"] = language

        result = mlx_whisper.transcribe(audio_path, **opts)

        detected_lang = result.get("language", language or "")
        segments_raw = result.get("segments", [])
        duration = max((s["end"] for s in segments_raw), default=1) if segments_raw else 1

        results = []
        for seg in segments_raw:
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
                "text": seg.get("text", "").strip(),
                "language": detected_lang,
                "words": words,
            })

            if on_progress and duration > 0:
                on_progress(min(seg["end"] / duration, 1.0))

        return results

    # ── faster-whisper backend (CUDA / CPU) ──

    def _fw_transcribe(self, audio_path, language, time_offset, on_progress):
        """Transcribe using faster-whisper (CTranslate2)."""
        segments_iter, info = self.model.transcribe(
            audio_path,
            language=language,
            beam_size=1,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            condition_on_previous_text=False,
        )

        detected_lang = info.language
        duration = info.duration or 1
        results = []

        for seg in segments_iter:
            results.append({
                "start": round(time_offset + seg.start, 3),
                "end": round(time_offset + seg.end, 3),
                "text": seg.text.strip(),
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
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
                capture_output=True, text=True, timeout=10,
            )
            return float(result.stdout.strip())
        except Exception:
            return None
