"""Tests for detect_clipping — ADC saturation detection on raw IQ bytes."""

import numpy as np
import pytest
from scanner import detect_clipping


def test_clean_signal_no_clipping():
    """Normal IQ data (mid-range values) should show no clipping."""
    rng = np.random.default_rng(42)
    iq = rng.integers(10, 245, size=2048, dtype=np.uint8).tobytes()

    result = detect_clipping(iq)

    assert result["clipped"] is False
    assert result["clip_fraction"] == 0.0
    assert result["n_clipped"] == 0
    assert result["n_samples"] == 2048


def test_fully_saturated():
    """All samples at 0 or 255 → 100% clipped."""
    iq = bytes([0, 255] * 1024)

    result = detect_clipping(iq)

    assert result["clipped"] is True
    assert result["clip_fraction"] == 1.0
    assert result["n_clipped"] == 2048
    assert result["n_samples"] == 2048


def test_partial_clipping_above_threshold():
    """10% clipped samples (above 5% threshold) → clipped=True."""
    # 2000 bytes total, 200 at boundary values = 10%
    clean = bytes([128] * 1800)
    clipped = bytes([0] * 100 + [255] * 100)
    iq = clean + clipped

    result = detect_clipping(iq)

    assert result["clipped"] is True
    assert result["clip_fraction"] == pytest.approx(0.10, abs=0.001)
    assert result["n_clipped"] == 200


def test_below_threshold():
    """2% clipped samples (below 5% threshold) → clipped=False."""
    # 2000 bytes, 40 clipped = 2%
    clean = bytes([128] * 1960)
    clipped = bytes([0] * 20 + [255] * 20)
    iq = clean + clipped

    result = detect_clipping(iq)

    assert result["clipped"] is False
    assert result["clip_fraction"] == pytest.approx(0.02, abs=0.001)


def test_custom_threshold():
    """Custom threshold should change the clipped decision."""
    # 3% clipped: above 1% threshold, below 5% default
    clean = bytes([128] * 1940)
    clipped = bytes([255] * 60)
    iq = clean + clipped

    assert detect_clipping(iq, threshold=0.01)["clipped"] is True
    assert detect_clipping(iq, threshold=0.05)["clipped"] is False


def test_only_zeros_count():
    """Only byte value 0 should count as clipped (low-end saturation)."""
    # Byte value 1 is NOT clipped, only 0
    iq = bytes([0] * 100 + [1] * 1900)

    result = detect_clipping(iq)

    assert result["n_clipped"] == 100
    assert result["clip_fraction"] == pytest.approx(0.05, abs=0.001)


def test_only_255_count():
    """Only byte value 255 should count as clipped (high-end saturation)."""
    # Byte value 254 is NOT clipped, only 255
    iq = bytes([255] * 100 + [254] * 1900)

    result = detect_clipping(iq)

    assert result["n_clipped"] == 100


def test_empty_input():
    """Empty byte string should return safe defaults."""
    result = detect_clipping(b"")

    assert result["clipped"] is False
    assert result["clip_fraction"] == 0.0
    assert result["n_clipped"] == 0
    assert result["n_samples"] == 0


def test_realistic_fm_overload():
    """Simulate strong FM signal saturating — I channel clips, Q stays mid-range.

    When a single strong signal overloads the ADC, one or both IQ channels
    rail to the limits while the other may stay normal. This pattern is
    common near Athens FM transmitters at high gain.
    """
    fft_size = 1024
    # I channel: 30% of samples clipped at 255 (strong positive signal)
    i_samples = np.full(fft_size, 128, dtype=np.uint8)
    i_samples[:307] = 255  # ~30% clipped
    # Q channel: normal range
    q_samples = np.random.default_rng(42).integers(60, 200, size=fft_size, dtype=np.uint8)

    iq = np.empty(fft_size * 2, dtype=np.uint8)
    iq[0::2] = i_samples
    iq[1::2] = q_samples

    result = detect_clipping(iq.tobytes())

    # ~15% of total bytes are clipped (307 out of 2048)
    assert result["clipped"] is True
    assert result["clip_fraction"] > 0.10
