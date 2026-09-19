#!/usr/bin/env python3
"""
spectrogram.py — time-frequency view of a voice segment vs room tone.

Renders an STFT as a terminal heatmap and a grayscale PNG (stdlib zlib, no
matplotlib), and reports a high-band energy ratio. A "voice" whose spectrogram
differs sharply from the surrounding room tone (a hard high shelf, a burst that
is not in the room) did not originate in the room. numpy + stdlib only.
"""

import argparse
import json
import struct
import sys
import zlib

import numpy as np

_RAMP = " .:-=+*#%@"


def stft(x: np.ndarray, fs: float, nperseg: int = 1024, overlap: float = 0.5):
    """Short-time Fourier transform. Returns (freqs, times, mag) with mag = freq x time."""
    x = np.asarray(x, dtype=np.float64)
    if x.size < nperseg:
        nperseg = max(256, 1 << int(np.log2(max(x.size, 256))))
    step = max(1, int(nperseg * (1.0 - overlap)))
    win = np.hanning(nperseg)
    frames = [np.abs(np.fft.rfft(x[s:s + nperseg] * win))
              for s in range(0, x.size - nperseg + 1, step)]
    if not frames:
        seg = np.zeros(nperseg); seg[: x.size] = x
        frames = [np.abs(np.fft.rfft(seg * win))]
    mag = np.array(frames).T
    freqs = np.fft.rfftfreq(nperseg, 1.0 / fs)
    times = np.arange(len(frames)) * step / fs
    return freqs, times, mag


def _to_u8(mag: np.ndarray) -> np.ndarray:
    """Log-scale a magnitude array to 0..255, percentile-clipped."""
    db = 20.0 * np.log10(mag + 1e-9)
    lo, hi = np.percentile(db, 5), np.percentile(db, 99.5)
    if hi <= lo:
        hi = lo + 1.0
    u = np.clip((db - lo) / (hi - lo), 0, 1)
    return (u * 255).astype(np.uint8)


def save_png(mag: np.ndarray, path: str) -> None:
    """Write a grayscale PNG of the spectrogram (low freq at the bottom)."""
    img = _to_u8(mag)[::-1]                      # flip so low freq is at the bottom
    img = np.ascontiguousarray(img, dtype=np.uint8)
    h, w = img.shape
    raw = bytearray()
    for row in img:
        raw.append(0)                            # PNG filter type 0
        raw.extend(row.tobytes())

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))


def terminal_heatmap(mag: np.ndarray, rows: int = 12, cols: int = 64) -> list:
    """ASCII heatmap (top = high freq). Cheap 'look at it' in the terminal."""
    u = _to_u8(mag).astype(np.float64) / 255.0
    fr, ft = u.shape
    ri = np.linspace(0, fr - 1, rows).astype(int)
    ci = np.linspace(0, ft - 1, cols).astype(int)
    small = u[np.ix_(ri, ci)][::-1]              # high freq on top
    return ["".join(_RAMP[min(len(_RAMP) - 1, int(v * (len(_RAMP) - 1)))] for v in row)
            for row in small]


def high_band_ratio(x: np.ndarray, fs: float, split_hz: float = 6000.0) -> float:
    """Energy above split_hz over total energy (a crude 'brightness')."""
    freqs, _, mag = stft(x, fs)
    p = (mag ** 2).sum(axis=1)
    total = p.sum() + 1e-12
    return float(p[freqs >= split_hz].sum() / total)


def main(argv=None):
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from audio_io import load
    p = argparse.ArgumentParser(description="STFT spectrogram of a segment")
    p.add_argument("input")
    p.add_argument("--fs", type=int, default=48000)
    p.add_argument("--start", type=float, default=None)
    p.add_argument("--dur", type=float, default=None)
    p.add_argument("--png", default=None, help="write a grayscale PNG here")
    p.add_argument("--out-dir", default="/data/rf_luv/ghost/forensics")
    args = p.parse_args(argv)
    x = load(args.input, args.out_dir, fs=args.fs, start_s=args.start, dur_s=args.dur)
    _, _, mag = stft(x, args.fs)
    for line in terminal_heatmap(mag):
        print(line)
    if args.png:
        save_png(mag, args.png)
    print(json.dumps({"high_band_ratio": round(high_band_ratio(x, args.fs), 4),
                      "png": args.png}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
