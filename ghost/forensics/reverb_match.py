#!/usr/bin/env python3
"""
reverb_match.py — RT60 of the room vs the reverb tail on a "voice".

RT60 is the time for sound energy to drop 60 dB. Estimated from an impulsive
event (a clap or door) by Schroeder backward integration of the energy decay
curve, fitting the -5 dB to -25 dB slope (T20) and scaling to 60 dB. If the
reverb tail on a "spirit voice" has a different RT60 from the room's own claps,
the tail was added in the box, not picked up in the room. numpy + stdlib only.
"""

import argparse
import json
import sys

import numpy as np


def energy_decay_db(x: np.ndarray) -> np.ndarray:
    """Schroeder backward-integrated energy decay curve, in dB (0 dB at t=0)."""
    e = np.asarray(x, dtype=np.float64) ** 2
    edc = np.cumsum(e[::-1])[::-1]
    edc /= (edc[0] + 1e-20)
    return 10.0 * np.log10(edc + 1e-12)


def rt60(x: np.ndarray, fs: float) -> dict:
    """RT60 from the T20 slope of an impulsive segment (clap/door)."""
    edc_db = energy_decay_db(x)

    def t_at(db):
        idx = np.where(edc_db <= db)[0]
        return idx[0] / fs if idx.size else None
    t5, t25 = t_at(-5.0), t_at(-25.0)
    if t5 is None or t25 is None or t25 <= t5:
        return {"rt60_s": None, "t20_s": None}
    t20 = t25 - t5
    return {"rt60_s": round(3.0 * t20, 3), "t20_s": round(t20, 3)}


def matches_room(voice_tail: np.ndarray, room_clap: np.ndarray, fs: float,
                 tol_frac: float = 0.3) -> dict:
    """Compare the voice tail's RT60 to the room's; flag a mismatch."""
    rv = rt60(voice_tail, fs)["rt60_s"]
    rr = rt60(room_clap, fs)["rt60_s"]
    if rv is None or rr is None:
        return {"voice_rt60_s": rv, "room_rt60_s": rr, "matches": None}
    matches = abs(rv - rr) <= tol_frac * rr
    return {"voice_rt60_s": rv, "room_rt60_s": rr, "matches": bool(matches)}


def main(argv=None):
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from audio_io import load
    p = argparse.ArgumentParser(description="RT60 of an impulsive segment")
    p.add_argument("input")
    p.add_argument("--fs", type=int, default=48000)
    p.add_argument("--start", type=float, default=None)
    p.add_argument("--dur", type=float, default=None)
    p.add_argument("--out-dir", default="/data/rf_luv/ghost/forensics")
    args = p.parse_args(argv)
    x = load(args.input, args.out_dir, fs=args.fs, start_s=args.start, dur_s=args.dur)
    print(json.dumps(rt60(x, args.fs), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
