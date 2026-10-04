# EasyScript — Premiere Pro panel: silence cut, transcription, translation, beat markers

## Architecture
- **cep-extension-v2.1/** — the Premiere panel (CEP). `index.html/js`, `styles.css`,
  `waveform.js` (canvas waveform), `beats.js` (beat markers), `bridge.js`
  (promise wrapper over `evalScript`), `host.jsx` (ExtendScript — ES3: no
  let/const/arrows; JSON is polyfilled).
- **backend/** — Python FastAPI server on `localhost:9876`, bundled with
  PyInstaller (`easyscript_backend.spec`, entry `backend_main.py`); the panel
  launches it from `~/.easyscript/backend`.
- Premiere edits go through `host.jsx`; analysis (silence, Whisper, pyannote,
  translation, beats, waveform peaks, XML cut) goes to the backend over fetch.
- UXP is not used: its Premiere API still has no razor/split.

## Tech Stack
- Panel: HTML/CSS/JS in CEP (CSXS 9+, Premiere 22+), ExtendScript + QE DOM
- Backend: Python 3.10/3.11, FastAPI, faster-whisper / mlx-whisper, pyannote,
  numpy beat tracker, FCP7-XML cutter
- Distribution: signed .zxp + PyInstaller backend, assembled by `scripts/make_release.sh`
  (macOS, see `docs/BUILD_MAC.md`) / `scripts/make_release_win.ps1` (Windows zip)

## Commands
- Build backend: `scripts/build_backend_win.ps1` (Windows, venv `backend/venv-win`) /
  `scripts/build_backend_mac.sh` (macOS, venv `backend/venv`) → `backend/dist_backend/`
- Package panel: `cep-extension-v2.1/package_zxp.ps1 -ZxpSign <ZXPSignCmd.exe>` /
  `package_zxp.sh` → `dist/EasyScript-Premiere.zxp`
- Dev install of the panel: `cep-extension-v2.1/install.bat` / `install.sh`
  (enables PlayerDebugMode; debugger on http://localhost:8088)
- Run dev server: `cd backend && python server.py`
- Panel in a browser (no Premiere): `start_dev.command`, or serve
  `cep-extension-v2.1/` and open `index.html?token=dev` with the backend
  started as `EASYSCRIPT_TOKEN=dev` (add `&port=…` for another port)
- Backend tests: `python -m unittest discover -s backend/tests -t backend`

## Backend access token
- Every request needs the per-launch token (`backend/security.py`): header
  `X-EasyScript-Token`, or `?token=` for `<audio src>` / WebSocket. Only `/health` is open.
- The panel reads it from `~/.easyscript/token-<port>` via ExtendScript.
- `EASYSCRIPT_TOKEN` pins the token (dev); `EASYSCRIPT_AUTH=off` disables the
  check — never ship that.

## Models
- ASR: faster-whisper 1.2 (CUDA/CPU) / mlx-whisper; default `large-v3-turbo`
  (FLEURS-vi long-form: 11 % WER at ~50x real time on an RTX 3060, same as
  large-v3 at 18x; PhoWhisper and batched mode were worse). Long files are cut
  into ~10 min chunks at the quietest point (`Transcriber._chunk_bounds`),
  fed as arrays from the shared PCM cache. Segments keep word timestamps.
- Speakers: pyannote.audio 4 + community-1 (CC-BY-4.0, ungated mirror
  `pyannote-community/...`), bundled from `backend/models/` (fetched by the
  build scripts, gitignored). Its embedding ResNet runs as ONNX
  (`tools/export_embedding_onnx.py`) on ONNX Runtime DirectML / CPU — Windows
  ships `onnxruntime-directml` instead of `onnxruntime`.
- `speakers.py`: word-level attribution on the exclusive diarization, splits
  lines at real speaker changes. `voices.py`: saved voices
  (`~/.easyscript/voices.json`) matched by cosine ≥ 0.5.
- Tag speaker "New sequence" = `/xml/cut` with `splits_ticks` + `labels`
  (+ `only_media` so music / B-roll aren't split).

## Frame math
- Always use the sequence's ticks-per-frame (`seq.timebase`; 254016000000
  ticks/s). Cut ranges are snapped inward (start up, end down) so a cut never
  reaches into speech. Never round fps (29.97 ≠ 30).
