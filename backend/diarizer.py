"""
Speaker diarization using pyannote-audio.
Identifies who speaks when in an audio file.
"""

import os
import platform

from ffmpeg_utils import run_ffmpeg


def detect_torch_device():
    """Detect best torch device for pyannote."""
    import torch
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        if torch.backends.mps.is_available():
            return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _merge_into_turns(segments):
    """Collapse a resolved (non-overlapping) timeline into speaker turns.

    A "turn" for speaker X starts the moment X first speaks and runs until
    just before a *different* speaker speaks. Short silence inside a turn
    stays with that speaker, so we don't fragment one person's continuous
    monologue into 5–10 micro-clips.

    Editorially this gives the right cut policy: cuts land only on actual
    speaker transitions, not every breath. And transcript labelling becomes
    a trivial point-in-turn lookup.

    Input: list of resolved (non-overlapping) segments, sorted-or-not.
    Output: list of turns, contiguous by definition (turn[i].end ==
    turn[i+1].start when speakers differ; the audio is partitioned).
    """
    if not segments:
        return []

    sorted_segs = sorted(segments, key=lambda s: s["start"])

    turns = []
    current = {
        "start": sorted_segs[0]["start"],
        "end": sorted_segs[0]["end"],
        "speaker": sorted_segs[0]["speaker"],
    }

    for s in sorted_segs[1:]:
        if s["speaker"] == current["speaker"]:
            # Same speaker — absorb (including any silence gap before it).
            if s["end"] > current["end"]:
                current["end"] = s["end"]
        else:
            # Different speaker → close current turn at the new speaker's
            # start, so the timeline has no gaps.
            current["end"] = s["start"]
            if current["end"] > current["start"]:
                turns.append(current)
            current = {
                "start": s["start"],
                "end": s["end"],
                "speaker": s["speaker"],
            }

    if current["end"] > current["start"]:
        turns.append(current)

    # Round + return
    return [{
        "start": round(t["start"], 3),
        "end": round(t["end"], 3),
        "speaker": t["speaker"],
    } for t in turns]


def _resolve_overlaps(segments):
    """Collapse overlapping speaker segments into a non-overlapping timeline.

    pyannote 3.x emits per-speaker segments. When two speakers talk at the
    same time, the windows overlap → if we pass that straight to the
    Premiere cut logic, razor cuts land in the middle of speech and clips
    end up multi-speaker.

    Algorithm: sweep through all start/end boundaries; in each gap, look at
    the set of active speakers and pick the one with the longest total
    presence in that gap. Then merge adjacent regions that share the same
    chosen speaker so we don't fragment a continuous turn.

    The "dominant speaker" heuristic favours whoever is speaking longer in
    each contested region, which matches what a human editor would mark as
    that clip's owner.
    """
    if not segments:
        return []

    # Collect every boundary as a candidate cut point
    boundaries = set()
    for s in segments:
        boundaries.add(round(s["start"], 3))
        boundaries.add(round(s["end"], 3))
    times = sorted(boundaries)

    resolved = []  # (start, end, speaker)
    for i in range(len(times) - 1):
        t0, t1 = times[i], times[i + 1]
        if t1 - t0 < 1e-6:
            continue
        # Speakers active in this slice
        active = [s for s in segments if s["start"] < t1 and s["end"] > t0]
        if not active:
            continue
        if len(active) == 1:
            speaker = active[0]["speaker"]
        else:
            # Pick the speaker most "present" in this slice. Ties go to the
            # LATER start — when speaker B briefly interjects while A talks
            # continuously, A's coverage equals B's in that slice but B is
            # the one the user wants to see marked (the interrupter), not
            # absorbed by A's ongoing turn.
            def coverage(s):
                lo = max(s["start"], t0)
                hi = min(s["end"], t1)
                return (hi - lo, s["start"])
            speaker = max(active, key=coverage)["speaker"]
        # Merge with previous if same speaker and contiguous
        if resolved and resolved[-1]["speaker"] == speaker \
                and abs(resolved[-1]["end"] - t0) < 1e-3:
            resolved[-1]["end"] = round(t1, 3)
        else:
            resolved.append({
                "start": round(t0, 3),
                "end": round(t1, 3),
                "speaker": speaker,
            })

    return resolved


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

        # pyannote 3.x uses `use_auth_token`, 4.x renamed it to `token`.
        # Try the new API first, fall back to old.
        try:
            self.pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                token=self.hf_token,
            )
        except TypeError:
            self.pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=self.hf_token,
            )

        # pyannote returns None (instead of raising) when the token can't access
        # the gated model — usually because the user hasn't accepted the model's
        # license terms on HuggingFace. Surface a clear, actionable error rather
        # than letting it blow up later as "'NoneType' object is not callable".
        if self.pipeline is None:
            raise ValueError(
                "Cannot access the speaker diarization model. Your HuggingFace "
                "token is valid, but you must accept the user conditions for "
                "BOTH models (one click each, while logged in):\n"
                "  1) https://hf.co/pyannote/speaker-diarization-3.1\n"
                "  2) https://hf.co/pyannote/segmentation-3.0\n"
                "Then click Speakers again. Also make sure the token has 'Read' "
                "access to public gated repos."
            )

        # Use GPU when available (CUDA or MPS)
        if self.device in ("cuda", "mps"):
            self.pipeline.to(torch.device(self.device))

    def _apply_sensitivity(self, sensitivity):
        """Tune pyannote 3.1 clustering for speaker count.

        pyannote defaults (min_cluster_size=12, threshold≈0.7045) are tuned
        for mixed conversational content and tend to over-cluster on movies,
        scripted speech, or audio with strong music/SFX — producing more
        "speakers" than actually exist. The levels here shift the baseline
        toward FEWER clusters by default (raising min_cluster_size + the
        cluster-distance threshold), with explicit knobs for catching brief
        speakers when the user knows they exist.

        Levels (default selection is "standard" = conservative):
          "standard"  — min_cluster_size=20, threshold=0.78
                        Merges similar voices more aggressively. Fewer false
                        speakers, may miss brief interjectors.
          "sensitive" — pyannote stock defaults (12, 0.7045). Use this when
                        you expect a few brief lines from secondary speakers.
          "max"       — min_cluster_size=4, threshold=0.68. Aggressively
                        keeps small clusters; risks splitting one person
                        across two labels but catches very brief speakers.
        """
        if not sensitivity:
            sensitivity = "standard"

        if sensitivity == "standard":
            cfg = {
                "clustering": {
                    "method": "centroid",
                    "min_cluster_size": 20,
                    "threshold": 0.78,
                },
            }
        elif sensitivity == "sensitive":
            cfg = {
                "clustering": {
                    "method": "centroid",
                    "min_cluster_size": 12,
                    "threshold": 0.7045654963945799,
                },
            }
        elif sensitivity == "max":
            cfg = {
                "clustering": {
                    "method": "centroid",
                    "min_cluster_size": 4,
                    "threshold": 0.68,
                },
            }
        else:
            return  # unknown level → leave defaults alone

        try:
            self.pipeline.instantiate(cfg)
        except Exception as e:
            # Some pyannote versions reject partial configs — best-effort.
            print(f"[diarizer] sensitivity tuning skipped: {e}")

    def diarize(self, audio_path, on_progress=None,
                num_speakers=None, min_speakers=None, max_speakers=None,
                sensitivity=None):
        """
        Run speaker diarization on audio file.

        Args:
            audio_path: path to audio file
            on_progress: progress callback(0..1)
            num_speakers: exact speaker count (skips clustering search → faster)
            min_speakers: lower bound when num_speakers unknown
            max_speakers: upper bound when num_speakers unknown

        Returns: list of { start, end, speaker } with overlaps resolved
                 so that no two segments overlap in time.
        """
        self._ensure_pipeline()
        # Apply optional sensitivity tuning. This is cheap (just reconfigures
        # thresholds), so we do it every call rather than caching.
        self._apply_sensitivity(sensitivity)

        if on_progress:
            on_progress(0.05)

        # Audio prep: pyannote needs a torch waveform + sample_rate. Most of
        # the time the upload pipeline has already produced a 16kHz mono PCM
        # WAV (videos are extracted at upload; /peaks reads in the same
        # format). If the input file already matches, we read it directly
        # with soundfile and skip the ~3-10s ffmpeg conversion entirely.
        import soundfile as sf
        import torch

        tmp_wav = None
        try:
            source_path = audio_path
            already_compatible = False
            try:
                info = sf.info(audio_path)
                if info.samplerate == 16000 and info.channels == 1:
                    already_compatible = True
            except Exception:
                already_compatible = False

            if not already_compatible:
                # Convert to standard WAV (16kHz mono PCM) — pyannote 3.x
                # works with any sample rate but resampling internally is
                # slower than letting ffmpeg do it once.
                import tempfile
                tmp_fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
                os.close(tmp_fd)
                if on_progress:
                    on_progress(0.08)
                run_ffmpeg(
                    ["-y", "-i", audio_path,
                     "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", tmp_wav],
                    capture_output=True, timeout=120,
                )
                source_path = tmp_wav

            if on_progress:
                on_progress(0.12)

            audio_data, sample_rate = sf.read(source_path, dtype="float32")
            if audio_data.ndim == 1:
                waveform = torch.from_numpy(audio_data).unsqueeze(0)
            else:
                waveform = torch.from_numpy(audio_data.T)
            # Move waveform to device so pyannote doesn't have to copy.
            if self.device in ("cuda", "mps"):
                waveform = waveform.to(self.device)

            # Build pipeline kwargs — speaker-count hints help pyannote skip
            # the cluster-size search step and run noticeably faster.
            pipe_kwargs = {}
            if num_speakers and num_speakers > 0:
                pipe_kwargs["num_speakers"] = int(num_speakers)
            else:
                if min_speakers and min_speakers > 0:
                    pipe_kwargs["min_speakers"] = int(min_speakers)
                if max_speakers and max_speakers > 0:
                    pipe_kwargs["max_speakers"] = int(max_speakers)

            # NOTE: previously this ran under torch.amp.autocast(fp16) for ~2×
            # speedup, but it degraded speaker-embedding precision enough that
            # the clusterer fell into a single-speaker solution on multi-talker
            # audio. fp32 is correct; we keep the speed wins from num_speakers
            # hints, GPU-resident waveform, and skipping redundant ffmpeg.
            diarization_output = self.pipeline(
                {"waveform": waveform, "sample_rate": sample_rate},
                **pipe_kwargs,
            )

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

        # Extract raw (possibly overlapping) segments
        raw_segments = []
        for turn, _, speaker in annotation.itertracks(yield_label=True):
            raw_segments.append({
                "start": round(turn.start, 3),
                "end": round(turn.end, 3),
                "speaker": speaker,
            })

        # Step 1: resolve overlaps → non-overlapping fine-grained timeline.
        # pyannote 3.x can emit two speakers active in the same window when
        # they actually overlap (cross-talk).
        resolved = _resolve_overlaps(raw_segments)

        # Step 2: collapse adjacent same-speaker segments (including silence
        # between them) into a single turn. Each turn ends precisely when the
        # next *different* speaker begins. This is the timeline the rest of
        # the app uses: one entry per speaker turn, cuts only at transitions.
        results = _merge_into_turns(resolved)

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
        """Get audio duration (uses bundled ffmpeg, no ffprobe needed)."""
        try:
            from ffmpeg_utils import get_audio_duration
            return get_audio_duration(audio_path)
        except Exception:
            return 0
