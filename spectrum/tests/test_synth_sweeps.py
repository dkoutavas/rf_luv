"""The synthetic fixture models what the tests downstream rely on.

If one of these fails, the emitter, TETRA and anomaly tests are testing a
different world than the one they claim to.
"""

from collections import defaultdict
from datetime import timedelta

import numpy as np

from tests import synth_sweeps as synth
from scanner import downsample_bins

FIXTURE = synth.build()


def _scanner_labels(start_hz: int, end_hz: int) -> set[int]:
    """Every bin label the scanner emits for one preset, from its own code."""
    labels = set()
    center = start_hz + 2_048_000 // 2
    while center < end_hz + 2_048_000 // 2:
        for b in downsample_bins(np.zeros(1024), center, 2_048_000, 100_000):
            if start_hz <= b["freq_hz"] <= end_hz:
                labels.add(b["freq_hz"])
        center += 2_048_000
    return labels


def _sweeps_by_id():
    return {(h["dongle_id"], h["sweep_id"]): h for h in FIXTURE.sweep_health}


def test_build_is_deterministic_for_a_seed():
    again = synth.build()
    assert again.peaks == FIXTURE.peaks
    assert again.sweep_health == FIXTURE.sweep_health
    assert synth.build(seed=8).peaks != FIXTURE.peaks


def test_every_peak_sits_on_a_real_scanner_bin_of_its_preset():
    labels = {"full": _scanner_labels(88_000_000, 470_000_000),
              "airband": _scanner_labels(118_000_000, 137_000_000)}
    for p in FIXTURE.peaks:
        preset = p["sweep_id"].split(":", 1)[0]
        assert p["freq_hz"] in labels[preset], (preset, p["freq_hz"])


def test_every_peak_belongs_to_a_sweep_of_its_dongle():
    sweeps = _sweeps_by_id()
    for p in FIXTURE.peaks:
        h = sweeps[(p["dongle_id"], p["sweep_id"])]
        assert h["timestamp"] == p["timestamp"]


def test_gap_day_has_no_sweeps_at_all():
    gap_start = synth.START + timedelta(days=synth.GAP_DAY)
    gap_end = gap_start + timedelta(days=1)
    assert not [h for h in FIXTURE.sweep_health if gap_start <= h["timestamp"] < gap_end]
    assert not [p for p in FIXTURE.peaks if gap_start <= p["timestamp"] < gap_end]


def test_v3_gain_changes_mid_day_and_clipping_stops():
    v3_full = [h for h in FIXTURE.sweep_health if h["dongle_id"] == synth.V3 and h["preset"] == "full"]
    before = [h for h in v3_full if h["timestamp"] < synth.GAIN_CHANGE_AT]
    after = [h for h in v3_full if h["timestamp"] >= synth.GAIN_CHANGE_AT]
    assert {h["gain_db"] for h in before} == {7.7}
    assert {h["gain_db"] for h in after} == {3.7}
    assert synth.GAIN_CHANGE_AT.hour == 12
    assert 0.3 < sum(h["clipped"] for h in before) / len(before) < 0.5
    assert not any(h["clipped"] for h in after)


def test_intermod_appears_only_in_clipped_sweeps():
    sweeps = _sweeps_by_id()
    hits = [p for p in FIXTURE.peaks if p["freq_hz"] == synth.INTERMOD]
    assert len(hits) > 50
    assert all(sweeps[(p["dongle_id"], p["sweep_id"])]["clipped"] for p in hits)


def test_one_transmitter_appears_and_one_stops_on_day_3():
    new = [p for p in FIXTURE.peaks if p["freq_hz"] == synth.NEW_ON_DAY3]
    assert new and min(p["timestamp"] for p in new) >= synth.APPEARS_AT
    gone = [p for p in FIXTURE.peaks if p["freq_hz"] == synth.STOPS_ON_DAY3]
    assert gone and max(p["timestamp"] for p in gone) < synth.STOPS_AT
    # The V3 keeps sweeping after the stop, so silence there is real.
    later = [h for h in FIXTURE.sweep_health
             if h["dongle_id"] == synth.V3 and h["timestamp"] > synth.STOPS_AT]
    assert len(later) > 1000


def test_tetra_cases_are_present():
    freqs = {p["freq_hz"] for p in FIXTURE.peaks if p["dongle_id"] == synth.V3}
    for f in (*synth.TETRA_PS_CONTINUOUS, synth.TETRA_PS_BURSTY,
              *synth.TETRA_CIVIL_CONTINUOUS, synth.TETRA_CIVIL_BURSTY,
              synth.TETRA_OUTSIDE, *synth.TETRA_SPURS):
        assert f in freqs
    labels = {(c["dongle_id"], c["freq_hz"]): c["class_id"] for c in FIXTURE.signal_classifications}
    for f in synth.TETRA_SPURS:
        assert labels[(synth.V3, f)] == "tetra"
    # The outside carrier is TETRA-class and no TETRA allocation covers it.
    assert labels[(synth.V3, synth.TETRA_OUTSIDE)] == "tetra"
    tetra_ranges = [(a["freq_start_hz"], a["freq_end_hz"]) for a in FIXTURE.allocations
                    if a["service"].startswith("tetra")]
    tetra_ranges += [(c["freq_lo_hz"], c["freq_hi_hz"]) for c in FIXTURE.signal_catalog
                     if c["service"].startswith("tetra")]
    assert not any(lo <= synth.TETRA_OUTSIDE <= hi for lo, hi in tetra_ranges)


def test_continuous_and_bursty_duty_cycles_differ():
    full_sweeps = defaultdict(int)
    for h in FIXTURE.sweep_health:
        if h["preset"] == "full":
            full_sweeps[h["dongle_id"]] += 1
    hits = defaultdict(int)
    for p in FIXTURE.peaks:
        hits[(p["dongle_id"], p["freq_hz"])] += 1
    duty = lambda f: hits[(synth.V3, f)] / full_sweeps[synth.V3]   # noqa: E731
    assert duty(synth.TETRA_PS_CONTINUOUS[1]) > 0.9
    assert duty(synth.TETRA_CIVIL_BURSTY) < 0.3
    assert duty(synth.APRS) < 0.06


def test_coast_station_gets_louder_on_day_4_at_the_same_gain():
    v3 = [p for p in FIXTURE.peaks if p["freq_hz"] == synth.COAST_STATION and p["dongle_id"] == synth.V3]
    day3 = [p["power_dbfs"] for p in v3 if synth.START + timedelta(days=3) <= p["timestamp"] < synth.LOUDER_FROM]
    day4 = [p["power_dbfs"] for p in v3 if p["timestamp"] >= synth.LOUDER_FROM]
    assert np.mean(day4) - np.mean(day3) > 9.0


def test_marine_channel_is_daytime_only_until_one_burst_at_5_local():
    v3 = [p for p in FIXTURE.peaks if p["freq_hz"] == synth.MARINE_CH16 and p["dongle_id"] == synth.V3]
    local = [p["timestamp"].astimezone(synth.ATHENS) for p in v3]
    odd = [t for t in local if not 8 <= t.hour < 18]
    assert odd and {t.hour for t in odd} == {5}
    assert all(t.day == 9 for t in odd)
