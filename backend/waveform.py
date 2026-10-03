"""
Waveform data for the panel's canvas renderer.

overview(): min/max peaks at a fixed fine resolution (200 bins/s for ≤1 h) for
the whole file; the panel builds coarser mip levels from it, so zooming never
waits on the backend.
slice_(): exact per-pixel min/max (or raw samples) for a short visible range,
used once the view is zoomed past the overview's resolution.

Everything reads a mono 16-bit 16 kHz WAV: the panel's analysis audio already
is one; anything else is decoded once into a cache next to the uploads.
"""

import base64
import hashlib
import os

import numpy as np
import soundfile as sf

from ffmpeg_utils import run_ffmpeg

TARGET_SR = 16000


def _b64(arr):
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode("ascii")


def _cache_key(path):
    st = os.stat(path)
    raw = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def pcm_source(path, cache_dir):
    """(wav_path, sample_rate) of a mono PCM_16 16 kHz version of `path`."""
    try:
        info = sf.info(path)
        if info.channels == 1 and info.samplerate == TARGET_SR and info.subtype == "PCM_16":
            return path, TARGET_SR
    except Exception:
        pass
    os.makedirs(cache_dir, exist_ok=True)
    out = os.path.join(cache_dir, _cache_key(path) + ".wav")
    if not os.path.isfile(out):
        tmp = out[:-4] + ".tmp.wav"
        run_ffmpeg(["-y", "-i", path, "-vn", "-ac", "1", "-ar", str(TARGET_SR),
                    "-acodec", "pcm_s16le", tmp], capture_output=True, timeout=3600)
        if not os.path.isfile(tmp):
            raise RuntimeError(f"Could not decode audio: {path}")
        os.replace(tmp, out)
    return out, TARGET_SR


def default_bins_per_sec(duration):
    if duration <= 3600:
        return 200
    if duration <= 3 * 3600:
        return 100
    return 50


def overview(path, cache_dir, bins_per_sec=None):
    src, sr = pcm_source(path, cache_dir)
    info = sf.info(src)
    n = int(info.frames)
    duration = n / float(sr) if sr else 0.0
    bps = int(bins_per_sec or default_bins_per_sec(duration))
    spb = max(1, sr // bps)

    cache_file = os.path.join(cache_dir, f"{_cache_key(src)}_{spb}.npz")
    if os.path.isfile(cache_file):
        try:
            z = np.load(cache_file)
            return _pack_overview(z["mins"], z["maxs"], int(z["peak"]), duration, sr, spb)
        except Exception:
            pass

    nbins = (n + spb - 1) // spb
    mins = np.zeros(nbins, dtype=np.int16)
    maxs = np.zeros(nbins, dtype=np.int16)
    pos = 0  # bin index
    block = spb * 4000  # whole bins per block → bins never straddle blocks
    for chunk in sf.blocks(src, blocksize=block, dtype="int16", always_2d=False):
        m = len(chunk)
        full = m // spb
        if full:
            b = chunk[: full * spb].reshape(full, spb)
            mins[pos:pos + full] = b.min(axis=1)
            maxs[pos:pos + full] = b.max(axis=1)
            pos += full
        if m % spb:
            tail = chunk[full * spb:]
            mins[pos] = tail.min()
            maxs[pos] = tail.max()
            pos += 1
    mins, maxs = mins[:pos], maxs[:pos]
    peak = 1
    if len(mins):
        peak = max(1, abs(int(mins.min())), int(maxs.max()))
    try:
        os.makedirs(cache_dir, exist_ok=True)
        np.savez(cache_file, mins=mins, maxs=maxs, peak=np.int32(peak))
    except Exception:
        pass
    return _pack_overview(mins, maxs, peak, duration, sr, spb)


def _scale8(values, peak):
    return np.clip(np.round(values.astype(np.float32) * (127.0 / peak)), -127, 127).astype(np.int8)


def _pack_overview(mins, maxs, peak, duration, sr, spb):
    return {
        "duration": round(duration, 4),
        "sample_rate": sr,
        "samples_per_bin": spb,
        "bins_per_sec": sr / spb,
        "bins": int(len(mins)),
        "peak": peak,  # int16 full scale of the loudest sample (scale for slices)
        "min": _b64(_scale8(mins, peak)),
        "max": _b64(_scale8(maxs, peak)),
    }


def slice_(path, cache_dir, start, end, bins, peak=None):
    """Per-bin min/max of [start, end) seconds, at most `bins` bins. When the
    range holds fewer samples than bins, raw samples are returned instead."""
    src, sr = pcm_source(path, cache_dir)
    info = sf.info(src)
    s0 = max(0, int(np.floor(start * sr)))
    s1 = min(int(info.frames), int(np.ceil(end * sr)))
    bins = int(max(1, min(int(bins or 1), 16384)))
    if s1 <= s0:
        return {"start": start, "end": end, "mode": "empty", "bins": 0}
    data, _sr = sf.read(src, start=s0, stop=s1, dtype="int16", always_2d=False)
    if data.ndim > 1:
        data = data[:, 0]
    peak = int(peak) if peak else int(max(1, np.abs(data.astype(np.int32)).max()))
    n = len(data)
    if n <= bins:
        return {"start": s0 / sr, "end": s1 / sr, "mode": "samples", "bins": n,
                "sample_rate": sr, "values": _b64(_scale8(data, peak))}
    edges = np.linspace(0, n, bins + 1).astype(np.int64)
    idx = edges[:-1]
    mins = np.minimum.reduceat(data, idx)
    maxs = np.maximum.reduceat(data, idx)
    return {"start": s0 / sr, "end": s1 / sr, "mode": "minmax", "bins": bins,
            "min": _b64(_scale8(mins, peak)), "max": _b64(_scale8(maxs, peak))}


def legacy_peaks(path, cache_dir, num_peaks=800):
    """The old /peaks format (normalized 0..1 list) — kept for the standalone app."""
    data = overview(path, cache_dir)
    mn = np.frombuffer(base64.b64decode(data["min"]), dtype=np.int8).astype(np.float32)
    mx = np.frombuffer(base64.b64decode(data["max"]), dtype=np.int8).astype(np.float32)
    amp = np.maximum(np.abs(mn), np.abs(mx)) / 127.0
    if len(amp) == 0:
        return [], data["duration"]
    num_peaks = max(1, int(num_peaks))
    edges = np.linspace(0, len(amp), num_peaks + 1).astype(np.int64)
    edges = np.clip(edges, 0, len(amp) - 1)
    peaks = np.maximum.reduceat(amp, edges[:-1])
    top = float(peaks.max()) or 1.0
    return [round(float(p) / top, 4) for p in peaks], data["duration"]
