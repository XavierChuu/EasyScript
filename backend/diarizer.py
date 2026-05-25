"""
Speaker diarization using pyannote-audio.
Identifies who speaks when in an audio file.
"""

import os
import platform
import subprocess


def detect_torch_device():
    """Detect best torch device for pyannote."""
    import torch
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        if torch.backends.mps.is_available():
            return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class Diarizer:
    """Speaker diarization using pyannote/speaker-diarization-3.1"""

    def __init__(self, hf_token=None):
        self.hf_token = hf_token or os.environ.get("HF_TOKEN", "")
        self.pipeline = None
        self.device = detect_torch_device()

    def _ensure_pipeline(self):
        """Lazy-load the diarization pipeline."""
        if self.pipeline is not None:
            return

        if not self.hf_token:
            raise ValueError(
                "HuggingFace token required for pyannote. "
                "Set HF_TOKEN environment variable or configure in settings. "
                "Get your token at https://huggingface.co/settings/tokens "
                "and accept terms at https://huggingface.co/pyannote/speaker-diarization-3.1"
            )

        import torch

        # ── PyTorch 2.6+ weights_only shim ──
        # torch.load default changed to weights_only=True; pyannote checkpoints
        # contain TorchVersion objects that need to be allowlisted.
        try:
            from torch.serialization import add_safe_globals
            from torch.torch_version import TorchVersion
            add_safe_globals([TorchVersion])
        except Exception:
            pass
        _orig_torch_load = torch.load
        def _patched_torch_load(*args, **kwargs):
            kwargs['weights_only'] = False  # force, not setdefault
            return _orig_torch_load(*args, **kwargs)
        torch.load = _patched_torch_load
        # Also patch in lightning_fabric/pytorch_lightning if already imported
        for _mod_name in ('lightning_fabric.utilities.cloud_io',
                          'pytorch_lightning.utilities.cloud_io'):
            try:
                import importlib
                _mod = importlib.import_module(_mod_name)
                if hasattr(_mod, 'pl_load'):
                    _orig_pl = _mod.pl_load
                    def _make_pl(orig):
                        def _patched(*a, **k):
                            k['weights_only'] = False
                            return orig(*a, **k)
                        return _patched
                    _mod.pl_load = _make_pl(_orig_pl)
            except Exception:
                pass

        # ── torchaudio 2.x compatibility shims ──
        # torchaudio 2.x removed the legacy I/O API that pyannote 3.x expects.
        import torchaudio
        if not hasattr(torchaudio, 'AudioMetaData'):
            from collections import namedtuple
            torchaudio.AudioMetaData = namedtuple(
                'AudioMetaData',
                ['sample_rate', 'num_frames', 'num_channels', 'bits_per_sample', 'encoding'],
            )
        if not hasattr(torchaudio, 'list_audio_backends'):
            torchaudio.list_audio_backends = lambda: ['soundfile']
        if not hasattr(torchaudio, 'info'):
            import soundfile as _sf
            def _torchaudio_info(path, backend=None, format=None):
                i = _sf.info(str(path))
                bps = 16
                try:
                    bps = int(i.subtype.split('_')[-1])
                except Exception:
                    pass
                return torchaudio.AudioMetaData(
                    sample_rate=i.samplerate, num_frames=i.frames,
                    num_channels=i.channels, bits_per_sample=bps, encoding=i.subtype,
                )
            torchaudio.info = _torchaudio_info

        # ── huggingface_hub 1.x compatibility shim ──
        # huggingface_hub 1.0+ renamed use_auth_token → token.
        # pyannote 3.x still passes use_auth_token internally.
        import huggingface_hub as _hfhub
        for _fn_name in ('hf_hub_download', 'snapshot_download'):
            _orig = getattr(_hfhub, _fn_name)
            def _make_patched(orig):
                def _patched(*args, **kwargs):
                    if 'use_auth_token' in kwargs:
                        kwargs['token'] = kwargs.pop('use_auth_token')
                    return orig(*args, **kwargs)
                return _patched
            setattr(_hfhub, _fn_name, _make_patched(_orig))

        from pyannote.audio import Pipeline

        self.pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=self.hf_token,
        )

        # Use GPU when available (CUDA or MPS)
        if self.device in ("cuda", "mps"):
            self.pipeline.to(torch.device(self.device))

    def diarize(self, audio_path, on_progress=None):
        """
        Run speaker diarization on audio file.

        Returns: list of { start, end, speaker }
        Example: [
            { "start": 0.5, "end": 5.2, "speaker": "SPEAKER_00" },
            { "start": 5.8, "end": 12.1, "speaker": "SPEAKER_01" },
        ]
        """
        self._ensure_pipeline()

        if on_progress:
            on_progress(0.05)

        # Convert to standard WAV (16kHz mono) to avoid sample count mismatches
        tmp_wav = None
        try:
            import tempfile
            tmp_fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
            os.close(tmp_fd)

            if on_progress:
                on_progress(0.08)

            subprocess.run(
                ["ffmpeg", "-y", "-i", audio_path,
                 "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", tmp_wav],
                capture_output=True, timeout=120,
            )

            if on_progress:
                on_progress(0.12)

            # Use soundfile to bypass torchaudio/torchcodec on Windows
            import soundfile as sf
            import numpy as np
            import torch
            audio_data, sample_rate = sf.read(tmp_wav, dtype="float32")
            if audio_data.ndim == 1:
                waveform = torch.from_numpy(audio_data).unsqueeze(0)
            else:
                waveform = torch.from_numpy(audio_data.T)
            diarization_output = self.pipeline({"waveform": waveform, "sample_rate": sample_rate})

        finally:
            if tmp_wav and os.path.exists(tmp_wav):
                try:
                    os.unlink(tmp_wav)
                except OSError:
                    pass

        if on_progress:
            on_progress(0.9)

        # pyannote v4+ returns DiarizeOutput dataclass with
        # .speaker_diarization (Annotation) field.
        # Older versions return Annotation directly.
        if hasattr(diarization_output, 'speaker_diarization'):
            annotation = diarization_output.speaker_diarization
        elif hasattr(diarization_output, 'itertracks'):
            annotation = diarization_output
        else:
            annotation = diarization_output

        # Extract segments
        results = []
        for turn, _, speaker in annotation.itertracks(yield_label=True):
            results.append({
                "start": round(turn.start, 3),
                "end": round(turn.end, 3),
                "speaker": speaker,
            })

        if on_progress:
            on_progress(1.0)

        return results

    @staticmethod
    def merge_speakers_into_segments(speech_segments, diarize_segments):
        """
        Merge diarization results into whisper speech segments.
        For each speech segment, find the speaker with maximum time overlap.

        Args:
            speech_segments: list of { start, end, text, ... }
            diarize_segments: list of { start, end, speaker }

        Returns:
            Updated speech_segments with 'speaker' and 'speakerLabel' fields added.
            Also returns a speakerMap: { "SPEAKER_00": "Speaker A", ... }
        """
        if not diarize_segments:
            return speech_segments, {}

        # Collect unique speakers in order of appearance
        seen_speakers = []
        for ds in diarize_segments:
            if ds["speaker"] not in seen_speakers:
                seen_speakers.append(ds["speaker"])

        # Create default labels: Speaker A, Speaker B, ...
        speaker_map = {}
        for i, spk in enumerate(seen_speakers):
            label = f"Speaker {chr(65 + i)}" if i < 26 else f"Speaker {i + 1}"
            speaker_map[spk] = label

        # For each speech segment, find best-matching speaker by overlap
        for seg in speech_segments:
            best_speaker = None
            best_overlap = 0

            for ds in diarize_segments:
                # Calculate overlap
                overlap_start = max(seg["start"], ds["start"])
                overlap_end = min(seg["end"], ds["end"])
                overlap = max(0, overlap_end - overlap_start)

                if overlap > best_overlap:
                    best_overlap = overlap
                    best_speaker = ds["speaker"]

            if best_speaker:
                seg["speaker"] = best_speaker
                seg["speakerLabel"] = speaker_map[best_speaker]
            else:
                seg["speaker"] = "UNKNOWN"
                seg["speakerLabel"] = "Unknown"

        return speech_segments, speaker_map

    @staticmethod
    def get_duration(audio_path):
        """Get audio duration using ffprobe."""
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
                capture_output=True, text=True, timeout=10,
            )
            return float(result.stdout.strip())
        except Exception:
            return 0
