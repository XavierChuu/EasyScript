"""
Demucs subprocess wrapper.

torchaudio 2.11+ routes all I/O through torchcodec, which needs FFmpeg
shared DLLs that aren't present on standard Windows installs. We patch
torchaudio.load/save/info to use soundfile instead before invoking Demucs.

Usage:
    python _demucs_runner.py <output_dir> <audio_path>
"""

import sys


def _install_torchaudio_soundfile_patches():
    """Replace torchaudio I/O functions with soundfile-backed equivalents."""
    import torchaudio
    import soundfile as sf
    import torch
    import numpy as np

    def _load(path, *args, **kwargs):
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        # soundfile gives (frames, channels); torchaudio expects (channels, frames)
        waveform = torch.from_numpy(np.ascontiguousarray(data.T))
        return waveform, sr

    def _save(path, waveform, sample_rate, *args, **kwargs):
        if hasattr(waveform, "detach"):
            waveform = waveform.detach().cpu().numpy()
        # torchaudio gives (channels, frames); soundfile wants (frames, channels)
        if waveform.ndim == 2:
            data = waveform.T
        else:
            data = waveform
        sf.write(str(path), data, int(sample_rate))

    def _info(path, *args, **kwargs):
        i = sf.info(str(path))
        from collections import namedtuple
        AMD = getattr(
            torchaudio, "AudioMetaData",
            namedtuple("AudioMetaData",
                       ["sample_rate", "num_frames", "num_channels",
                        "bits_per_sample", "encoding"]),
        )
        bps = 16
        try:
            bps = int(i.subtype.split("_")[-1])
        except Exception:
            pass
        return AMD(
            sample_rate=i.samplerate, num_frames=i.frames,
            num_channels=i.channels, bits_per_sample=bps, encoding=i.subtype,
        )

    torchaudio.load = _load
    torchaudio.save = _save
    torchaudio.info = _info
    if not hasattr(torchaudio, "list_audio_backends"):
        torchaudio.list_audio_backends = lambda: ["soundfile"]


def run_demucs_main(output_dir, audio_path):
    """In-process entry point (callable from bundled app)."""
    _install_torchaudio_soundfile_patches()
    from demucs.separate import main as demucs_main
    demucs_main([
        "--two-stems=vocals",
        "-n", "htdemucs",
        "-o", output_dir,
        audio_path,
    ])


def main():
    if len(sys.argv) < 3:
        print("Usage: _demucs_runner.py <output_dir> <audio_path>", file=sys.stderr)
        sys.exit(2)
    run_demucs_main(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
