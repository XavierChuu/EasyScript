#!/bin/bash
# Assemble the customer distribution folder for EasyScript (Premiere CEP).
# Output: release/EasyScript/  with the signed panel, installers, guide, and a
# placeholder backend/ folder (drop your backend build there).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/release/EasyScript"

echo "[release] Signing ZXP..."
bash "$ROOT/cep-extension-v2.1/package_zxp.sh"

echo "[release] Assembling $OUT"
rm -rf "$OUT"; mkdir -p "$OUT/backend"
cp "$ROOT/dist/EasyScript-Premiere.zxp" "$OUT/"
cp "$ROOT/release-template/install-mac.command" "$OUT/"
cp "$ROOT/release-template/install-win.bat" "$OUT/"
chmod +x "$OUT/install-mac.command"
cp "$ROOT/release-template/"*.pdf "$OUT/" 2>/dev/null || true

cat > "$OUT/backend/PUT-BACKEND-BUILD-HERE.txt" <<'EOF'
Put the bundled backend here. It must contain an executable named:
  - macOS:   EasyScript-backend
  - Windows: EasyScript-backend.exe
(Build it from backend/ with PyInstaller — a headless server on port 9876.)
The installer copies this folder to ~/.easyscript/backend and the panel
launches EasyScript-backend automatically.
EOF

echo "[release] Done → $OUT"
echo "  Drop the backend build into release/EasyScript/backend/ then zip the folder for customers."
