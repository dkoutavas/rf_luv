"""emitters.py: clustering, stable IDs, names, artifact flags and duty cycles."""

from datetime import timedelta

import pytest

import emitters
from emitters import BinStat, Cluster, Previous
from tests import emitter_world as world
from tests import synth_sweeps as synth

FIXTURE = synth.build()


def _bin(freq, hits=10, dongle="v3-01", power=-40.0):
    return BinStat(dongle, freq, hits, 0, 3600, power)


def _refs(**kw):
    base = dict(listening_log=[], known_frequencies=[], classifications={}, class_names={}, ranges=[])
    base.update(kw)
    return emitters.References(**base)


# ─── Clustering ─────────────────────────────────────────────


def test_adjacent_bins_chain_and_one_empty_bin_splits():
    clusters = emitters.cluster_bins([_bin(100_000_000), _bin(100_100_000), _bin(100_300_000)])
    assert [(c.lo_hz, c.hi_hz) for c in clusters] == [(100_000_000, 100_100_000),
                                                       (100_300_000, 100_300_000)]


def test_airband_and_full_grid_bins_of_one_channel_chain():
    clusters = emitters.cluster_bins([_bin(synth.APPROACH_AIRBAND), _bin(synth.APPROACH_FULL)])
    assert len(clusters) == 1


def test_rare_bins_neither_seed_nor_bridge():
    bins = [_bin(100_000_000), _bin(100_100_000, hits=2), _bin(100_200_000)]
    clusters = emitters.cluster_bins(bins)
    assert [(c.lo_hz, c.hi_hz) for c in clusters] == [(100_000_000, 100_000_000),
                                                       (100_200_000, 100_200_000)]


def test_dongles_never_share_a_cluster():
    clusters = emitters.cluster_bins([_bin(100_000_000, dongle="v3-01"), _bin(100_100_000, dongle="v4-01")])
    assert sorted(c.dongle_id for c in clusters) == ["v3-01", "v4-01"]


# ─── Stable IDs ─────────────────────────────────────────────


def _cluster(*freqs, hits=10):
    return Cluster("v3-01", [_bin(f, hits) for f in freqs])


def test_new_id_names_the_anchor_bin():
    c = Cluster("v3-01", [_bin(391_354_000, hits=5), _bin(391_454_000, hits=90)])
    assert emitters.assign_ids("v3-01", [c], []) == ["v3-01:391454"]


def test_ids_carry_forward_when_a_cluster_grows_or_shifts():
    first = emitters.assign_ids("v3-01", [_cluster(391_454_000)], [])
    prev = [Previous(first[0], 391_454_000, 391_454_000, 100)]
    assert emitters.assign_ids("v3-01", [_cluster(391_454_000, 391_554_000)], prev) == first
    assert emitters.assign_ids("v3-01", [_cluster(391_554_000)], prev) == first


def test_split_keeps_the_id_on_the_larger_overlap():
    prev = [Previous("v3-01:100100", 100_000_000, 100_300_000, 100)]
    ids = emitters.assign_ids("v3-01", [_cluster(100_000_000), _cluster(100_200_000, 100_300_000)], prev)
    assert ids[1] == "v3-01:100100"
    assert ids[0] == "v3-01:100000"


def test_merge_keeps_the_dominant_id_and_retires_the_other():
    prev = [Previous("v3-01:100000", 100_000_000, 100_000_000, 10),
            Previous("v3-01:100200", 100_100_000, 100_300_000, 500)]
    ids = emitters.assign_ids("v3-01", [_cluster(100_000_000, 100_100_000, 100_200_000, 100_300_000)], prev)
    assert ids == ["v3-01:100200"]


def test_minted_id_never_collides_with_a_previous_one():
    # The emitter born at 100.0 MHz moved up a bin and kept its ID. A new one
    # is born on its old bin, so the new ID needs a suffix.
    prev = [Previous("v3-01:100000", 100_100_000, 100_100_000, 100)]
    ids = emitters.assign_ids("v3-01", [_cluster(99_900_000), _cluster(100_200_000)], prev)
    assert ids == ["v3-01:99900", "v3-01:100000"]
    ids = emitters.assign_ids("v3-01", [_cluster(100_000_000 - 200_000), _cluster(100_100_000),
                                        _cluster(100_000_000 + 400_000)], prev)
    assert ids[1] == "v3-01:100000"
    ids = emitters.assign_ids("v3-01", [_cluster(100_100_000), _cluster(100_400_000)],
                              [Previous("v3-01:100400", 100_100_000, 100_100_000, 100)])
    assert ids == ["v3-01:100400", "v3-01:100400-2"]


def test_ids_are_stable_across_runs_on_the_fixture():
    first = world.run(FIXTURE, now=world.at(3, 12))
    second = world.run(FIXTURE, previous=world.as_previous(first))
    ids_then = {(r["dongle_id"], r["center_hz"]): r["emitter_id"] for r in first}
    for r in second:
        key = (r["dongle_id"], r["center_hz"])
        if key in ids_then:
            assert r["emitter_id"] == ids_then[key]
    # The transmitter born on day 3 at 08:00 existed at noon and kept its ID.
    born = world.find(first, "v3-01", synth.NEW_ON_DAY3)["emitter_id"]
    assert world.find(second, "v3-01", synth.NEW_ON_DAY3)["emitter_id"] == born


# ─── Names ──────────────────────────────────────────────────


def test_operator_label():
    assert emitters.operator_label("Greek voices", "port", "x") == "Greek voices"
    assert emitters.operator_label(
        "confirmed", "Greek ham radio 2m band — voice conversation. Source: x.", "nfm") == "Greek ham radio 2m band"
    assert emitters.operator_label("", "", "tetra") == "tetra"


def test_name_precedence_operator_known_classifier_allocation_catalog_unnamed():
    lo = hi = center = 150_000_000
    log_entry = {"freq_hz": 150_050_000, "ts": 1, "class_id": "x", "label": "heard it"}
    known = {"freq_hz": 150_000_000, "name": "Known", "class_id": "y"}
    cls = {"v3-01": [{"freq_hz": 150_000_000, "class_id": "nfm_voice_repeater", "confidence": 0.8}]}
    names = {"nfm_voice_repeater": "NFM voice repeater"}
    ranges = [(140_000_000, 160_000_000, "wide_alloc", "allocations"),
              (149_000_000, 151_000_000, "narrow_cat", "signal_catalog")]

    def name(**kw):
        return emitters.name_emitter("v3-01", lo, hi, center, {}, _refs(**kw))

    assert name(listening_log=[log_entry], known_frequencies=[known], classifications=cls,
                class_names=names, ranges=ranges).source == "listening_log"
    assert name(known_frequencies=[known], classifications=cls, class_names=names,
                ranges=ranges).name == "Known"
    assert name(classifications=cls, class_names=names, ranges=ranges).name == "NFM voice repeater"
    got = name(ranges=ranges)
    assert (got.name, got.source) == ("narrow_cat", "signal_catalog")
    assert name(ranges=ranges[:1]).source == "allocation"
    assert name().name == "unnamed"


def test_unknown_classes_do_not_name_but_still_set_the_class():
    cls = {"v3-01": [{"freq_hz": 150_000_000, "class_id": "unknown_bursty", "confidence": 0.2}]}
    got = emitters.name_emitter("v3-01", 150_000_000, 150_000_000, 150_000_000, {},
                                _refs(classifications=cls,
                                      ranges=[(140_000_000, 160_000_000, "land_mobile", "allocations")]))
    assert (got.name, got.source, got.class_id) == ("land_mobile", "allocation", "unknown_bursty")


def test_names_match_within_one_bin_only():
    near = {"freq_hz": 150_100_000, "name": "Near", "class_id": ""}
    far = {"freq_hz": 150_101_000, "name": "Far", "class_id": ""}
    got = emitters.name_emitter("v3-01", 150_000_000, 150_000_000, 150_000_000, {}, _refs(known_frequencies=[far]))
    assert got.name == "unnamed"
    got = emitters.name_emitter("v3-01", 150_000_000, 150_000_000, 150_000_000, {}, _refs(known_frequencies=[near]))
    assert got.name == "Near"


def test_classifier_name_ignores_other_dongles():
    cls = {"v4-01": [{"freq_hz": 150_000_000, "class_id": "tetra", "confidence": 0.8}]}
    got = emitters.name_emitter("v3-01", 150_000_000, 150_000_000, 150_000_000, {},
                                _refs(classifications=cls, class_names={"tetra": "TETRA"}))
    assert got.name == "unnamed"


# ─── Artifact suspects ──────────────────────────────────────


def test_tile_grid_needs_every_bin_on_a_dc_bin():
    assert "tile_grid" in emitters.artifact_reasons([383_962_000], 383_962_000, 383_962_000, 0, 50, 0, 50)
    assert not emitters.artifact_reasons([383_962_000, 384_062_000], 383_962_000, 384_062_000, 0, 50, 0, 50)


def test_clipped_only_compares_rates_not_counts():
    # A real carrier on a dongle that clips most of the time: many clipped hits, but
    # the same rate when clean.
    assert "clipped_only" not in emitters.artifact_reasons([200_000_000], 0, 0, 900, 95, 950, 100)
    # A product that is there when the ADC clips and gone when it does not.
    assert "clipped_only" in emitters.artifact_reasons([200_000_000], 0, 0, 300, 2, 400, 600)
    # Too few clean sweeps to judge.
    assert "clipped_only" not in emitters.artifact_reasons([200_000_000], 0, 0, 300, 0, 400, 5)


def test_intermod_matches_within_one_bin():
    assert "intermod" in emitters.artifact_reasons([116_822_000], 116_822_000, 116_822_000, 0, 9, 0, 9)
    assert "intermod" not in emitters.artifact_reasons([117_022_000], 117_022_000, 117_022_000, 0, 9, 0, 9)


# ─── Duty cycles over swept time ────────────────────────────


def test_duty_counts_only_swept_hours():
    act = emitters.Activity(hits=20, hits_recent=0, first_seen=0, last_seen=7200)
    # Two swept hours of 10 sweeps, then a long gap with no rows at all.
    hours = {0: (10, 0), 3600: (10, 0)}
    nums = emitters.duty_numbers(act, hours, split_at=10 * 86400)
    assert nums.duty_overall == 1.0
    assert nums.hours_observed == 2
    assert nums.duty_24h is None


def test_hours_before_first_seen_do_not_count():
    act = emitters.Activity(hits=5, first_seen=7200, last_seen=7300)
    nums = emitters.duty_numbers(act, {0: (10, 0), 3600: (10, 0), 7200: (10, 0)}, split_at=0)
    assert nums.duty_overall == 0.5
    assert nums.hours_observed == 1


# ─── The fixture, end to end ────────────────────────────────


@pytest.fixture(scope="module")
def rows():
    return world.run(FIXTURE)


def test_continuous_carrier_duty_ignores_the_gap_day(rows):
    e = world.find(rows, "v3-01", synth.TETRA_PS_CONTINUOUS[1])
    assert 0.93 < e["duty_overall"] < 0.99
    assert e["hours_observed"] == 4 * 24
    assert 0.9 < e["duty_24h"] < 1.0


def test_duty_24h_is_none_after_a_whole_day_unplugged():
    after_gap = world.run(FIXTURE, now=world.at(3))
    assert world.find(after_gap, "v3-01", synth.TETRA_PS_CONTINUOUS[1])["duty_24h"] is None


def test_fixture_names(rows):
    def named(freq, dongle="v3-01"):
        e = world.find(rows, dongle, freq)
        return e["name"], e["name_source"]

    assert named(synth.FM_KOSMOS) == ("Kosmos FM 99.6", "known_frequencies")
    assert named(synth.APPROACH_AIRBAND) == ("Athens Approach", "known_frequencies")
    assert named(synth.TETRA_PS_CONTINUOUS[1]) == ("TETRA BS ~391.45 (public safety)", "known_frequencies")
    assert named(synth.TETRA_OUTSIDE) == ("TETRA (encrypted)", "classifier")
    assert named(synth.TETRA_CIVIL_BURSTY) == ("tetra_gr", "allocation")
    assert named(synth.STOPS_ON_DAY3) == ("land_mobile_safety", "allocation")
    assert named(synth.TILE_SPUR) == ("mil_uhf_aero", "signal_catalog")
    assert named(synth.NEW_ON_DAY3) == ("unnamed", "unnamed")


def test_aprs_bin_takes_the_operator_name_and_notes_the_rest(rows):
    e = world.find(rows, "v3-01", synth.APRS)
    assert (e["name"], e["name_source"]) == ("Greek ham radio 2m band", "listening_log")
    assert "APRS (IARU Region 1) [known_frequencies]" in e["name_note"]
    assert "Greek 2m Ham [known_frequencies]" in e["name_note"]


def test_fixture_artifacts(rows):
    def reasons(freq):
        return set(filter(None, world.find(rows, "v3-01", freq)["artifact_reason"].split(",")))

    for f in synth.TETRA_SPURS:
        assert reasons(f) == {"tile_grid"}
    assert reasons(synth.TILE_SPUR) == {"tile_grid"}
    assert reasons(synth.INTERMOD) == {"intermod", "clipped_only"}
    for f in (synth.FM_KOSMOS, synth.APPROACH_AIRBAND, synth.APRS, synth.TETRA_PS_CONTINUOUS[0],
              synth.TETRA_CIVIL_CONTINUOUS[0], synth.TETRA_OUTSIDE, synth.STOPS_ON_DAY3,
              synth.MARINE_CH16, synth.COAST_STATION, synth.NEW_ON_DAY3):
        assert reasons(f) == set(), f


def test_the_operator_tetra_entry_does_not_clear_a_dc_spur(rows):
    # 384.000 MHz (listening_log, April 2026) is 38 kHz from the DC bin at 383.962.
    e = world.find(rows, "v3-01", synth.TETRA_SPURS[1])
    assert e["name_source"] == "listening_log"
    assert e["artifact_suspect"] and e["artifact_reason"] == "tile_grid"


def test_new_transmitter_first_seen_on_day_3(rows):
    e = world.find(rows, "v3-01", synth.NEW_ON_DAY3)
    assert e["first_seen"] >= synth.ch_time(synth.APPEARS_AT)
    assert e["first_seen"] < synth.ch_time(synth.APPEARS_AT + timedelta(hours=1))


def test_each_dongle_gets_its_own_emitters(rows):
    v4 = world.find(rows, "v4-01", synth.TETRA_PS_CONTINUOUS[1])
    v3 = world.find(rows, "v3-01", synth.TETRA_PS_CONTINUOUS[1])
    assert v4["emitter_id"].startswith("v4-01:") and v3["emitter_id"].startswith("v3-01:")
    assert not [r for r in rows if r["dongle_id"] == "v4-01" and r["lo_hz"] <= synth.INTERMOD <= r["hi_hz"]]
