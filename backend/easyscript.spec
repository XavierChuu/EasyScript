# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for EasyScript standalone desktop app.
Bundles: FastAPI backend + pywebview + frontend (plugin/) + ML libs.
"""
import os
import sys
import platform
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Collect ML library data files
faster_whisper_data = collect_data_files("faster_whisper")
ctranslate2_data = collect_data_files("ctranslate2")
scipy_data = collect_data_files("scipy")

# Diarization data files (pyannote / speechbrain / torch / torchcodec)
pyannote_data = []
speechbrain_data = []
torchcodec_data = []
torchcodec_binaries = []
try:
    pyannote_data += collect_data_files("pyannote")
    speechbrain_data += collect_data_files("speechbrain")
except Exception:
    pass
try:
    torchcodec_data += collect_data_files("torchcodec")
    from PyInstaller.utils.hooks import collect_dynamic_libs
    torchcodec_binaries += collect_dynamic_libs("torchcodec")
except Exception:
    pass

# Frontend files (plugin/)
plugin_dir = os.path.join(os.path.dirname(os.path.abspath(SPEC)), "..", "plugin")
plugin_files = []
for f in ["index.html", "index.js", "styles.css"]:
    src = os.path.join(plugin_dir, f)
    if os.path.isfile(src):
        plugin_files.append((src, "plugin"))

# Bundled ffmpeg/ffprobe binaries (so users don't need to install separately)
ffmpeg_binaries = []
_bin_dir = os.path.join(os.path.dirname(os.path.abspath(SPEC)), "bin")
for _bn in ("ffmpeg", "ffprobe"):
    _bp = os.path.join(_bin_dir, _bn)
    if os.path.isfile(_bp):
        # Put in "bin" subfolder of the bundle
        ffmpeg_binaries.append((_bp, "bin"))

# Demucs data (song mode — vocal separation)
demucs_data = []
demucs_submodules = []
try:
    demucs_data += collect_data_files("demucs")
    demucs_submodules += collect_submodules("demucs")
except Exception:
    pass

# Transformers data (Hy-MT2 translator)
transformers_data = []
try:
    transformers_data += collect_data_files("transformers")
except Exception:
    pass

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
    ["main.py"],
    pathex=[],
    binaries=mlx_binaries + torchcodec_binaries + ffmpeg_binaries,
    datas=faster_whisper_data + ctranslate2_data + scipy_data + pyannote_data + speechbrain_data + torchcodec_data + mlx_data + demucs_data + transformers_data + plugin_files + [
        ("server.py", "."),
        ("transcriber.py", "."),
        ("silence_detector.py", "."),
        ("diarizer.py", "."),
        ("translator.py", "."),
        ("_demucs_runner.py", "."),
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
        # pywebview
        "webview",
        "bottle",
        "proxy_tools",
        # pyannote / torch (diarization)
        "torch",
        "torchaudio",
        "torchcodec",
        "torchcodec.decoders",
        "pyannote.audio",
        "pyannote.core",
        "pyannote.pipeline",
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
        # Demucs (song mode — vocal separation)
        "demucs",
        "demucs.api",
        "demucs.pretrained",
        "soundfile",
        # Transformers (Hy-MT2 translator)
        "transformers",
        "huggingface_hub",
        "sentencepiece",
        "tokenizers",
    ]
    + mlx_submodules
    + demucs_submodules
    + collect_submodules("faster_whisper")
    + collect_submodules("ctranslate2")
    + collect_submodules("scipy")
    + collect_submodules("pyannote")
    + collect_submodules("speechbrain")
    + collect_submodules("pytorch_lightning")
    + collect_submodules("lightning_fabric")
    + collect_submodules("torchcodec"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="EasyScript",
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
    name="EasyScript",
)

# macOS .app bundle
if platform.system() == "Darwin":
    app = BUNDLE(
        coll,
        name="EasyScript.app",
        icon=None,  # Add .icns path here
        bundle_identifier="com.easyscript.app",
        info_plist={
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleName": "EasyScript",
            "NSHighResolutionCapable": True,
            "NSMicrophoneUsageDescription": "EasyScript needs microphone access for live transcription.",
        },
    )
