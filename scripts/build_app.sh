#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../backend"
DIST_DIR="$SCRIPT_DIR/../dist"

echo "=== Building EasyScript Standalone App ==="

cd "$BACKEND_DIR"

if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  python3.11 -m venv venv
fi

echo "Installing dependencies..."
source venv/bin/activate
pip install -r requirements.txt
pip install pywebview pyinstaller demucs soundfile

# Download static ffmpeg/ffprobe for bundling (so users don't need to install)
mkdir -p "$BACKEND_DIR/bin"
if [ "$(uname)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
  if [ ! -f "$BACKEND_DIR/bin/ffmpeg" ]; then
    echo "Downloading static ffmpeg (macOS arm64)..."
    curl -L --max-time 120 -o /tmp/ffmpeg.zip "https://www.osxexperts.net/ffmpeg711arm.zip"
    unzip -o /tmp/ffmpeg.zip -d "$BACKEND_DIR/bin/" && rm /tmp/ffmpeg.zip
    rm -rf "$BACKEND_DIR/bin/__MACOSX"
    chmod +x "$BACKEND_DIR/bin/ffmpeg"
  fi
  if [ ! -f "$BACKEND_DIR/bin/ffprobe" ]; then
    echo "Downloading static ffprobe (macOS arm64)..."
    curl -L --max-time 120 -o /tmp/ffprobe.zip "https://www.osxexperts.net/ffprobe711arm.zip"
    unzip -o /tmp/ffprobe.zip -d "$BACKEND_DIR/bin/" && rm /tmp/ffprobe.zip
    rm -rf "$BACKEND_DIR/bin/__MACOSX"
    chmod +x "$BACKEND_DIR/bin/ffprobe"
  fi
fi

echo ""
echo "Building app with PyInstaller..."
pyinstaller easyscript.spec --distpath "$DIST_DIR" --workpath "$BACKEND_DIR/build" -y

echo ""
if [ "$(uname)" = "Darwin" ]; then
  echo "=== Build complete ==="
  echo "  App:    $DIST_DIR/EasyScript.app"
  echo "  Folder: $DIST_DIR/EasyScript/"
  echo ""
  echo "To run:  open \"$DIST_DIR/EasyScript.app\""
  echo "To distribute: zip the .app or create a DMG"
else
  echo "=== Build complete ==="
  echo "  Folder: $DIST_DIR/EasyScript/"
  echo "  Run:    $DIST_DIR/EasyScript/EasyScript.exe"
fi
