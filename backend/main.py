"""
EasyScript — Standalone Desktop App
Launches FastAPI backend + pywebview window.
"""
import multiprocessing
import os
import sys

# CRITICAL: Must be called before anything else in a PyInstaller bundle.
# Without this, every multiprocessing subprocess re-executes main() → fork bomb.
multiprocessing.freeze_support()

# Prevent PyTorch/etc from spawning via "fork" (unsafe on macOS)
try:
    multiprocessing.set_start_method("spawn", force=True)
except RuntimeError:
    pass


def resource_path(relative_path):
    """Get path to resource, works for dev and PyInstaller bundle."""
    if getattr(sys, '_MEIPASS', None):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


def get_free_port():
    """Find a free port to avoid conflicts."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port, frontend_dir):
    """Start FastAPI server with frontend served at root."""
    log_path = os.path.join(os.path.expanduser("~"), ".easyscript", "server.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    def _log(msg):
        with open(log_path, "a") as f:
            f.write(f"{msg}\n")

    try:
        _log(f"[1] Starting server on port {port}, frontend: {frontend_dir}")
        import uvicorn
        _log("[2] uvicorn imported")
        from server import app
        _log("[3] server.app imported")
        from starlette.responses import HTMLResponse, Response
        _log("[4] starlette imported")

        # Read and inject BACKEND_URL into index.html
        index_path = os.path.join(frontend_dir, "index.html")
        with open(index_path, "r", encoding="utf-8") as f:
            index_html = f.read()
        inject = f'<script>window.BACKEND_URL = "http://127.0.0.1:{port}";</script>'
        index_html = index_html.replace("</head>", f"  {inject}\n</head>")

        @app.get("/", response_class=HTMLResponse)
        async def serve_index():
            return index_html

        @app.get("/plugin/index.html", response_class=HTMLResponse)
        async def serve_plugin_index():
            return index_html

        @app.get("/styles.css")
        @app.get("/plugin/styles.css")
        async def serve_css():
            css_path = os.path.join(frontend_dir, "styles.css")
            with open(css_path, "r", encoding="utf-8") as f:
                return Response(content=f.read(), media_type="text/css")

        @app.get("/index.js")
        @app.get("/plugin/index.js")
        async def serve_js():
            js_path = os.path.join(frontend_dir, "index.js")
            with open(js_path, "r", encoding="utf-8") as f:
                return Response(content=f.read(), media_type="application/javascript")

        _log(f"[5] Starting uvicorn on 127.0.0.1:{port}")
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except Exception as e:
        # Write crash log for debugging
        import traceback
        crash_log = os.path.join(os.path.expanduser("~"), ".easyscript", "crash.log")
        os.makedirs(os.path.dirname(crash_log), exist_ok=True)
        with open(crash_log, "w") as f:
            f.write(f"Server failed to start:\n{traceback.format_exc()}\n")
        raise


def main():
    import threading
    import time

    _main_log = os.path.join(os.path.expanduser("~"), ".easyscript", "main.log")
    os.makedirs(os.path.dirname(_main_log), exist_ok=True)
    def mlog(msg):
        with open(_main_log, "a") as f:
            f.write(f"{msg}\n")

    mlog("[main] Starting...")

    port = get_free_port()
    os.environ["PORT"] = str(port)
    mlog(f"[main] Port: {port}")

    # Disable PyTorch multiprocessing workers
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    # Ensure ffmpeg/ffprobe are findable — PyInstaller bundles strip PATH
    extra_paths = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/opt/local/bin",
        os.path.expanduser("~/bin"),
    ]
    current_path = os.environ.get("PATH", "")
    for p in extra_paths:
        if os.path.isdir(p) and p not in current_path:
            current_path = p + ":" + current_path
    os.environ["PATH"] = current_path

    # Log ffmpeg status for debugging
    import shutil
    ffmpeg_loc = shutil.which("ffmpeg")
    mlog(f"[main] ffmpeg: {ffmpeg_loc or 'NOT FOUND'}")
    mlog(f"[main] PATH: {os.environ['PATH']}")

    # Determine frontend source path
    frontend_dir = resource_path(os.path.join("..", "plugin"))
    if not os.path.isdir(frontend_dir):
        frontend_dir = resource_path("plugin")

    if not os.path.isdir(frontend_dir):
        print(f"ERROR: Frontend not found. Searched: {frontend_dir}")
        sys.exit(1)

    mlog(f"[main] Frontend dir: {frontend_dir}")

    # Start backend + frontend server in a THREAD
    server_thread = threading.Thread(
        target=start_server, args=(port, frontend_dir), daemon=True
    )
    server_thread.start()
    mlog("[main] Server thread started")

    # Wait for server to be ready
    import urllib.request
    server_ready = False
    for i in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            server_ready = True
            break
        except Exception:
            time.sleep(0.1)
    mlog(f"[main] Server ready: {server_ready}")

    # Launch pywebview window with JS API
    import webview
    import webbrowser

    class Api:
        """JS-callable API exposed as window.pywebview.api"""
        def open_browser(self, url=None):
            """Open EasyScript in the user's default browser for System Audio support."""
            target = url or f"http://127.0.0.1:{port}/plugin/index.html"
            webbrowser.open(target)

    api = Api()

    window = webview.create_window(
        title="EasyScript",
        url=f"http://127.0.0.1:{port}/",
        width=520,
        height=900,
        min_size=(400, 600),
        text_select=True,
        js_api=api,
    )

    webview.start(debug=("--debug" in sys.argv))


if __name__ == "__main__":
    main()
