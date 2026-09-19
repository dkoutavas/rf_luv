#!/usr/bin/env python3
"""
Self-test for the spirit-box replica. numpy + stdlib ONLY — no pytest, no scipy.

Run bare:   python3 ghost/tests/test_spiritbox.py   (prints PASS lines, exit 0)
Or:         pytest ghost/tests/test_spiritbox.py

Covers: the WFM demod chain (tone recovery), de-emphasis roll-off, the sweep
planner, WAV round-trip, an end-to-end run_live via an injected fake client
(no hardware, no ClickHouse), and the RDS station pre-pass on a synthetic MPX.
"""

import os
import sys
import json
import wave
import tempfile

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_GHOST = os.path.dirname(_HERE)
_REPO = os.path.dirname(_GHOST)
for _p in (_GHOST, os.path.join(_REPO, "rds"), os.path.join(_REPO, "rds", "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dsp
import spiritbox


# ── synthetic signal helpers ──────────────────────────────────────────────────
def make_wfm_tone_cu8(fs=dsp.FS_CAPTURE, dur=0.25, tone=1000.0, dev=75000.0):
    """A single-tone WFM broadcast as rtl_tcp cu8 bytes."""
    n = int(fs * dur)
    t = np.arange(n)
    mpx = 0.6 * np.sin(2.0 * np.pi * tone * t / fs)
    phase = 2.0 * np.pi * dev * np.cumsum(mpx) / fs
    iq = np.exp(1j * phase)
    i8 = np.clip(np.round(iq.real * 100.0 + 127.5), 0, 255).astype(np.uint8)
    q8 = np.clip(np.round(iq.imag * 100.0 + 127.5), 0, 255).astype(np.uint8)
    cu8 = np.empty(2 * n, dtype=np.uint8)
    cu8[0::2] = i8
    cu8[1::2] = q8
    return cu8.tobytes()


def make_rds_cu8():
    """A synthetic RDS MPX at 228 kHz, reusing the rds decoder's own test rig."""
    import test_rds_decoder as rds_t
    diff, _ = rds_t.build_bitstream(repeats=6, prefix_bits=200, seed=7)
    cu8 = rds_t.modulate_cu8(diff, fs=spiritbox.FS_RDS, snr_db=35.0, seed=3)
    return cu8.tobytes() if hasattr(cu8, "tobytes") else bytes(cu8)


class FakeClient:
    """Serves fixed cu8 buffers keyed by the currently-set sample rate."""
    def __init__(self, buffers):
        self.buffers = buffers          # {rate_hz: bytes}
        self.rate = dsp.FS_CAPTURE
        self.freq = 0
        self.closed = False

    def set_sample_rate(self, r): self.rate = int(r)
    def set_frequency(self, f): self.freq = int(f)
    def set_gain(self, g): pass
    def discard(self, n): pass

    def read_samples(self, n):
        buf = self.buffers.get(self.rate)
        if buf is None or len(buf) == 0:
            return bytes(n)
        reps = (n // len(buf)) + 1
        return (buf * reps)[:n]

    def close(self): self.closed = True


def _peak_freq(audio, fs=dsp.FS_AUDIO):
    spec = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
    freqs = np.fft.rfftfreq(audio.size, 1.0 / fs)
    return freqs[int(np.argmax(spec[1:]) + 1)]   # skip DC


# ── tests ─────────────────────────────────────────────────────────────────────
def test_wfm_demod_recovers_tone():
    buf = make_wfm_tone_cu8(tone=1200.0, dur=0.25)
    iq = dsp.cu8_to_complex(buf)
    audio = dsp.wfm_demod(iq)
    assert audio.size == iq.size // dsp.DECIM or abs(audio.size - iq.size // dsp.DECIM) <= 1
    peak = _peak_freq(audio)
    assert abs(peak - 1200.0) < 60.0, f"recovered {peak:.0f} Hz, expected ~1200"


def test_deemphasis_rolls_off_highs():
    fs = dsp.FS_AUDIO
    h = dsp._deemphasis_kernel(fs, dsp.DEEMPH_TAU_S)
    assert abs(h.sum() - 1.0) < 0.05, "de-emphasis DC gain must be ~unity"

    def mag(f):
        k = np.arange(h.size)
        return abs(np.sum(h * np.exp(-2j * np.pi * f * k / fs)))
    assert mag(10000.0) < mag(1000.0) < 1.05, "highs must be attenuated more than lows"


def test_plan_sweep_modes():
    fs, fe, step = 87_500_000, 88_000_000, 100_000
    fwd = dsp.plan_sweep(fs, fe, step, "forward")
    rev = dsp.plan_sweep(fs, fe, step, "reverse")
    rnd = dsp.plan_sweep(fs, fe, step, "random", seed=1)
    assert fwd[0] == fs and fwd[-1] == fe
    assert rev == fwd[::-1]
    assert sorted(rnd) == fwd, "random must be a permutation of the same grid"
    assert dsp.plan_sweep(fs, fe, step, "random", seed=1) == rnd, "seeded random is stable"


def test_wav_roundtrip():
    audio = (0.5 * np.sin(2 * np.pi * 440 * np.arange(4800) / dsp.FS_AUDIO))
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "rt.wav")
        spiritbox.write_wav(path, audio)
        with wave.open(path, "rb") as w:
            assert w.getframerate() == dsp.FS_AUDIO
            assert w.getnchannels() == 1
            back = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        assert back.size == audio.size
        assert np.allclose(back, dsp.to_int16(audio))


def test_run_live_end_to_end(monkeypatch=None):
    buffers = {dsp.FS_CAPTURE: make_wfm_tone_cu8(dur=0.2)}
    client = FakeClient(buffers)
    # Narrow the band so the sweep is a handful of steps, and skip RDS/lock/CH.
    orig = (spiritbox.FM_START, spiritbox.FM_END, spiritbox.WAV_DIR)
    with tempfile.TemporaryDirectory() as d:
        spiritbox.FM_START, spiritbox.FM_END = 100_000_000, 100_400_000
        spiritbox.WAV_DIR = d
        args = spiritbox.build_parser().parse_args(
            ["--mode", "forward", "--dwell-ms", "150", "--no-rds",
             "--no-lock", "--dry-run"])
        try:
            wav_path = spiritbox.run_live(args, client_factory=lambda: client)
            assert os.path.exists(wav_path)
            side = os.path.splitext(wav_path)[0] + ".json"
            meta = json.load(open(side))
            assert meta["sweep_mode"] == "forward"
            assert len(meta["steps"]) == 5          # 100.0..100.4 MHz @ 100 kHz
            s0 = meta["steps"][0]
            for k in ("step_idx", "t_start", "t_end", "freq_hz", "dwell_ms",
                      "rssi_db", "rds_ps", "rds_rt"):
                assert k in s0, f"sidecar step missing {k}"
            assert client.closed, "client must be closed after the session"
        finally:
            spiritbox.FM_START, spiritbox.FM_END, spiritbox.WAV_DIR = orig


def test_rds_prepass_finds_a_station():
    buffers = {spiritbox.FS_RDS: make_rds_cu8()}
    client = FakeClient(buffers)
    orig = (spiritbox.FM_START, spiritbox.FM_END)
    try:
        spiritbox.FM_START, spiritbox.FM_END = 100_000_000, 100_200_000  # 3 grid points
        table, rows = spiritbox.build_station_table(
            client, "testsession", step_hz=100_000, top_n=2, dwell_s=2.0)
        assert rows, "RDS pre-pass produced no station rows"
        assert any(r["pi_code"] != 0 or r["ps"] for r in rows), \
            "no PI/PS recovered from the synthetic MPX"
    finally:
        spiritbox.FM_START, spiritbox.FM_END = orig


def _run_all():
    tests = sorted(k for k, v in globals().items()
                   if k.startswith("test_") and callable(v))
    failed = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {e!r}")
    if failed:
        print(f"\n{failed}/{len(tests)} FAILED")
        return 1
    print(f"\nOK ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
