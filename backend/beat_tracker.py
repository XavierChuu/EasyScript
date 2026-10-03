"""
Beat tracking for beat markers — numpy only (nothing new to bundle).

1. Onset envelope: log-magnitude spectral flux with a SuperFlux-style max
   filter over frequency, at 100 frames/s; plus a low-band (≈30–200 Hz) flux
   the panel uses to find downbeats (kick drums land on them far more often).
2. Local tempo: autocorrelation of the envelope in 8 s windows, weighted by a
   tempo prior and smoothed with Viterbi, so the period can drift (live music,
   tempo changes, several songs on one timeline) without octave jumps.
3. Beats: Ellis-style dynamic programming with that time-varying period.
4. Every beat is snapped to the nearest flux peak (±20 ms, parabolic
   interpolation) and dropped where there is no music.

Input must be mono 16 kHz audio (waveform.pcm_source provides it).
"""

import numpy as np
import soundfile as sf

SR = 16000
HOP = 160            # 10 ms → 100 frames/s
WIN = 1024           # 64 ms
FPS = SR / HOP
LOW_BINS = slice(2, 14)   # 31–203 Hz (15.6 Hz per bin)
# Where the onset sits relative to the start of the frame whose log-flux peaks.
# Log compression makes the flux peak while the onset is still in the rising
# part of the Hann window — about 3/4 into the frame (measured on percussive
# test signals: 771 of 1024 samples, std < 0.3 ms).
_LATENCY = 0.75 * WIN / SR


# Below this normalized autocorrelation at the beat period there is no pulse
# to follow (speech measures ≤ 0.3, music with a beat well above).
MIN_PERIODICITY = 0.3


class Cancelled(Exception):
    pass


def _median_filter(x, width):
    if len(x) == 0 or width <= 1:
        return x
    half = width // 2
    padded = np.pad(x, (half, half), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, width)
    return np.median(windows, axis=1)


def _moving_average(x, width):
    width = max(1, int(width))
    if width == 1 or len(x) == 0:
        return x.astype(np.float64)
    c = np.cumsum(np.concatenate(([0.0], x.astype(np.float64))))
    half = width // 2
    idx = np.arange(len(x))
    lo = np.clip(idx - half, 0, len(x))
    hi = np.clip(idx + half + 1, 0, len(x))
    return (c[hi] - c[lo]) / (hi - lo)


def _gaussian_smooth(x, sigma):
    radius = int(3 * sigma)
    k = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    return np.convolve(x, k / k.sum(), mode="same")


def _maxfilter3(x):
    out = x.copy()
    out[:, 1:] = np.maximum(out[:, 1:], x[:, :-1])
    out[:, :-1] = np.maximum(out[:, :-1], x[:, 1:])
    return out


def onset_envelope(path, progress=None):
    """(flux, low_flux) per 10 ms frame."""
    info = sf.info(path)
    if info.samplerate != SR:
        raise ValueError(f"beat tracker expects {SR} Hz audio, got {info.samplerate}")
    n = int(info.frames)
    if n < WIN:
        return np.zeros(0, np.float32), np.zeros(0, np.float32)
    n_frames = 1 + (n - WIN) // HOP
    flux = np.zeros(n_frames, np.float32)
    low = np.zeros(n_frames, np.float32)
    window = np.hanning(WIN).astype(np.float32)
    block = 4096
    prev_ref = None
    for f0 in range(0, n_frames, block):
        nf = min(block, n_frames - f0)
        s0 = f0 * HOP
        s1 = s0 + (nf - 1) * HOP + WIN
        x, _sr = sf.read(path, start=s0, stop=s1, dtype="float32", always_2d=False)
        if x.ndim > 1:
            x = x.mean(axis=1)
        if len(x) < s1 - s0:
            x = np.pad(x, (0, s1 - s0 - len(x)))
        frames = np.lib.stride_tricks.sliding_window_view(x, WIN)[::HOP][:nf]
        spec = np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float32)
        ls = np.log1p(spec * 10.0)
        ref = _maxfilter3(ls)
        prev = np.empty_like(ls)
        prev[0] = prev_ref if prev_ref is not None else ls[0]
        prev[1:] = ref[:-1]
        d = ls - prev
        np.maximum(d, 0, out=d)
        flux[f0:f0 + nf] = d.sum(axis=1)
        low[f0:f0 + nf] = d[:, LOW_BINS].sum(axis=1)
        prev_ref = ref[-1]
        if progress:
            progress(0.05 + 0.55 * (f0 + nf) / n_frames, "Analyzing onsets…")
    return flux, low


def normalize_envelope(flux):
    """High-pass + rectify + divide by the running level (≈8 s), with a floor
    so near-silent passages are not blown up into fake onsets."""
    if len(flux) == 0:
        return flux.astype(np.float64)
    env = flux - _moving_average(flux, 31)
    env[env < 0] = 0
    level = _moving_average(env, 801)
    positive = level[level > 0]
    floor = 0.25 * float(np.median(positive)) if len(positive) else 1e-6
    return env / np.maximum(level, max(floor, 1e-9))


def local_tempo(env, min_bpm=60.0, max_bpm=200.0, prior_bpm=120.0,
                win_s=8.0, hop_s=2.0):
    """Per-window beat period (frames), Viterbi-smoothed.

    Returns (centres, periods, periodicity): periodicity is the envelope's
    normalized autocorrelation at the chosen period — high for music with a
    pulse, low for speech, whose onsets are irregular.
    """
    n = len(env)
    min_lag = max(2, int(np.floor(60.0 * FPS / max_bpm)))
    max_lag = int(np.ceil(60.0 * FPS / min_bpm))
    W = min(int(win_s * FPS), n)
    H = max(1, int(hop_s * FPS))
    starts = list(range(0, max(1, n - W + 1), H))
    if starts[-1] + W < n:
        starts.append(n - W)
    nfft = 1 << int(np.ceil(np.log2(max(2 * W, 2 * max_lag + 4))))
    lags = np.arange(min_lag, max_lag + 1)
    prior = np.exp(-0.5 * (np.log2(lags / (60.0 * FPS / prior_bpm))) ** 2)
    S = np.zeros((len(starts), len(lags)))
    A = np.zeros((len(starts), len(lags)))   # raw normalized autocorrelation
    for k, s in enumerate(starts):
        seg = env[s:s + W]
        seg = seg - seg.mean()
        spec = np.fft.rfft(seg, nfft)
        ac = np.fft.irfft(spec * np.conj(spec), nfft)
        if ac[0] <= 1e-12:
            continue
        ac = ac / ac[0]
        a1 = ac[lags]
        a2 = ac[np.minimum(2 * lags, len(ac) - 1)]
        A[k] = a1
        S[k] = np.maximum(a1 + 0.5 * a2, 0) * prior

    obs = np.log(S + 1e-4)
    logl = np.log(lags.astype(np.float64))
    trans = -((logl[:, None] - logl[None, :]) ** 2) / (2 * 0.08 ** 2)
    delta = obs[0].copy()
    back = np.zeros((len(starts), len(lags)), np.int32)
    for k in range(1, len(starts)):
        m = delta[:, None] + trans
        back[k] = np.argmax(m, axis=0)
        delta = m[back[k], np.arange(len(lags))] + obs[k]
    path = np.zeros(len(starts), np.int64)
    path[-1] = int(np.argmax(delta))
    for k in range(len(starts) - 1, 0, -1):
        path[k - 1] = back[k][path[k]]

    periods = lags[path].astype(np.float64)
    for k, j in enumerate(path):  # sub-frame period via parabolic interpolation
        if 0 < j < len(lags) - 1:
            y0, y1, y2 = S[k, j - 1], S[k, j], S[k, j + 1]
            den = y0 - 2 * y1 + y2
            if den < 0:
                periods[k] += 0.5 * (y0 - y2) / den
    periodicity = np.maximum(A[np.arange(len(starts)), path], 0.0)
    centres = np.array(starts, np.float64) + W / 2.0
    return centres, periods, periodicity


def track_beats(env, period, tightness=100.0):
    """Ellis (2007) DP beat tracker with a per-frame target period."""
    n = len(env)
    if n == 0:
        return np.zeros(0, np.int64)
    local = _gaussian_smooth(env, 2.0)
    score = np.zeros(n)
    back = np.full(n, -1, np.int64)
    cache = {}
    for t in range(n):
        pq = round(float(period[t]) * 4.0) / 4.0
        pen = cache.get(pq)
        if pen is None:
            dmin, dmax = int(round(pq / 2.0)), int(round(2.0 * pq))
            d = np.arange(dmax, dmin - 1, -1, dtype=np.float64)  # ascending tau
            pen = (dmin, dmax, -tightness * np.log(d / pq) ** 2)
            cache[pq] = pen
        dmin, dmax, txc = pen
        hi = t - dmin
        if hi < 1:
            score[t] = local[t]
            continue
        lo = t - dmax
        if lo < 0:
            txc = txc[-lo:]
            lo = 0
        cand = score[lo:hi + 1] + txc
        j = int(np.argmax(cand))
        score[t] = local[t] + cand[j]
        back[t] = lo + j

    # Last beat: the last local maximum of the cumulative score that is still
    # at least half the median of all maxima (librosa's rule).
    is_max = np.zeros(n, bool)
    is_max[1:-1] = (score[1:-1] > score[:-2]) & (score[1:-1] >= score[2:])
    maxima = np.flatnonzero(is_max)
    if len(maxima) == 0:
        last = int(np.argmax(score))
    else:
        med = np.median(score[maxima])
        good = maxima[score[maxima] >= 0.5 * med]
        last = int(good[-1]) if len(good) else int(maxima[-1])
    beats = [last]
    while back[beats[-1]] >= 0:
        beats.append(int(back[beats[-1]]))
    return np.array(beats[::-1], np.int64)


def _refine(beats, flux):
    """Snap to the strongest flux frame within ±2 frames, sub-frame precision."""
    out = np.zeros(len(beats))
    n = len(flux)
    for i, f in enumerate(beats):
        lo, hi = max(1, f - 2), min(n - 2, f + 2)
        g = f if hi < lo else lo + int(np.argmax(flux[lo:hi + 1]))
        delta = 0.0
        if 0 < g < n - 1:
            y0, y1, y2 = float(flux[g - 1]), float(flux[g]), float(flux[g + 1])
            den = y0 - 2 * y1 + y2
            if den < 0:
                delta = max(-0.5, min(0.5, 0.5 * (y0 - y2) / den))
        out[i] = (g + delta) * HOP / SR + _LATENCY
    return out


def detect(path, min_bpm=60.0, max_bpm=200.0, bpm=None, tightness=100.0, progress=None):
    """Beats of a 16 kHz mono file. Returns {beats:[{t, s, lf}], bpm, duration, tempo}."""
    duration = sf.info(path).frames / float(SR)
    flux, low = onset_envelope(path, progress)
    if len(flux) < int(2 * FPS):
        return {"beats": [], "bpm": 0.0, "duration": duration, "tempo": []}
    env = normalize_envelope(flux)
    if progress:
        progress(0.65, "Estimating tempo…")

    centres, periods, periodicity = local_tempo(env, min_bpm=min_bpm, max_bpm=max_bpm)
    if bpm and bpm > 0:
        # A fixed BPM means "this is music": follow it everywhere, no pulse gate.
        period = np.full(len(env), 60.0 * FPS / float(bpm))
        periods = np.full(len(centres), 60.0 * FPS / float(bpm))
    else:
        period = np.interp(np.arange(len(env)), centres, periods)
    if progress:
        progress(0.75, "Tracking beats…")
    frames = track_beats(env, period, tightness=tightness)
    if len(frames) == 0:
        return {"beats": [], "bpm": 0.0, "duration": duration, "tempo": []}

    # Gate: no beats where there is no music. Silent passages fail the activity
    # test; speech has plenty of onsets but no steady pulse, so it fails the
    # periodicity test (auto tempo only — a fixed BPM overrides it).
    activity = _moving_average(flux, int(2 * FPS))
    at_beats = activity[frames]
    keep = at_beats >= 0.2 * float(np.median(at_beats))
    if not (bpm and bpm > 0):
        pulse = np.interp(frames, centres, _median_filter(periodicity, 5))
        keep &= pulse >= MIN_PERIODICITY
    strength = env[frames]
    s_ref = float(np.percentile(strength, 95)) or 1.0
    # Trim weak leading/trailing beats (DP keeps marching through intros/outros).
    weak = strength < 0.1 * s_ref
    i0, i1 = 0, len(frames)
    while i0 < i1 and weak[i0]:
        i0 += 1
    while i1 > i0 and weak[i1 - 1]:
        i1 -= 1
    keep[:i0] = False
    keep[i1:] = False
    frames = frames[keep]
    if progress:
        progress(0.92, "Refining beats…")
    if len(frames) == 0:
        return {"beats": [], "bpm": 0.0, "duration": duration, "tempo": []}

    times = _refine(frames, flux)
    lf = np.array([low[max(0, f - 2):f + 3].sum() for f in frames])
    lf_ref = float(np.percentile(lf, 95)) or 1.0
    strength = env[frames]
    s_ref = float(np.percentile(strength, 95)) or 1.0
    ibi = np.diff(times)
    est_bpm = 60.0 / float(np.median(ibi)) if len(ibi) else 0.0

    beats = [{"t": round(float(t), 4),
              "s": round(min(1.0, float(s) / s_ref), 3),
              "lf": round(min(1.0, float(v) / lf_ref), 3)}
             for t, s, v in zip(times, strength, lf)]
    tempo = [{"t": round(float(c) / FPS, 2), "bpm": round(60.0 * FPS / float(p), 2)}
             for c, p in zip(centres, periods)]
    return {"beats": beats, "bpm": round(est_bpm, 2), "duration": round(duration, 3), "tempo": tempo}
