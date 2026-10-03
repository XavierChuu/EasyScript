#!/bin/bash
# EasyScript for Premiere Pro — macOS installer
# Installs the panel + backend. Double-click to run.
HERE="$(cd "$(dirname "$0")" && pwd)"
echo "=== Installing EasyScript ==="

# 1) Enable CEP debug mode (loads the panel)
for v in 9 10 11 12 13 14 15; do
  defaults write com.adobe.CSXS.${v} PlayerDebugMode 1 2>/dev/null || true
done
killall cfprefsd 2>/dev/null || true

# 2) Install the panel (extract the signed .zxp into the CEP extensions folder)
EXT="$HOME/Library/Application Support/Adobe/CEP/extensions/com.easyscript.premiere"
rm -rf "$EXT"; mkdir -p "$EXT"
unzip -oq "$HERE/EasyScript-Premiere.zxp" -d "$EXT"
echo "  Panel  → $EXT"

# 3) Install the backend
BK="$HOME/.easyscript/backend"
rm -rf "$BK"; mkdir -p "$BK"
cp -R "$HERE/backend/." "$BK/"
chmod +x "$BK/EasyScript-backend" 2>/dev/null || true
xattr -dr com.apple.quarantine "$BK" 2>/dev/null || true
echo "  Backend → $BK"

echo ""
echo "=== Done ==="
echo "1) RESTART Premiere Pro (fully quit, Cmd+Q)"
echo "2) Window > Extensions > EasyScript"
echo "The panel starts the backend automatically on first open."
echo ""
read -p "Press Enter to close..."
