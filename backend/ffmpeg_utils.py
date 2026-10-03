"""
FFmpeg + subprocess utilities — resolves a usable ffmpeg.exe regardless of
whether the app runs in dev (uses system PATH) or as a PyInstaller bundle
(uses imageio-ffmpeg's bundled binary), and provides a no-console subprocess
wrapper so spawned children don't flash console windows on Windows.
"""

import os
import re
import shutil
import subprocess
import sys


_FFMPEG_PATH = None


# ── Windows: hide console windows when spawning child processes ──
if sys.platform == "win32":
    _CREATE_NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW (py3.7+)
    _STARTUPINFO = subprocess.STARTUPINFO()
    _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _STARTUPINFO.wShowWindow = 0  # SW_HIDE
else:
    _CREATE_NO_WINDOW = 0
    _STARTUPINFO = None


def _silent_kwargs(kwargs):
    """Inject Windows no-console flags into subprocess kwargs (idempotent)."""
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", 0)
        kwargs["creationflags"] |= _CREATE_NO_WINDOW
        kwargs.setdefault("startupinfo", _STARTUPINFO)
    return kwargs


def run_silent(cmd, **kwargs):
    """subprocess.run wrapper that suppresses Windows console flashes."""
    return subprocess.run(cmd, **_silent_kwargs(kwargs))


def popen_silent(cmd, **kwargs):
    """subprocess.Popen wrapper that suppresses Windows console flashes."""
    return subprocess.Popen(cmd, **_silent_kwargs(kwargs))


def get_ffmpeg_exe():
    """Return path to a usable ffmpeg executable.

    Order of preference:
    1. imageio-ffmpeg bundled binary (works in PyInstaller bundle and dev)
    2. System ffmpeg on PATH (dev convenience)
    """
    global _FFMPEG_PATH
    if _FFMPEG_PATH:
        return _FFMPEG_PATH

    try:
        import imageio_ffmpeg
        _FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
        return _FFMPEG_PATH
    except Exception:
        pass

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        _FFMPEG_PATH = system_ffmpeg
        return _FFMPEG_PATH

    # Last resort: assume "ffmpeg" works (will fail loudly if not)
    return "ffmpeg"


def setup_ffmpeg_path():
    """Prepend ffmpeg's directory to PATH so subprocess('ffmpeg', ...) works."""
    exe = get_ffmpeg_exe()
    folder = os.path.dirname(exe)
    if folder and folder not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")


def run_ffmpeg(args, **kwargs):
    """Run the bundled (or system) ffmpeg with the given args list.

    `args` is the argument list *after* the executable name, e.g.
        run_ffmpeg(["-i", path, "-f", "null", "-"], capture_output=True, timeout=30)
    """
    return run_silent([get_ffmpeg_exe(), *args], **kwargs)


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def get_audio_duration(audio_path):
    """Get audio duration in seconds.

    WAV/FLAC/OGG: read from the header (exact, instant). Otherwise ffmpeg's
    header parse — without an output, so it doesn't decode the whole file just
    to print "Duration:" (that took seconds for every call on hour-long media).
    Falls back to a full decode only when the header can't be trusted.
    """
    try:
        import soundfile as sf
        info = sf.info(str(audio_path))
        if info.samplerate > 0 and info.frames > 0:
            return info.frames / float(info.samplerate)
    except Exception:
        pass
    try:
        result = run_ffmpeg(["-hide_banner", "-i", str(audio_path)],
                            capture_output=True, text=True, timeout=30)
        stderr = result.stderr or ""
        m = _DURATION_RE.search(stderr)
        if m and "Estimating duration from bitrate" not in stderr:
            h, mn, s = m.groups()
            return int(h) * 3600 + int(mn) * 60 + float(s)
        # No usable header (or a bitrate estimate): decode and take the last
        # progress timestamp, which is the real decoded length.
        result = run_ffmpeg(["-i", str(audio_path), "-vn", "-f", "null", "-"],
                            capture_output=True, text=True, timeout=600)
        times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
        if times:
            h, mn, s = times[-1]
            return int(h) * 3600 + int(mn) * 60 + float(s)
        if m:
            h, mn, s = m.groups()
            return int(h) * 3600 + int(mn) * 60 + float(s)
        return 0.0
    except Exception:
        return 0.0
