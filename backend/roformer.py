"""
Mel-Band RoFormer — inference-only model definition for vocal separation.

Adapted from lucidrains/BS-RoFormer (MIT) as shipped with Kimberley Jensen's
Mel-Band-Roformer-Vocal-Model, whose weights (KimberleyJSN/melbandroformer) are
MIT-licensed. Parameter names match the original so its checkpoint loads
unchanged. Dropped vs. the original: training losses, beartype, and the
librosa / rotary_embedding_torch dependencies (both re-implemented below,
output-identical to librosa 0.11 / rotary-embedding-torch 0.3.5).
"""

from functools import partial

import numpy as np
import torch
import torch.nn.functional as F
from einops import pack, rearrange, reduce, repeat, unpack
from torch import nn


# ── librosa.filters.mel (Slaney scale + Slaney norm), for the band layout ──

def _hz_to_mel(f):
    f = np.asanyarray(f, dtype=np.float64)
    f_sp = 200.0 / 3
    mels = f / f_sp
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    log_t = f >= min_log_hz
    mels = np.where(log_t, min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep, mels)
    return mels


def _mel_to_hz(m):
    m = np.asanyarray(m, dtype=np.float64)
    f_sp = 200.0 / 3
    freqs = f_sp * m
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    log_t = m >= min_log_mel
    return np.where(log_t, min_log_hz * np.exp(logstep * (m - min_log_mel)), freqs)


def mel_filter_bank(sr, n_fft, n_mels):
    fftfreqs = np.fft.rfftfreq(n=n_fft, d=1.0 / sr)
    mel_f = _mel_to_hz(np.linspace(_hz_to_mel(0.0), _hz_to_mel(sr / 2.0), n_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = np.subtract.outer(mel_f, fftfreqs)
    weights = np.zeros((n_mels, 1 + n_fft // 2), dtype=np.float32)
    for i in range(n_mels):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0, np.minimum(lower, upper))
    enorm = 2.0 / (mel_f[2:n_mels + 2] - mel_f[:n_mels])
    weights *= enorm[:, np.newaxis]
    return weights


# ── Rotary position embedding (rotary-embedding-torch, 'lang' frequencies) ──

class RotaryEmbedding(nn.Module):
    """rotate_half() as a pair-swapping gather with a signed sine — the same
    numbers as the original's stack/rearrange, ~3x faster on MPS."""

    def __init__(self, dim, theta=10000):
        super().__init__()
        freqs = 1.0 / (theta ** (torch.arange(0, dim, 2)[:(dim // 2)].float() / dim))
        self.freqs = nn.Parameter(freqs, requires_grad=False)
        self._cache = {}

    def _tables(self, seq_len, device, dtype):
        key = (seq_len, device, dtype)
        hit = self._cache.get(key)
        if hit is None:
            pos = torch.arange(seq_len, device=device, dtype=dtype)
            freqs = torch.einsum("i, f -> i f", pos.type(self.freqs.dtype), self.freqs.to(device))
            freqs = repeat(freqs, "... n -> ... (n r)", r=2).to(dtype)
            dim = freqs.shape[-1]
            perm = torch.arange(dim, device=device).view(-1, 2).flip(-1).reshape(-1)
            sign = torch.tensor([-1.0, 1.0], device=device, dtype=dtype).repeat(dim // 2)
            hit = self._cache[key] = (freqs.cos(), freqs.sin() * sign, perm)
        return hit

    def rotate_queries_or_keys(self, t):
        """t [..., seq, dim] (sequence on dim -2)."""
        cos, sin, perm = self._tables(t.shape[-2], t.device, t.dtype)
        return t * cos + t[..., perm] * sin


# ── Blocks ──

class RMSNorm(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.scale = dim ** 0.5
        self.gamma = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        # Normalised in fp32: in a half-precision model the squared norm of a
        # 384-wide vector can overflow fp16.
        return (F.normalize(x.float(), dim=-1) * self.scale).to(x.dtype) * self.gamma


class FeedForward(nn.Module):
    def __init__(self, dim, mult=4, dropout=0.0):
        super().__init__()
        dim_inner = int(dim * mult)
        self.net = nn.Sequential(
            RMSNorm(dim), nn.Linear(dim, dim_inner), nn.GELU(), nn.Dropout(dropout),
            nn.Linear(dim_inner, dim), nn.Dropout(dropout))

    def forward(self, x):
        return self.net(x)


class Attention(nn.Module):
    def __init__(self, dim, heads=8, dim_head=64, dropout=0.0, rotary_embed=None):
        super().__init__()
        self.heads = heads
        dim_inner = heads * dim_head
        self.rotary_embed = rotary_embed
        self.norm = RMSNorm(dim)
        self.to_qkv = nn.Linear(dim, dim_inner * 3, bias=False)
        self.to_gates = nn.Linear(dim, heads)
        self.to_out = nn.Sequential(nn.Linear(dim_inner, dim, bias=False), nn.Dropout(dropout))

    def forward(self, x):
        x = self.norm(x)
        qkv = rearrange(self.to_qkv(x), "b n (qkv h d) -> qkv b h n d", qkv=3, h=self.heads)
        v = qkv[2]
        if self.rotary_embed is not None:
            q, k = self.rotary_embed.rotate_queries_or_keys(qkv[:2])  # q and k in one pass
        else:
            q, k = qkv[0], qkv[1]
        out = F.scaled_dot_product_attention(q, k, v)
        gates = self.to_gates(x)
        out = out * rearrange(gates, "b n h -> b h n 1").sigmoid()
        out = rearrange(out, "b h n d -> b n (h d)")
        return self.to_out(out)


class Transformer(nn.Module):
    def __init__(self, *, dim, depth, dim_head=64, heads=8, attn_dropout=0.0, ff_dropout=0.0,
                 ff_mult=4, norm_output=True, rotary_embed=None, flash_attn=True):
        super().__init__()
        self.layers = nn.ModuleList([
            nn.ModuleList([
                Attention(dim=dim, dim_head=dim_head, heads=heads, dropout=attn_dropout,
                          rotary_embed=rotary_embed),
                FeedForward(dim=dim, mult=ff_mult, dropout=ff_dropout),
            ]) for _ in range(depth)])
        self.norm = RMSNorm(dim) if norm_output else nn.Identity()

    def forward(self, x):
        for attn, ff in self.layers:
            x = attn(x) + x
            x = ff(x) + x
        return self.norm(x)


class BandSplit(nn.Module):
    def __init__(self, dim, dim_inputs):
        super().__init__()
        self.dim_inputs = dim_inputs
        self.bounds = [(sum(dim_inputs[:i]), sum(dim_inputs[:i + 1])) for i in range(len(dim_inputs))]
        self.to_features = nn.ModuleList([
            nn.Sequential(RMSNorm(dim_in), nn.Linear(dim_in, dim)) for dim_in in dim_inputs])

    def forward(self, x):
        # Slices, not x.split(): the ONNX Split's 60-entry size tensor is rejected by
        # ONNX Runtime 1.23 ("Cannot parse data from external tensors"), which sent
        # DirectML separation back to the CPU. Same views, same result.
        return torch.stack([f(x[..., a:b]) for (a, b), f in zip(self.bounds, self.to_features)], dim=-2)


def _mlp(dim_in, dim_out, dim_hidden=None, depth=1, activation=nn.Tanh):
    dim_hidden = dim_hidden if dim_hidden is not None else dim_in
    dims = (dim_in, *((dim_hidden,) * depth), dim_out)
    net = []
    for ind, (a, b) in enumerate(zip(dims[:-1], dims[1:])):
        net.append(nn.Linear(a, b))
        if ind != len(dims) - 2:
            net.append(activation())
    return nn.Sequential(*net)


class MaskEstimator(nn.Module):
    def __init__(self, dim, dim_inputs, depth, mlp_expansion_factor=4):
        super().__init__()
        self.dim_inputs = dim_inputs
        dim_hidden = dim * mlp_expansion_factor
        self.to_freqs = nn.ModuleList([
            nn.Sequential(_mlp(dim, dim_in * 2, dim_hidden=dim_hidden, depth=depth), nn.GLU(dim=-1))
            for dim_in in dim_inputs])

    def forward(self, x):
        x = x.unbind(dim=-2)
        return torch.cat([mlp(band) for band, mlp in zip(x, self.to_freqs)], dim=-1)


class MelBandRoformer(nn.Module):
    """Mel-Band RoFormer. forward(audio [b, s, t]) -> separated stem(s) [b, s, t]."""

    def __init__(self, dim, *, depth, stereo=False, num_stems=1, time_transformer_depth=2,
                 freq_transformer_depth=2, num_bands=60, dim_head=64, heads=8, attn_dropout=0.1,
                 ff_dropout=0.1, flash_attn=True, dim_freqs_in=1025, sample_rate=44100,
                 stft_n_fft=2048, stft_hop_length=512, stft_win_length=2048, stft_normalized=False,
                 mask_estimator_depth=1, **_training_only):
        super().__init__()
        self.stereo = stereo
        self.audio_channels = 2 if stereo else 1
        self.num_stems = num_stems

        kw = dict(dim=dim, heads=heads, dim_head=dim_head, attn_dropout=attn_dropout,
                  ff_dropout=ff_dropout, flash_attn=flash_attn)
        time_rotary = RotaryEmbedding(dim=dim_head)
        freq_rotary = RotaryEmbedding(dim=dim_head)
        self.layers = nn.ModuleList([
            nn.ModuleList([
                Transformer(depth=time_transformer_depth, rotary_embed=time_rotary, **kw),
                Transformer(depth=freq_transformer_depth, rotary_embed=freq_rotary, **kw),
            ]) for _ in range(depth)])

        self.stft_window_fn = partial(torch.hann_window, stft_win_length)
        self.stft_kwargs = dict(n_fft=stft_n_fft, hop_length=stft_hop_length,
                                win_length=stft_win_length, normalized=stft_normalized)
        freqs = stft_n_fft // 2 + 1

        mel = torch.from_numpy(mel_filter_bank(sample_rate, stft_n_fft, num_bands))
        # As in the original: the first and last bins are forced into the bands.
        mel[0][0] = 1.0
        mel[-1, -1] = 1.0
        freqs_per_band = mel > 0
        assert freqs_per_band.any(dim=0).all(), "all frequencies need to be covered by the bands"

        freq_indices = repeat(torch.arange(freqs), "f -> b f", b=num_bands)[freqs_per_band]
        if stereo:
            freq_indices = repeat(freq_indices, "f -> f s", s=2)
            freq_indices = freq_indices * 2 + torch.arange(2)
            freq_indices = rearrange(freq_indices, "f s -> (f s)")
        self.register_buffer("freq_indices", freq_indices, persistent=False)
        self.register_buffer("freqs_per_band", freqs_per_band, persistent=False)
        num_freqs_per_band = reduce(freqs_per_band, "b f -> b", "sum")
        num_bands_per_freq = reduce(freqs_per_band, "b f -> f", "sum")
        self.register_buffer("num_freqs_per_band", num_freqs_per_band, persistent=False)
        self.register_buffer("num_bands_per_freq", num_bands_per_freq, persistent=False)

        dims_with_complex = tuple(2 * f * self.audio_channels for f in num_freqs_per_band.tolist())
        self.band_split = BandSplit(dim=dim, dim_inputs=dims_with_complex)
        self.mask_estimators = nn.ModuleList([
            MaskEstimator(dim=dim, dim_inputs=dims_with_complex, depth=mask_estimator_depth)
            for _ in range(num_stems)])

    # The spectral parts (STFT, scatter of complex masks, iSTFT) and the
    # transformer core are split so a caller can run them on different devices:
    # MPS has no complex scatter, so on Apple Silicon only core() runs on the GPU
    # and spectrum()/reconstruct() take CPU tensors (they move the small index
    # buffers to the input's device).

    def spectrum(self, raw_audio):
        """audio [b, s, t] -> (stft [b, (f s), t, 2], band features [b, t, (f c)])."""
        if raw_audio.ndim == 2:
            raw_audio = rearrange(raw_audio, "b t -> b 1 t")
        batch = raw_audio.shape[0]
        raw_audio, ps = pack([raw_audio], "* t")
        window = self.stft_window_fn(device=raw_audio.device)
        stft = torch.stft(raw_audio, **self.stft_kwargs, window=window, return_complex=True)
        stft = torch.view_as_real(stft)
        stft = unpack(stft, ps, "* f t c")[0]
        stft = rearrange(stft, "b s f t c -> b (f s) t c")
        batch_arange = torch.arange(batch, device=stft.device)[..., None]
        x = stft[batch_arange, self.freq_indices.to(stft.device)]
        x = rearrange(x, "b f t c -> b t (f c)")
        return stft, x

    def core(self, x):
        """band features [b, t, (f c)] -> masks [b, n, t, (f c)]."""
        x = self.band_split(x)
        for time_transformer, freq_transformer in self.layers:
            x = rearrange(x, "b t f d -> b f t d")
            x, ps = pack([x], "* t d")
            x = time_transformer(x)
            x, = unpack(x, ps, "* t d")
            x = rearrange(x, "b f t d -> b t f d")
            x, ps = pack([x], "* f d")
            x = freq_transformer(x)
            x, = unpack(x, ps, "* f d")
        return torch.stack([fn(x) for fn in self.mask_estimators], dim=1)

    def reconstruct(self, stft, masks, length=None):
        """Apply the band masks (averaged where bands overlap) and invert the STFT."""
        batch, channels = stft.shape[0], self.audio_channels
        num_stems = masks.shape[1]
        masks = rearrange(masks, "b n t (f c) -> b n f t c", c=2)
        stft = torch.view_as_complex(rearrange(stft, "b f t c -> b 1 f t c").contiguous())
        masks = torch.view_as_complex(masks.contiguous()).type(stft.dtype)
        freq_indices = self.freq_indices.to(stft.device)
        scatter_indices = repeat(freq_indices, "f -> b n f t", b=batch, n=num_stems, t=stft.shape[-1])
        expanded = repeat(stft, "b 1 ... -> b n ...", n=num_stems)
        masks_summed = torch.zeros_like(expanded).scatter_add_(2, scatter_indices, masks)
        denom = repeat(self.num_bands_per_freq.to(stft.device), "f -> (f r) 1", r=channels)
        stft = stft * (masks_summed / denom.clamp(min=1e-8))
        stft = rearrange(stft, "b n (f s) t -> (b n s) f t", s=channels)
        window = self.stft_window_fn(device=stft.device)
        audio = torch.istft(stft, **self.stft_kwargs, window=window, return_complex=False, length=length)
        audio = rearrange(audio, "(b n s) t -> b n s t", b=batch, s=channels, n=num_stems)
        if num_stems == 1:
            audio = rearrange(audio, "b 1 s t -> b s t")
        return audio

    def forward(self, raw_audio):
        stft, x = self.spectrum(raw_audio)
        return self.reconstruct(stft, self.core(x))
