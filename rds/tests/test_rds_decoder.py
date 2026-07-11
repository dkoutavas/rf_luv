#!/usr/bin/env python3
"""
Standalone synthetic self-test for the RDS decoder — numpy only.

No hardware, no network, no scipy, no pytest required:

    python3 rds/tests/test_rds_decoder.py

prints per-test PASS and exits 0 on success (assert on failure). Each stage
is also a def test_*() so pytest discovers it later without being required.

Fixed ground truth transmitted through the whole chain:
    PI   = 0x1234
    PS   = 'KOSMOS  '                      (8 chars incl. trailing pad)
    RT   = 'RF_LUV RDS SELF TEST'          (20 chars, no 0x0D needed)
    PTY  = 10, TP = 1
    CT   = 2026-07-11 12:34 UTC, +180 min local offset

The ENCODER below is the exact inverse of the framing spec in
rds_decoder.py, so it doubles as executable documentation of the format.
"""

import os
import sys
from datetime import date

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rds_decoder import (  # noqa: E402
    FS_DEFAULT,
    OFFSET_WORDS,
    PILOT_HZ,
    RDSDemodulator,
    crc10,
    crc_remainder26,
    block_ok,
    GroupAssembler,
    Framer,
)

# ─── Ground truth ────────────────────────────────────────

PI = 0x1234
PS = "KOSMOS  "
RT = "RF_LUV RDS SELF TEST"
PTY = 10
TP = 1
TA = 0
CT_HOUR = 12
CT_MIN = 34
CT_OFFSET_MIN = 180
CT_MJD = (date(2026, 7, 11) - date(1858, 11, 17)).days   # derived, not hardcoded


# ─── Test-only RDS group encoder (inverse of the framing spec) ───

def _block_bits(info16: int, offset_name: str) -> list[int]:
    """One 26-bit block, MSB first: 16 info bits + (crc10 ^ offset)."""
    check = crc10(info16) ^ OFFSET_WORDS[offset_name]
    block = ((info16 & 0xFFFF) << 10) | (check & 0x3FF)
    return [(block >> i) & 1 for i in range(25, -1, -1)]


def _group_bits(a: int, b: int, c: int, d: int) -> list[int]:
    bits = []
    bits += _block_bits(a, "A")
    bits += _block_bits(b, "B")
    bits += _block_bits(c, "C")   # test uses version-A groups only
    bits += _block_bits(d, "D")
    return bits


def _block_b(gtype: int, ver: int, tp: int, pty: int, tail5: int) -> int:
    return ((gtype & 0xF) << 12) | ((ver & 1) << 11) | ((tp & 1) << 10) \
        | ((pty & 0x1F) << 5) | (tail5 & 0x1F)


def build_groups() -> list[list[int]]:
    """The 10-group program: 4x 0A (PS), 5x 2A (RT), 1x 4A (CT)."""
    groups = []

    # 0A — Programme Service name, 4 segments.
    for seg in range(4):
        b = _block_b(0, 0, TP, PTY, (TA << 4) | seg)   # ms=0, di=0
        c = 0xE0E0                                      # AF filler (ignored)
        c0 = ord(PS[2 * seg])
        c1 = ord(PS[2 * seg + 1])
        d = (c0 << 8) | c1
        groups.append(_group_bits(PI, b, c, d))

    # 2A — RadioText, 4 chars/segment, A/B flag = 0.
    padded = RT.ljust(20)
    for seg in range(5):
        b = _block_b(2, 0, TP, PTY, seg)                # bit4 = text A/B flag = 0
        ch = [ord(padded[4 * seg + i]) for i in range(4)]
        c = (ch[0] << 8) | ch[1]
        d = (ch[2] << 8) | ch[3]
        groups.append(_group_bits(PI, b, c, d))

    # 4A — clock-time / date.
    mjd = CT_MJD
    half_hours = CT_OFFSET_MIN // 30
    b = _block_b(4, 0, TP, PTY, (mjd >> 15) & 0x3)      # tail bits1-0 = MJD high 2
    c = (((mjd & 0x7FFF) << 1) | ((CT_HOUR >> 4) & 1)) & 0xFFFF
    d = ((CT_HOUR & 0xF) << 12) | ((CT_MIN & 0x3F) << 6) \
        | (0 << 5) | (half_hours & 0x1F)               # sign 0 (+), 6 half-hours
    groups.append(_group_bits(PI, b, c, d))

    return groups


def build_bitstream(repeats: int = 4, prefix_bits: int = 200, seed: int = 7):
    """Junk prefix + repeated program, then differentially encoded."""
    rng = np.random.default_rng(seed)
    groups = build_groups()
    n_groups = len(groups) * repeats

    raw = list(rng.integers(0, 2, size=prefix_bits))
    for _ in range(repeats):
        for g in groups:
            raw += g

    # Differential ENCODE: d[i] = bit[i] ^ d[i-1], carry starts at 0.
    diff = []
    prev = 0
    for bit in raw:
        prev = bit ^ prev
        diff.append(prev)
    return np.array(diff, dtype=np.int8), n_groups


# ─── MPX synthesizer + FM modulator ──────────────────────

def modulate_cu8(diff_bits, fs: int = FS_DEFAULT, snr_db: float = 30.0, seed: int = 3):
    """Build a phase-locked MPX (mono audio + 19 kHz pilot + 57 kHz DBPSK
    biphase), FM-modulate, add AWGN, and quantize to interleaved CU8 bytes —
    exercising the reader's exact byte path.
    """
    hb = fs // 1187  # ~96 samples per biphase half-bit; use exact fs/BITRATE/2
    half = int(round(fs / 1187.5 / 2))   # 96
    # Biphase waveform: diff bit 1 -> [+1]*96 then [-1]*96; 0 -> inverse.
    up = np.concatenate((np.ones(half), -np.ones(half)))
    biphase = np.concatenate([up if bit else -up for bit in diff_bits])

    n = np.arange(biphase.size)
    theta = 2.0 * np.pi * PILOT_HZ * n / fs
    mpx = (
        0.25 * np.sin(2.0 * np.pi * 1000.0 * n / fs)   # mono audio tone
        + 0.09 * np.cos(theta)                          # 19 kHz stereo pilot
        + 0.05 * biphase * np.cos(3.0 * theta)          # 57 kHz DBPSK, phase-locked
    )

    dev = 75000.0 / np.max(np.abs(mpx))
    phase = 2.0 * np.pi * dev * np.cumsum(mpx) / fs
    iq = np.exp(1j * phase)

    rng = np.random.default_rng(seed)
    sigma = np.sqrt(10.0 ** (-snr_db / 10.0) / 2.0)
    iq = iq + (rng.standard_normal(iq.size) + 1j * rng.standard_normal(iq.size)) * sigma

    i8 = np.clip(np.round(iq.real * 100.0 + 127.5), 0, 255).astype(np.uint8)
    q8 = np.clip(np.round(iq.imag * 100.0 + 127.5), 0, 255).astype(np.uint8)
    cu8 = np.empty(iq.size * 2, dtype=np.uint8)
    cu8[0::2] = i8
    cu8[1::2] = q8
    return cu8


def cu8_to_iq(cu8: np.ndarray) -> np.ndarray:
    raw = cu8.astype(np.float64)
    return ((raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)) / 127.5


# ─── Tests ───────────────────────────────────────────────

def test_crc10():
    # crc10 followed by its own remainder is zero (clean codeword, no offset).
    for info in (0x0000, 0x1234, 0xE0E0, 0xFFFF, 0xABCD):
        block = (info << 10) | crc10(info)
        assert crc_remainder26(block) == 0

    # Every offset word round-trips through block_ok.
    for name in OFFSET_WORDS:
        info = 0x1234
        check = crc10(info) ^ OFFSET_WORDS[name]
        block = (info << 10) | check
        assert block_ok(block, name), name
        # A single flipped info bit must fail the check.
        assert not block_ok(block ^ (1 << 20), name), name
    print("PASS test_crc10")


def test_block_sync_bitstream():
    # Bypass DSP: feed the encoded differential stream straight to the framer.
    diff, n_groups = build_bitstream()
    assembler = GroupAssembler()
    framer = Framer(assembler)

    # Differential DECODE (the DSP stage normally does this).
    prev = None
    records = []
    for i, d in enumerate(diff):
        d = int(d)
        if prev is None:
            prev = d
            continue
        records.extend(framer.push_bit(d ^ prev))
        prev = d

    assert len(records) == n_groups, f"got {len(records)} groups, want {n_groups}"
    assert all(r["block_errors"] == 0 for r in records)
    assert all(r["pi"] == PI for r in records)
    # Sync must be acquired quickly after the 200-bit junk prefix.
    assert framer.synced
    print(f"PASS test_block_sync_bitstream ({len(records)} groups, 0 block errors)")


def test_full_mpx_roundtrip():
    diff, n_groups = build_bitstream()
    cu8 = modulate_cu8(diff)

    demod = RDSDemodulator(fs=FS_DEFAULT)
    records = []
    chunk = FS_DEFAULT * 2   # 1 second of CU8 (I+Q interleaved)
    for start in range(0, cu8.size, chunk):
        iq = cu8_to_iq(cu8[start:start + chunk])
        records.extend(demod.process(iq))

    assert records, "no groups decoded from synthetic MPX"

    # PI on every emitted group.
    assert all(r["pi"] == PI for r in records), "PI mismatch in decoded groups"

    # PS fully assembled at least once.
    ps_values = {r["ps"].rstrip() for r in records if r["ps"]}
    assert "KOSMOS" in ps_values, f"PS not recovered, saw {ps_values}"

    # RadioText recovered.
    assert any("RF_LUV RDS SELF TEST" in r["radiotext"] for r in records), \
        "RadioText not recovered"

    # Clock-time recovered exactly.
    clocks = [(r["clock_utc"], r["clock_offset_min"]) for r in records if r["clock_utc"]]
    assert clocks, "no clock-time group decoded"
    assert any(cu and cu.startswith("2026-07-11 12:34") and off == CT_OFFSET_MIN
               for cu, off in clocks), f"clock mismatch, saw {clocks}"

    # End-to-end recovery rate.
    recovered = len(records)
    frac = recovered / n_groups
    assert frac >= 0.90, f"only {recovered}/{n_groups} groups ({frac:.0%}) recovered"
    print(f"PASS test_full_mpx_roundtrip ({recovered}/{n_groups} groups, {frac:.0%})")


def main():
    test_crc10()
    test_block_sync_bitstream()
    test_full_mpx_roundtrip()
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
