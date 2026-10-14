"""sweep_grid mirrors the scanner's binning. These tests hold it to that."""

import numpy as np

import scanner
import sweep_grid


def _scanner_bins(center_hz: int) -> list[dict]:
    return scanner.downsample_bins(np.zeros(scanner.FFT_SIZE), center_hz,
                                   scanner.SAMPLE_RATE, scanner.BIN_WIDTH)


def test_defaults_match_the_scanner():
    assert sweep_grid.SAMPLE_RATE == scanner.SAMPLE_RATE
    assert sweep_grid.FFT_SIZE == scanner.FFT_SIZE
    assert sweep_grid.BIN_WIDTH_HZ == scanner.BIN_WIDTH
    assert sweep_grid.PRESETS["full"] == (scanner.FREQ_START, scanner.FREQ_END)
    assert sweep_grid.PRESETS["airband"] == (scanner.AIRBAND_START, scanner.AIRBAND_END)


def test_bin_labels_match_downsample_bins_for_both_presets():
    for preset, (start, end) in sweep_grid.PRESETS.items():
        expected = []
        for center in sweep_grid.tile_centers(preset):
            expected += [b["freq_hz"] for b in _scanner_bins(center) if start <= b["freq_hz"] <= end]
        assert sweep_grid.bin_labels(preset) == expected


def test_dc_bin_is_the_bin_that_holds_the_lo():
    center = sweep_grid.tile_centers("full")[148]
    labels = [b["freq_hz"] for b in _scanner_bins(center)]
    # The output bin holding the LO covers [label - 50 kHz, label + 50 kHz).
    holder = [f for f in labels if f - 50_000 <= center < f + 50_000]
    assert holder == [sweep_grid.dc_bin(center)] == [center + 26_000]


def test_the_tile_grid_spurs_from_the_host_are_dc_bins():
    # The classifier's tetra rows at 381.91 ... 398.3 MHz, about 2.05 MHz apart.
    for mhz in (381.914, 383.962, 386.010, 388.058, 394.202, 396.250, 398.298):
        assert sweep_grid.is_dc_bin(round(mhz * 1e6))


def test_known_tetra_carrier_bins_are_not_dc_bins():
    for hz in (390_506_000, 391_454_000, 392_654_000, 424_822_000, 425_122_000, 425_422_000):
        assert not sweep_grid.is_dc_bin(hz)


def test_airband_grid_has_its_own_dc_bins():
    first = sweep_grid.tile_centers("airband")[0]
    assert first == 119_024_000
    assert sweep_grid.is_dc_bin(119_050_000)
    # The full sweep's bin next to it is not a DC bin of either grid.
    assert not sweep_grid.is_dc_bin(119_070_000)


def test_presets_covering():
    assert sweep_grid.presets_covering(99_590_000) == ["full"]
    assert sweep_grid.presets_covering(118_550_000) == ["full", "airband"]
    assert sweep_grid.presets_covering(500_000_000) == []


def test_sweep_edges_where_no_peak_can_fire_are_not_covered():
    # detect_peaks skips the first and last PEAK_NEIGHBOR_BINS bins of a sweep.
    bins = [{"freq_hz": f, "power_dbfs": -80.0} for f in sweep_grid.bin_labels("airband")]
    for i in (0, 4, 5, len(bins) - 6, len(bins) - 5, len(bins) - 1):
        spiked = [dict(b) for b in bins]
        spiked[i]["power_dbfs"] = -20.0
        found = [p["freq_hz"] for p in scanner.detect_peaks(spiked)]
        covered = "airband" in sweep_grid.presets_covering(bins[i]["freq_hz"])
        assert covered == bool(found), (i, bins[i]["freq_hz"])
    # Athens Tower and VDL2 sit in the airband sweep's blind edges. Only the full sweep sees them.
    assert sweep_grid.presets_covering(118_100_000) == ["full"]
    assert sweep_grid.presets_covering(136_975_000) == ["full"]
