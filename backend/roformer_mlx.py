"""
Mel-Band RoFormer transformer core on Apple Silicon (MLX, Metal).

Same computation as roformer.MelBandRoformer.core(), built on MLX's fused
kernels (rms_norm, rope, scaled_dot_product_attention): on an M-series GPU it
runs several times faster than PyTorch MPS, which dispatches the many small
element-wise ops of this model one by one. Weights come from the same PyTorch
checkpoint; the STFT and mask application stay in roformer.py on the CPU.
"""

import mlx.core as mx
import mlx.nn as nn


def _rms(x, gamma):
    # roformer.RMSNorm: normalize(x) * sqrt(d) * gamma == x / rms(x) * gamma
    return mx.fast.rms_norm(x, gamma, 1e-12)


class MLXCore:
    def __init__(self, state, cfg, dtype=mx.float16):
        def w(name):
            return mx.array(state[name].float().numpy()).astype(dtype)

        def norm(name):
            # Same dtype as the activations: an fp32 weight would promote every
            # following matmul to fp32. rms_norm accumulates in fp32 regardless.
            return w(name)

        self.dtype = dtype
        self.heads = cfg["heads"]
        self.dim_head = cfg["dim_head"]
        self.scale = self.dim_head ** -0.5

        def transformer(prefix, depth):
            layers = []
            for i in range(depth):
                a = f"{prefix}.layers.{i}.0."
                f = f"{prefix}.layers.{i}.1.net."
                layers.append(dict(
                    attn_norm=norm(a + "norm.gamma"),
                    qkv=w(a + "to_qkv.weight").T,
                    gates_w=w(a + "to_gates.weight").T, gates_b=w(a + "to_gates.bias"),
                    out=w(a + "to_out.0.weight").T,
                    ff_norm=norm(f + "0.gamma"),
                    ff1_w=w(f + "1.weight").T, ff1_b=w(f + "1.bias"),
                    ff2_w=w(f + "4.weight").T, ff2_b=w(f + "4.bias"),
                ))
            return dict(layers=layers, norm=norm(f"{prefix}.norm.gamma"))

        self.layers = [
            (transformer(f"layers.{d}.0", cfg["time_transformer_depth"]),
             transformer(f"layers.{d}.1", cfg["freq_transformer_depth"]))
            for d in range(cfg["depth"])]

        nb = cfg["num_bands"]
        self.band_dims = [state[f"band_split.to_features.{b}.0.gamma"].shape[0] for b in range(nb)]
        self.band_split = [
            (norm(f"band_split.to_features.{b}.0.gamma"),
             w(f"band_split.to_features.{b}.1.weight").T, w(f"band_split.to_features.{b}.1.bias"))
            for b in range(nb)]
        depth = cfg["mask_estimator_depth"]
        self.mask_estimators = []
        for s in range(cfg["num_stems"]):
            bands = []
            for b in range(nb):
                p = f"mask_estimators.{s}.to_freqs.{b}.0."
                # nn.Sequential(Linear, Tanh, Linear, Tanh, ..., Linear): linears at even indices
                bands.append([(w(p + f"{2 * i}.weight").T, w(p + f"{2 * i}.bias"))
                              for i in range(depth + 1)])
            self.mask_estimators.append(bands)
        mx.eval(self._params())

    def _params(self):
        out = []
        for tt in self.layers:
            for t in tt:
                out.append(t["norm"])
                for layer in t["layers"]:
                    out.extend(layer.values())
        for g, wt, b in self.band_split:
            out += [g, wt, b]
        for bands in self.mask_estimators:
            for mlp in bands:
                for wt, b in mlp:
                    out += [wt, b]
        return out

    def _transformer(self, t, x):
        h, dh = self.heads, self.dim_head
        for p in t["layers"]:
            y = _rms(x, p["attn_norm"])
            b, n, _ = y.shape
            qkv = (y @ p["qkv"]).reshape(b, n, 3, h, dh).transpose(2, 0, 3, 1, 4)
            qk = mx.fast.rope(qkv[:2], dh, traditional=True, base=10000.0, scale=1.0, offset=0)
            o = mx.fast.scaled_dot_product_attention(qk[0], qk[1], qkv[2], scale=self.scale)
            gates = mx.sigmoid(y @ p["gates_w"] + p["gates_b"])           # b n h
            o = o * gates.transpose(0, 2, 1)[..., None]
            o = o.transpose(0, 2, 1, 3).reshape(b, n, h * dh)
            x = o @ p["out"] + x
            y = _rms(x, p["ff_norm"])
            y = nn.gelu(y @ p["ff1_w"] + p["ff1_b"])                     # exact (erf), as torch
            x = y @ p["ff2_w"] + p["ff2_b"] + x
        return _rms(x, t["norm"])

    def __call__(self, feats):
        """feats: numpy [b, t, (f c)] float32 -> masks numpy [b, n, t, (f c)] float32."""
        import numpy as np

        x = mx.array(feats).astype(self.dtype)
        parts, at = [], 0
        for (g, wt, bias), d in zip(self.band_split, self.band_dims):
            parts.append(_rms(x[..., at:at + d], g) @ wt + bias)
            at += d
        x = mx.stack(parts, axis=-2)                                 # b t f d
        b, t, f, d = x.shape
        for time_t, freq_t in self.layers:
            x = x.transpose(0, 2, 1, 3).reshape(b * f, t, d)         # time attention
            x = self._transformer(time_t, x)
            x = x.reshape(b, f, t, d).transpose(0, 2, 1, 3).reshape(b * t, f, d)  # freq attention
            x = self._transformer(freq_t, x)
            x = x.reshape(b, t, f, d)
        stems = []
        for bands in self.mask_estimators:
            outs = []
            for i, mlp in enumerate(bands):
                y = x[:, :, i]
                for j, (wt, bias) in enumerate(mlp):
                    y = y @ wt + bias
                    if j < len(mlp) - 1:
                        y = mx.tanh(y)
                a, gate = mx.split(y, 2, axis=-1)                     # nn.GLU
                outs.append(a * mx.sigmoid(gate))
            stems.append(mx.concatenate(outs, axis=-1))
        masks = mx.stack(stems, axis=1).astype(mx.float32)
        mx.eval(masks)
        return np.array(masks)

