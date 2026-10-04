#!/bin/bash
# Package the EasyScript CEP extension into a signed .zxp (self-signed cert).
# Requires Adobe's ZXPSignCmd:
#   https://github.com/Adobe-CEP/CEP-Resources  (folder ZXPSignCMD-*)
#   Put the binary in PATH, or next to this script, or set ZXPSIGN=/path/to/ZXPSignCmd
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"

ZXPSIGN="${ZXPSIGN:-}"
if [ -z "$ZXPSIGN" ]; then
  if command -v ZXPSignCmd >/dev/null; then ZXPSIGN="$(command -v ZXPSignCmd)";
  elif [ -x "$DIR/ZXPSignCmd" ]; then ZXPSIGN="$DIR/ZXPSignCmd"; fi
fi
if [ -z "$ZXPSIGN" ] || [ ! -x "$ZXPSIGN" ]; then
  echo "ERROR: ZXPSignCmd not found."
  echo "Download it from https://github.com/Adobe-CEP/CEP-Resources (ZXPSignCMD)"
  echo "then put it in PATH or next to this script (chmod +x), or set ZXPSIGN=/path/to/ZXPSignCmd"
  exit 1
fi

# Staging: copy only the runtime files (drop dev-only files).
STAGE="$(mktemp -d)/EasyScript"
mkdir -p "$STAGE"
rsync -a \
  --exclude ".debug" \
  --exclude "install.sh" \
  --exclude "install.bat" \
  --exclude "package_zxp.sh" \
  --exclude "package_zxp.ps1" \
  --exclude "ZXPSignCmd" \
  --exclude "*.zxp" \
  --exclude "*.p12" \
  --exclude ".DS_Store" \
  "$DIR/" "$STAGE/"

CERT="$DIR/easyscript_cert.p12"
PW="easyscript"
if [ ! -f "$CERT" ]; then
  echo "[zxp] Creating self-signed certificate..."
  "$ZXPSIGN" -selfSignedCert VN HCM "EasyScript" "EasyScript" "$PW" "$CERT"
fi

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/EasyScript-Premiere.zxp"
rm -f "$OUT"
echo "[zxp] Signing..."
"$ZXPSIGN" -sign "$STAGE" "$OUT" "$CERT" "$PW" -tsa http://timestamp.digicert.com || \
  "$ZXPSIGN" -sign "$STAGE" "$OUT" "$CERT" "$PW"   # retry without timestamp if TSA fails

echo "[zxp] Done: $OUT"
echo "Install it with a ZXP installer (Anastasiy's Extension Manager / ZXP Installer / aescripts)."
