#!/bin/bash
# Build the headless EasyScript backend for macOS (for the Premiere CEP panel).
# Produces backend/dist_backend/EasyScript-backend/EasyScript-backend  — a
# console-less FastAPI server on 127.0.0.1:9876 that the panel auto-launches.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BK="$ROOT/backend"
PY="$BK/venv/bin/python"
PYI="$BK/venv/bin/pyinstaller"

echo "=== Building EasyScript headless backend (macOS) ==="
[ -x "$PY" ] || { echo "venv missing — create: python3.11 -m venv backend/venv && backend/venv/bin/pip install -r backend/requirements.txt"; exit 1; }

"$PY" -m pip install -q pyinstaller >/dev/null 2>&1 || true
echo "Fetching the speaker model (community-1, ~33 MB)..."
"$PY" -c "from huggingface_hub import snapshot_download; snapshot_download('pyannote-community/speaker-diarization-community-1', local_dir='$ROOT/backend/models/speaker-diarization-community-1', allow_patterns=['config.yaml', 'README.md', 'segmentation/*', 'embedding/*', 'plda/*'])"
"$PY" "$ROOT/backend/tools/export_embedding_onnx.py"

cd "$BK"
"$PYI" easyscript_backend.spec --distpath ./dist_backend --workpath ./build_backend -y

# IMPORTANT: transformers' lazy loader reads cached .pyc at runtime. Without
# these, diarization (pyannote/transformers) fails with a missing __init__.pyc.
TX="$BK/dist_backend/EasyScript-backend/_internal/transformers"
if [ -d "$TX" ]; then
  echo "Compiling transformers .py -> .pyc..."
  "$PY" -m compileall -q -b "$TX" 2>&1 | tail -2 || true
fi

EXE="$BK/dist_backend/EasyScript-backend/EasyScript-backend"
if [ -x "$EXE" ]; then
  echo "=== Done ==="
  echo "  Executable: $EXE"
  echo "  Copy the CONTENTS of $(dirname "$EXE") into release/EasyScript/backend/"
else
  echo "Build failed — $EXE not found"; exit 1
fi
