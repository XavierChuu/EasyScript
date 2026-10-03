# EasyScript — Transcription & Translation App

## Architecture
- **plugin/** — Frontend (HTML/CSS/JS), runs in browser (dev) or Premiere Pro (UXP)
- **backend/** — Python FastAPI server, bundled via PyInstaller
- Frontend gọi backend qua `localhost:9876`

## Tech Stack
- Frontend: HTML/CSS/JS, Premiere DOM API (manifest v6, Premiere 25.0+)
- Backend: Python 3.11, FastAPI, mlx-whisper/faster-whisper, webrtcvad, websockets
- Live mode: WebSocket streaming, VAD-based sentence splitting, realtime translation
- Distribution: PyInstaller bundled executable

## Commands
- Build backend: `./scripts/build_backend.sh`
- Run dev server: `cd backend && python server.py`
- Run dev frontend: `npx serve ./plugin`
- Load plugin: UXP Developer Tool → Add Plugin → select `plugin/manifest.json`
- Backend tests: `python -m unittest discover -s backend/tests -t backend`
- Premiere panel (current): `cep-extension-v2.1/` (CEP + ExtendScript `host.jsx`)

## Backend access token
- Every request needs the per-launch token (`backend/security.py`): header
  `X-EasyScript-Token`, or `?token=` for `<audio src>` / WebSocket. Only `/health` is open.
- The panel reads it from `~/.easyscript/token-<port>` via ExtendScript; the
  standalone app gets it injected by `main.py`.
- Browser dev of the CEP panel: run the backend with `EASYSCRIPT_TOKEN=dev`
  (optionally `PORT=9877`) and open `index.html?token=dev&port=9877`.
  `EASYSCRIPT_AUTH=off` disables the check — never ship that.

## Phase Roadmap
1. Audio analysis backend (faster-whisper + silence detection)
2. Review UI in Premiere (waveform viewer, cut controls, markers)
3. Subtitle engine (SRT, Captions track, song ngữ Việt-Anh)
4. Translation engine (Ollama local + Claude API cloud)
5. Live transcription (WebSocket streaming, VAD, realtime translation) — **current**
