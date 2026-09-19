#!/usr/bin/env python3
"""
Self-test for the ghost forensics tools. numpy + stdlib ONLY (no pytest, no scipy).

Run:  python3 ghost/tests/test_forensics.py

Covers: slapback-delay recovery on synthetic echoes, upper-bandwidth measurement
on band-limited vs full-band noise, and the ffmpeg audio decode round-trip.
The fpcalc fingerprint check is skipped when fpcalc is absent.
"""

import os
import sys
import wave
import tempfile

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_GHOST = os.path.dirname(_HERE)
_REPO = os.path.dirname(_GHOST)
for _p in (_GHOST, os.path.join(_GHOST, "forensics"), os.path.join(_REPO, "rds")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dsp
import delay_estimate
import bandlimit
import audio_io
import fingerprint

FS = 48000


def _noise(n, seed=0):
    return np.random.default_rng(seed).standard_normal(n).astype(np.float64)


def _add_echo(x, fs, delay_ms, feedback):
    d = int(round(fs * delay_ms / 1000.0))
    y = x.copy()
    y[d:] += feedback * x[: y.size - d]
    return y


def test_delay_estimate_recovers_tap():
    x = _noise(FS * 2, seed=1)
    for true_ms in (45.0, 90.0, 130.0):
        y = _add_echo(x, FS, true_ms, 0.35)
        res = delay_estimate.estimate_delay(y, FS)
        assert res["delay_ms"] is not None
        assert abs(res["delay_ms"] - true_ms) < 6.0, \
            f"delay {res['delay_ms']} vs {true_ms}"
        assert 0.15 < res["feedback"] < 0.6, f"feedback {res['feedback']}"


def test_delay_estimate_null_on_clean_signal():
    # A clean broadband signal has no echo peak; feedback should be small.
    x = _noise(FS, seed=2)
    res = delay_estimate.estimate_delay(x, FS)
    assert res["feedback"] < 0.2, f"clean signal reported feedback {res['feedback']}"


def test_bandlimit_lowpassed_vs_fullband():
    x = _noise(FS * 2, seed=3)
    full = bandlimit.upper_bandwidth(x, FS)
    assert full["upper_bw_hz"] > 20000, f"full-band upper {full['upper_bw_hz']}"

    h = dsp.sinc_lpf(8000.0, FS, 201)
    band = np.convolve(x, h, mode="same")
    lim = bandlimit.upper_bandwidth(band, FS)
    assert 6500 < lim["upper_bw_hz"] < 9500, f"lowpassed upper {lim['upper_bw_hz']}"
    assert lim["upper_bw_hz"] < full["upper_bw_hz"] - 5000, "shelf not detected"


def test_audio_io_ffmpeg_roundtrip():
    if not audio_io.have("ffmpeg"):
        print("SKIP ffmpeg roundtrip (ffmpeg absent)")
        return
    t = np.arange(FS)  # 1 s
    sig = 0.5 * np.sin(2 * np.pi * 1000.0 * t / FS)
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "tone.wav")
        with wave.open(wav, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(FS)
            w.writeframes((np.clip(sig, -1, 1) * 32767).astype(np.int16).tobytes())
        back = audio_io.decode_audio(wav, fs=FS)
        assert abs(back.size - FS) <= FS // 10, f"decoded {back.size} samples"
        spec = np.abs(np.fft.rfft(back * np.hanning(back.size)))
        freqs = np.fft.rfftfreq(back.size, 1.0 / FS)
        peak = freqs[int(np.argmax(spec[1:]) + 1)]
        assert abs(peak - 1000.0) < 20.0, f"decoded tone peak {peak:.0f} Hz"


def test_fingerprint_reuse_detection():
    if not fingerprint.have_fpcalc():
        print("SKIP fingerprint (fpcalc absent)")
        return
    t = np.arange(FS * 8)
    tone = 0.5 * np.sin(2 * np.pi * 440.0 * t / FS)
    noise = _noise(FS * 8, seed=9) * 0.5
    fp_tone_a = fingerprint.fingerprint_audio(tone, FS)
    fp_tone_b = fingerprint.fingerprint_audio(tone, FS)
    fp_noise = fingerprint.fingerprint_audio(noise, FS)
    assert fp_tone_a.size >= fingerprint.MIN_FRAMES, "too few fingerprint frames"
    same = fingerprint.similarity(fp_tone_a, fp_tone_b)
    diff = fingerprint.similarity(fp_tone_a, fp_noise)
    assert same > 0.95, f"identical clips scored {same:.3f}"
    assert diff < same - 0.15, f"different clips scored {diff:.3f} vs {same:.3f}"


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
