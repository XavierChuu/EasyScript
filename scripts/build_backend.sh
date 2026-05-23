#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/../backend"
DIST_DIR="$SCRIPT_DIR/../dist"

echo "=== Building Pro Cut Backend ==="

cd "$BACKEND_DIR"

if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  python3.11 -m venv venv
fi

echo "Installing dependencies..."
source venv/bin/activate
pip install -r requirements.txt

echo "Building executable with PyInstaller..."
pyinstaller procut_backend.spec --distpath "$DIST_DIR" --workpath "$BACKEND_DIR/build" -y

echo ""
echo "Build complete: $DIST_DIR/procut-backend/"
echo "Run with: $DIST_DIR/procut-backend/procut-backend"
