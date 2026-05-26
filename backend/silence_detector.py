import re
import math

from ffmpeg_utils import run_ffmpeg


class SilenceDetector:
    CHUNK_DURATION = 600  # 10 minutes per chunk

    @staticmethod
    def detect(audio_path, min_silence_ms=500, silence_thresh_db=-40, on_progress=None):
        """
        Detect silence and breath segments using ffmpeg silencedetect filter.
        For files > 10 minutes, splits into chunks to avoid timeout/memory issues.
        """
        total_duration = SilenceDetector._get_duration(audio_path) or 0

        if total_duration <= 0:
            if on_progress:
                on_progress(1.0)
            return []

        # Short files: process in one go
        if total_duration <= SilenceDetector.CHUNK_DURATION:
            if on_progress:
                on_progress(0.1)
            segments = SilenceDetector._detect_chunk(
                audio_path, 0, total_duration,
                min_silence_ms, silence_thresh_db
            )
            if on_progress:
                on_progress(1.0)
            return segments

        # Long files: split into 10-min chunks with 2s overlap
        chunk_dur = SilenceDetector.CHUNK_DURATION
        overlap = 2.0
        num_chunks = math.ceil(total_duration / chunk_dur)
        all_segments = []

        for i in range(num_chunks):
            chunk_start = i * chunk_dur
            # Add overlap to catch silence at boundaries
            chunk_end = min(chunk_start + chunk_dur + overlap, total_duration)

            chunk_segments = SilenceDetector._detect_chunk(
                audio_path, chunk_start, chunk_end - chunk_start,
                min_silence_ms, silence_thresh_db
            )

            # Merge with previous segments, avoiding duplicates from overlap
            for seg in chunk_segments:
                if all_segments and seg["start"] < all_segments[-1]["end"] + 0.1:
                    # Extend previous segment if overlapping
                    all_segments[-1]["end"] = max(all_segments[-1]["end"], seg["end"])
                else:
                    all_segments.append(seg)

            if on_progress:
                on_progress((i + 1) / num_chunks)

        return all_segments

    @staticmethod
    def _detect_chunk(audio_path, start_sec, duration_sec, min_silence_ms, silence_thresh_db):
        """Run ffmpeg silencedetect on a specific chunk of the audio."""
        min_silence_sec = min_silence_ms / 1000.0

        args = [
            "-ss", str(start_sec),
            "-t", str(duration_sec),
            "-i", audio_path,
            "-af", f"silencedetect=noise={silence_thresh_db}dB:d={min_silence_sec}",
            "-f", "null", "-",
        ]

        try:
            result = run_ffmpeg(args, capture_output=True, text=True, timeout=180)
            stderr = result.stderr
        except Exception:
            return []

        # Parse silence_start / silence_end from stderr
        starts = re.findall(r"silence_start:\s*([\d.]+)", stderr)
        ends = re.findall(r"silence_end:\s*([\d.]+)", stderr)

        segments = []
        for i, start_str in enumerate(starts):
            # Timestamps from ffmpeg are relative to chunk start,
            # so add start_sec to get absolute position
            local_start = float(start_str)
            abs_start = start_sec + local_start

            if i < len(ends):
                local_end = float(ends[i])
                abs_end = start_sec + local_end
            else:
                abs_end = start_sec + duration_sec

            duration_ms = (abs_end - abs_start) * 1000
            seg_type = "breath" if duration_ms < 400 else "silence"

            segments.append({
                "start": round(abs_start, 3),
                "end": round(abs_end, 3),
                "type": seg_type,
            })

        return segments

    @staticmethod
    def _get_duration(audio_path):
        """Get audio duration (uses bundled ffmpeg, no ffprobe needed)."""
        try:
            from ffmpeg_utils import get_audio_duration
            d = get_audio_duration(audio_path)
            return d if d > 0 else None
        except Exception:
            return None
