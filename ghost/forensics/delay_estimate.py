#!/usr/bin/env python3
"""
delay_estimate.py — measure the slapback echo on a "spirit voice" segment.

The channel's voices sound "creepy" largely because of a short slapback delay
(a single-tap echo) plus reverb. A slapback that is the SAME across episodes is a
plugin setting, not a haunting. This tool measures the tap in milliseconds and its
feedback from a voice segment, using autocorrelation with a cepstrum cross-check.
numpy + stdlib only.
"""

import argparse
import json
import sys

import numpy as np


def _autocorr_delay(x, fs, lo_ms, hi_ms):
    """First strong autocorrelation peak in the [lo_ms, hi_ms] lag window."""
    x = np.asarray(x, dtype=np.float64)
    x = x - np.mean(x)
    n = x.size
    # Full autocorrelation via FFT, kept for positive lags only.
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    X = np.fft.rfft(x, nfft)
    ac = np.fft.irfft(np.abs(X) ** 2, nfft)[:n]
    ac0 = ac[0] if ac[0] != 0 else 1.0
    ac = ac / ac0
    lo = max(1, int(fs * lo_ms / 1000.0))
    hi = min(n - 1, int(fs * hi_ms / 1000.0))
    if hi <= lo:
        return None, 0.0
    seg = ac[lo:hi]
    k = int(np.argmax(seg))
    return (lo + k) / fs * 1000.0, float(seg[k])


def _cepstrum_delay(x, fs, lo_ms, hi_ms):
    """Echo quefrency from the real cepstrum, as a cross-check."""
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    spec = np.abs(np.fft.rfft(x * np.hanning(n)))
    ceps = np.fft.irfft(np.log(spec + 1e-12))
    lo = max(1, int(fs * lo_ms / 1000.0))
    hi = min(ceps.size - 1, int(fs * hi_ms / 1000.0))
    if hi <= lo:
        return None
    k = int(np.argmax(np.abs(ceps[lo:hi])))
    return (lo + k) / fs * 1000.0


def estimate_delay(x: np.ndarray, fs: float, lo_ms: float = 20.0,
                   hi_ms: float = 200.0) -> dict:
    """Estimate slapback delay (ms) and feedback from a voice segment."""
    delay_ms, feedback = _autocorr_delay(x, fs, lo_ms, hi_ms)
    ceps_ms = _cepstrum_delay(x, fs, lo_ms, hi_ms)
    agree = (delay_ms is not None and ceps_ms is not None
             and abs(delay_ms - ceps_ms) <= 5.0)
    return {
        "delay_ms": round(delay_ms, 1) if delay_ms is not None else None,
        "feedback": round(feedback, 3),
        "cepstrum_ms": round(ceps_ms, 1) if ceps_ms is not None else None,
        "methods_agree": bool(agree),
    }


def main(argv=None):
    from audio_io import load
    p = argparse.ArgumentParser(description="Measure slapback delay on a segment")
    p.add_argument("input", help="audio/video file or URL")
    p.add_argument("--fs", type=int, default=48000)
    p.add_argument("--start", type=float, default=None)
    p.add_argument("--dur", type=float, default=None)
    p.add_argument("--lo-ms", type=float, default=20.0)
    p.add_argument("--hi-ms", type=float, default=200.0)
    p.add_argument("--out-dir", default="/data/rf_luv/ghost/forensics")
    args = p.parse_args(argv)
    x = load(args.input, args.out_dir, fs=args.fs, start_s=args.start, dur_s=args.dur)
    print(json.dumps(estimate_delay(x, args.fs, args.lo_ms, args.hi_ms), indent=2))
    return 0


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
