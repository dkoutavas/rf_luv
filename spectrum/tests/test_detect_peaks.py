"""Tests for detect_peaks — prominence-based spectral peak detection."""

import pytest
from tests.helpers import make_bins
from scanner import detect_peaks


def _flat_bins(n: int, power: float = -60.0, start_freq: int = 100_000_000, step: int = 100_000):
    """Helper: create n bins at uniform power."""
    return make_bins([(start_freq + i * step, power) for i in range(n)])


def test_single_obvious_peak():
    """One bin 30 dB above flat floor → exactly 1 peak."""
    bins = _flat_bins(21)
    bins[10]["power_dbfs"] = -30.0  # 30 dB prominence

    peaks = detect_peaks(bins)

    assert len(peaks) == 1
    assert peaks[0]["freq_hz"] == bins[10]["freq_hz"]
    assert peaks[0]["power_dbfs"] == -30.0
    assert peaks[0]["prominence_db"] == pytest.approx(30.0, abs=0.1)


def test_flat_spectrum_no_peaks():
    """All equal power → zero prominence everywhere → no peaks."""
    bins = _flat_bins(50)
    assert detect_peaks(bins) == []


def test_too_few_bins_returns_empty():
    """Fewer than 2*PEAK_NEIGHBOR_BINS+1 = 11 bins → empty result."""
    bins = _flat_bins(10)
    bins[5]["power_dbfs"] = -10.0  # would be a peak if there were enough bins
    assert detect_peaks(bins) == []


def test_below_threshold_ignored():
    """8 dB prominence (below 10 dB threshold) → no peak."""
    bins = _flat_bins(21, power=-50.0)
    bins[10]["power_dbfs"] = -42.0  # prominence = 8 dB

    assert detect_peaks(bins) == []


def test_at_exact_threshold():
    """10 dB prominence (exactly at threshold) → peak detected (>= condition)."""
    bins = _flat_bins(21, power=-50.0)
    bins[10]["power_dbfs"] = -40.0  # prominence = 10 dB

    peaks = detect_peaks(bins)
    assert len(peaks) == 1


def test_multiple_peaks():
    """Two separated peaks should both be detected."""
    bins = _flat_bins(50)
    bins[10]["power_dbfs"] = -30.0
    bins[30]["power_dbfs"] = -25.0

    peaks = detect_peaks(bins)

    assert len(peaks) == 2
    peak_freqs = {p["freq_hz"] for p in peaks}
    assert bins[10]["freq_hz"] in peak_freqs
    assert bins[30]["freq_hz"] in peak_freqs


def test_edge_bins_excluded():
    """Bins within PEAK_NEIGHBOR_BINS of the edges are never evaluated."""
    bins = _flat_bins(21)
    # Put loudest signals at the very edges
    bins[0]["power_dbfs"] = -10.0
    bins[20]["power_dbfs"] = -10.0

    # Neither should be detected — they're in the excluded edge zones
    assert detect_peaks(bins) == []


def test_output_dict_keys():
    """Each peak dict should have the expected keys."""
    bins = _flat_bins(21)
    bins[10]["power_dbfs"] = -20.0

    peaks = detect_peaks(bins)
    assert len(peaks) == 1

    p = peaks[0]
    assert p["peak"] is True
    assert "freq_hz" in p
    assert "power_dbfs" in p
    assert "prominence_db" in p


# ─── Edge cases ──────────────────────────────────────────


def test_adjacent_peaks_suppress_each_other():
    """Two strong bins within PEAK_NEIGHBOR_BINS inflate each other's neighbor avg.

    In Athens, FM stations are every 200 kHz. At 100 kHz bin width, two
    adjacent strong bins (e.g. index 10 and 12) fall within each other's
    5-bin neighbor window. This raises the neighbor average, reducing
    prominence. One or both may drop below threshold.
    """
    # 30 bins at -60, two adjacent strong bins at -30
    bins = _flat_bins(30, power=-60.0)
    bins[12]["power_dbfs"] = -30.0
    bins[14]["power_dbfs"] = -30.0  # 2 bins apart, within PEAK_NEIGHBOR_BINS=5

    peaks = detect_peaks(bins)

    # Each peak's neighbor window includes the other strong bin.
    # For bin 12: left neighbors are [-60]*5, right neighbors include bin 14 at -30.
    # neighbor_avg = mean([-60]*5 + [-30, -60, -60, -60, -60]) ≈ -54
    # prominence = -30 - (-54) = 24 dB — still above 10 dB threshold
    # Both should still be detected, but with reduced prominence vs isolated
    assert len(peaks) >= 1  # at minimum one is detected

    # Verify prominence is lower than 30 dB (what it would be if isolated)
    for p in peaks:
        assert p["prominence_db"] < 30.0


def test_plateau_not_detected_as_peak():
    """A wide raised region (plateau) should not produce peaks.

    If 15 consecutive bins are at -30 dB while the rest are at -60,
    the center bins have neighbors also at -30 → prominence ≈ 0.
    Only the edges of the plateau might show prominence.
    """
    bins = _flat_bins(40, power=-60.0)
    for i in range(10, 25):
        bins[i]["power_dbfs"] = -30.0

    peaks = detect_peaks(bins)

    # Interior bins (15-20) have all neighbors at -30 → no prominence
    interior_freqs = {bins[i]["freq_hz"] for i in range(15, 20)}
    peak_freqs = {p["freq_hz"] for p in peaks}
    assert interior_freqs.isdisjoint(peak_freqs), (
        "Interior of plateau should not be detected as peaks"
    )
