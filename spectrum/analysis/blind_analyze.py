#!/usr/bin/env python3
"""Blind signal analyzer — the "what is this?" identity card (feature D3).

Eats a raw-IQ .cs8 capture (produced by spectrum/iq_capture.py, D2) and prints
an identity card with ZERO prior knowledge of the signal:

    occupied bandwidth, carrier offset, modulation family
    (CW / AM / WFM / NFM / OOK / 2FSK / 4FSK / BPSK / QPSK-PSK / OFDM /
     noise / digital-unknown), symbol/baud rate, and an OFDM flag,

plus a numbered reasoning trace, exactly like detect_compression.py writes a
sub-signature trace. One verdict row lands in spectrum.blind_signal_features.

This ONE analyzer absorbs the separate AMC (automatic modulation
classification) and cyclostationary ideas — it is not three tools. The pipeline
is rule-based (Hilbert/instantaneous discriminants + higher-order cumulants +
the squaring-trick baud line + a cyclic-prefix OFDM test). No scikit-learn /
torch: the plan says rule-based first, an ML classifier only once a hand-labeled
capture set actually accrues (it has not — ground-floor Polygono UHF wall loss
makes labeling slow).

LEGAL POSTURE
    Characterizing a waveform's modulation, bandwidth, and baud extracts NO
    message content — it is analysis of physics, legal on any received signal.
    It works on encrypted carriers WITHOUT touching the payload; the correct
    output for those is to LABEL them "digital, likely encrypted -> out of
    scope" (the digital-unknown fallback) and never attempt to decode.

HONESTY
    At 2.048 MS/s the observable baud caps well under ~1 Msym/s. That is fine
    for the in-range NFM / FSK / paging / marine-data targets. Wideband OFDM /
    DVB-T only partially fits one capture span, so those verdicts are emitted at
    low confidence and say so in the reasoning.

Usage:
    python3 blind_analyze.py --file capture.cs8                 # sidecar .json auto-loaded
    python3 blind_analyze.py --file capture.cs8 --rate 2048000  # no sidecar -> supply rate
    python3 blind_analyze.py --file capture.cs8 --dry-run       # fully offline, no DB
    python3 blind_analyze.py --file capture.cs8 --json          # dump the raw feature dict
    python3 blind_analyze.py --capture-id <uuid>                # look up path in iq_captures

Dependencies: numpy + stdlib. No scipy (analytic signal, PSD and FIR are all
done by hand via np.fft / segment averaging / windowed-sinc), matching the
scanner.py / rds_decoder.py posture.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# analysis/ is a subdir; db.py / config.py live in spectrum/. Mirror
# detect_compression.py so `import db` resolves when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

# ─── Config (named thresholds; all module-level so the test can tweak) ───
ANALYZER_VERSION = "v1"

ANALYZE_MAX_S = 4.0          # cap analysis window (8.4M cplx @ 2.048 MS/s ~ 67 MB)
SETTLE_S = 0.010             # drop first 10 ms (tuner/AGC settle) before stats
WELCH_NPERSEG = 4096         # Welch PSD segment length
WELCH_OVERLAP = 0.5          # 50 % overlap

OBW_POWER_FRACTION = 0.99    # 99 % power-containment occupied bandwidth
OBW_LO_FRAC = 0.005          # cumulative-power edges (0.5 % / 99.5 %)
OBW_HI_FRAC = 0.995

SNR_MIN_DB = 5.0             # below this -> 'noise' (no signal clears the floor)
NOISE_FLATNESS_MIN = 0.90    # spectral flatness above this reads as noise-like
NOISE_C42_MAX = 0.30         # ...and near-Gaussian 4th-order cumulant

CW_OBW_MAX_HZ = 2_000.0      # carrier/CW: essentially a single tone
CW_SIGMA_AF_MAX = 5.0e-3     # ...with no frequency wander (on the SMOOTHED f_i)
CW_GAMMA_MAX = 0.10          # ...and no amplitude modulation

# Instantaneous-frequency smoothing. Raw per-sample f_i is dominated by phase
# noise (std ~ sigma_phi*fs/2pi, which at 20 dB SNR is ~2.9 kHz at 256 kS/s —
# larger than an FSK tone spacing). A moving average of ~fs/IFREQ_SMOOTH_DIV
# samples denoises f_i by sqrt(that length) while preserving the symbol dwell,
# and it scales with fs so the discriminant is rate-independent.
IFREQ_SMOOTH_DIV = 8_000

OOK_SIGMA_AA_MIN = 0.40      # on/off keying: envelope swings hard (>AM's 0.35)
FSK_SIGMA_AA_MAX = 0.25      # constant-envelope gate (excludes OOK / AM)
FSK_DWELL_MIN = 0.60         # inst-freq must be PIECEWISE-CONSTANT (vs analog FM's sweep)
DWELL_FLAT_FRAC = 0.15       # |d level| below this * span counts as "flat"

WFM_OBW_MIN_HZ = 50_000.0    # WFM vs NFM split on occupied bandwidth

AM_SIGMA_AA_MIN = 0.25       # AM: real envelope modulation...
AM_GAMMA_MAX_MIN = 0.08      # ...concentrated in an amplitude spectral line
AM_SIGMA_AF_MAX = 5.0e-3     # ...but no FM (small normalized inst-freq spread)

CUM_C20_BPSK_MIN = 0.60      # BPSK keeps a real 2nd-order cumulant
CUM_C42_BPSK_MIN = 1.40      # |C42| ~ 2 for BPSK
CUM_C40_BPSK_MIN = 1.40
CUM_C20_PSK_MAX = 0.30       # QPSK/8PSK suppress C20
CUM_C40_QPSK_MIN = 0.50      # QPSK |C40| ~ 1; 8PSK |C40| ~ 0 (=> digital-unknown)
CUM_C42_PSK_LO = 0.50
CUM_C42_PSK_HI = 1.40

BAUD_MIN_HZ = 50.0           # squaring-trick search floor
BAUD_SEARCH_HI_FRAC = 0.25   # ...ceiling = fs/4
BAUD_LINE_MIN_DB = 6.0       # a line must clear the local median by this much
BAUD_AGREE_TOL = 0.10        # spectral line vs autocorr agreement window

OFDM_TAU_MIN = 48            # cyclic-prefix autocorr scan floor (samples)
OFDM_PEAK_MIN = 0.05         # normalized CP correlation floor
OFDM_PEAK_MED_MULT = 4.0     # ...or 4x the local median, whichever is larger
OFDM_C20_MAX = 0.30          # OFDM time samples are ~complex Gaussian
OFDM_C42_MAX = 0.30
OFDM_FLATNESS_MIN = 0.50
OFDM_WIDE_FRAC = 0.80        # occupied > this * fs -> "wider than capture span"

CONF_FLOOR = 0.25            # every verdict floored here (honest, not fabricated)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("blind_analyze")


# ─── Per-signature dataclasses ───────────────────────────
@dataclass
class SpectrumInfo:
    occupied_bw_hz: float
    carrier_offset_hz: float
    psd_peak_offset_hz: float
    snr_db: float
    flatness: float
    psd_kurtosis: float
    noise_floor: float


@dataclass
class EnvelopeInfo:
    gamma_max: float
    sigma_aa: float
    env_modes: int
    env_mode_positions: list[float]


@dataclass
class FreqInfo:
    sigma_ap: float
    sigma_dp: float
    sigma_af: float
    freq_modes: int
    freq_mode_positions: list[float]
    freq_concentration: float


@dataclass
class CumulantInfo:
    c20_abs: float
    c20_re: float
    c20_im: float
    c40_abs: float
    c42_abs: float
    derotate_hz: float


@dataclass
class BaudInfo:
    baud_hz: float | None
    confidence: str            # 'high' | 'medium' | 'none'
    line_snr_db: float
    source: str                # 'envelope^2' | 'transition' | 'none'


@dataclass
class OfdmInfo:
    flag: int
    tu_samples: int
    tu_us: float | None
    subcarrier_hz: float | None
    cp_ratio: float
    peak: float


@dataclass
class Verdict:
    modulation: str
    confidence: float
    baud_hz: float | None
    baud_confidence: str
    occupied_bw_hz: float
    carrier_offset_hz: float
    snr_db: float
    ofdm_flag: int
    ofdm_tu_us: float | None
    ofdm_subcarrier_hz: float | None
    features: dict = field(default_factory=dict)
    reasoning: list[str] = field(default_factory=list)


# ─── Pure DSP feature layer (numpy-only, no I/O) ─────────
def fft_hilbert(real_x: np.ndarray) -> np.ndarray:
    """Analytic signal of a REAL sequence via the FFT recipe: zero the negative
    half of the spectrum and double the positive half (the classic Hilbert
    trick). Returned complex signal has |x| = envelope, angle = instantaneous
    phase. Provided for completeness/tests — real captures are already complex
    IQ, so instantaneous_afp() takes the complex signal directly.
    """
    real_x = np.asarray(real_x, dtype=np.float64)
    n = real_x.size
    X = np.fft.fft(real_x)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1.0
        h[1:n // 2] = 2.0
    else:
        h[0] = 1.0
        h[1:(n + 1) // 2] = 2.0
    return np.fft.ifft(X * h)


def welch_psd(x: np.ndarray, fs: float, nperseg: int = WELCH_NPERSEG,
              overlap: float = WELCH_OVERLAP) -> tuple[np.ndarray, np.ndarray]:
    """Segment-averaged periodogram (Welch) of a COMPLEX signal, Hann-windowed,
    fftshift-ed so the returned freqs run negative..positive (baseband).

    Replaces scipy.signal.welch: per-segment P = |fftshift(fft(w*seg))|^2 /
    (fs*sum(w^2)); PSD = mean over segments.
    """
    x = np.asarray(x, dtype=np.complex128)
    n = x.size
    if n < nperseg:
        nperseg = n
    w = np.hanning(nperseg)
    wnorm = fs * np.sum(w ** 2)
    step = max(1, int(nperseg * (1.0 - overlap)))
    segs = []
    for start in range(0, n - nperseg + 1, step):
        seg = x[start:start + nperseg]
        P = np.abs(np.fft.fftshift(np.fft.fft(w * seg))) ** 2 / wnorm
        segs.append(P)
    if not segs:  # signal shorter than one segment
        seg = np.zeros(nperseg, dtype=np.complex128)
        seg[:n] = x
        segs.append(np.abs(np.fft.fftshift(np.fft.fft(w * seg))) ** 2 / wnorm)
    psd = np.mean(segs, axis=0)
    freqs = np.fft.fftshift(np.fft.fftfreq(nperseg, 1.0 / fs))
    return freqs, psd


def occupied_bandwidth(freqs: np.ndarray, psd: np.ndarray,
                       fs: float) -> SpectrumInfo:
    """99 % power-containment occupied bandwidth + carrier offset from a
    floor-subtracted PSD.

    Noise floor = median(PSD) (robust: for a narrowband signal most bins ARE
    noise). SNR = 10log10(signal_power / noise_power) where signal_power is the
    floor-subtracted spectral energy and noise_power = floor * nbins. The
    occupied edges are the 0.5 %/99.5 % points of the cumulative floor-subtracted
    power; the carrier offset is that spectrum's power centroid (the argmax
    offset is reported alongside).
    """
    floor = float(np.median(psd))
    s = np.maximum(psd - floor, 0.0)
    total = float(np.sum(s))
    signal_power = total
    noise_power = floor * psd.size
    snr_db = 10.0 * np.log10(signal_power / noise_power + 1e-12)

    if total <= 0:
        return SpectrumInfo(float(fs), 0.0, 0.0, snr_db, 1.0, 0.0, floor)

    cum = np.cumsum(s) / total
    lo_i = int(np.searchsorted(cum, OBW_LO_FRAC))
    hi_i = int(np.searchsorted(cum, OBW_HI_FRAC))
    lo_i = min(lo_i, psd.size - 1)
    hi_i = min(hi_i, psd.size - 1)
    f_lo, f_hi = freqs[lo_i], freqs[hi_i]
    occupied_bw = float(f_hi - f_lo)
    centroid = float(np.sum(freqs * s) / total)
    peak_off = float(freqs[int(np.argmax(psd))])

    # spectral flatness + kurtosis over the occupied band (geometric/arithmetic)
    band = psd[lo_i:hi_i + 1]
    band = band[band > 0]
    if band.size:
        flatness = float(np.exp(np.mean(np.log(band))) / np.mean(band))
        m = np.mean(band)
        sd = np.std(band)
        kurt = float(np.mean((band - m) ** 4) / (sd ** 4 + 1e-30)) if sd > 0 else 0.0
    else:
        flatness, kurt = 1.0, 0.0

    return SpectrumInfo(occupied_bw, centroid, peak_off, snr_db, flatness, kurt, floor)


def instantaneous_afp(x: np.ndarray, fs: float
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Instantaneous amplitude, frequency and (unwrapped) phase of a COMPLEX
    baseband signal. IQ is already analytic, so amp = |x|, phase = unwrap(angle),
    inst_freq = (fs/2pi) * d/dt phase (length N-1, aligned to amp[:-1]).
    """
    amp = np.abs(x)
    phase = np.unwrap(np.angle(x))
    inst_freq = np.diff(phase) * (fs / (2.0 * np.pi))
    return amp, inst_freq, phase


def _find_modes(values: np.ndarray, nbins: int = 64, rel_thresh: float = 0.08,
                min_sep: int = 4) -> tuple[int, list[float], np.ndarray]:
    """Count histogram modes of `values`. A mode is a smoothed-histogram local
    max > rel_thresh*global_max, at least min_sep bins from the previous kept
    mode, with an intervening valley below 50 % of the smaller of the two peaks
    (else the two merge into one). Returns (n_modes, centers, bin_centers).
    """
    lo, hi = np.percentile(values, 1.0), np.percentile(values, 99.0)
    if hi <= lo:
        return 1, [float(np.median(values))], np.array([np.median(values)])
    hist, edges = np.histogram(np.clip(values, lo, hi), bins=nbins)
    centers = 0.5 * (edges[:-1] + edges[1:])
    k = np.ones(5) / 5.0
    h = np.convolve(hist.astype(float), k, mode="same")
    gmax = h.max()
    if gmax <= 0:
        return 0, [], centers
    cand = [i for i in range(1, len(h) - 1)
            if h[i] >= h[i - 1] and h[i] >= h[i + 1] and h[i] > rel_thresh * gmax]
    merged: list[int] = []
    for i in cand:
        if merged and i - merged[-1] < min_sep:
            if h[i] > h[merged[-1]]:
                merged[-1] = i
        else:
            merged.append(i)
    if not merged:
        return 0, [], centers
    final = [merged[0]]
    for i in merged[1:]:
        j = final[-1]
        valley = h[j:i + 1].min()
        if valley < 0.5 * min(h[i], h[j]):
            final.append(i)
        elif h[i] > h[j]:
            final[-1] = i
    return len(final), [float(centers[i]) for i in final], centers


def _smooth_window(fs: float) -> int:
    return max(4, int(fs / IFREQ_SMOOTH_DIV))


def smooth(sig: np.ndarray, fs: float) -> np.ndarray:
    """Moving-average denoiser for the instantaneous frequency, window
    ~fs/IFREQ_SMOOTH_DIV samples (scales with fs so the discriminants below are
    rate-independent)."""
    w = _smooth_window(fs)
    return np.convolve(sig, np.ones(w) / w, mode="same")


def block_median(sig: np.ndarray, w: int) -> np.ndarray:
    """Non-overlapping block median — the frequency-LEVEL detector. Unlike a
    moving average, the median rejects the brief impulsive spikes that PSK/2-ary
    phase jumps put into the instantaneous frequency, while preserving the
    sustained tone level of a real FSK symbol. That is exactly what stops a BPSK
    burst (inst-freq ~0 with edge impulses) from masquerading as multi-tone FSK.
    """
    n = (sig.size // w) * w
    if n < w:
        return np.array([float(np.median(sig))]) if sig.size else np.array([0.0])
    return np.median(sig[:n].reshape(-1, w), axis=1)


def amc_discriminants(x: np.ndarray, amp: np.ndarray, level: np.ndarray,
                      fs: float) -> tuple[EnvelopeInfo, FreqInfo]:
    """Azzouz/Nandi-style instantaneous discriminants (all on the recentered,
    unit-power baseband signal).

    gamma_max — fraction of amplitude-fluctuation power in the single strongest
                spectral line of a_cn = a/mean(a) - 1 (N-independent: high for
                AM/OOK's tonal envelope, low for constant-envelope signals).
    sigma_aa  — std(a_cn) = std(amp)/mean(amp), the envelope coefficient of
                variation. SIGNED (not |a_cn|): abs() collapses OOK's two
                envelope levels to one, which is exactly the case we must keep.
                Anchors: OOK ~1.0, AM(0.5 depth) ~0.35, constant-envelope ~0.07,
                OFDM (Rayleigh) ~0.5.
    sigma_ap/sigma_dp — spread of the (absolute / signed) non-linear phase on
                strong samples; sigma_af — normalized SMOOTHED inst-freq spread.
    env_modes / freq_modes — histogram-mode counts feeding the OOK / FSK rules,
                freq modes taken on the block-median LEVEL (raw f_i is noise);
    freq_concentration — the PIECEWISE-CONSTANT dwell fraction: the share of
                level samples whose step to the next is essentially flat. FSK
                holds a tone for a whole symbol (dwell ~0.85); analog FM sweeps
                continuously (dwell ~0.1). This is what separates them even
                though sinusoidal-audio FM has multi-peaked freq STATISTICS that
                would otherwise read as multiple "tones".
    """
    mean_a = float(np.mean(amp)) or 1e-12
    a_cn = amp / mean_a - 1.0
    A = np.abs(np.fft.fft(a_cn - a_cn.mean())) ** 2
    A[0] = 0.0  # kill DC
    gamma_max = float(A.max() / (A.sum() + 1e-30))
    sigma_aa = float(np.std(a_cn))

    # strong-sample gate for the phase spread (keeps noise-only samples out)
    strong = amp[:-1] > mean_a
    if strong.sum() < 16:
        strong = np.ones_like(strong, dtype=bool)
    phase = np.unwrap(np.angle(x))
    n = np.arange(phase.size)
    slope = np.polyfit(n, phase, 1)[0]
    phi_nl = (phase - slope * n)[:-1][strong]
    sigma_ap = float(np.std(np.abs(phi_nl)))
    sigma_dp = float(np.std(phi_nl))

    f_cn = (level - np.mean(level)) / fs
    sigma_af = float(np.std(f_cn))
    env_modes, env_pos, _ = _find_modes(amp / (np.median(amp) or 1e-12))
    freq_modes, freq_pos, _ = _find_modes(level)
    span = float(np.percentile(level, 99) - np.percentile(level, 1))
    if span > 0 and level.size > 1:
        dwell = float(np.mean(np.abs(np.diff(level)) < DWELL_FLAT_FRAC * span))
    else:
        dwell = 1.0

    return (EnvelopeInfo(gamma_max, sigma_aa, env_modes, env_pos),
            FreqInfo(sigma_ap, sigma_dp, sigma_af, freq_modes, freq_pos, dwell))


def _dominant_line(sig: np.ndarray) -> tuple[int, float]:
    """Argmax bin index + peak/median ratio of |FFT(sig)| (DC excluded)."""
    S = np.abs(np.fft.fft(sig))
    S[0] = 0.0
    i = int(np.argmax(S))
    med = float(np.median(S)) or 1e-30
    return i, float(S[i] / med)


def cumulants(x: np.ndarray, fs: float) -> CumulantInfo:
    """2nd/4th-order cumulants on the unit-power baseband signal, AFTER removing
    any residual carrier via the x^2 / x^4 squaring trick.

    A residual frequency offset df would smear C20 = mean(y^2) to zero (its
    e^{j2pi*2df*t} term averages out), so we first estimate df: y^2 is a PURE
    tone at 2df for BPSK (data^2 == 1), y^4 a pure tone at 4df for QPSK. We
    derotate by whichever nonlinearity shows the more dominant line, then:

        C20 = mean(y^2)                       C21 = mean(|y|^2) = 1
        C40 = mean(y^4) - 3*C20^2             C42 = mean(|y|^4) - |C20|^2 - 2

    Theory anchors (clean rect pulses, unit power):
        BPSK |C20|=1 |C40|=2 |C42|=2 ; QPSK C20=0 |C40|=1 |C42|=1 ;
        8PSK C20=0 |C40|=0 |C42|=1 ; complex-Gaussian/OFDM all ~0.
    |C40| is what splits QPSK (~1) from 8PSK (~0).
    """
    y = np.asarray(x, dtype=np.complex128)
    y = y / (np.sqrt(np.mean(np.abs(y) ** 2)) + 1e-12)

    i2, r2 = _dominant_line(y ** 2)
    i4, r4 = _dominant_line(y ** 4)
    freqs = np.fft.fftfreq(y.size, 1.0 / fs)
    df = 0.0
    if r2 >= r4 and r2 > 50.0:
        df = freqs[i2] / 2.0
    elif r4 > 50.0:
        df = freqs[i4] / 4.0
    if df != 0.0:
        n = np.arange(y.size)
        y = y * np.exp(-2j * np.pi * df * n / fs)
        y = y / (np.sqrt(np.mean(np.abs(y) ** 2)) + 1e-12)

    c20 = complex(np.mean(y ** 2))
    c40 = complex(np.mean(y ** 4) - 3.0 * c20 ** 2)
    c42 = complex(np.mean(np.abs(y) ** 4) - abs(c20) ** 2 - 2.0)
    return CumulantInfo(abs(c20), c20.real, c20.imag, abs(c40), abs(c42), df)


def _spectral_line(metric: np.ndarray, fs: float) -> tuple[float, float]:
    """Strongest line in [BAUD_MIN_HZ, fs/4] of a real metric's Hann-windowed
    power spectrum, as (freq_hz, line_snr_db over the band median). freq 0 if
    nothing qualifies."""
    m = metric - np.mean(metric)
    w = np.hanning(m.size)
    M = np.abs(np.fft.rfft(m * w)) ** 2
    freqs = np.fft.rfftfreq(m.size, 1.0 / fs)
    band = (freqs >= BAUD_MIN_HZ) & (freqs <= BAUD_SEARCH_HI_FRAC * fs)
    if not np.any(band):
        return 0.0, 0.0
    Mb = M[band]
    fb = freqs[band]
    med = float(np.median(Mb)) or 1e-30
    i = int(np.argmax(Mb))
    snr_db = 10.0 * np.log10(Mb[i] / med + 1e-12)
    return float(fb[i]), float(snr_db)


def _autocorr_baud(metric: np.ndarray, fs: float, min_lag: int = 4
                   ) -> tuple[float, float]:
    """Fundamental symbol rate from the autocorrelation of a symbol-boundary
    metric, as (baud_hz, peak_height). A random impulse train on the symbol grid
    autocorrelates into a comb of peaks at multiples of the symbol period T; the
    SMALLEST-lag strong peak is the fundamental T (spectral-line argmax instead
    grabs an arbitrary comb harmonic — hence autocorr, not FFT-argmax, here).
    `min_lag` skips short-lag artifacts (e.g. a smoothing/decimation correlation
    length) below the plausible symbol period.
    """
    m = metric - np.mean(metric)
    n = m.size
    nfft = 1
    while nfft < 2 * n:
        nfft *= 2
    S = np.fft.rfft(m, nfft)
    ac = np.fft.irfft(np.abs(S) ** 2, nfft)[:n]
    if ac[0] <= 0:
        return 0.0, 0.0
    ac = ac / ac[0]
    min_lag = max(3, min_lag)
    max_lag = min(n - 2, int(fs / BAUD_MIN_HZ))
    if max_lag <= min_lag + 2:
        return 0.0, 0.0
    region = ac[min_lag:max_lag]
    peaks = [(min_lag + i, region[i]) for i in range(1, region.size - 1)
             if region[i] > region[i - 1] and region[i] > region[i + 1]
             and region[i] > 0.08]
    if not peaks:
        return 0.0, 0.0
    maxh = max(h for _, h in peaks)
    fundamental = min(lag for lag, h in peaks if h >= 0.5 * maxh)
    return fs / fundamental, maxh


def baud_estimate(x: np.ndarray, level: np.ndarray, fs: float, w: int) -> BaudInfo:
    """The cyclostationary baud estimate (squaring / non-linearity trick).

    Two symbol-boundary metrics, each a random impulse train on the symbol grid
    (spikes at symbol edges), whose fundamental period the autocorrelation reads:
      - x-transition:   |diff(x)|^2 at full rate — envelope/phase steps of OOK,
                        BPSK, QPSK.
      - f-transition:   |diff(level)|^2 in the decimated frequency-LEVEL domain
                        (rate fs/w) — tone changes of FSK. (Symmetric FSK has NO
                        |diff(x)| line: both tones share |x| and |e^{jw}-1|, so
                        the discriminated frequency is the only place the symbol
                        boundary shows up. Using the clean block-median level,
                        not the moving-averaged f_i, avoids a false lag-w peak.)
    The metric with the stronger autocorr peak wins. Confidence is 'high' when a
    spectral line also sits at that baud or an integer harmonic (the squaring
    line and the autocorr agree), 'medium' on the autocorr alone, and baud is
    None when neither metric shows structure (the honest NULL, like
    detect_compression's NULL emitter).
    """
    x_trans = np.abs(np.diff(x)) ** 2
    fs_level = fs / w
    f_trans = np.diff(level) ** 2 if level.size > 4 else np.array([0.0])
    best = None
    for name, metric, mfs, mlag in (
            ("x-transition", x_trans, fs, 4),
            ("f-transition", f_trans, fs_level, 3)):
        baud, height = _autocorr_baud(metric, mfs, min_lag=mlag)
        if baud > 0 and (best is None or height > best[0]):
            f_line, line_snr = _spectral_line(metric, mfs)
            best = (height, baud, name, line_snr, f_line)
    if best is None:
        return BaudInfo(None, "none", 0.0, "none")
    _height, baud, name, line_snr, f_line = best
    conf = "medium"
    if f_line > 0 and line_snr >= BAUD_LINE_MIN_DB:
        r = f_line / baud
        if abs(r - round(r)) < 0.12 and round(r) >= 1:
            conf = "high"
    return BaudInfo(round(baud, 2), conf, round(line_snr, 2), name)


def ofdm_cp_detect(x: np.ndarray, fs: float) -> OfdmInfo:
    """Cyclic-prefix autocorrelation test for OFDM.

    gamma(tau) = |sum_n x[n] conj(x[n+tau])| / sum|x|^2 shows an isolated bump at
    tau = Tu (the useful-symbol length) of height ~ Ncp/(Ncp+Nu), because each
    symbol's CP is a copy of the last Ncp body samples. We scan tau in
    [OFDM_TAU_MIN, N/4] for an INTERIOR local maximum (higher than its neighbors
    +/- 16 samples) clearing max(OFDM_PEAK_MIN, OFDM_PEAK_MED_MULT*local median).
    The interior-max requirement rejects the monotonically-decaying autocorr
    triangle of a plain rect-pulse PSK signal. Returns the smallest strong Tu.
    """
    x = np.asarray(x, dtype=np.complex128)
    n = x.size
    nfft = 1
    while nfft < 2 * n:
        nfft *= 2
    X = np.fft.fft(x, nfft)
    r = np.fft.ifft(X * np.conj(X))          # r[tau] = sum x[n+tau] conj(x[n])
    r0 = np.abs(r[0]) or 1e-30
    gamma = np.abs(r[:n]) / r0

    hi = n // 4
    if hi <= OFDM_TAU_MIN + 16:
        return OfdmInfo(0, 0, None, None, 0.0, 0.0)
    band = gamma[OFDM_TAU_MIN:hi]
    med = float(np.median(band)) or 1e-30
    thresh = max(OFDM_PEAK_MIN, OFDM_PEAK_MED_MULT * med)

    k = 16
    peaks = []
    for tau in range(OFDM_TAU_MIN, hi):
        v = gamma[tau]
        if v < thresh:
            continue
        if v >= gamma[tau - k] and v >= gamma[min(n - 1, tau + k)]:
            peaks.append((tau, v))
    if not peaks:
        return OfdmInfo(0, 0, None, None, 0.0, 0.0)
    # The smallest-lag strong peak is the fundamental useful-symbol period Tu,
    # not a 2*Tu autocorrelation echo (which can be taller). Pick the earliest
    # lag among the peaks within 70% of the tallest.
    vmax = max(v for _, v in peaks)
    best_tau = min(tau for tau, v in peaks if v >= 0.7 * vmax)
    best_val = float(gamma[best_tau])
    tu_us = best_tau / fs * 1e6
    subcarrier = fs / best_tau
    cp_ratio = best_val / (1.0 - best_val) if best_val < 1.0 else 0.0
    return OfdmInfo(1, best_tau, round(tu_us, 3), round(subcarrier, 3),
                    round(cp_ratio, 4), round(best_val, 4))


# ─── Rule engine ─────────────────────────────────────────
def _conf(margin: float) -> float:
    return float(max(CONF_FLOOR, min(1.0, margin)))


def classify(spec: SpectrumInfo, env: EnvelopeInfo, fr: FreqInfo,
             cum: CumulantInfo, baud: BaudInfo, ofdm: OfdmInfo,
             fs: float) -> Verdict:
    """Ordered decision list. Every fired/considered rule appends one line to the
    reasoning trace (detect_compression.py's honest style — no fabricated
    probabilities). Confidence is a per-rule margin ratio, floored at 0.25.
    """
    R: list[str] = []
    baud_present = baud.baud_hz is not None

    def verdict(mod, conf, use_baud):
        # Only surface the OFDM flag/Tu when the verdict IS OFDM. A rect-pulse
        # PSK/FSK autocorr can throw a coincidental CP-lag bump; the raw cp_peak
        # is still preserved in features for offline retuning.
        is_ofdm = mod == "OFDM"
        return Verdict(
            modulation=mod, confidence=round(conf, 3),
            baud_hz=(baud.baud_hz if use_baud else None),
            baud_confidence=(baud.confidence if use_baud else "none"),
            occupied_bw_hz=round(spec.occupied_bw_hz, 2),
            carrier_offset_hz=round(spec.carrier_offset_hz, 2),
            snr_db=round(spec.snr_db, 2),
            ofdm_flag=(1 if is_ofdm else 0),
            ofdm_tu_us=(ofdm.tu_us if is_ofdm else None),
            ofdm_subcarrier_hz=(ofdm.subcarrier_hz if is_ofdm else None),
            reasoning=R)

    # (0) SNR gate. A CP-OFDM signal FILLS the band, so median(PSD) tracks the
    # signal (not the noise) and the median-floor SNR collapses — the cp flag
    # exempts it, since a genuine cyclic prefix is unambiguous structure.
    R.append(f"[0] SNR gate: snr={spec.snr_db:.1f} dB (min {SNR_MIN_DB}), "
             f"cp_flag={ofdm.flag}.")
    if spec.snr_db < SNR_MIN_DB and ofdm.flag == 0:
        R.append("    -> no signal clears the Welch floor by 6 dB -> noise.")
        return verdict("noise", _conf((SNR_MIN_DB - spec.snr_db) / SNR_MIN_DB), False)

    # (1) noise: flat, Gaussian, band-filling, no baud
    R.append(f"[1] noise test: flatness={spec.flatness:.2f}, |C42|={cum.c42_abs:.2f}, "
             f"occ/fs={spec.occupied_bw_hz / fs:.2f}, baud={'y' if baud_present else 'n'}.")
    if (spec.flatness > NOISE_FLATNESS_MIN and cum.c42_abs < NOISE_C42_MAX
            and not baud_present and spec.occupied_bw_hz > 0.9 * fs and ofdm.flag == 0):
        R.append("    -> flat, Gaussian, band-filling, no structure -> noise.")
        return verdict("noise", _conf(spec.flatness), False)

    # (2) CW / carrier
    R.append(f"[2] CW test: occ_bw={spec.occupied_bw_hz:.0f} Hz, sigma_af={fr.sigma_af:.1e}, "
             f"gamma_max={env.gamma_max:.2f}, env_modes={env.env_modes}.")
    if (spec.occupied_bw_hz < CW_OBW_MAX_HZ and fr.sigma_af < CW_SIGMA_AF_MAX
            and env.gamma_max < CW_GAMMA_MAX):
        R.append("    -> single narrow tone, no AM/FM -> CW.")
        return verdict("CW", _conf(CW_OBW_MAX_HZ / (spec.occupied_bw_hz + 1e-9)), False)

    # (3) OOK
    R.append(f"[3] OOK test: env_modes={env.env_modes}, sigma_aa={env.sigma_aa:.2f}, "
             f"baud={'y' if baud_present else 'n'}.")
    if env.env_modes == 2 and env.sigma_aa > OOK_SIGMA_AA_MIN and baud_present:
        R.append("    -> bimodal envelope + hard on/off swing + baud line -> OOK.")
        c = _conf(env.sigma_aa / OOK_SIGMA_AA_MIN)
        return verdict("OOK", min(1.0, c + (0.1 if baud.confidence == "high" else 0.0)), True)

    # (4) FSK (constant envelope, discrete inst-freq dwell)
    R.append(f"[4] FSK test: sigma_aa={env.sigma_aa:.2f} (<{FSK_SIGMA_AA_MAX}), "
             f"freq_modes={fr.freq_modes}, dwell={fr.freq_concentration:.2f}.")
    if (env.sigma_aa < FSK_SIGMA_AA_MAX and fr.freq_modes in (2, 4)
            and fr.freq_concentration > FSK_DWELL_MIN):
        fam = "2FSK" if fr.freq_modes == 2 else "4FSK"
        R.append(f"    -> constant envelope, {fr.freq_modes} discrete tones "
                 f"(spacing {_tone_spacing(fr.freq_mode_positions)}) -> {fam}.")
        return verdict(fam, _conf(fr.freq_concentration / FSK_DWELL_MIN), True)

    # (5) analog FM (constant envelope, continuous freq, no discrete dwell).
    # Guard on baud: a pulse-shaped digital mode (GFSK/GMSK) is also constant-
    # envelope with smoothed, low-dwell frequency, but it carries a symbol clock.
    # Analog voice FM does not, so a detectable baud line means this is NOT analog
    # FM -> fall through to the digital-unknown posture rather than mislabel it.
    R.append(f"[5] FM test: sigma_aa={env.sigma_aa:.2f}, dwell={fr.freq_concentration:.2f}, "
             f"occ_bw={spec.occupied_bw_hz:.0f} Hz, baud={'y' if baud_present else 'n'}.")
    if env.sigma_aa < FSK_SIGMA_AA_MAX and fr.freq_concentration <= FSK_DWELL_MIN:
        fam = "WFM" if spec.occupied_bw_hz >= WFM_OBW_MIN_HZ else "NFM"
        if baud_present:
            # A pulse-shaped digital mode (GFSK/GMSK) is also constant-envelope
            # with smoothed, low-dwell frequency; analog voice FM carries no
            # symbol clock. A baud line here means we can't rule digital out, so
            # keep the analog label but drop confidence and say so.
            R.append(f"    -> constant envelope, continuous frequency, but a baud "
                     f"line is present -> {fam} at low confidence (could be a "
                     f"pulse-shaped digital mode such as GFSK/GMSK).")
            return verdict(fam, _conf(0.35), False)
        R.append(f"    -> constant envelope, continuous frequency, no baud -> {fam}.")
        return verdict(fam, _conf(0.6), False)

    # (6) AM
    R.append(f"[6] AM test: gamma_max={env.gamma_max:.2f}, sigma_aa={env.sigma_aa:.2f}, "
             f"sigma_af={fr.sigma_af:.1e}.")
    if (env.gamma_max > AM_GAMMA_MAX_MIN and env.sigma_aa > AM_SIGMA_AA_MIN
            and fr.sigma_af < AM_SIGMA_AF_MAX):
        R.append("    -> tonal envelope modulation, no FM, carrier present -> AM.")
        return verdict("AM", _conf(env.gamma_max / AM_GAMMA_MAX_MIN), False)

    # (7) PSK by cumulants (post carrier-derotation)
    R.append(f"[7] PSK test: |C20|={cum.c20_abs:.2f}, |C40|={cum.c40_abs:.2f}, "
             f"|C42|={cum.c42_abs:.2f} (derotate {cum.derotate_hz:.0f} Hz).")
    if cum.c20_abs > CUM_C20_BPSK_MIN and cum.c42_abs > CUM_C42_BPSK_MIN:
        R.append("    -> real C20 + |C42|~2 -> BPSK.")
        c = _conf(cum.c42_abs / CUM_C42_BPSK_MIN)
        return verdict("BPSK", min(1.0, c + (0.1 if baud_present else 0.0)), True)
    if (cum.c20_abs < CUM_C20_PSK_MAX and cum.c40_abs > CUM_C40_QPSK_MIN
            and CUM_C42_PSK_LO < cum.c42_abs < CUM_C42_PSK_HI
            and env.sigma_aa < 0.30):
        R.append("    -> suppressed C20, |C40|~1, |C42|~1 -> QPSK/PSK "
                 "(family only; 8PSK+ not separated at these features).")
        c = _conf(cum.c40_abs / CUM_C40_QPSK_MIN)
        return verdict("QPSK/PSK", min(1.0, c + (0.1 if baud_present else 0.0)), True)

    # (8) OFDM
    R.append(f"[8] OFDM test: cp_flag={ofdm.flag}, |C20|={cum.c20_abs:.2f}, "
             f"|C42|={cum.c42_abs:.2f}, flatness={spec.flatness:.2f}.")
    if (ofdm.flag == 1 and cum.c20_abs < OFDM_C20_MAX and cum.c42_abs < OFDM_C42_MAX
            and spec.flatness > OFDM_FLATNESS_MIN):
        v = verdict("OFDM", _conf(0.7), False)
        v.reasoning.append(f"    -> cyclic-prefix bump at Tu={ofdm.tu_us:.1f} us "
                           f"(subcarrier {ofdm.subcarrier_hz:.0f} Hz) -> OFDM.")
        if spec.occupied_bw_hz > OFDM_WIDE_FRAC * fs:
            v.reasoning.append("    -> signal wider than capture span "
                               "-> verdict low-confidence.")
            v.confidence = min(v.confidence, 0.5)
        return v

    # (9) fallback: digital-unknown (the encrypted-labeling / legal posture)
    R.append(f"[9] fallback: digital indicators baud={'y' if baud_present else 'n'}, "
             f"|C42|={cum.c42_abs:.2f}.")
    R.append("    -> digital, likely encrypted -> out of scope; payload will not "
             "be decoded.")
    conf = _conf(0.3)
    return verdict("digital-unknown", conf, baud_present)


def _tone_spacing(positions: list[float]) -> str:
    if len(positions) < 2:
        return "n/a"
    p = sorted(positions)
    diffs = np.diff(p)
    return f"{np.mean(diffs) / 1000.0:.1f} kHz"


# ─── Whole-capture entry point ───────────────────────────
def preprocess(x: np.ndarray, fs: float) -> np.ndarray:
    """Drop the settle window, remove the complex mean (DC/LO spike), normalize
    to unit RMS, and cap at ANALYZE_MAX_S."""
    n0 = int(SETTLE_S * fs)
    x = x[n0:]
    max_n = int(ANALYZE_MAX_S * fs)
    if x.size > max_n:
        log.info("truncating %d -> %d samples (%.1f s cap)", x.size, max_n, ANALYZE_MAX_S)
        x = x[:max_n]
    x = x - np.mean(x)
    rms = np.sqrt(np.mean(np.abs(x) ** 2)) or 1e-12
    return (x / rms).astype(np.complex128)


def analyze_array(x: np.ndarray, fs: float, freq_hz: int = 0) -> Verdict:
    """Run the full feature chain on a complex baseband array and classify.
    Pure/offline: no DB, no file I/O — the unit test drives this directly.
    """
    x = preprocess(x, fs)
    freqs, psd = welch_psd(x, fs)
    spec = occupied_bandwidth(freqs, psd, fs)

    # recenter so all downstream stats sit at baseband
    n = np.arange(x.size)
    xr = x * np.exp(-2j * np.pi * spec.carrier_offset_hz * n / fs)

    amp, inst_freq, _phase = instantaneous_afp(xr, fs)
    inst_freq_s = smooth(inst_freq, fs)
    w = _smooth_window(fs)
    level = block_median(inst_freq_s, w)   # clean piecewise freq levels (FSK)
    env, fr = amc_discriminants(xr, amp, level, fs)
    cum = cumulants(xr, fs)
    baud = baud_estimate(xr, level, fs, w)
    ofdm = ofdm_cp_detect(xr, fs)

    v = classify(spec, env, fr, cum, baud, ofdm, fs)
    v.features = {
        "gamma_max": round(env.gamma_max, 4),
        "sigma_aa": round(env.sigma_aa, 4),
        "sigma_ap": round(fr.sigma_ap, 4),
        "sigma_dp": round(fr.sigma_dp, 4),
        "sigma_af": round(fr.sigma_af, 6),
        "C20_abs": round(cum.c20_abs, 4),
        "C20_re": round(cum.c20_re, 4),
        "C20_im": round(cum.c20_im, 4),
        "C40_abs": round(cum.c40_abs, 4),
        "C42_abs": round(cum.c42_abs, 4),
        "derotate_hz": round(cum.derotate_hz, 2),
        "flatness": round(spec.flatness, 4),
        "psd_kurtosis": round(spec.psd_kurtosis, 3),
        "noise_floor": spec.noise_floor,
        "env_modes": env.env_modes,
        "env_mode_positions": [round(p, 4) for p in env.env_mode_positions],
        "freq_modes": fr.freq_modes,
        "freq_mode_positions_hz": [round(p, 1) for p in fr.freq_mode_positions],
        "freq_concentration": round(fr.freq_concentration, 4),
        "baud_hz": baud.baud_hz,
        "baud_line_snr_db": baud.line_snr_db,
        "baud_source": baud.source,
        "psd_peak_offset_hz": round(spec.psd_peak_offset_hz, 1),
        "ofdm_cp_peak": ofdm.peak,
        "ofdm_cp_ratio": ofdm.cp_ratio,
        "ofdm_tu_samples": ofdm.tu_samples,
        "sample_rate_hz": int(fs),
        "freq_hz": int(freq_hz),
    }
    return v


# ─── I/O layer ───────────────────────────────────────────
def read_cs8(path: str, max_samples: int | None = None) -> np.ndarray:
    """Read a D2 .cs8 file (signed int8 interleaved I,Q,I,Q,...) into complex64.
    Odd trailing byte (half a sample) is dropped. Scaled by 1/128 so full-scale
    is ~unit magnitude."""
    raw = np.fromfile(path, dtype=np.int8, count=(2 * max_samples if max_samples else -1))
    if raw.size % 2:
        raw = raw[:-1]
    return (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 128.0


def read_manifest(cs8_path: str) -> dict | None:
    """Load the D2 .json sidecar (same basename), or None if absent."""
    mp = cs8_path[:-4] + ".json" if cs8_path.endswith(".cs8") else cs8_path + ".json"
    p = Path(mp)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:  # pragma: no cover - defensive
        log.warning("could not read manifest %s: %s", mp, e)
        return None


def _sql_str(value: str) -> str:
    """Escape a Python str for a single-quoted ClickHouse SQL literal
    (the iq_capture.py pattern)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ─── Thin ClickHouse shims (stubbed by the self-test via attribute assign) ──
def _ch_rows(sql: str):
    return db.query_rows(sql)


def _ch_insert(table: str, rows):
    db.insert(table, rows)


def resolve_capture(capture_id: str) -> dict:
    """Look up an iq_captures row (migration 023) by capture_id. Raises with a
    clear message if the row (or its now-rotated file) is gone."""
    rows = _ch_rows(
        "SELECT capture_id, trigger_id, dongle_id, freq_hz, sample_rate_hz, "
        "duration_s, path, source FROM iq_captures "
        f"WHERE capture_id='{_sql_str(capture_id)}' LIMIT 1")
    if not rows:
        raise SystemExit(f"no iq_captures row for capture_id={capture_id}")
    row = rows[0]
    if not Path(row["path"]).exists():
        raise SystemExit(
            f"capture {capture_id} row exists but file {row['path']} is gone "
            "(rotated away by iq_capture's 2 GB cap) — nothing to analyze.")
    return row


def insert_result(v: Verdict, *, capture_id: str = "", trigger_id: str = "",
                  dongle_id: str = "", file_path: str = "", freq_hz: int = 0,
                  sample_rate_hz: int, source: str = "") -> None:
    """Write one spectrum.blind_signal_features row (migration 024). Funnels
    through _ch_insert so the self-test can capture rows without ClickHouse."""
    row = {
        "capture_id": capture_id,
        "trigger_id": trigger_id,
        "dongle_id": dongle_id,
        "file_path": file_path,
        "freq_hz": int(freq_hz),
        "sample_rate_hz": int(sample_rate_hz),
        "snr_db": round(float(v.snr_db), 3),
        "occupied_bw_hz": round(float(v.occupied_bw_hz), 3),
        "carrier_offset_hz": round(float(v.carrier_offset_hz), 3),
        "modulation": v.modulation,
        "confidence": round(float(v.confidence), 4),
        "baud_hz": (None if v.baud_hz is None else round(float(v.baud_hz), 3)),
        "baud_confidence": v.baud_confidence,
        "ofdm_flag": int(v.ofdm_flag),
        "ofdm_tu_us": (None if v.ofdm_tu_us is None else round(float(v.ofdm_tu_us), 4)),
        "ofdm_subcarrier_hz": (None if v.ofdm_subcarrier_hz is None
                               else round(float(v.ofdm_subcarrier_hz), 4)),
        "features": json.dumps(v.features),
        "reasoning": "\n".join(v.reasoning),
        "source": source,
        "analyzer_version": ANALYZER_VERSION,
    }
    _ch_insert("blind_signal_features", [row])


# ─── Waterfall thumbnail (cosmetic, numpy-only) ──────────
_WF_RAMP = " ▁▂▃▄▅▆▇█"   # 9 shades, low -> high power


def waterfall_thumbnail(x: np.ndarray, fs: float, rows: int = 6,
                        cols: int = 48) -> list[str]:
    """A compact time x frequency waterfall as Unicode shading — rows are time
    (top = earliest), columns are frequency (DC centered). A tiny STFT over the
    loaded IQ: split into `rows` time slices, FFT each, bin to `cols`, map power
    to a shade. Purely for the identity card; no effect on classification.
    """
    n = x.size
    if n < rows * cols * 2:
        return []
    x = x - np.mean(x)
    seg = n // rows
    win = np.hanning(seg)
    grid = np.empty((rows, cols), dtype=np.float64)
    for r in range(rows):
        s = x[r * seg:(r + 1) * seg] * win
        p = np.abs(np.fft.fftshift(np.fft.fft(s))) ** 2
        b = p.size // cols
        grid[r] = 10.0 * np.log10(p[:b * cols].reshape(cols, b).mean(axis=1) + 1e-12)
    lo = np.percentile(grid, 20.0)
    hi = np.percentile(grid, 99.5)
    idx = np.rint(np.clip((grid - lo) / (hi - lo + 1e-9), 0.0, 1.0)
                  * (len(_WF_RAMP) - 1)).astype(int)
    return ["".join(_WF_RAMP[i] for i in row) for row in idx]


# ─── Identity-card printer ───────────────────────────────
def print_identity_card(v: Verdict, *, freq_hz: int = 0,
                        waterfall: list[str] | None = None) -> None:
    center = f"{freq_hz / 1e6:.4f} MHz" if freq_hz else "unknown (offset relative to capture center)"
    baud = "n/a" if v.baud_hz is None else f"{v.baud_hz:,.0f} Bd ({v.baud_confidence})"
    ofdm = "no"
    if v.ofdm_flag:
        ofdm = f"yes (Tu={v.ofdm_tu_us:.1f} us, subcarrier {v.ofdm_subcarrier_hz:.0f} Hz)"
    lines = [
        "",
        "  ┌─ blind signal identity card ──────────────────────────",
        f"  │ modulation      : {v.modulation}   (confidence {v.confidence:.2f})",
        f"  │ occupied bw     : {v.occupied_bw_hz / 1e3:,.1f} kHz",
        f"  │ carrier offset  : {v.carrier_offset_hz / 1e3:+.2f} kHz",
        f"  │ capture center  : {center}",
        f"  │ symbol/baud     : {baud}",
        f"  │ OFDM            : {ofdm}",
        f"  │ SNR (est)       : {v.snr_db:.1f} dB",
    ]
    if waterfall:
        lines.append("  │ waterfall       :  time ↓  freq across (DC centered)")
        for row in waterfall:
            lines.append("  │   " + row)
    lines += [
        "  └───────────────────────────────────────────────────────",
        "  reasoning:",
    ]
    for r in v.reasoning:
        lines.append("    " + r)
    print("\n".join(lines))


# ─── CLI ─────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", metavar="PATH.cs8", help="analyze a .cs8 file directly")
    g.add_argument("--capture-id", metavar="UUID",
                   help="look the .cs8 path up in spectrum.iq_captures")
    ap.add_argument("--rate", type=int, help="sample rate Hz (required if no sidecar)")
    ap.add_argument("--freq", type=int, default=None,
                    help="capture center Hz (default 0 = unknown)")
    ap.add_argument("--dry-run", action="store_true", help="print only, no INSERT")
    ap.add_argument("--json", action="store_true", help="print the feature dict as JSON")
    args = ap.parse_args(argv)

    capture_id = trigger_id = dongle_id = source = ""
    freq_hz = args.freq or 0

    if args.capture_id:
        row = resolve_capture(args.capture_id)
        path = row["path"]
        rate = int(row["sample_rate_hz"])
        freq_hz = int(row["freq_hz"]) if args.freq is None else args.freq
        capture_id = row["capture_id"]
        trigger_id = row.get("trigger_id", "")
        dongle_id = row.get("dongle_id", "")
        source = row.get("source", "")
    else:
        path = args.file
        man = read_manifest(path)
        if man is not None:
            rate = int(man["sample_rate_hz"])
            if args.freq is None:
                freq_hz = int(man.get("freq_hz", 0))
            trigger_id = man.get("trigger_id", "")
            dongle_id = man.get("dongle_id", "")
        else:
            rate = args.rate
        if args.rate:                       # explicit --rate always overrides
            rate = args.rate
        if rate is None:
            ap.error("no sidecar manifest found — supply --rate")

    x = read_cs8(path, max_samples=int(ANALYZE_MAX_S * rate) + 1)
    log.info("analyzing %s (%d samples, fs=%d Hz)", path, x.size, rate)
    v = analyze_array(x, float(rate), freq_hz=freq_hz)

    if args.json:
        print(json.dumps({
            "modulation": v.modulation, "confidence": v.confidence,
            "baud_hz": v.baud_hz, "baud_confidence": v.baud_confidence,
            "occupied_bw_hz": v.occupied_bw_hz, "carrier_offset_hz": v.carrier_offset_hz,
            "snr_db": v.snr_db, "ofdm_flag": v.ofdm_flag, "ofdm_tu_us": v.ofdm_tu_us,
            "ofdm_subcarrier_hz": v.ofdm_subcarrier_hz,
            "features": v.features, "reasoning": v.reasoning,
        }, indent=2))
    else:
        wf = waterfall_thumbnail(x, float(rate))
        print_identity_card(v, freq_hz=freq_hz, waterfall=wf)

    if args.dry_run:
        log.info("dry-run: skipping INSERT.")
        return 0

    insert_result(v, capture_id=capture_id, trigger_id=trigger_id,
                  dongle_id=dongle_id, file_path=path, freq_hz=freq_hz,
                  sample_rate_hz=rate, source=source)
    log.info("wrote blind_signal_features row (modulation=%s)", v.modulation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
