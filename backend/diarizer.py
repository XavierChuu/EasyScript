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
        from pyannote.audio import Pipeline

        self.pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=self.hf_token,
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

            # Run diarization on the clean WAV
            diarization_output = self.pipeline(tmp_wav)

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
