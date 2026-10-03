# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the EasyScript backend used by the Premiere panel.
Bundles: headless FastAPI backend (backend_main.py) + ML libs + ffmpeg.
"""
import os
import sys
import platform
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

block_cipher = None

# ── Filesystem-based submodule discovery ──
# PyInstaller 6.x runs collect_submodules() in an isolated subprocess, so any
# torchaudio shim we apply here doesn't propagate — which means importing
# pyannote.audio fails (it touches torchaudio.AudioMetaData, removed in
# torchaudio 2.x) and PyInstaller silently bundles an empty package shell.
# We sidestep the import entirely by enumerating .py files on disk.
def _fs_submodules(pkg_name):
    import importlib.util
    try:
        spec = importlib.util.find_spec(pkg_name)
    except Exception:
        return []
    if spec is None or not spec.submodule_search_locations:
        return []
    out = [pkg_name]
    for base in spec.submodule_search_locations:
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            rel = os.path.relpath(root, base)
            prefix = pkg_name if rel == "." else pkg_name + "." + rel.replace(os.sep, ".")
            if rel != "." and "__init__.py" in files:
                out.append(prefix)
            for f in files:
                if f.endswith(".py") and f != "__init__.py":
                    out.append(prefix + "." + f[:-3])
    return sorted(set(out))

# Collect ML library data files
faster_whisper_data = collect_data_files("faster_whisper")
ctranslate2_data = collect_data_files("ctranslate2")
scipy_data = collect_data_files("scipy")

# Diarization data files (pyannote / speechbrain / torch / torchcodec /
# lightning_fabric — lightning reads version.info from its package dir).
pyannote_data = []
speechbrain_data = []
torchcodec_data = []
torchcodec_binaries = []
lightning_data = []
try:
    pyannote_data += collect_data_files("pyannote")
    # include_py_files: bundle the actual .py source of these packages so the
    # runtime can import them. PyInstaller's static analyzer otherwise skips
    # them (their top-level imports fail due to torchaudio/huggingface_hub
    # shims needing to run first — see diarizer.py:_ensure_pipeline).
    pyannote_data += collect_data_files("pyannote.audio", include_py_files=True)
    pyannote_data += collect_data_files("pyannote.core", include_py_files=True)
    pyannote_data += collect_data_files("pyannote.database", include_py_files=True)
    pyannote_data += collect_data_files("pyannote.metrics", include_py_files=True)
    pyannote_data += collect_data_files("pyannote.pipeline", include_py_files=True)
    speechbrain_data += collect_data_files("speechbrain", include_py_files=True)
except Exception:
    pass
try:
    lightning_data += collect_data_files("lightning_fabric")
    lightning_data += collect_data_files("pytorch_lightning")
except Exception:
    pass
# torchcodec is NOT bundled: its native library must match the torch build
# exactly and needs FFmpeg DLLs; a mismatch makes Windows block LoadLibrary
# behind an invisible error dialog (the windowed backend hangs). pyannote falls
# back cleanly without it, and audio always reaches pyannote / Demucs as
# in-memory tensors.
from PyInstaller.utils.hooks import collect_dynamic_libs

# Bundled ffmpeg via imageio-ffmpeg (no system FFmpeg install needed)
ffmpeg_data = []
try:
    ffmpeg_data += collect_data_files("imageio_ffmpeg")
except Exception:
    pass

# Demucs (song-mode vocal isolation) — bundle data + submodules so the
# multiprocessing.Process spawn in _separate_vocals can locate the model
demucs_data = []
demucs_submodules = []
try:
    demucs_data += collect_data_files("demucs")
    demucs_submodules += collect_submodules("demucs")
except Exception:
    pass

# Soundfile native libs (used as torchaudio fallback)
soundfile_data = []
try:
    soundfile_data += collect_data_files("soundfile")
    from PyInstaller.utils.hooks import collect_dynamic_libs as _cdl
    soundfile_data += _cdl("soundfile")
except Exception:
    pass

# Speaker model (community-1, CC-BY-4.0), fetched into backend/models by the
# build scripts so diarization works offline and without a HuggingFace token.
speaker_model_data = []
_spk_dir = os.path.join(os.path.dirname(os.path.abspath(SPEC)), "models", "speaker-diarization-community-1")
if os.path.isfile(os.path.join(_spk_dir, "config.yaml")):
    for _root, _dirs, _files in os.walk(_spk_dir):
        if ".cache" in _root:
            continue
        for _f in _files:
            _rel = os.path.relpath(_root, os.path.dirname(os.path.abspath(SPEC)))
            speaker_model_data.append((os.path.join(_root, _f), _rel))
else:
    print("WARNING: backend/models/speaker-diarization-community-1 missing — "
          "diarization will download it on first use")

# opentelemetry (pyannote 4): namespace packages resolved at runtime
otel_data = []
try:
    otel_data += collect_data_files("opentelemetry", include_py_files=True)
except Exception:
    pass

# Transformers (Hy-MT2 / HunYuan translator) — submodules + data files
transformers_data = []
try:
    transformers_data += collect_data_files("transformers")
except Exception:
    pass

# Package metadata (.dist-info) — required by importlib.metadata.version() calls
# inside transformers/audio_utils.py, etc. PyInstaller doesn't include
# .dist-info folders by default, causing StopIteration at runtime.
package_metadata = []
for _pkg in (
    "torchaudio", "torch", "transformers", "tokenizers",
    "huggingface_hub", "soundfile", "demucs", "pyannote.audio",
    "pyannote.core", "pyannote.pipeline", "pyannote.database", "pyannote.metrics",
    "pyannoteai-sdk", "scipy", "numpy", "faster_whisper", "ctranslate2",
    # pyannote 4 imports opentelemetry, which finds its context implementation
    # through entry points — those live in the .dist-info metadata.
    "opentelemetry-api", "opentelemetry-sdk",
):
    try:
        package_metadata += copy_metadata(_pkg)
    except Exception:
        pass

# NVIDIA CUDA runtime DLLs (Windows): faster-whisper/CTranslate2 needs
# cublas64_12.dll, cudnn64_9.dll, etc. when device="cuda". We bundle them so
# the app can use the GPU; the transcriber falls back to CPU at runtime if
# loading the DLLs fails (e.g. on a machine with no NVIDIA driver).
#
# `nvidia` is a PEP 420 namespace package (no __init__.py), which trips up
# PyInstaller's collect_dynamic_libs(). We scan site-packages directly and
# place every DLL at the bundle root ("."), because CTranslate2 calls
# LoadLibrary("cublas64_12.dll") with the default Win32 search order — which
# checks the bundle's _internal/ folder but NOT subdirectories. (Putting
# them in subfolders breaks GPU loading even though os.add_dll_directory()
# is called at import time.)
nvidia_binaries = []
if platform.system() == "Windows":
    import glob as _glob
    import site as _site
    _nvidia_dirs = []
    for _sp in _site.getsitepackages():
        _cand = os.path.join(_sp, "nvidia")
        if os.path.isdir(_cand):
            _nvidia_dirs.append(_cand)
    _venv_nvidia = os.path.join(os.path.dirname(os.path.abspath(SPEC)),
                                "venv", "Lib", "site-packages", "nvidia")
    if os.path.isdir(_venv_nvidia) and _venv_nvidia not in _nvidia_dirs:
        _nvidia_dirs.append(_venv_nvidia)
    _seen_dll = set()
    for _nd in _nvidia_dirs:
        for _dll in _glob.glob(os.path.join(_nd, "*", "bin", "*.dll")):
            _name = os.path.basename(_dll).lower()
            if _name in _seen_dll:
                continue
            _seen_dll.add(_name)
            nvidia_binaries.append((_dll, "."))

# delvewheel side folders (<pkg>.libs, e.g. pandas.libs' private msvcp140-*.dll).
# PyInstaller's hooks miss some of them, and the .pyd files then fail with
# "DLL load failed" at runtime — so bundle every one found in site-packages.
delvewheel_binaries = []
if platform.system() == "Windows":
    import glob as _glob
    import site as _site
    _seen_libs = set()
    for _sp in _site.getsitepackages():
        for _libdir in _glob.glob(os.path.join(_sp, "*.libs")):
            _name = os.path.basename(_libdir)
            if _name in _seen_libs or not os.path.isdir(_libdir):
                continue
            _seen_libs.add(_name)
            for _dll in _glob.glob(os.path.join(_libdir, "*.dll")):
                delvewheel_binaries.append((_dll, _name))

# Bundled ffmpeg/ffprobe binaries (so users don't need to install separately)
ffmpeg_binaries = []
_bin_dir = os.path.join(os.path.dirname(os.path.abspath(SPEC)), "bin")
for _bn in ("ffmpeg", "ffprobe"):
    _bp = os.path.join(_bin_dir, _bn)
    if os.path.isfile(_bp):
        # Put in "bin" subfolder of the bundle
        ffmpeg_binaries.append((_bp, "bin"))

# MLX data + binaries (Apple Silicon only)
mlx_data = []
mlx_binaries = []
mlx_submodules = []
if platform.system() == "Darwin" and platform.machine() == "arm64":
    try:
        mlx_data += collect_data_files("mlx")
        mlx_data += collect_data_files("mlx_whisper")
        mlx_submodules += collect_submodules("mlx")
        mlx_submodules += collect_submodules("mlx_whisper")
        # Ensure native libs are bundled
        from PyInstaller.utils.hooks import collect_dynamic_libs
        mlx_binaries += collect_dynamic_libs("mlx")
    except Exception:
        pass

a = Analysis(
    ["backend_main.py"],
    pathex=[],
    binaries=mlx_binaries + torchcodec_binaries + ffmpeg_binaries + nvidia_binaries + delvewheel_binaries,
    datas=faster_whisper_data + ctranslate2_data + scipy_data + pyannote_data + speechbrain_data + torchcodec_data + mlx_data + demucs_data + transformers_data + package_metadata + ffmpeg_data + soundfile_data + lightning_data + speaker_model_data + otel_data + collect_data_files("torchmetrics", include_py_files=True) + [
        ("server.py", "."),
        ("transcriber.py", "."),
        ("silence_detector.py", "."),
        ("diarizer.py", "."),
        ("translator.py", "."),
        ("ffmpeg_utils.py", "."),
        ("_demucs_runner.py", "."),
        ("security.py", "."),
        ("waveform.py", "."),
        ("beat_tracker.py", "."),
        ("xml_cut.py", "."),
        ("speakers.py", "."),
        ("voices.py", "."),
    ],
    hiddenimports=[
        # uvicorn internals
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        # FastAPI / Starlette
        "multipart",
        "python_multipart",
        "multipart.multipart",
        "starlette.responses",
        "starlette.staticfiles",
        "anyio._backends._asyncio",
        # stdlib
        "mimetypes",
        # setuptools / jaraco deps
        "backports",
        "backports.tarfile",
        # pydub / audio
        "pydub",
        "webrtcvad",
        # WebSocket (live mode)
        "websockets",
        "websockets.legacy",
        "websockets.legacy.server",
        # network / model download
        "requests",
        "urllib3",
        "charset_normalizer",
        # pyannote / torch (diarization)
        "torch",
        "torchaudio",
        "pyannote.audio",
        "pyannote.audio.pipelines.speaker_diarization",
        "pyannote.audio.pipelines.clustering",
        "pyannote.core",
        "pyannote.pipeline",
        "pyannoteai",
        "pyannoteai.sdk",
        "opentelemetry",
        "opentelemetry.context.contextvars_context",
        "speakers",
        "voices",
        "lightning_fabric",
        "pytorch_lightning",
        "speechbrain",
        # scipy (required by pyannote/speechbrain)
        "scipy",
        "scipy.signal",
        "scipy.fft",
        "scipy.linalg",
        "scipy.sparse",
        "scipy.special",
        "scipy.ndimage",
        # MLX (Apple Silicon)
        "mlx",
        "mlx.core",
        "mlx_whisper",
        # Bundled ffmpeg
        "imageio_ffmpeg",
        # Demucs (song mode — vocal separation)
        "demucs",
        "demucs.api",
        "demucs.separate",
        "demucs.audio",
        "demucs.apply",
        "demucs.pretrained",
        # Audio I/O fallback when torchcodec is broken on Windows
        "soundfile",
        # Transformers (Hy-MT2 translator)
        "transformers",
        "huggingface_hub",
        "sentencepiece",
        "tokenizers",
        # Local helpers (also bundled as data so import paths resolve)
        "ffmpeg_utils",
        "_demucs_runner",
        "security",
        "waveform",
        "beat_tracker",
        "xml_cut",
    ]
    + mlx_submodules
    + demucs_submodules
    + collect_submodules("faster_whisper")
    + collect_submodules("ctranslate2")
    + collect_submodules("scipy")
    + _fs_submodules("pyannote")
    + _fs_submodules("pyannote.audio")
    + collect_submodules("speechbrain")
    + collect_submodules("pytorch_lightning")
    + collect_submodules("lightning_fabric")
    + collect_submodules("transformers")
    + collect_submodules("torchmetrics"),
    hookspath=[os.path.join(os.path.dirname(os.path.abspath(SPEC)), "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torchcodec"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EasyScript-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=None,  # Add .icns (Mac) or .ico (Win) path here
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="EasyScript-backend",
)
