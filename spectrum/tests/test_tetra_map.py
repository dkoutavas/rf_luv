"""The TETRA network map: which emitters count as TETRA, in which network, and how."""

import pytest

import emitters
from tests import emitter_world as world
from tests import synth_sweeps as synth

FIXTURE = synth.build()


def _refs(**kw):
    base = dict(listening_log=[], known_frequencies=[], classifications={}, class_names={},
                ranges=[(380_000_000, 399_999_999, "tetra_gr", "allocations"),
                        (410_000_000, 430_000_000, "tetra_civil", "signal_catalog"),
                        (225_000_000, 400_000_000, "mil_uhf_aero", "signal_catalog")])
    base.update(kw)
    return emitters.References(**base)


def test_each_kind_of_evidence_on_its_own():
    f = 405_990_000
    assert emitters.tetra_evidence("tetra", f, f, f, _refs()) == ["class"]
    assert emitters.tetra_evidence("", 391_454_000, 391_454_000, 391_454_000, _refs()) == ["allocation"]
    named = _refs(known_frequencies=[{"freq_hz": 406_000_000, "name": "x", "class_id": "tetra"}])
    assert emitters.tetra_evidence("", f, f, f, named) == ["named"]
    logged = _refs(listening_log=[{"freq_hz": 406_050_000, "ts": 0, "label": "x", "class_id": "tetra"}])
    assert emitters.tetra_evidence("", f, f, f, logged) == ["named"]
    assert emitters.tetra_evidence("nfm_voice_repeater", 300_000_000, 300_000_000, 300_000_000, _refs()) == []


def test_a_name_more_than_one_bin_away_is_not_evidence():
    f = 405_990_000
    far = _refs(known_frequencies=[{"freq_hz": 406_100_000, "name": "x", "class_id": "tetra"}])
    assert emitters.tetra_evidence("", f, f, f, far) == []


def test_networks_by_band():
    assert emitters.tetra_network(379_999_999) == "other"
    assert emitters.tetra_network(380_000_000) == "public_safety"
    assert emitters.tetra_network(399_999_999) == "public_safety"
    assert emitters.tetra_network(405_990_000) == "other"
    assert emitters.tetra_network(410_000_000) == "civil"
    assert emitters.tetra_network(430_000_000) == "other"


def test_carrier_mode_threshold():
    assert emitters.carrier_mode(0.5) == "continuous"
    assert emitters.carrier_mode(0.49) == "bursty"


@pytest.fixture(scope="module")
def rows():
    return world.run(FIXTURE)


def _carriers(rows, dongle="v3-01"):
    """What the tetra_carriers view keeps: TETRA evidence and not an artifact suspect."""
    return {r["center_hz"]: r for r in rows
            if r["dongle_id"] == dongle and r["tetra_network"] and not r["artifact_suspect"]}


def test_fixture_networks(rows):
    got = {f: (r["tetra_network"], r["tetra_mode"]) for f, r in _carriers(rows).items()}
    assert got == {
        synth.TETRA_PS_CONTINUOUS[0]: ("public_safety", "continuous"),
        synth.TETRA_PS_CONTINUOUS[1]: ("public_safety", "continuous"),
        synth.TETRA_PS_BURSTY: ("public_safety", "bursty"),
        synth.TETRA_CIVIL_CONTINUOUS[0]: ("civil", "continuous"),
        synth.TETRA_CIVIL_CONTINUOUS[1]: ("civil", "continuous"),
        synth.TETRA_CIVIL_BURSTY: ("civil", "bursty"),
        synth.TETRA_OUTSIDE: ("other", "continuous"),
    }


def test_outside_carrier_is_kept_and_marked(rows):
    e = world.find(rows, "v3-01", synth.TETRA_OUTSIDE)
    assert e["tetra_evidence"] == "class"
    assert "allocation" not in e["tetra_evidence"]


def test_bursty_civil_carrier_counts_by_allocation_alone(rows):
    assert world.find(rows, "v3-01", synth.TETRA_CIVIL_BURSTY)["tetra_evidence"] == "allocation"


def test_tile_grid_spurs_have_tetra_evidence_but_stay_off_the_map(rows):
    for f in synth.TETRA_SPURS:
        e = world.find(rows, "v3-01", f)
        assert e["tetra_network"] == "public_safety"
        assert e["artifact_suspect"]
        assert f not in _carriers(rows)


def test_non_tetra_emitters_have_no_network(rows):
    for f in (synth.FM_KOSMOS, synth.APRS, synth.TILE_SPUR, synth.NEW_ON_DAY3):
        e = world.find(rows, "v3-01", f)
        assert (e["tetra_network"], e["tetra_mode"], e["tetra_evidence"]) == ("", "", "")


def test_diurnal_pattern_counts_only_swept_hours(rows):
    bursty = world.find(rows, "v3-01", synth.TETRA_PS_BURSTY)
    duty = [h / s for h, s in zip(bursty["diurnal_hits"], bursty["diurnal_sweeps"])]
    # About 50 sweeps per hour of day, so allow a few points of sampling noise.
    assert all(0.2 < duty[h] < 0.55 for h in range(9, 19))
    assert all(duty[h] < 0.15 for h in (0, 1, 2, 3, 4, 5, 21, 22, 23))
    # The V3 swept 4 of the 5 days. Each hour of day saw about 4 x 13 full sweeps.
    continuous = world.find(rows, "v3-01", synth.TETRA_PS_CONTINUOUS[1])
    assert all(48 <= s <= 56 for s in continuous["diurnal_sweeps"])
    # The V4 never swept at 03:00 local, so that hour has no denominator.
    v4 = world.find(rows, "v4-01", synth.TETRA_PS_CONTINUOUS[1])
    assert v4["diurnal_sweeps"][3] == 0 and v4["diurnal_hits"][3] == 0
    assert sum(v4["diurnal_sweeps"]) > 0
