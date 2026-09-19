#!/usr/bin/env python3
"""
emf_sync.py — line up EMF-meter LED events with RF activity in the audio.

When an EMF meter lights up on camera, log the timestamp. This tool checks the
audio at that moment for the signature of a nearby transmitter: an impulsive
burst (a PTT click) or the 217 Hz GSM TDMA buzz a phone induces. A coincidence
means the "energy" was a radio, not a ghost. numpy + stdlib only.
"""

import argparse
import json
import sys

import numpy as np


def envelope(x: np.ndarray, fs: float, smooth_ms: float = 5.0) -> np.ndarray:
    e = np.abs(np.asarray(x, dtype=np.float64))
    w = max(1, int(fs * smooth_ms / 1000.0))
    return np.convolve(e, np.ones(w) / w, mode="same")


def detect_bursts(x: np.ndarray, fs: float, thresh_k: float = 5.0,
                  min_gap_s: float = 0.2) -> list:
    """Onset times of impulsive bursts (robust median+MAD threshold on the envelope)."""
    env = envelope(x, fs)
    med = np.median(env)
    mad = np.median(np.abs(env - med)) + 1e-9
    peak = float(np.max(env))
    if peak <= med * 3:                       # nothing impulsive in this segment
        return []
    # Anchor to the dynamic range so a near-constant noise floor (tiny MAD) does
    # not spray false onsets; the MAD term is a floor for structured audio.
    thr = med + max(thresh_k * mad, 0.30 * (peak - med))
    above = env > thr
    onsets, last = [], -1e9
    rising = np.where(above[1:] & ~above[:-1])[0] + 1
    for i in rising:
        t = i / fs
        if t - last >= min_gap_s:
            onsets.append(round(t, 3))
            last = t
    return onsets


def gsm_buzz_score(x: np.ndarray, fs: float, target_hz: float = 217.0) -> float:
    """Peak-to-median ratio of the envelope spectrum near 217 Hz (GSM TDMA buzz)."""
    env = np.abs(np.asarray(x, dtype=np.float64))
    env = env - np.mean(env)
    E = np.abs(np.fft.rfft(env * np.hanning(env.size)))
    f = np.fft.rfftfreq(env.size, 1.0 / fs)
    band = (f > target_hz - 12) & (f < target_hz + 12)
    if not band.any():
        return 0.0
    return float(E[band].max() / (np.median(E) + 1e-9))


def sync(emf_times: list, bursts: list, tol_s: float = 0.5) -> list:
    """Match each logged EMF event to the nearest audio burst within tol_s."""
    out = []
    for t in emf_times:
        near = sorted((abs(b - t), b) for b in bursts)
        hit = near[0][1] if near and near[0][0] <= tol_s else None
        out.append({"emf_t": t, "audio_burst": hit, "coincident": hit is not None})
    return out


def main(argv=None):
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from audio_io import load
    p = argparse.ArgumentParser(description="EMF-event vs audio-burst correlation")
    p.add_argument("input")
    p.add_argument("--fs", type=int, default=48000)
    p.add_argument("--emf-times", default="", help="comma-separated EMF LED timestamps (s)")
    p.add_argument("--tol", type=float, default=0.5)
    p.add_argument("--out-dir", default="/data/rf_luv/ghost/forensics")
    args = p.parse_args(argv)
    x = load(args.input, args.out_dir, fs=args.fs)
    bursts = detect_bursts(x, args.fs)
    emf = [float(t) for t in args.emf_times.split(",") if t.strip()]
    print(json.dumps({
        "bursts": bursts,
        "gsm_buzz_score": round(gsm_buzz_score(x, args.fs), 2),
        "matches": sync(emf, bursts, args.tol) if emf else [],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
