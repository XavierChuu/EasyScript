"""
Speaker diarization with pyannote.audio 4 and the community-1 pipeline.

community-1 (CC-BY-4.0) improves on 3.1 (AMI DER 18.8 → 17.0, AliMeeting
24.5 → 20.3), returns an *exclusive* diarization (one speaker at a time —
what transcript attribution needs) and per-speaker embeddings (used by the
voice library). The weights come from the bundled copy when present, else
from pyannote's ungated mirror, so no HuggingFace token is needed.

The speaker-embedding ResNet (≈98 % of the run time) is swapped for an ONNX
export run by ONNX Runtime: DirectML on any Windows GPU (≈16x torch-on-CPU
on an RTX 3060, identical embeddings), its CPU provider otherwise (≈1.8x).
"""

import os
import platform
import sys

from ffmpeg_utils import run_ffmpeg

COMMUNITY_REPO = "pyannote-community/speaker-diarization-community-1"
MODEL_DIRNAME = "speaker-diarization-community-1"
ONNX_NAME = "resnet34.onnx"   # written next to embedding/pytorch_model.bin by tools/export_embedding_onnx.py

# community-1 clustering is VBx: `threshold` seeds the initial agglomerative
# clusters (higher → fewer), Fb penalises extra speakers (higher → fewer; it is
# the knob that matters). On AMI ES2004a (4 speakers): Fb 1.5 / 0.8 → 4,
# 0.45 / 0.25 → 5 (brief extra voices), 0.2 → 6, 0.05 → 17.
SENSITIVITY = {
    "fewer":     {"threshold": 0.7, "Fa": 0.07, "Fb": 1.5},
    "standard":  {"threshold": 0.6, "Fa": 0.07, "Fb": 0.8},   # pipeline defaults
    "sensitive": {"threshold": 0.5, "Fa": 0.07, "Fb": 0.45},
    "max":       {"threshold": 0.45, "Fa": 0.07, "Fb": 0.25},
}
# Segmentation window hop as a fraction of the 10 s window. On AMI ES2004a
# (17.5 min, 4 speakers) DER was 18.8 % at 0.1, 18.7 % at 0.2 / 0.3 and
# 20.3 % at 0.5, with run time roughly ∝ 1/step — so 0.2 is the default.
STEP = {"accurate": 0.1, "balanced": 0.2, "fast": 0.3}
DEFAULT_STEP = "balanced"


def _ort_resnet(session, fallback):
    """A module standing in for WeSpeakerResNet34.resnet (same call, ONNX
    Runtime inside); 3-D weights, which the export doesn't cover, use torch."""
    import numpy as np
    import torch

    class OrtResNet(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.fallback = fallback

        def forward(self, fbank, weights=None):
            if weights is None:
                weights = torch.ones(fbank.shape[0], fbank.shape[1])
            if weights.dim() != 2:
                return self.fallback(fbank, weights=weights)
            out = session.run(None, {
                "fbank": np.ascontiguousarray(fbank.detach().cpu().numpy(), dtype=np.float32),
                "weights": np.ascontiguousarray(weights.detach().cpu().numpy(), dtype=np.float32),
            })[0]
            return None, torch.from_numpy(out).to(fbank.device)

    return OrtResNet()


def _onnx_session(path):
    """(session, label) on the best ONNX Runtime provider, or (None, None)."""
    try:
        import onnxruntime as ort
    except Exception:
        return None, None
    available = ort.get_available_providers()
    for provider, label in (("DmlExecutionProvider", "GPU · DirectML"),
                            ("CPUExecutionProvider", "CPU · ONNX")):
        if provider not in available:
            continue
        try:
            so = ort.SessionOptions()
            so.log_severity_level = 3
            if provider == "DmlExecutionProvider":
                # Required by the DirectML provider.
                so.enable_mem_pattern = False
                so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            return ort.InferenceSession(path, so, providers=[provider]), label
        except Exception as e:
            print(f"[diarizer] {provider} unavailable: {e}")
    return None, None


def detect_torch_device():
    """Best torch device for pyannote (EASYSCRIPT_DIARIZE_DEVICE=cpu|cuda|mps overrides)."""
    import torch
    forced = os.environ.get("EASYSCRIPT_DIARIZE_DEVICE", "").strip().lower()
    if forced in ("cpu", "cuda", "mps"):
        return forced
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        if torch.backends.mps.is_available():
            return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def bundled_model_dir():
    """community-1 shipped inside the backend (PyInstaller) or backend/models."""
    roots = [getattr(sys, "_MEIPASS", None), os.path.dirname(os.path.abspath(__file__))]
    for r in roots:
        if r:
            d = os.path.join(r, "models", MODEL_DIRNAME)
            if os.path.isfile(os.path.join(d, "config.yaml")):
                return d
    return None


def _merge_into_turns(segments):
    """Collapse a non-overlapping timeline into contiguous speaker turns.

    A turn for speaker X runs from X's first word until just before a
    different speaker starts, so silence inside a monologue stays with it and
    the audio is partitioned (turn[i].end == turn[i+1].start).
    """
    if not segments:
        return []
    sorted_segs = sorted(segments, key=lambda s: s["start"])
    turns = []
    current = dict(sorted_segs[0])
    for s in sorted_segs[1:]:
        if s["speaker"] == current["speaker"]:
            current["end"] = max(current["end"], s["end"])
        else:
            current["end"] = s["start"]
            if current["end"] > current["start"]:
                turns.append(current)
            current = dict(s)
    if current["end"] > current["start"]:
        turns.append(current)
    return [{"start": round(t["start"], 3), "end": round(t["end"], 3), "speaker": t["speaker"]}
            for t in turns]


class Diarizer:
    def __init__(self, hf_token=None):
        self.hf_token = hf_token or os.environ.get("HF_TOKEN", "") or None
        self.pipeline = None
        self.device = detect_torch_device()
        self.source = None
        self.engine = None   # what runs the embeddings, for the UI

    def _ensure_pipeline(self):
        if self.pipeline is not None:
            return
        import torch
        from pyannote.audio import Pipeline

        self.source = bundled_model_dir() or COMMUNITY_REPO
        self.pipeline = Pipeline.from_pretrained(self.source, token=self.hf_token)
        if self.pipeline is None:
            raise ValueError(f"Could not load the speaker model ({self.source}). "
                             "Check the internet connection for the first download.")
        self._defaults = self.pipeline.parameters(instantiated=True)
        # The bundle sets OMP_NUM_THREADS=1; segmentation and fbank still run
        # in torch on the CPU and are ~2x slower single-threaded.
        try:
            torch.set_num_threads(max(1, min(8, os.cpu_count() or 4)))
        except Exception:
            pass
        if self.device in ("cuda", "mps"):
            self.pipeline.to(torch.device(self.device))
            self.engine = {"cuda": "GPU · CUDA", "mps": "GPU · Metal"}[self.device]
            return
        self.engine = "CPU"
        onnx_path = os.path.join(self.source, "embedding", ONNX_NAME) if os.path.isdir(self.source) else None
        if onnx_path and os.path.isfile(onnx_path):
            model = getattr(self.pipeline._embedding, "model_", None)
            resnet = getattr(model, "resnet", None)
            session, label = _onnx_session(onnx_path) if resnet is not None else (None, None)
            if session is not None:
                model.resnet = _ort_resnet(session, resnet)
                self.engine = label

    def _configure(self, sensitivity, speed):
        params = {k: dict(v) for k, v in self._defaults.items()}
        params["clustering"].update(SENSITIVITY.get(sensitivity or "standard", SENSITIVITY["standard"]))
        self.pipeline.instantiate(params)
        seg = self.pipeline._segmentation
        seg.step = STEP.get(speed or DEFAULT_STEP, STEP[DEFAULT_STEP]) * seg.duration

    def diarize(self, audio_path, on_progress=None, num_speakers=None, min_speakers=None,
                max_speakers=None, sensitivity=None, speed=None, cancelled=None):
        """Diarize `audio_path`.

        Returns {"turns": contiguous turns (timeline / clip tagging),
                 "exclusive": exclusive segments (word attribution),
                 "embeddings": {speaker: [floats]}}
        """
        self._ensure_pipeline()
        self._configure(sensitivity, speed)
        if on_progress:
            on_progress(0.02)

        import numpy as np
        import soundfile as sf
        import torch

        tmp_wav = None
        try:
            source_path = audio_path
            try:
                info = sf.info(audio_path)
                compatible = info.samplerate == 16000 and info.channels == 1
            except Exception:
                compatible = False
            if not compatible:
                import tempfile
                fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                run_ffmpeg(["-y", "-i", audio_path, "-ac", "1", "-ar", "16000",
                            "-acodec", "pcm_s16le", tmp_wav], capture_output=True, timeout=600)
                source_path = tmp_wav
            audio, sr = sf.read(source_path, dtype="float32")
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            waveform = torch.from_numpy(np.ascontiguousarray(audio)).unsqueeze(0)
        finally:
            if tmp_wav and os.path.exists(tmp_wav):
                try:
                    os.unlink(tmp_wav)
                except OSError:
                    pass
        if on_progress:
            on_progress(0.05)

        # Progress from pyannote's step hook: segmentation ≈ 15 %, embeddings
        # (the slow part) ≈ 80 %, clustering the rest.
        spans = {"segmentation": (0.05, 0.2), "embeddings": (0.2, 0.95)}

        def hook(step_name, step_artefact, file=None, total=None, completed=None):
            if cancelled and cancelled():
                raise InterruptedError("cancelled")
            if on_progress and step_name in spans and total:
                a, b = spans[step_name]
                on_progress(a + (b - a) * min(1.0, (completed or 0) / total))

        kwargs = {}
        if num_speakers and num_speakers > 0:
            kwargs["num_speakers"] = int(num_speakers)
        else:
            if min_speakers and min_speakers > 0:
                kwargs["min_speakers"] = int(min_speakers)
            if max_speakers and max_speakers > 0:
                kwargs["max_speakers"] = int(max_speakers)

        out = self.pipeline({"waveform": waveform, "sample_rate": sr}, hook=hook, **kwargs)

        exclusive_ann = getattr(out, "exclusive_speaker_diarization", None) or out.speaker_diarization
        exclusive = [{"start": round(t.start, 3), "end": round(t.end, 3), "speaker": spk}
                     for t, _, spk in exclusive_ann.itertracks(yield_label=True)]
        exclusive.sort(key=lambda s: s["start"])

        embeddings = {}
        emb = getattr(out, "speaker_embeddings", None)
        if emb is not None:
            labels = out.speaker_diarization.labels()
            emb = np.asarray(emb)
            for i, spk in enumerate(labels):
                if i < len(emb) and np.all(np.isfinite(emb[i])):
                    embeddings[spk] = [round(float(x), 6) for x in emb[i]]

        if on_progress:
            on_progress(1.0)
        return {"turns": _merge_into_turns(exclusive), "exclusive": exclusive, "embeddings": embeddings}

    @staticmethod
    def get_duration(audio_path):
        try:
            from ffmpeg_utils import get_audio_duration
            return get_audio_duration(audio_path)
        except Exception:
            return 0
