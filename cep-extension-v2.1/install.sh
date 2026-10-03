#!/bin/bash
# EasyScript CEP extension — install (real copy; CEP does NOT load symlinks).
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXT_ID="com.easyscript.premiere"
EXT_ROOT="$HOME/Library/Application Support/Adobe/CEP/extensions"
INSTALL_DIR="$EXT_ROOT/$EXT_ID"

echo "[EasyScript] Enabling CEP debug mode (unsigned extensions)..."
for v in 8 9 10 11 12 13 14 15; do
  defaults write com.adobe.CSXS.${v} PlayerDebugMode 1 2>/dev/null || true
done
killall cfprefsd 2>/dev/null || true

echo "[EasyScript] Installing (copy) to: $INSTALL_DIR"
mkdir -p "$EXT_ROOT"
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# Copy everything incl. dotfiles (.debug); CEP needs a real directory.
rsync -a --exclude ".git" --exclude ".DS_Store" "$SCRIPT_DIR/" "$INSTALL_DIR/"
# Ensure files are readable by the CEP engine.
chmod -R u+rwX,go+rX "$INSTALL_DIR"

echo "[EasyScript] Installed."
echo "[EasyScript] 1) Start backend:  cd backend && ./venv/bin/python server.py"
echo "[EasyScript] 2) FULLY QUIT Premiere (Cmd+Q) and reopen"
echo "[EasyScript] 3) Window > Extensions > EasyScript"
echo "[EasyScript] Debug: open http://localhost:8088 in Chrome while the panel is open"
echo "[EasyScript] NOTE: re-run this script after any code change (it's a copy, not a symlink)."
