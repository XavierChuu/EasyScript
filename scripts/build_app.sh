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
pip install pywebview pyinstaller

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
