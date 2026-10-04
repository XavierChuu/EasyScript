"""
Voice / music separation.

Model: Mel-Band RoFormer "Kim vocal" (KimberleyJSN/melbandroformer, MIT; see
roformer.py) — the strongest openly licensed vocal model: ~11 dB vocal SDR
against ~8-9 dB for htdemucs. It predicts the vocal stem; the music stem is
the source minus the vocals at the source's own sample rate, so the two stems
add back up to the original.

Devices, GPU first: Apple Silicon → MLX on Metal (roformer_mlx.py, fp16);
NVIDIA with a CUDA torch → CUDA (fp16 autocast); any other Windows GPU →
ONNX Runtime DirectML on a one-time ONNX export of the transformer core (the
Windows torch build is CPU-only); other Metal Macs → torch MPS; else torch on
the CPU. The STFT / mask application always runs in torch on the CPU except
under CUDA. EASYSCRIPT_SEPARATE_DEVICE=mlx|cuda|dml|onnx|mps|cpu overrides
("onnx" = the ONNX path on ONNX Runtime's CPU provider, for testing).

Audio goes through raw float32 files that are memory-mapped, so an hour-long
sequence doesn't need gigabytes of RAM.
"""

import os
import re
import shutil
import tempfile
import threading
import time

import numpy as np

from ffmpeg_utils import get_ffmpeg_exe, popen_silent, run_ffmpeg

MODEL_REPO = "KimberleyJSN/melbandroformer"
MODEL_FILE = "MelBandRoformer.ckpt"
MODEL_BYTES = 913_106_900
MODEL_DIR = os.path.join(os.path.expanduser("~"), ".easyscript", "models", "separation",
                         "mel-band-roformer-kim")

# configs/config_vocals_mel_band_roformer.yaml of the model's repository
CONFIG = dict(
    dim=384, depth=6, stereo=True, num_stems=1, time_transformer_depth=1,
    freq_transformer_depth=1, num_bands=60, dim_head=64, heads=8, attn_dropout=0,
    ff_dropout=0, flash_attn=True, dim_freqs_in=1025, sample_rate=44100, stft_n_fft=2048,
    stft_hop_length=441, stft_win_length=2048, stft_normalized=False, mask_estimator_depth=2,
)
SR = 44100
CHUNK = 352800          # 8 s — the training length
OVERLAP = 2             # chunks per window step (the model's default)
BLOCK = SR * 10         # samples per read/write block when streaming files


class SeparationCancelled(Exception):
    pass


def model_path():
    return os.path.join(MODEL_DIR, MODEL_FILE)


def is_downloaded():
    p = model_path()
    return os.path.isfile(p) and os.path.getsize(p) == MODEL_BYTES


def download(progress=None, cancelled=None):
    """Fetch the checkpoint (resumable). progress(done_bytes, total_bytes)."""
    if is_downloaded():
        return model_path()
    import requests
    from huggingface_hub import hf_hub_url

    os.makedirs(MODEL_DIR, exist_ok=True)
    part = model_path() + ".part"
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with requests.get(hf_hub_url(MODEL_REPO, MODEL_FILE), headers=headers, stream=True,
                      timeout=60) as r:
        if r.status_code == 416:            # .part already complete
            r.close()
        else:
            r.raise_for_status()
            if have and r.status_code != 206:  # server ignored the range
                have = 0
            with open(part, "ab" if have else "wb") as f:
                done = have
                for block in r.iter_content(chunk_size=1 << 20):
                    if cancelled and cancelled():
                        raise SeparationCancelled()
                    f.write(block)
                    done += len(block)
                    if progress:
                        progress(done, MODEL_BYTES)
    if os.path.getsize(part) != MODEL_BYTES:
        raise RuntimeError("Separation model download is incomplete — try again.")
    os.replace(part, model_path())
    return model_path()


def _has_mlx_metal():
    try:
        import mlx.core as mx
        return bool(mx.metal.is_available())
    except Exception:
        return False


def _has_directml():
    try:
        import onnxruntime as ort
        return "DmlExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def detect_device():
    import torch
    forced = os.environ.get("EASYSCRIPT_SEPARATE_DEVICE", "").strip().lower()
    if forced in ("mlx", "cuda", "dml", "onnx", "mps", "cpu"):
        return forced
    if _has_mlx_metal():
        return "mlx"
    if torch.cuda.is_available():
        return "cuda"
    if _has_directml():
        return "dml"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def device_label(device):
    if device == "cuda":
        import torch
        return "GPU · " + torch.cuda.get_device_name(0)
    return {"mlx": "GPU · Metal", "mps": "GPU · Metal", "dml": "GPU · DirectML",
            "onnx": "CPU · ONNX"}.get(device, "CPU")


def _onnx_core(model, progress=None):
    """ONNX export of model.core (cached next to the checkpoint)."""
    import torch

    # v2: band split exported as Slices (2.3.0's core.onnx fails to load in ORT 1.23).
    stale = os.path.join(MODEL_DIR, "core.onnx")
    if os.path.isfile(stale):
        os.remove(stale)
    path = os.path.join(MODEL_DIR, "core-v2.onnx")
    if os.path.isfile(path):
        return path
    if progress:
        progress("Optimizing the model for your GPU (one time, about a minute)…")

    class Core(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, feats):
            return self.m.core(feats)

    n_feats = sum(2 * f * model.audio_channels for f in model.num_freqs_per_band.tolist())
    frames = CHUNK // CONFIG["stft_hop_length"] + 1
    tmp = path + ".part"
    with torch.inference_mode():
        torch.onnx.export(Core(model), (torch.zeros(1, frames, n_feats),), tmp,
                          input_names=["feats"], output_names=["masks"], opset_version=17,
                          dynamo=False, dynamic_axes={"feats": {0: "b"}, "masks": {0: "b"}})
    os.replace(tmp, path)
    return path


def _ort_session(path, provider):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.log_severity_level = 3
    if provider == "DmlExecutionProvider":
        # Required by the DirectML provider.
        so.enable_mem_pattern = False
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return ort.InferenceSession(path, so, providers=[provider])


_AUDIO_RE = re.compile(r"Stream #\S+.*?Audio:[^\n]*?(\d+) Hz, ([^,\n]+)")


def probe_audio(path):
    """(sample_rate, channels) of the first audio stream; channels capped at 2."""
    r = run_ffmpeg(["-hide_banner", "-i", path], capture_output=True, text=True, timeout=30)
    m = _AUDIO_RE.search(r.stderr or "")
    if not m:
        raise RuntimeError("No audio stream found in the source.")
    sr, layout = int(m.group(1)), m.group(2).strip()
    ch = 1 if layout.startswith("mono") or layout == "1 channels" else 2
    return sr, ch


def _decode(src, dst, sr, channels, start=0.0, duration=0.0):
    """Decode the source (optionally a range) to raw interleaved float32."""
    args = ["-y", "-hide_banner", "-loglevel", "error"]
    if start > 0:
        args += ["-ss", f"{start:.6f}"]
    if duration > 0:
        args += ["-t", f"{duration:.6f}"]
    args += ["-i", src, "-vn", "-map", "0:a:0", "-ac", str(channels), "-ar", str(sr),
             "-f", "f32le", dst]
    r = run_ffmpeg(args, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(dst):
        raise RuntimeError(f"Could not decode the audio: {(r.stderr or '').strip()[-300:]}")
    n = os.path.getsize(dst) // (4 * channels)
    if n == 0:
        raise RuntimeError("The audio range is empty.")
    return np.memmap(dst, dtype=np.float32, mode="r", shape=(n, channels))


class _RawWriter:
    """Pipe raw float32 frames into ffmpeg (resample / encode on the way)."""

    def __init__(self, out_args, sr, channels):
        import subprocess
        self.proc = popen_silent(
            [get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-f", "f32le",
             "-ar", str(sr), "-ac", str(channels), "-i", "-", *out_args],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        # Drain stderr so a chatty ffmpeg can never block on a full pipe.
        self.err = []
        self.reader = threading.Thread(target=lambda: self.err.append(self.proc.stderr.read()), daemon=True)
        self.reader.start()

    def write(self, frames):
        self.proc.stdin.write(np.ascontiguousarray(frames, dtype=np.float32).tobytes())

    def close(self):
        self.proc.stdin.close()
        rc = self.proc.wait()
        self.reader.join()
        self.proc.stderr.close()
        if rc != 0:
            msg = (self.err[0] if self.err else b"").decode("utf-8", "replace").strip()
            raise RuntimeError(f"ffmpeg failed while writing a stem: {msg[-300:]}")

    def abort(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for f in (self.proc.stdin, self.proc.stderr):
            try:
                f.close()
            except Exception:
                pass


class Separator:
    """Loaded model on its device; separate() can be called repeatedly."""

    def __init__(self, device=None, progress=None):
        """progress(detail) reports one-time setup steps (ONNX export)."""
        import torch
        from roformer import MelBandRoformer

        # The bundle sets OMP_NUM_THREADS=1; the STFT / masks (every device but CUDA)
        # and the torch CPU fallback are several times slower single-threaded.
        torch.set_num_threads(max(1, min(8, os.cpu_count() or 4)))
        device = device or detect_device()
        state = torch.load(model_path(), map_location="cpu", weights_only=True)
        model = MelBandRoformer(**CONFIG).eval()
        model.load_state_dict(state)
        self.model = model          # spectrum() / reconstruct() always run here (CPU)
        self.core = None            # callable: feats numpy [b, t, fc] -> masks numpy
        self.batch = 1              # one chunk saturates every backend except CUDA

        if device == "mlx":
            try:
                from roformer_mlx import MLXCore
                self.core = MLXCore(state, CONFIG)
            except Exception as e:
                print(f"[separator] MLX unavailable ({e}); falling back")
                device = "mps" if torch.backends.mps.is_available() else "cpu"
        if device in ("dml", "onnx"):
            try:
                provider = "DmlExecutionProvider" if device == "dml" else "CPUExecutionProvider"
                session = _ort_session(_onnx_core(model, progress), provider)
                self.core = lambda feats: session.run(None, {"feats": feats})[0]
            except Exception as e:
                print(f"[separator] ONNX {device} unavailable ({e}); using torch on the CPU")
                device = "cpu"
        if device == "cuda":
            self.model = model.to("cuda")
            self.batch = 4
        elif device == "mps":
            core = model.to("mps").half()    # core only; the spectral steps take CPU tensors

            def mps_core(feats):
                with torch.inference_mode():
                    return core.core(torch.from_numpy(feats).to("mps", torch.float16)).float().cpu().numpy()
            self.core = mps_core
        del state
        if device in ("mlx", "dml", "onnx"):
            # The core runs elsewhere; spectrum()/reconstruct() need no weights.
            del model.layers, model.band_split, model.mask_estimators
        self.device = device
        self.label = device_label(device)
        print(f"[separator] device: {self.label}")

    def _infer(self, parts):
        """[b, 2, CHUNK] float32 CPU tensor -> vocals, same shape (CPU)."""
        import torch
        m = self.model
        with torch.inference_mode():
            if self.device == "cuda":
                with torch.autocast("cuda", dtype=torch.float16):
                    return m(parts.to("cuda")).float().cpu()
            if self.core is not None:
                stft, feats = m.spectrum(parts)
                masks = self.core(np.ascontiguousarray(feats.numpy()))
                return m.reconstruct(stft, torch.from_numpy(masks))
            return m(parts)

    def separate(self, src, out_dir, start=0.0, duration=0.0, progress=None, cancelled=None):
        """Write vocals.wav and music.wav (source rate / channels, 32-bit float) into out_dir.

        progress(fraction, detail) covers decode → inference → encode.
        Returns {"vocals", "music", "sample_rate", "channels", "duration"}.
        """
        def report(p, detail):
            if cancelled and cancelled():
                raise SeparationCancelled()
            if progress:
                progress(p, detail)

        os.makedirs(out_dir, exist_ok=True)
        for stale in os.listdir(out_dir):     # left behind by a crash / Windows file lock
            if stale.startswith("sep_"):
                shutil.rmtree(os.path.join(out_dir, stale), ignore_errors=True)
        work = tempfile.mkdtemp(prefix="sep_", dir=out_dir)
        try:
            # The memmaps live in _run's frame, so they are closed once it returns
            # (Windows can't delete a mapped file).
            return self._run(src, out_dir, work, start, duration, report)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _run(self, src, out_dir, work, start, duration, report):
        import torch
        import torch.nn.functional as F

        report(0.01, "Reading audio…")
        src_sr, src_ch = probe_audio(src)
        mix = _decode(src, os.path.join(work, "mix44.f32"), SR, 2, start, duration)
        orig = _decode(src, os.path.join(work, "orig.f32"), src_sr, src_ch, start, duration)
        n = mix.shape[0]

        # Overlap-add exactly as the model's reference demix: reflect-pad by
        # `border`, fade windows, average where windows overlap.
        step = CHUNK // OVERLAP
        fade = CHUNK // 10
        border = CHUNK - step
        pad = border if n > 2 * border else 0
        total = n + 2 * pad
        acc = np.memmap(os.path.join(work, "acc.f32"), dtype=np.float32, mode="w+", shape=(total, 2))
        cnt = np.memmap(os.path.join(work, "cnt.f32"), dtype=np.float32, mode="w+", shape=(total,))
        win = np.ones(CHUNK, dtype=np.float32)
        win[:fade] = np.linspace(0, 1, fade, dtype=np.float32)
        win[-fade:] = np.linspace(1, 0, fade, dtype=np.float32)

        def padded(a, b):
            """Frames [a, b) of the reflect-padded mix, as [2, b-a]."""
            idx = np.arange(a, min(b, total)) - pad
            if len(idx) and idx[0] >= 0 and idx[-1] < n:
                return np.asarray(mix[idx[0]:idx[-1] + 1]).T
            idx = np.abs(idx)
            idx = np.where(idx >= n, 2 * (n - 1) - idx, idx)
            return np.asarray(mix[idx]).T

        starts = list(range(0, total, step))
        t0 = time.time()
        for bi in range(0, len(starts), self.batch):
            group = starts[bi:bi + self.batch]
            parts, lengths = [], []
            for i in group:
                part = torch.from_numpy(np.ascontiguousarray(padded(i, i + CHUNK)))
                length = part.shape[-1]
                if length < CHUNK:
                    if length > CHUNK // 2 + 1:
                        part = F.pad(part.unsqueeze(0), (0, CHUNK - length), mode="reflect")[0]
                    else:
                        part = F.pad(part, (0, CHUNK - length))
                parts.append(part)
                lengths.append(length)
            out = self._infer(torch.stack(parts)).numpy()
            for k, i in enumerate(group):
                w = win.copy()
                if i == 0:
                    w[:fade] = 1
                elif i + CHUNK >= total:
                    w[-fade:] = 1
                ln = lengths[k]
                acc[i:i + ln] += (out[k][:, :ln] * w[:ln]).T
                cnt[i:i + ln] += w[:ln]
            done = bi + len(group)
            eta = (time.time() - t0) / done * (len(starts) - done)
            report(0.05 + 0.85 * done / len(starts),
                   f"Separating ({self.label})… {done}/{len(starts)} · ~{int(eta)} s left")

        # Vocals: back to the source rate. Music: source − vocals, block by block.
        report(0.92, "Writing stems…")
        voc_raw = os.path.join(work, "voc.f32")
        rs = _RawWriter(["-ar", str(src_sr), "-ac", str(src_ch), "-f", "f32le", voc_raw], SR, 2)
        try:
            for a in range(pad, pad + n, BLOCK):
                b = min(a + BLOCK, pad + n)
                v = acc[a:b] / np.maximum(cnt[a:b], 1e-8)[:, None]
                rs.write(np.nan_to_num(v))
        except BaseException:
            rs.abort()
            raise
        rs.close()
        m = orig.shape[0]
        voc = np.memmap(voc_raw, dtype=np.float32, mode="r")
        voc = voc[:len(voc) - len(voc) % src_ch].reshape(-1, src_ch)

        # 32-bit float: source − vocals can exceed full scale, and integer PCM
        # would clip it (the stems would no longer add up to the source).
        wav = ["-c:a", "pcm_f32le"]
        vocals_path = os.path.join(out_dir, "vocals.wav")
        music_path = os.path.join(out_dir, "music.wav")
        wv = _RawWriter([*wav, vocals_path], src_sr, src_ch)
        wm = _RawWriter([*wav, music_path], src_sr, src_ch)
        try:
            for a in range(0, m, BLOCK):
                b = min(a + BLOCK, m)
                o = np.asarray(orig[a:b])
                v = np.zeros_like(o)
                have = max(0, min(b, voc.shape[0]) - a)
                v[:have] = voc[a:a + have]
                wv.write(v)
                wm.write(o - v)
        except BaseException:
            wv.abort()
            wm.abort()
            raise
        wv.close()
        wm.close()
        report(1.0, "Done")
        return {"vocals": vocals_path, "music": music_path, "sample_rate": src_sr,
                "channels": src_ch, "duration": m / float(src_sr)}
