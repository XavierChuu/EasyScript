#!/bin/bash
#
# EasyScript — Dev launcher
# Double-click this file (or run it) to start the backend server and open the
# frontend in your browser for testing. No build needed.
#
# - Backend:  FastAPI on http://127.0.0.1:9876  (logs shown in this window)
# - Frontend: the Premiere panel (cep-extension-v2.1) opened in your browser
#             in dev mode (no Premiere APIs; token fixed to "dev")
#
# Close this Terminal window (or press Ctrl+C) to stop the server.

set -e

# Resolve the directory this script lives in (project root).
ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="file://$ROOT/cep-extension-v2.1/index.html?token=dev"
# Fixed token so the browser page can authenticate (see backend/security.py).
export EASYSCRIPT_TOKEN=dev

# Pick the Python interpreter: prefer the project venv, fall back to python3.
PY="$BACKEND/venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "⚠️  venv not found at $PY — falling back to 'python3'."
  echo "    If imports fail, create it:  python3 -m venv backend/venv && backend/venv/bin/pip install -r backend/requirements.txt"
  PY="python3"
fi

echo "──────────────────────────────────────────────"
echo " EasyScript dev server"
echo " Python : $PY"
echo " Backend: http://127.0.0.1:9876"
echo " Frontend: $FRONTEND"
echo "──────────────────────────────────────────────"

# If a server is already up, just (re)open the frontend and stop.
if curl -s http://127.0.0.1:9876/health >/dev/null 2>&1; then
  echo "✅ Server already running on port 9876 — opening frontend."
  open "$FRONTEND"
  echo "   (Leave the other window running. You can close this one.)"
  exit 0
fi

# Open the frontend once the server reports healthy (runs in the background).
(
  for _ in $(seq 1 60); do
    if curl -s http://127.0.0.1:9876/health >/dev/null 2>&1; then
      open "$FRONTEND"
      echo "✅ Frontend opened in your browser."
      break
    fi
    sleep 0.5
  done
) &

# Start the backend in the foreground so its logs show here.
# Closing this window or pressing Ctrl+C stops the server.
cd "$BACKEND"
exec "$PY" server.py
