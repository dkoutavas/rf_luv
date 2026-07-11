#!/usr/bin/env python3
"""Generate the offline demo capture for `pipeline.sh demo`.

Synthesizes a clean 2-FSK burst and writes it in the exact .cs8 + .json manifest
format that spectrum/iq_capture.py produces, so spectrum/analysis/blind_analyze.py
can print an identity card with zero hardware, zero Docker, zero ClickHouse. This
is the "it works" hit before any host bring-up.

Deterministic (seeded), numpy+stdlib only. Re-run to regenerate:
    python3 samples/make_demo.py
"""
import json
from pathlib import Path

import numpy as np

FS = 256_000          # sample rate (S/s)
BAUD = 2_000          # symbol rate
DUR = 0.6             # seconds
DEV = 22_000          # tone deviation: two tones at +/-DEV
FREQ_HZ = 433_920_000  # pretend an ISM-band mystery blip
SCALE = 90.0          # int8 amplitude (well under +/-127 to avoid clipping)

OUT = Path(__file__).resolve().parent / "demo"


def main() -> None:
    n = int(FS * DUR)
    sps = int(FS / BAUD)
    nsym = n // sps
    bits = np.random.default_rng(7).integers(0, 2, nsym)
    # Per-sample instantaneous frequency: +DEV for a 1, -DEV for a 0.
    f = np.repeat(np.where(bits == 1, DEV, -DEV), sps).astype(np.float64)
    f = np.resize(f, n)
    x = np.exp(1j * 2 * np.pi * np.cumsum(f) / FS)         # constant-envelope FSK
    rng = np.random.default_rng(11)
    x += (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.1  # ~20 dB SNR

    i8 = np.empty(2 * n, dtype=np.int8)
    i8[0::2] = np.clip(np.round(x.real * SCALE), -128, 127).astype(np.int8)
    i8[1::2] = np.clip(np.round(x.imag * SCALE), -128, 127).astype(np.int8)
    i8.tofile(str(OUT) + ".cs8")

    manifest = {
        "freq_hz": FREQ_HZ,
        "sample_rate_hz": FS,
        "duration_s": DUR,
        "gain_db": 0.0,
        "dongle_id": "demo",
        "trigger_id": "",
        "captured_at": "1970-01-01 00:00:00.000",
        "format": "cs8",
        "note": f"synthetic 2-FSK, {BAUD} baud, +/-{DEV} Hz tones — offline demo",
    }
    with open(str(OUT) + ".json", "w") as fh:
        json.dump(manifest, fh, indent=2)

    print(f"wrote {OUT}.cs8 ({i8.nbytes} bytes) + {OUT}.json "
          f"(2-FSK, {BAUD} baud, fs={FS})")


if __name__ == "__main__":
    main()
