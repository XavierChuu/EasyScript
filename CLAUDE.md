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

## Phase Roadmap
1. Audio analysis backend (faster-whisper + silence detection)
2. Review UI in Premiere (waveform viewer, cut controls, markers)
3. Subtitle engine (SRT, Captions track, song ngữ Việt-Anh)
4. Translation engine (Ollama local + Claude API cloud)
5. Live transcription (WebSocket streaming, VAD, realtime translation) — **current**
