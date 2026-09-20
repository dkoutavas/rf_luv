#!/usr/bin/env python3
"""
fingerprint.py — reused-clip detection across episodes (chromaprint).

If the same "spirit voice" audio appears in more than one episode, it was
pre-recorded, not captured live. Chromaprint acoustic fingerprints make that
provable: two segments with a near-identical fingerprint are the same audio.

This is the one ghost tool that is not numpy-only: it shells out to `fpcalc`
(chromaprint). Install it with `sudo zypper install chromaprint-fpcalc`.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

MIN_FRAMES = 8          # ~1 s of audio; fpcalc emits ~8 frames/s
REUSE_THRESHOLD = 0.90  # bit-agreement above this = same clip


def have_fpcalc() -> bool:
    from shutil import which
    return which("fpcalc") is not None


def _parse_raw(stdout: str) -> np.ndarray:
    for line in stdout.splitlines():
        if line.startswith("FINGERPRINT="):
            vals = [int(x) & 0xFFFFFFFF for x in line[12:].split(",") if x]
            return np.array(vals, dtype=np.uint32)
    return np.array([], dtype=np.uint32)


def raw_fingerprint(path: str) -> np.ndarray:
    """chromaprint raw fingerprint of a file (one uint32 per ~0.12 s frame)."""
    out = subprocess.run(["fpcalc", "-raw", path], capture_output=True,
                         text=True, check=True).stdout
    return _parse_raw(out)


def fingerprint_b64(path: str) -> str:
    """The compact base64 fingerprint (what goes in ghost.segments.fingerprint)."""
    out = subprocess.run(["fpcalc", path], capture_output=True,
                         text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("FINGERPRINT="):
            return line[12:]
    return ""


def fingerprint_audio(x: np.ndarray, fs: int) -> np.ndarray:
    """Raw fingerprint of an in-memory mono float segment (via a temp WAV)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        path = tf.name
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(fs)
            w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
        return raw_fingerprint(path)
    finally:
        os.unlink(path)


def similarity(a: np.ndarray, b: np.ndarray, max_offset: int = 60,
               min_frames: int = MIN_FRAMES) -> float:
    """Best bit-agreement (0..1) of two raw fingerprints over a small time offset."""
    if a.size == 0 or b.size == 0:
        return 0.0
    best = 0.0
    for off in range(-max_offset, max_offset + 1):
        if off >= 0:
            x, y = a[off:], b[: a[off:].size]
        else:
            y, x = b[-off:], a[: b[-off:].size]
        n = min(x.size, y.size)
        if n < min_frames:
            continue
        xor = np.bitwise_xor(x[:n], y[:n]).astype(np.uint32)
        diff = int(np.unpackbits(xor.view(np.uint8)).sum())
        best = max(best, 1.0 - diff / (32.0 * n))
    return best


def main(argv=None):
    p = argparse.ArgumentParser(description="Fingerprint clips and flag reuse")
    p.add_argument("files", nargs="+", help="audio/video files to fingerprint")
    p.add_argument("--threshold", type=float, default=REUSE_THRESHOLD)
    args = p.parse_args(argv)
    if not have_fpcalc():
        print("fpcalc not installed (sudo zypper install chromaprint-fpcalc)",
              file=sys.stderr)
        return 2
    fps = {f: raw_fingerprint(f) for f in args.files}
    pairs = []
    names = list(fps)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            sim = similarity(fps[names[i]], fps[names[j]])
            pairs.append({"a": os.path.basename(names[i]),
                          "b": os.path.basename(names[j]),
                          "similarity": round(sim, 3),
                          "reused": sim >= args.threshold})
    print(json.dumps({"frames": {os.path.basename(k): int(v.size)
                                 for k, v in fps.items()},
                      "pairs": pairs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
