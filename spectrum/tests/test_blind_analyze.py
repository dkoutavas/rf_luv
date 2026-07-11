#!/usr/bin/env python3
"""Standalone self-test for spectrum/analysis/blind_analyze.py (feature D3).

Runs with numpy + stdlib ONLY — no pytest, no scipy. Two ways to run:

    python3 spectrum/tests/test_blind_analyze.py   # bare, prints OK, exit 0
    pytest spectrum/tests/test_blind_analyze.py      # also discoverable

Every test is a plain assert-based function; the __main__ block calls each one.
DB access in blind_analyze is stubbed by attribute assignment on the two _ch_*
shims, so no ClickHouse is needed.

HONESTY OF THIS GATE
    The synthetic signals below are CLEAN rect-pulse waveforms with 20 dB AWGN.
    This test gates correctness on that clean case and claims NOTHING about
    low-SNR, fading, or pulse-shaped robustness — a real Polygono capture may
    legitimately land in digital-unknown / low-confidence. It exists to prove
    the feature math and the rule ordering are wired correctly, and to lock the
    baud estimator against known symbol rates.

    Known limit documented inline: 8PSK is asserted to fall through to
    digital-unknown (QPSK and 8PSK share |C42|~1; only |C40| separates them, and
    that separation is what the fallback relies on) — the analyzer deliberately
    does not claim to name PSK orders above QPSK.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import blind_analyze as ba  # noqa: E402

FS = 256_000
DUR = 2.0
SNR_DB = 20.0
RNG = np.random.default_rng(42)


# ─── synthetic generators (return complex128 + ground-truth dict) ────
def _awgn(x: np.ndarray, snr_db: float) -> np.ndarray:
    sp = np.mean(np.abs(x) ** 2)
    npow = sp / (10 ** (snr_db / 10.0))
    noise = (RNG.standard_normal(x.size) + 1j * RNG.standard_normal(x.size))
    noise *= np.sqrt(npow / 2.0)
    return (x + noise).astype(np.complex128)


def _t(fs=FS, dur=DUR):
    return np.arange(int(fs * dur)) / fs


def gen_noise(fs=FS, dur=DUR):
    n = int(fs * dur)
    x = (RNG.standard_normal(n) + 1j * RNG.standard_normal(n)) / np.sqrt(2)
    return x.astype(np.complex128), {"mod": "noise"}


def gen_cw(fs=FS, dur=DUR, f_off=18_500.0):
    t = _t(fs, dur)
    x = np.exp(2j * np.pi * f_off * t)
    return _awgn(x, SNR_DB), {"mod": "CW", "offset": f_off}


def gen_am(fs=FS, dur=DUR, f_off=12_000.0):
    t = _t(fs, dur)
    x = (1 + 0.5 * np.cos(2 * np.pi * 1000 * t)) * np.exp(2j * np.pi * f_off * t)
    return _awgn(x, SNR_DB), {"mod": "AM", "offset": f_off}


def _fm(dev, fs, dur):
    t = _t(fs, dur)
    audio = np.cos(2 * np.pi * 1000 * t) + 0.5 * np.cos(2 * np.pi * 5000 * t)
    ph = 2 * np.pi * dev * np.cumsum(audio) / fs
    return np.exp(1j * ph)


def gen_wfm(fs=FS, dur=DUR):
    return _awgn(_fm(75_000, fs, dur), SNR_DB), {"mod": "WFM"}


def gen_nfm(fs=FS, dur=DUR):
    return _awgn(_fm(2_500, fs, dur), SNR_DB), {"mod": "NFM"}


def gen_ook(fs=FS, dur=DUR, baud=1000, f_off=10_000.0):
    sps = int(fs / baud)
    nsym = int(fs * dur) // sps
    bits = RNG.integers(0, 2, nsym)
    env = np.repeat(bits, sps).astype(np.float64)
    t = np.arange(env.size) / fs
    x = env * np.exp(2j * np.pi * f_off * t)
    return _awgn(x, SNR_DB), {"mod": "OOK", "baud": baud}


def _cpfsk(tones, baud, fs, dur):
    sps = int(fs / baud)
    nsym = int(fs * dur) // sps
    syms = RNG.integers(0, len(tones), nsym)
    f_sym = np.repeat(np.array(tones)[syms], sps)
    ph = 2 * np.pi * np.cumsum(f_sym) / fs
    return np.exp(1j * ph)


def gen_fsk2(fs=FS, dur=DUR, baud=1000):
    x = _cpfsk([-2000, 2000], baud, fs, dur)
    return _awgn(x, SNR_DB), {"mod": "2FSK", "baud": baud}


def gen_fsk4(fs=FS, dur=DUR, baud=2000):
    x = _cpfsk([-3000, -1000, 1000, 3000], baud, fs, dur)
    return _awgn(x, SNR_DB), {"mod": "4FSK", "baud": baud}


def _psk(order, baud, fs, dur, f_off):
    sps = int(fs / baud)
    nsym = int(fs * dur) // sps
    syms = RNG.integers(0, order, nsym)
    phase0 = np.pi / order  # offset constellation off the real axis
    sym_phase = phase0 + syms * (2 * np.pi / order)
    base = np.exp(1j * np.repeat(sym_phase, sps))
    t = np.arange(base.size) / fs
    return base * np.exp(2j * np.pi * f_off * t)


def gen_bpsk(fs=FS, dur=DUR, baud=4000, f_off=5000.0):
    x = _psk(2, baud, fs, dur, f_off)
    return _awgn(x, SNR_DB), {"mod": "BPSK", "baud": baud, "offset": f_off}


def gen_qpsk(fs=FS, dur=DUR, baud=4000, f_off=5000.0):
    x = _psk(4, baud, fs, dur, f_off)
    return _awgn(x, SNR_DB), {"mod": "QPSK/PSK", "baud": baud, "offset": f_off}


def gen_8psk(fs=FS, dur=DUR, baud=4000, f_off=5000.0):
    x = _psk(8, baud, fs, dur, f_off)
    return _awgn(x, SNR_DB), {"mod": "digital-unknown", "baud": baud, "offset": f_off}


def gen_ofdm(fs=FS, dur=DUR, nsub=64, ncp=16):
    total = nsub + ncp
    nsym = int(fs * dur) // total
    out = np.empty(nsym * total, dtype=np.complex128)
    for k in range(nsym):
        data = RNG.integers(0, 4, nsub)
        const = np.exp(1j * (np.pi / 4 + data * np.pi / 2))
        body = np.fft.ifft(const) * np.sqrt(nsub)
        sym = np.concatenate([body[-ncp:], body])
        out[k * total:(k + 1) * total] = sym
    return _awgn(out, SNR_DB), {"mod": "OFDM", "tu_us": nsub / fs * 1e6}


# ─── (a)-(d) end-to-end classification per generator ─────
def _within(a, b, tol):
    return abs(a - b) <= tol * abs(b)


def _classify(gen):
    x, truth = gen()
    v = ba.analyze_array(x, float(FS), freq_hz=0)
    return v, truth


def test_noise():
    v, _ = _classify(gen_noise)
    assert v.modulation == "noise", (v.modulation, v.reasoning[-2:])
    assert v.baud_hz is None
    assert v.ofdm_flag == 0


def test_cw():
    v, tr = _classify(gen_cw)
    assert v.modulation == "CW", (v.modulation, v.features)
    assert v.occupied_bw_hz < 2000, v.occupied_bw_hz
    assert v.baud_hz is None
    assert _within(v.carrier_offset_hz, tr["offset"], 0.02) or \
        abs(v.carrier_offset_hz - tr["offset"]) < 1000


def test_am():
    v, tr = _classify(gen_am)
    assert v.modulation == "AM", (v.modulation, v.features)
    assert v.baud_hz is None
    assert abs(v.carrier_offset_hz - tr["offset"]) < 1000, v.carrier_offset_hz


def test_wfm():
    v, _ = _classify(gen_wfm)
    assert v.modulation == "WFM", (v.modulation, v.features)
    assert 100_000 <= v.occupied_bw_hz <= 300_000, v.occupied_bw_hz
    assert v.baud_hz is None


def test_nfm():
    v, _ = _classify(gen_nfm)
    assert v.modulation == "NFM", (v.modulation, v.features)
    assert 5_000 <= v.occupied_bw_hz <= 30_000, v.occupied_bw_hz
    assert v.baud_hz is None


def test_ook():
    v, tr = _classify(gen_ook)
    assert v.modulation == "OOK", (v.modulation, v.features)
    assert v.baud_hz is not None and _within(v.baud_hz, tr["baud"], 0.10), v.baud_hz


def test_fsk2():
    v, tr = _classify(gen_fsk2)
    assert v.modulation == "2FSK", (v.modulation, v.features)
    assert v.baud_hz is not None and _within(v.baud_hz, tr["baud"], 0.10), v.baud_hz


def test_fsk4():
    v, tr = _classify(gen_fsk4)
    assert v.modulation == "4FSK", (v.modulation, v.features)
    assert v.baud_hz is not None and _within(v.baud_hz, tr["baud"], 0.10), v.baud_hz


def test_bpsk():
    v, tr = _classify(gen_bpsk)
    assert v.modulation == "BPSK", (v.modulation, v.features)
    assert v.baud_hz is not None and _within(v.baud_hz, tr["baud"], 0.10), v.baud_hz


def test_qpsk():
    v, tr = _classify(gen_qpsk)
    assert v.modulation == "QPSK/PSK", (v.modulation, v.features)
    assert v.baud_hz is not None and _within(v.baud_hz, tr["baud"], 0.10), v.baud_hz
    assert v.ofdm_flag == 0


def test_8psk_falls_through():
    # 8PSK shares |C42|~1 with QPSK; only |C40| (~0 vs ~1) separates them, so the
    # honest output is the digital-unknown fallback with the out-of-scope note.
    v, _ = _classify(gen_8psk)
    assert v.modulation == "digital-unknown", (v.modulation, v.features)
    assert any("out of scope" in r for r in v.reasoning)


def test_ofdm():
    v, tr = _classify(gen_ofdm)
    assert v.modulation == "OFDM", (v.modulation, v.features)
    assert v.ofdm_flag == 1
    assert v.ofdm_tu_us is not None and _within(v.ofdm_tu_us, tr["tu_us"], 0.10), v.ofdm_tu_us


# ─── (g) unit checks for the DSP primitives ──────────────
def test_fft_hilbert_of_cosine():
    fs = 48_000
    t = np.arange(fs) / fs
    f = 3_000
    xr = np.cos(2 * np.pi * f * t)
    a = ba.fft_hilbert(xr)
    # envelope ~ 1, ignore edges where the FIR-free FFT trick rings
    env = np.abs(a)[1000:-1000]
    assert 0.95 < np.mean(env) < 1.05, np.mean(env)
    inst = np.diff(np.unwrap(np.angle(a))) * fs / (2 * np.pi)
    assert abs(np.median(inst[1000:-1000]) - f) < 50, np.median(inst)


def test_welch_psd_tone_power():
    fs = 256_000
    t = np.arange(fs) / fs
    f = 20_000
    x = np.exp(2j * np.pi * f * t)   # unit-power tone
    freqs, psd = ba.welch_psd(x, fs)
    # integrated PSD ~ signal power (1.0) within ~1 dB
    total = np.sum(psd) * (freqs[1] - freqs[0])
    assert abs(10 * np.log10(total + 1e-12)) < 1.0, total
    # spectral peak sits at the tone
    assert abs(freqs[np.argmax(psd)] - f) < 200, freqs[np.argmax(psd)]


def test_rate_independence_fsk2():
    # rerun 2FSK at the real 2.048 MS/s to prove the chain is rate-agnostic.
    hi_fs = 2_048_000
    x = _cpfsk([-2000, 2000], 1000, hi_fs, 0.5)
    x = _awgn(x, SNR_DB)
    v = ba.analyze_array(x, float(hi_fs), freq_hz=0)
    assert v.modulation == "2FSK", (v.modulation, v.features)
    assert v.baud_hz is not None and _within(v.baud_hz, 1000, 0.10), v.baud_hz


# ─── (h) end-to-end I/O: .cs8 + manifest, stubbed DB ─────
class _StubDB:
    """Capture blind_analyze's inserts + route its selects, no ClickHouse."""

    def __init__(self, rows=None):
        self._rows = rows or (lambda sql: [])
        self.inserts = []

    def __enter__(self):
        self._orig = (ba._ch_rows, ba._ch_insert)
        ba._ch_rows = self._rows
        ba._ch_insert = lambda table, rows: self.inserts.append((table, list(rows)))
        return self

    def __exit__(self, *exc):
        ba._ch_rows, ba._ch_insert = self._orig
        return False

    def inserted_rows(self, table):
        out = []
        for t, rows in self.inserts:
            if t == table:
                out.extend(rows)
        return out


def _write_cs8(path, x):
    """Write complex array as D2 .cs8 (signed int8 interleaved), scaled to ~half
    scale so nothing clips."""
    iq = np.empty(2 * x.size, dtype=np.int8)
    scale = 100.0 / (np.max(np.abs(x)) + 1e-12)
    iq[0::2] = np.clip(np.round(x.real * scale), -128, 127).astype(np.int8)
    iq[1::2] = np.clip(np.round(x.imag * scale), -128, 127).astype(np.int8)
    iq.tofile(path)


def test_end_to_end_file_mode():
    x, tr = gen_fsk2()
    d = tempfile.mkdtemp()
    cs8 = os.path.join(d, "cap_162000_2s.cs8")
    _write_cs8(cs8, x)
    with open(cs8[:-4] + ".json", "w") as f:
        json.dump({"freq_hz": 162_000_000, "sample_rate_hz": FS, "duration_s": DUR,
                   "gain_db": 20.0, "dongle_id": "v4-01", "trigger_id": "tid-e2e",
                   "captured_at": "2026-07-11 10:00:00.000", "format": "cs8"}, f)

    with _StubDB() as sdb:
        rc = ba.main(["--file", cs8])
        assert rc == 0
        rows = sdb.inserted_rows("blind_signal_features")
        assert len(rows) == 1, rows
        row = rows[0]
        # row carries the 024 schema keys and the right verdict
        assert row["modulation"] == "2FSK", row["modulation"]
        assert row["sample_rate_hz"] == FS
        assert row["freq_hz"] == 162_000_000
        assert row["dongle_id"] == "v4-01"
        assert row["trigger_id"] == "tid-e2e"
        assert row["baud_hz"] is not None and _within(row["baud_hz"], tr["baud"], 0.10)
        for key in ("analyzer_version", "ofdm_flag", "confidence", "occupied_bw_hz",
                    "carrier_offset_hz", "snr_db", "baud_confidence", "features",
                    "reasoning", "source", "file_path"):
            assert key in row, key
        json.loads(row["features"])  # features column is valid JSON


def test_end_to_end_capture_id_mode():
    x, _ = gen_ook()
    d = tempfile.mkdtemp()
    cs8 = os.path.join(d, "cap_433920_2s.cs8")
    _write_cs8(cs8, x)   # no sidecar — capture-id path supplies rate from the row

    fake_row = {
        "capture_id": "cap-xyz", "trigger_id": "tid-xyz", "dongle_id": "v4-01",
        "freq_hz": 433_920_000, "sample_rate_hz": FS, "duration_s": DUR,
        "path": cs8, "source": "operator:cli",
    }
    with _StubDB(rows=lambda sql: [fake_row]) as sdb:
        rc = ba.main(["--capture-id", "cap-xyz"])
        assert rc == 0
        rows = sdb.inserted_rows("blind_signal_features")
        assert len(rows) == 1
        assert rows[0]["capture_id"] == "cap-xyz"
        assert rows[0]["freq_hz"] == 433_920_000
        assert rows[0]["modulation"] == "OOK"
        assert rows[0]["source"] == "operator:cli"


def test_dry_run_no_insert():
    x, _ = gen_cw()
    d = tempfile.mkdtemp()
    cs8 = os.path.join(d, "cap_cw_2s.cs8")
    _write_cs8(cs8, x)
    with _StubDB() as sdb:
        rc = ba.main(["--file", cs8, "--rate", str(FS), "--dry-run"])
        assert rc == 0
        assert sdb.inserted_rows("blind_signal_features") == []


# ─── runner ──────────────────────────────────────────────
_MOD_TESTS = [
    ("noise", test_noise), ("CW", test_cw), ("AM", test_am), ("WFM", test_wfm),
    ("NFM", test_nfm), ("OOK", test_ook), ("2FSK", test_fsk2), ("4FSK", test_fsk4),
    ("BPSK", test_bpsk), ("QPSK/PSK", test_qpsk), ("8PSK->unknown", test_8psk_falls_through),
    ("OFDM", test_ofdm),
]
_OTHER_TESTS = [
    ("fft_hilbert", test_fft_hilbert_of_cosine),
    ("welch_psd", test_welch_psd_tone_power),
    ("rate_independence(2.048MS/s)", test_rate_independence_fsk2),
    ("io_file_mode", test_end_to_end_file_mode),
    ("io_capture_id_mode", test_end_to_end_capture_id_mode),
    ("dry_run", test_dry_run_no_insert),
]


def _run_all():
    print("── modulation classification ──")
    passed = failed = 0
    for label, fn in _MOD_TESTS:
        try:
            fn()
            print(f"  PASS  {label}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {label}: {e}")
            failed += 1
    print("── DSP primitives + I/O ──")
    for label, fn in _OTHER_TESTS:
        try:
            fn()
            print(f"  PASS  {label}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {label}: {e}")
            failed += 1
    total = passed + failed
    print(f"\n{'OK' if failed == 0 else 'FAILED'} — {passed}/{total} tests passed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
