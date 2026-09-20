#!/usr/bin/env python3
"""
Spirit-box DSP: wideband-FM demodulation and the sweep planner, numpy only.

A commercial spirit box (SB7/SB11) is an FM/AM receiver that sweeps stations at
a fixed rate with no squelch and no lock, dumping raw demodulated audio. The
"voices" are broadcast fragments; the meaning is supplied by the listener. This
module rebuilds the receiver half: per FM channel it runs a quadrature
discriminator, EU 50 us de-emphasis, an anti-alias low-pass, and decimation down
to a 48 kHz audio stream.

Reuses the DSP the RDS pipeline already ships rather than reinventing it:
  - fm_discriminate  (rds/rds_decoder.py:105) — the WFM discriminator
  - sinc_lpf         (rds/rds_decoder.py:93)  — windowed-sinc low-pass kernel

Pure functions, no I/O and no hardware — spiritbox.py does the rtl_tcp,
ClickHouse, and WAV work. That split keeps this file testable on synthetic IQ.
"""

import os
import sys
import math
import numpy as np

# Reuse the RDS pipeline's discriminator + FIR kernel (numpy-only, no scipy).
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_REPO, "rds") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO, "rds"))
from rds_decoder import fm_discriminate, sinc_lpf  # noqa: E402

# Capture rate for the sweep: a valid RTL-SDR rate (225001-300000 band) that
# covers one 200 kHz WFM channel and divides cleanly to the 48 kHz WAV rate.
FS_CAPTURE = 240000
FS_AUDIO = 48000
DECIM = FS_CAPTURE // FS_AUDIO          # 5
AUDIO_LP_HZ = 15000                     # WFM audio bandwidth
DEEMPH_TAU_S = 50e-6                    # EU de-emphasis time constant (75 us in the US)


def cu8_to_complex(buf: bytes) -> np.ndarray:
    """Unsigned-8-bit interleaved IQ (rtl_tcp wire format) -> complex64 in [-1, 1].

    Same convention as spectrum/scanner.py: subtract the 127.5 midpoint and scale.
    """
    raw = np.frombuffer(buf, dtype=np.uint8).astype(np.float32)
    iq = (raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)
    return (iq / 127.5).astype(np.complex64)


def cs8_to_complex(buf) -> np.ndarray:
    """Signed-8-bit interleaved IQ (.cs8 capture format) -> complex64 in [-1, 1]."""
    raw = np.frombuffer(buf, dtype=np.int8).astype(np.float32)
    return ((raw[0::2] + 1j * raw[1::2]) / 127.5).astype(np.complex64)


def rssi_dbfs(iq: np.ndarray) -> float:
    """Mean power of a complex IQ block, in dBFS (0 dBFS = full-scale sine)."""
    if iq.size == 0:
        return -120.0
    p = float(np.mean((iq.real.astype(np.float64)) ** 2 + (iq.imag.astype(np.float64)) ** 2))
    return 10.0 * math.log10(max(p, 1e-12))


def clip_fraction_u8(buf: bytes) -> float:
    """Fraction of raw u8 samples pinned at 0 or 255 (ADC rail = clipping)."""
    if not buf:
        return 0.0
    raw = np.frombuffer(buf, dtype=np.uint8)
    return float(np.count_nonzero((raw == 0) | (raw == 255))) / raw.size


def _deemphasis_kernel(fs: float, tau: float) -> np.ndarray:
    """FIR impulse response of a one-pole de-emphasis IIR y[n]=a*x[n]+(1-a)*y[n-1].

    The one-pole all-pole filter equals convolution with h[k] = a*(1-a)^k, which
    decays geometrically, so a truncated FIR is the same filter to within the
    truncation floor. This keeps it vectorized and scipy-free.
    ponytail: FIR-approximated IIR; if a sample-exact tail ever matters, swap in a
    recursive one-pole (a Python loop is fine at the 48 kHz audio rate).
    """
    a = 1.0 - math.exp(-1.0 / (fs * tau))
    b = 1.0 - a
    # Truncate where the tail drops below ~1e-4 of the peak.
    n = max(1, int(math.ceil(math.log(1e-4) / math.log(b)))) if b > 0 else 1
    k = np.arange(n, dtype=np.float64)
    return (a * (b ** k)).astype(np.float64)


def deemphasis(x: np.ndarray, fs: float = FS_AUDIO, tau: float = DEEMPH_TAU_S) -> np.ndarray:
    """Apply EU 50 us FM de-emphasis to a real audio array at rate fs."""
    h = _deemphasis_kernel(fs, tau)
    y = np.convolve(x.astype(np.float64), h, mode="full")[: x.size]
    return y


def wfm_demod(
    iq: np.ndarray,
    fs_in: int = FS_CAPTURE,
    fs_out: int = FS_AUDIO,
    audio_lp_hz: float = AUDIO_LP_HZ,
    deemph_tau: float = DEEMPH_TAU_S,
    lp_taps: int = 65,
) -> np.ndarray:
    """Wideband-FM demodulate a complex IQ block to real audio at fs_out.

    Chain: quadrature discriminator -> anti-alias low-pass -> decimate -> de-emphasis.
    Returns float audio roughly in [-1, 1] (not normalized to full-scale).
    """
    if iq.size < 4:
        return np.zeros(0, dtype=np.float64)
    decim = int(round(fs_in / fs_out))
    disc = fm_discriminate(iq).astype(np.float64)          # rad/sample, len-1
    h = sinc_lpf(audio_lp_hz, fs_in, lp_taps)              # unity-DC FIR
    lp = np.convolve(disc, h, mode="same")
    audio = lp[::decim]                                    # decimate to fs_out
    audio = deemphasis(audio, fs_out, deemph_tau)
    # Scale so a typical broadcast lands near unity without clipping the WAV.
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0:
        audio = audio / (peak + 1e-9) * 0.9
    return audio


def to_int16(x: np.ndarray) -> np.ndarray:
    """Clip float audio in [-1, 1] to int16 PCM for the WAV writer."""
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)


# ─── sweep planner ───────────────────────────────────────────────────────────

def plan_sweep(f_start_hz: int, f_end_hz: int, step_hz: int, mode: str,
               seed=None) -> list:
    """Ordered list of step frequencies over [f_start, f_end] at step_hz spacing.

    mode: 'forward' (ascending), 'reverse' (descending), 'random' (shuffled).
    A real SB7 offers forward/reverse; 'random' is for the module-5 blind test.
    """
    if step_hz <= 0:
        raise ValueError("step_hz must be positive")
    freqs = list(range(int(f_start_hz), int(f_end_hz) + 1, int(step_hz)))
    if mode == "forward":
        return freqs
    if mode == "reverse":
        return freqs[::-1]
    if mode == "random":
        rng = np.random.default_rng(seed)
        idx = rng.permutation(len(freqs))
        return [freqs[i] for i in idx]
    raise ValueError(f"unknown sweep mode: {mode!r}")


# ─── post-fx (off by default in spiritbox.py; this is what "creepy voice" is) ──

def slapback(x: np.ndarray, fs: int, delay_ms: float = 90.0,
             feedback: float = 0.25) -> np.ndarray:
    """Single-tap slapback echo. Shows that a 'spirit voice' is dry radio + delay."""
    d = int(round(fs * delay_ms / 1000.0))
    if d <= 0:
        return x.astype(np.float64)
    y = x.astype(np.float64).copy()
    # One feedback tap is enough to hear the effect; iterate a few decaying taps.
    tap = float(feedback)
    shift = d
    while tap > 0.02 and shift < y.size:
        y[shift:] += tap * x[: y.size - shift]
        tap *= feedback
        shift += d
    m = float(np.max(np.abs(y))) if y.size else 0.0
    return y / (m + 1e-9) * 0.9 if m > 0 else y


def reverb(x: np.ndarray, fs: int, decay: float = 0.3,
           taps_ms=(23.0, 37.0, 53.0, 71.0)) -> np.ndarray:
    """Cheap Schroeder-ish reverb: a few decaying early reflections. Demo-grade."""
    y = x.astype(np.float64).copy()
    for i, tms in enumerate(taps_ms):
        d = int(round(fs * tms / 1000.0))
        if 0 < d < y.size:
            y[d:] += (decay ** (i + 1)) * x[: y.size - d]
    m = float(np.max(np.abs(y))) if y.size else 0.0
    return y / (m + 1e-9) * 0.9 if m > 0 else y
