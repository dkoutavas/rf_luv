#!/usr/bin/env python3
"""
bandlimit.py — measure the effective upper audio bandwidth of a segment.

A Bluetooth speaker (SBC or AAC over A2DP) imposes a distinct high-frequency
roll-off that live room audio through the camera mic does not have. If a flagged
"spirit voice" has a hard upper shelf that the surrounding room tone lacks, the
voice was rendered by a speaker, not spoken in the room.

Method: a Welch PSD on the real audio, then the highest frequency whose smoothed
power stays above the noise floor by a margin. numpy + stdlib only.
"""

import argparse
import json
import sys

import numpy as np


def welch_psd_real(x: np.ndarray, fs: float, nperseg: int = 4096,
                   overlap: float = 0.5):
    """Segment-averaged PSD of a real signal. Returns (freqs, psd_linear)."""
    x = np.asarray(x, dtype=np.float64)
    if x.size < nperseg:
        nperseg = max(256, 1 << int(np.log2(max(x.size, 256))))
    step = max(1, int(nperseg * (1.0 - overlap)))
    win = np.hanning(nperseg)
    norm = np.sum(win ** 2) * fs
    acc = None
    count = 0
    for start in range(0, x.size - nperseg + 1, step):
        seg = x[start:start + nperseg] * win
        p = (np.abs(np.fft.rfft(seg)) ** 2) / norm
        acc = p if acc is None else acc + p
        count += 1
    if acc is None:                       # signal shorter than one segment
        seg = np.zeros(nperseg)
        seg[: x.size] = x
        acc = (np.abs(np.fft.rfft(seg * win)) ** 2) / norm
        count = 1
    psd = acc / count
    freqs = np.fft.rfftfreq(nperseg, 1.0 / fs)
    return freqs, psd


def upper_bandwidth(x: np.ndarray, fs: float, drop_db: float = 25.0,
                    margin_db: float = 6.0) -> dict:
    """Effective upper edge: highest frequency still within drop_db of the in-band peak.

    The threshold is anchored to the in-band peak, not the noise floor, so
    stopband ripple near Nyquist cannot masquerade as occupied bandwidth. A hard
    shelf (Bluetooth SBC/AAC) shows up as an upper edge well below Nyquist; live
    room audio runs to the top of the band.
    """
    freqs, psd = welch_psd_real(x, fs)
    psd_db = 10.0 * np.log10(psd + 1e-20)
    # Smooth to stabilize the edge search.
    w = max(1, psd_db.size // 128)
    smooth = np.convolve(psd_db, np.ones(w) / w, mode="same")
    in_band_peak = float(np.max(smooth))
    floor = float(np.median(np.sort(smooth)[: max(1, smooth.size // 4)]))
    # Anchor to the peak; also require a real margin over the floor so a flat
    # near-silent segment does not report spurious bandwidth.
    thresh = max(in_band_peak - drop_db, floor + margin_db)
    above = np.where(smooth > thresh)[0]
    upper = float(freqs[above[-1]]) if above.size else 0.0
    return {
        "upper_bw_hz": int(round(upper)),
        "shelf_depth_db": round(in_band_peak - floor, 1),
        "noise_floor_db": round(floor, 1),
        "nyquist_hz": int(fs // 2),
    }


def main(argv=None):
    from audio_io import load  # local import so --help needs no ffmpeg
    p = argparse.ArgumentParser(description="Measure effective upper audio bandwidth")
    p.add_argument("input", help="audio/video file or URL")
    p.add_argument("--fs", type=int, default=48000)
    p.add_argument("--start", type=float, default=None, help="segment start (s)")
    p.add_argument("--dur", type=float, default=None, help="segment duration (s)")
    p.add_argument("--out-dir", default="/data/rf_luv/ghost/forensics")
    args = p.parse_args(argv)
    x = load(args.input, args.out_dir, fs=args.fs, start_s=args.start, dur_s=args.dur)
    print(json.dumps(upper_bandwidth(x, args.fs), indent=2))
    return 0


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
