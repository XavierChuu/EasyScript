"""
EasyScript headless backend (for the Premiere CEP panel).
Runs the FastAPI API on 127.0.0.1:9876 — no window, no frontend serving.
Bundled as `EasyScript-backend` and launched automatically by the panel.
"""
import multiprocessing
import os
import sys


# In windowed bundles, stdout/stderr are None → satisfy the write/flush/isatty
# protocol so uvicorn and libs that print don't crash.
class _NullStream:
    def write(self, *a, **k): return 0
    def flush(self): pass
    def isatty(self): return False
    def fileno(self): raise OSError("no fileno")


# Redirect ALL stdout/stderr (Python AND native libs like mlx/whisper/ffmpeg)
# to a log file. The panel launches us with a stdout pipe it never reads; once
# transcription prints progress, that pipe fills and writes fail with
# [Errno 32] Broken pipe. Pointing fds 1/2 at a real file avoids that entirely.
try:
    _outlog = os.path.join(os.path.expanduser("~"), ".easyscript", "backend_out.log")
    os.makedirs(os.path.dirname(_outlog), exist_ok=True)
    _fd = os.open(_outlog, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(_fd, 1)
    os.dup2(_fd, 2)
    if _fd > 2:
        os.close(_fd)
    sys.stdout = os.fdopen(1, "w", buffering=1)
    sys.stderr = os.fdopen(2, "w", buffering=1)
except Exception:
    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()

# Must run before anything else in a PyInstaller bundle (avoids fork bombs).
multiprocessing.freeze_support()
try:
    multiprocessing.set_start_method("spawn", force=True)
except RuntimeError:
    pass


def _log(msg):
    try:
        log_path = os.path.join(os.path.expanduser("~"), ".easyscript", "backend.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as f:
            f.write(str(msg) + "\n")
    except Exception:
        pass


def main():
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    port = int(os.environ.get("PORT", "9876"))

    if getattr(sys, "_MEIPASS", None):
        sys.path.insert(0, sys._MEIPASS)

    # ffmpeg (bundled via imageio-ffmpeg / backend/bin)
    try:
        from ffmpeg_utils import setup_ffmpeg_path, get_ffmpeg_exe
        setup_ffmpeg_path()
        _log(f"[backend] ffmpeg: {get_ffmpeg_exe()}")
    except Exception as e:
        _log(f"[backend] ffmpeg setup failed: {e}")

    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/opt/local/bin",
             os.path.expanduser("~/bin")]
    cur = os.environ.get("PATH", "")
    for p in extra:
        if os.path.isdir(p) and p not in cur:
            cur = p + os.pathsep + cur
    os.environ["PATH"] = cur

    _log(f"[backend] starting on 127.0.0.1:{port}")
    try:
        import uvicorn
        from server import app
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except Exception as e:
        import traceback
        _log("[backend] CRASH:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
