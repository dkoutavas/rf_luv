#!/usr/bin/env python3
"""Generate a small synthetic WFM capture for the offline spirit-box demo.

Writes spirit_demo.cs8 (signed-8-bit IQ, the .cs8 capture format) plus a JSON
sidecar with the sample rate, so `spiritbox.py --file` can demod it with no
hardware. The signal is a two-tone "broadcast" FM-modulated at 240 kHz.
"""
import json
import os

import numpy as np

FS = 240000
DUR = 0.6
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spirit_demo.cs8")


def main():
    n = int(FS * DUR)
    t = np.arange(n)
    mpx = 0.5 * np.sin(2 * np.pi * 500.0 * t / FS) + 0.3 * np.sin(2 * np.pi * 1500.0 * t / FS)
    dev = 60000.0
    phase = 2 * np.pi * dev * np.cumsum(mpx) / FS
    iq = np.exp(1j * phase)
    i8 = np.clip(np.round(iq.real * 100.0), -128, 127).astype(np.int8)
    q8 = np.clip(np.round(iq.imag * 100.0), -128, 127).astype(np.int8)
    cs8 = np.empty(2 * n, dtype=np.int8)
    cs8[0::2] = i8
    cs8[1::2] = q8
    cs8.tofile(OUT)
    with open(os.path.splitext(OUT)[0] + ".json", "w") as f:
        json.dump({"sample_rate_hz": FS, "center_freq_hz": 99600000,
                   "format": "cs8", "note": "synthetic two-tone WFM, demo only"}, f, indent=2)
    print(f"wrote {OUT} ({cs8.nbytes} bytes) + sidecar")


if __name__ == "__main__":
    main()
