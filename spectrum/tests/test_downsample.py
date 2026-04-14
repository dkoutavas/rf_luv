"""Tests for downsample_bins — FFT bin aggregation and frequency labeling."""

import math

import numpy as np
import pytest
from scanner import downsample_bins

FFT_SIZE = 1024
SAMPLE_RATE = 2_048_000
FFT_BIN_HZ = SAMPLE_RATE / FFT_SIZE  # 2000 Hz


def test_linear_averaging_not_db():
    """Averaging must happen in linear domain, not dB domain.

    Two bins at -10 and -30 dB:
      dB average would give -20.0 dB (wrong)
      Linear average: mean(0.1, 0.001) = 0.0505 → 10*log10(0.0505) ≈ -12.97 dB (correct)

    This is the Jensen's inequality issue noted in scanner.py.
    """
    # Create a 2-bin input that will be merged into 1 output bin
    power_db = np.array([-10.0, -30.0])
    center_freq = 100_000_000
    # sample_rate / fft_size must give fft_bin_hz, and bins_per_output = 2
    # With fft_size=2, fft_bin_hz = sample_rate/2, output_bin_hz = sample_rate
    # Simpler: just use len(power_db) as fft_size
    sample_rate = 4000  # fft_bin_hz = 4000/2 = 2000
    output_bin_hz = 4000  # bins_per_output = 4000/2000 = 2

    result = downsample_bins(power_db, center_freq, sample_rate, output_bin_hz)

    assert len(result) == 1
    # Linear average of 10^(-10/10) and 10^(-30/10) = mean(0.1, 0.001) = 0.0505
    expected_db = 10 * math.log10(0.0505)  # ≈ -12.97
    assert result[0]["power_dbfs"] == pytest.approx(expected_db, abs=0.1)
    # Confirm it's NOT the dB average
    assert abs(result[0]["power_dbfs"] - (-20.0)) > 5


def test_equal_bins_stay_same():
    """Bins with equal power should average to the same power."""
    power_db = np.array([-40.0, -40.0, -40.0, -40.0])
    result = downsample_bins(power_db, 100_000_000, 8000, 4000)  # bins_per_output=2

    for r in result:
        assert r["power_dbfs"] == pytest.approx(-40.0, abs=0.1)


def test_frequency_labels():
    """First output bin center frequency should be correctly computed."""
    center_freq = 100_000_000
    power_db = np.zeros(FFT_SIZE)

    result = downsample_bins(power_db, center_freq, SAMPLE_RATE, 100_000)

    # freq_start = center - sample_rate/2 = 100M - 1.024M = 98_976_000
    freq_start = center_freq - SAMPLE_RATE // 2
    bins_per_output = int(100_000 / FFT_BIN_HZ)  # 50
    # First bin center = freq_start + (0 + 25) * fft_bin_hz
    expected_first = int(freq_start + (0 + bins_per_output // 2) * FFT_BIN_HZ)
    assert result[0]["freq_hz"] == expected_first


def test_output_bin_count():
    """Correct number of output bins for 1024 FFT / 100kHz output."""
    power_db = np.zeros(FFT_SIZE)
    result = downsample_bins(power_db, 100_000_000, SAMPLE_RATE, 100_000)

    bins_per_output = int(100_000 / FFT_BIN_HZ)  # 50
    expected_count = len(range(0, FFT_SIZE, bins_per_output))
    assert len(result) == expected_count


def test_power_rounded_to_1dp():
    """All power_dbfs values should be rounded to 1 decimal place."""
    power_db = np.random.default_rng(42).uniform(-60, -10, size=FFT_SIZE)
    result = downsample_bins(power_db, 100_000_000, SAMPLE_RATE, 100_000)

    for r in result:
        assert r["power_dbfs"] == round(r["power_dbfs"], 1)


# ─── Edge cases ──────────────────────────────────────────


def test_last_chunk_partial():
    """When fft_size isn't divisible by bins_per_output, the last chunk is smaller.

    With 10 bins and bins_per_output=3, we get chunks of [3, 3, 3, 1].
    The last chunk has only 1 bin — its average is just that single bin's value.
    """
    # 10 bins, group into 3 → 4 output bins (3+3+3+1)
    power_db = np.array([-50.0] * 9 + [-20.0])  # last bin is loud
    sample_rate = 20_000  # fft_bin_hz = 20000/10 = 2000
    output_bin_hz = 6000  # bins_per_output = 6000/2000 = 3

    result = downsample_bins(power_db, 100_000_000, sample_rate, output_bin_hz)

    assert len(result) == 4  # ceil(10/3) = 4 chunks
    # Last output bin: single value of -20.0
    assert result[-1]["power_dbfs"] == pytest.approx(-20.0, abs=0.1)
    # First three: uniform -50.0
    for r in result[:3]:
        assert r["power_dbfs"] == pytest.approx(-50.0, abs=0.1)


def test_single_bin_passthrough():
    """When bins_per_output=1 (output_bin_hz == fft_bin_hz), output matches input."""
    power_db = np.array([-30.0, -45.0, -60.0, -15.0])
    sample_rate = 8000  # fft_bin_hz = 8000/4 = 2000
    output_bin_hz = 2000  # bins_per_output = 1

    result = downsample_bins(power_db, 100_000_000, sample_rate, output_bin_hz)

    assert len(result) == 4
    for i, r in enumerate(result):
        # Linear→dB→linear round-trip introduces tiny floating-point error
        assert r["power_dbfs"] == pytest.approx(power_db[i], abs=0.1)
