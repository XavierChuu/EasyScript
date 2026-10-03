"""
Export the community-1 speaker-embedding ResNet to ONNX.

The diarizer runs this ResNet (≈98 % of diarization time) through ONNX
Runtime — DirectML on any Windows GPU, else CPU — instead of torch on CPU.
fbank extraction stays in torch. Run by the build scripts after the model is
fetched; needs the `onnx` package (build time only).

Usage: python tools/export_embedding_onnx.py [model_dir]
"""

import os
import sys

import numpy as np
import torch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from diarizer import ONNX_NAME, bundled_model_dir  # noqa: E402


class _ResNet(torch.nn.Module):
    def __init__(self, resnet):
        super().__init__()
        self.resnet = resnet

    def forward(self, fbank, weights):
        return self.resnet(fbank, weights=weights)[1]


def main():
    model_dir = sys.argv[1] if len(sys.argv) > 1 else bundled_model_dir()
    if not model_dir:
        raise SystemExit("community-1 model not found (backend/models/speaker-diarization-community-1)")
    from pyannote.audio import Pipeline
    pipe = Pipeline.from_pretrained(model_dir)
    model = pipe._embedding.model_
    model.eval()

    x = torch.randn(4, 1, 160000) * 0.1
    w = torch.rand(4, 589)
    with torch.no_grad():
        fb = model.compute_fbank(x)
        ref = model.resnet(fb, weights=w)[1].numpy()

    out_path = os.path.join(model_dir, "embedding", ONNX_NAME)
    torch.onnx.export(_ResNet(model.resnet), (fb.clone(), w.clone()), out_path,
                      input_names=["fbank", "weights"], output_names=["embedding"],
                      opset_version=17, dynamo=False,
                      dynamic_axes={"fbank": {0: "batch", 1: "frames"},
                                    "weights": {0: "batch", 1: "weight_frames"},
                                    "embedding": {0: "batch"}})

    import onnxruntime as ort
    sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    got = sess.run(None, {"fbank": fb.numpy(), "weights": w.numpy()})[0]
    cos = (got * ref).sum(1) / (np.linalg.norm(got, axis=1) * np.linalg.norm(ref, axis=1))
    if cos.min() < 0.9999:
        os.remove(out_path)
        raise SystemExit(f"ONNX export mismatch (min cosine {cos.min():.6f})")
    print(f"exported {out_path} ({os.path.getsize(out_path) / 1e6:.1f} MB, cosine {cos.min():.6f})")


if __name__ == "__main__":
    main()
