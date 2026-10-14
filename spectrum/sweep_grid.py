"""The scanner's sweep geometry: presets, 2.048 MHz tiles and 100 kHz bins.

scanner.py sweeps by retuning the dongle in steps of one sample rate (2.048 MHz,
one "tile" per step) and averaging each tile's 1024 FFT bins into 100 kHz
output bins. Two consequences matter to anything that reads peaks:

- The bins are not one uniform 100 kHz grid. Every tile restarts the grid, so
  the labels slip by 48 kHz every 2.048 MHz. The airband preset starts at
  118 MHz and so has its own grid, a few tens of kHz off the full sweep's.
- The RTL-SDR leaks its local oscillator into the middle of every tile. That DC
  spike lands in one bin per tile, labeled tile center + 26 kHz. A peak that
  only ever shows up in that bin is most likely the dongle hearing itself, like
  the hum of a mixing desk that sits at the same place in every recording.

This module mirrors scanner.py's arithmetic and reads the same env variables
with the same defaults. test_sweep_grid.py checks it against
scanner.downsample_bins, so a change to the scanner's binning fails a test.
"""

from __future__ import annotations

import os

from config import config

SAMPLE_RATE = config.SAMPLE_RATE
FFT_SIZE = config.FFT_SIZE
BIN_WIDTH_HZ = int(os.environ.get("SCAN_BIN_WIDTH", "100000"))

# preset name (the prefix of every sweep_id) -> (first Hz, last Hz)
PRESETS = {
    "full": (int(os.environ.get("SCAN_FREQ_START", "88000000")),
             int(os.environ.get("SCAN_FREQ_END", "470000000"))),
    "airband": (int(os.environ.get("SCAN_AIRBAND_START", "118000000")),
                int(os.environ.get("SCAN_AIRBAND_END", "137000000"))),
}

_FFT_BIN_HZ = SAMPLE_RATE / FFT_SIZE
_FFT_BINS_PER_BIN = max(1, int(BIN_WIDTH_HZ / _FFT_BIN_HZ))
# After fftshift, DC (the LO) is FFT bin FFT_SIZE // 2. This is the first FFT
# bin of the output bin that holds it.
_DC_CHUNK_START = (FFT_SIZE // 2 // _FFT_BINS_PER_BIN) * _FFT_BINS_PER_BIN


def _label(center_hz: int, chunk_start: int) -> int:
    """The label downsample_bins gives the output bin whose FFT bins start at chunk_start."""
    return int(center_hz - SAMPLE_RATE // 2 + (chunk_start + _FFT_BINS_PER_BIN // 2) * _FFT_BIN_HZ)


def tile_centers(preset: str) -> list[int]:
    """Tuning frequencies of one preset's tiles, in sweep order."""
    start, end = PRESETS[preset]
    centers = []
    center = start + SAMPLE_RATE // 2
    while center < end + SAMPLE_RATE // 2:
        centers.append(center)
        center += SAMPLE_RATE
    return centers


def bin_labels(preset: str) -> list[int]:
    """Every bin label one sweep of this preset writes, low to high."""
    start, end = PRESETS[preset]
    labels = []
    for center in tile_centers(preset):
        for chunk in range(0, FFT_SIZE, _FFT_BINS_PER_BIN):
            f = _label(center, chunk)
            if start <= f <= end:
                labels.append(f)
    return labels


def dc_bin(center_hz: int) -> int:
    """Label of the bin that holds this tile's LO, where the DC spike sits."""
    return _label(center_hz, _DC_CHUNK_START)


def is_dc_bin(freq_hz: int) -> bool:
    """True if freq_hz is the DC bin of some tile of some preset."""
    for preset in PRESETS:
        start, _ = PRESETS[preset]
        first = start + SAMPLE_RATE // 2
        n = round((freq_hz - first) / SAMPLE_RATE)
        centers = tile_centers(preset)
        if 0 <= n < len(centers) and dc_bin(centers[n]) == freq_hz:
            return True
    return False


def detectable_range(preset: str) -> tuple[int, int]:
    """The frequencies where this preset's sweeps can produce a peak.

    The peak detector compares each bin with PEAK_NEIGHBOR_BINS neighbors on
    each side, so the first and last few bins of a sweep can never peak. For
    the airband preset that is 118.05-118.45 and 136.58-136.98 MHz, where
    Athens Tower (118.100) lives. A sweep that cannot see a frequency must
    not count as a look at it.
    """
    labels = bin_labels(preset)
    n = config.PEAK_NEIGHBOR_BINS
    half = BIN_WIDTH_HZ // 2
    return labels[n] - half, labels[-n - 1] + half


def presets_covering(freq_hz: int) -> list[str]:
    """Presets whose sweeps could have peaked at freq_hz: the denominators of a duty cycle."""
    return [p for p in PRESETS if detectable_range(p)[0] <= freq_hz <= detectable_range(p)[1]]
