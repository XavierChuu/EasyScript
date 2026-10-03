# EasyScript

**Premiere Pro panel for silence cutting, transcription, translation and beat markers**, backed by a local Python server (Whisper, pyannote, numpy beat tracker). Everything runs on your machine — macOS (Apple Silicon / Metal) and Windows (NVIDIA CUDA or CPU).

---

## Features

### Cut
- **Silence & breath detection** on the selected clip or the whole sequence, with padding / min-silence / threshold controls and a live preview on the waveform
- **Apply Cut** directly on the timeline (frame-exact: cuts are snapped inward to the sequence's frame grid, so they never reach into speech)
- **Export XML cut** — rebuilds the sequence as an FCP XML in one pass (much faster than razor-by-razor on hour-long timelines), optionally imported straight back into the project
- **Split speakers** onto separate tracks after diarization

### Transcript
- **Whisper transcription** — mlx-whisper on Apple Silicon, faster-whisper on CUDA/CPU; models from Tiny to Large V3 / Turbo
- **Music / Song mode** — Demucs vocal isolation + tunable vocal sensitivity, phrase gap and decode quality
- **Speaker diarization** (pyannote; needs a HuggingFace token)
- **Translation** — local (NLLB / Ollama) or Claude API, multiple target languages side by side
- **Search & replace**, display modes (sentence / word / punctuation / max words per line)
- **Captions** — export SRT (original or after cuts) and create a Premiere captions track

### Beats
- Beat / downbeat detection with auto or fixed BPM, tempo range and tightness
- Marker spacing from 1/4 beat up to 4 bars, with meter, downbeat offset and half/double tempo — reviewed on the waveform before applying
- Markers placed on the **selected clip** or on the **sequence timeline**; clear them again in one click

### Waveform
- Canvas renderer with multi-resolution peaks — smooth zoom from a full 3-hour overview down to individual samples
- Speaker colours, cut regions, beat markers; click to seek Premiere's playhead

---

## Install

1. Install the panel: double-click `EasyScript-Premiere.zxp` with a ZXP installer (e.g. ZXPInstaller / Anastasiy's Extension Manager).
2. Copy the backend into `~/.easyscript/backend` (the release bundle's installer does this). The panel starts it automatically.
3. Open Premiere Pro → **Window → Extensions → EasyScript**.

Requirements: Premiere Pro 22+ (CEP 9+), macOS 13+ or Windows 10+, 8 GB RAM (16 GB for large models). FFmpeg is bundled.

---

## Development

```
EasyScript/
├── cep-extension-v2.1/     # Premiere panel (CEP)
│   ├── index.html / index.js / styles.css
│   ├── waveform.js         # canvas waveform
│   ├── beats.js            # beat-marker UI
│   ├── bridge.js           # promise wrapper over evalScript
│   ├── host.jsx            # ExtendScript (ES3, JSON polyfilled)
│   └── package_zxp.*       # sign → dist/EasyScript-Premiere.zxp
├── backend/                # FastAPI server (localhost:9876)
│   ├── server.py           # API
│   ├── security.py         # per-launch access token, Host/CORS checks
│   ├── transcriber.py / diarizer.py / translator.py / silence_detector.py
│   ├── waveform.py         # multi-resolution peaks
│   ├── beat_tracker.py     # numpy beat tracker
│   ├── xml_cut.py          # FCP7 XML sequence cutter
│   ├── backend_main.py     # PyInstaller entry
│   ├── easyscript_backend.spec
│   └── tests/
├── scripts/
│   ├── build_backend_win.ps1   # Windows backend build (venv: backend/venv-win)
│   ├── build_backend_mac.sh    # macOS backend build (venv: backend/venv)
│   └── make_release.sh         # assemble the release bundle
└── release-template/       # installer files copied into each release
```

### Run the backend

```bash
cd backend
python -m venv venv && venv/bin/pip install -r requirements.txt
EASYSCRIPT_TOKEN=dev venv/bin/python server.py
```

Every request needs the access token (header `X-EasyScript-Token`, or `?token=` for `<audio>` / WebSocket); only `/health` is open. The panel reads it from `~/.easyscript/token-<port>`. `EASYSCRIPT_TOKEN` pins it for development.

### Panel without Premiere

Serve `cep-extension-v2.1/` and open `index.html?token=dev` (add `&port=…` for another backend port), or run `start_dev.command` on macOS. Premiere-only actions are disabled in the browser.

### Panel inside Premiere (dev)

Run `cep-extension-v2.1/install.bat` (Windows) or `install.sh` (macOS). It enables PlayerDebugMode and copies the panel into the CEP extensions directory; the debugger is at http://localhost:8088.

### Build

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_backend_win.ps1
```

```bash
./scripts/build_backend_mac.sh
```

Output: `backend/dist_backend/EasyScript-backend/`. Package the panel with `cep-extension-v2.1/package_zxp.ps1 -ZxpSign <ZXPSignCmd.exe>` (or `package_zxp.sh`). The signing certificate and ZXPSignCmd stay out of git.

### Tests

```bash
python -m unittest discover -s backend/tests -t backend
```

---

## License

MIT License

## Credits

- [Whisper](https://github.com/openai/whisper) by OpenAI
- [mlx-whisper](https://github.com/ml-explore/mlx-examples) by Apple MLX team
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) by SYSTRAN
- [pyannote-audio](https://github.com/pyannote/pyannote-audio) for speaker diarization
- [FastAPI](https://fastapi.tiangolo.com/) for the backend framework
