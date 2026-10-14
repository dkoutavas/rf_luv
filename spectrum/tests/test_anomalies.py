"""anomalies.py: the five rules, the interest score, the signal of the day and the digest."""

from datetime import timedelta

import pytest

import anomalies
import emitters
from anomalies import Activity, Anomaly, Emitter, LevelStat, Role
from tests import ch_twin
from tests import emitter_world as world
from tests import synth_sweeps as synth

FIXTURE = synth.build()
H = 3600
DAY = 24 * H
T0 = int(synth.START.timestamp())      # Monday 2026-10-05 00:00 UTC, 03:00 in Athens


def _emitter(**kw):
    base = dict(emitter_id="v3-01:150000", dongle_id="v3-01", center_hz=150_000_000,
                lo_hz=150_000_000, hi_hz=150_000_000, first_seen=T0, last_seen=T0 + H,
                duty_overall=0.5, name="Known", name_source="known_frequencies", class_id="",
                service="land_mobile_business", artifact_suspect=False, artifact_reason="",
                tetra_network="", tetra_mode="", tetra_evidence="")
    base.update(kw)
    return Emitter(**base)


def _hours(start, count, sweeps=13):
    return {start + i * H: (sweeps, 0) for i in range(count)}


# ─── gone_quiet ─────────────────────────────────────────────


def _always_on(first, last, hours):
    """An emitter that peaked in every covering sweep from first to last."""
    act = Activity(first_seen=first, last_seen=last)
    for h, (s, _) in hours.items():
        if first - first % H <= h <= last:
            act.hits += s
            act.hod_hits[emitters.local_hour(h)] += s
    return act


def test_a_session_gap_is_not_silence():
    hours = _hours(T0, 12)                          # swept 12 h, then unplugged for days
    act = _always_on(T0, T0 + 12 * H - 60, hours)
    now = T0 + 3 * DAY
    assert anomalies.gone_quiet(_emitter(), act, hours, now - DAY, now, TRUSTED) is None


def test_gone_quiet_counts_swept_hours_across_a_gap():
    hours = _hours(T0, 12)
    act = _always_on(T0, T0 + 12 * H - 60, hours)
    # Back after a day away: 3 silent swept hours, a gap, then 3 more.
    hours.update(_hours(T0 + 2 * DAY, 3))
    hours.update(_hours(T0 + 2 * DAY + 10 * H, 3))
    now = T0 + 2 * DAY + 13 * H
    found = anomalies.gone_quiet(_emitter(), act, hours, now - DAY, now, TRUSTED)
    assert found and found.kind == "gone_quiet"
    assert found.event_at == T0 + 2 * DAY + 13 * H      # the end of the 6th silent swept hour
    del hours[T0 + 2 * DAY + 12 * H]
    assert anomalies.gone_quiet(_emitter(), act, hours, now - DAY, now, TRUSTED) is None


def test_gone_quiet_on_an_untrusted_dongle_is_low_confidence():
    # The V4 lost 20 dB at FM when its antenna changed: every FM station on it
    # fell under the peak detector's line at once. The receiver changed, not 35
    # transmitters, so the rule reports but marks it low confidence.
    hours = _hours(T0, 12)
    act = _always_on(T0, T0 + 12 * H - 60, hours)
    hours.update(_hours(T0 + 2 * DAY, 6))
    now = T0 + 2 * DAY + 7 * H
    found = anomalies.gone_quiet(_emitter(dongle_id="v4-01"), act, hours, now - DAY, now, UNTRUSTED)
    assert found and found.confidence == "low" and "not trusted" in found.detail
    assert anomalies.gone_quiet(_emitter(), act, hours, now - DAY, now, TRUSTED).confidence == "normal"


def test_the_unfinished_current_hour_does_not_count_as_silence():
    hours = _hours(T0, 72)
    act = _always_on(T0, T0 + 72 * H - 60, hours)
    hours.update(_hours(T0 + 72 * H, 5))          # 5 full silent hours
    hours[T0 + 77 * H] = (1, 0)                   # and 2 minutes of the current one
    now = T0 + 77 * H + 120
    assert anomalies.gone_quiet(_emitter(), act, hours, now - DAY, now, TRUSTED) is None
    later = T0 + 78 * H + 120                     # once that hour is over, it counts
    hours[T0 + 77 * H] = (13, 0)
    found = anomalies.gone_quiet(_emitter(), act, hours, later - DAY, later, TRUSTED)
    assert found and found.event_at == T0 + 78 * H


def test_a_rare_emitter_is_not_quiet_after_a_few_hours():
    hours = _hours(T0, 30)
    act = Activity(hits=8, first_seen=T0, last_seen=T0 + 3 * H)
    act.hod_hits[emitters.local_hour(T0)] = 8
    now = T0 + 30 * H
    assert anomalies.gone_quiet(_emitter(), act, hours, now - DAY, now, TRUSTED) is None


def test_a_daytime_channel_is_not_quiet_at_night():
    # Peaks only between 08:00 and 18:00 local for two days.
    hours = _hours(T0, 2 * 24)
    act = Activity(first_seen=T0 + 5 * H, last_seen=T0 + DAY + 15 * H - 60)
    for h, (s, _) in hours.items():
        if 8 <= emitters.local_hour(h) < 18 and h <= act.last_seen:
            act.hits += s // 2
            act.hod_hits[emitters.local_hour(h)] += s // 2
    hours.update(_hours(T0 + 2 * DAY, 3))         # the night after: swept, silent
    now = T0 + 2 * DAY + 3 * H
    silent = [h for h in hours if h > act.last_seen]
    assert len(silent) >= anomalies.GONE_QUIET_SWEPT_HOURS    # enough silence to fire...
    assert anomalies.gone_quiet(_emitter(), act, hours, now - DAY, now, TRUSTED) is None   # ...but it is night
    # The same silence in daytime hours would fire.
    act.hod_hits = [act.hits // 24] * 24
    assert anomalies.gone_quiet(_emitter(), act, hours, now - DAY, now, TRUSTED) is not None


# ─── broken_pattern ─────────────────────────────────────────


def _daytime_history(days, recent_hour_local=None, recent_peaks=4):
    """Swept around the clock for `days` days, peaks only 08-18 local. Optionally an odd burst today."""
    hours = _hours(T0, days * 24)
    window_start = T0 + (days - 1) * DAY
    act = Activity(first_seen=T0 + 5 * H, last_seen=window_start - H)
    for h, (s, _) in hours.items():
        hod = emitters.local_hour(h)
        if 8 <= hod < 18 and h >= act.first_seen:
            hits = s // 2
            act.hits += hits
            act.hod_hits[hod] += hits
            if h >= window_start:
                act.hits_recent += hits
                act.hod_hits_recent[hod] += hits
                act.hod_first_recent[hod] = act.hod_first_recent[hod] or h
    if recent_hour_local is not None:
        burst = window_start + ((recent_hour_local - 3) % 24) * H
        act.hits += recent_peaks
        act.hits_recent += recent_peaks
        act.hod_hits[recent_hour_local] += recent_peaks
        act.hod_hits_recent[recent_hour_local] += recent_peaks
        act.hod_first_recent[recent_hour_local] = burst
    return act, hours, window_start


def test_broken_pattern_fires_on_a_night_burst_of_a_day_channel():
    act, hours, window_start = _daytime_history(4, recent_hour_local=5)
    found = anomalies.broken_pattern(_emitter(), act, hours, window_start, TRUSTED)
    assert found and "05:00" in found.detail


def test_no_broken_pattern_without_swept_history_at_that_hour():
    act, hours, window_start = _daytime_history(4, recent_hour_local=5)
    for h in [h for h in hours if h < window_start and emitters.local_hour(h) == 5]:
        del hours[h]
    assert anomalies.broken_pattern(_emitter(), act, hours, window_start, TRUSTED) is None


def test_one_odd_peak_is_luck_not_a_broken_pattern():
    act, hours, window_start = _daytime_history(4, recent_hour_local=5, recent_peaks=1)
    assert anomalies.broken_pattern(_emitter(), act, hours, window_start, TRUSTED) is None


def test_poisson_tail():
    assert anomalies.poisson_tail(0, 3.0) == 1.0
    assert anomalies.poisson_tail(1, 2.0) == pytest.approx(1 - 2.718281828 ** -2)
    assert anomalies.poisson_tail(4, 0.17) < 1e-4


# ─── louder_than_usual ──────────────────────────────────────


TRUSTED, UNTRUSTED = Role(True, True), Role(False, False)


def test_louder_at_the_same_gain():
    levels = [LevelStat(3.7, False, 200, -45.0, 1.5, T0), LevelStat(3.7, True, 50, -35.0, 1.5, T0 + DAY)]
    found = anomalies.louder_than_usual(_emitter(), levels, TRUSTED)
    assert found and found.confidence == "normal" and "+10.0 dB at gain 3.7 dB" in found.detail


def test_a_gain_change_is_not_louder():
    # The AGC moved from 3.7 to 7.7 dB: every carrier reads 4 dB hotter, and
    # even a 10 dB jump has no baseline at the new gain.
    levels = [LevelStat(3.7, False, 200, -45.0, 1.5, T0), LevelStat(7.7, True, 50, -35.0, 1.5, T0 + DAY)]
    assert anomalies.louder_than_usual(_emitter(), levels, TRUSTED) is None


def test_untrusted_levels_are_low_confidence():
    levels = [LevelStat(12.0, False, 200, -45.0, 1.5, T0), LevelStat(12.0, True, 50, -35.0, 1.5, T0 + DAY)]
    found = anomalies.louder_than_usual(_emitter(dongle_id="v4-01"), levels, UNTRUSTED)
    assert found.confidence == "low" and "not trusted" in found.detail


def test_a_noisy_emitter_needs_more_than_6_db():
    levels = [LevelStat(3.7, False, 200, -45.0, 4.0, T0), LevelStat(3.7, True, 50, -37.0, 4.0, T0 + DAY)]
    assert anomalies.louder_than_usual(_emitter(), levels, TRUSTED) is None


def test_clipped_sweeps_are_left_out_of_levels():
    # Make one carrier 25 dB hotter in clipped sweeps only, inside the window.
    now = world.at(1, 12)
    window_start = emitters.recent_split(now)
    peaks = [dict(p) for p in FIXTURE.peaks if p["timestamp"] < now]
    clipped = {(h["dongle_id"], h["sweep_id"]) for h in FIXTURE.sweep_health if h["clipped"]}
    for p in peaks:
        if (p["freq_hz"] == synth.TETRA_PS_CONTINUOUS[0] and p["timestamp"].timestamp() >= window_start
                and (p["dongle_id"], p["sweep_id"]) in clipped):
            p["power_dbfs"] += 25.0
    e = _emitter(lo_hz=synth.TETRA_PS_CONTINUOUS[0], hi_hz=synth.TETRA_PS_CONTINUOUS[0])
    levels = anomalies.group_levels(ch_twin.levels(peaks, FIXTURE.sweep_health, [e.footprint], window_start))
    assert anomalies.louder_than_usual(e, levels[1], TRUSTED) is None
    # The same jump in every sweep is real.
    for p in peaks:
        if p["freq_hz"] == synth.TETRA_PS_CONTINUOUS[0] and p["timestamp"].timestamp() >= window_start:
            p["power_dbfs"] += 25.0
    levels = anomalies.group_levels(ch_twin.levels(peaks, FIXTURE.sweep_health, [e.footprint], window_start))
    assert anomalies.louder_than_usual(e, levels[1], TRUSTED) is not None


# ─── new_emitter and new_tetra_carrier ──────────────────────


def test_new_emitter_only_inside_the_window():
    act = Activity(hits=9, first_seen=T0 + DAY)
    assert anomalies.new_emitter(_emitter(), act, T0 + DAY - H)
    assert anomalies.new_emitter(_emitter(), act, T0 + DAY + H) is None


def test_artifact_suspects_raise_nothing():
    e = _emitter(artifact_suspect=True, artifact_reason="tile_grid", tetra_network="public_safety",
                 tetra_evidence="class,allocation")
    act = Activity(hits=9, hits_recent=9, first_seen=T0 + DAY, last_seen=T0 + DAY)
    cov = emitters.Coverage([])
    assert anomalies.detect([e], {1: act}, cov, {}, {}, T0 + DAY + H) == []


def test_new_tetra_carrier_rules():
    act_old = Activity(hits=900, hits_recent=40, first_seen=T0, last_seen=T0 + DAY)
    outside = _emitter(tetra_network="other", tetra_mode="continuous", tetra_evidence="class",
                       name_source="classifier")
    found = anomalies.new_tetra_carrier(outside, act_old, T0 + DAY - H)
    assert found and "outside every known TETRA allocation" in found.detail
    named = _emitter(tetra_network="other", tetra_evidence="class,named", name_source="known_frequencies")
    assert anomalies.new_tetra_carrier(named, act_old, T0 + DAY - H) is None
    inside_old = _emitter(tetra_network="civil", tetra_evidence="allocation", name_source="allocation")
    assert anomalies.new_tetra_carrier(inside_old, act_old, T0 + DAY - H) is None
    act_new = Activity(hits=9, hits_recent=9, first_seen=T0 + DAY, last_seen=T0 + DAY)
    found = anomalies.new_tetra_carrier(inside_old, act_new, T0 + DAY - H)
    assert found and "civil band" in found.detail
    assert anomalies.new_tetra_carrier(_emitter(), act_new, T0 + DAY - H) is None


# ─── Interest, the signal of the day, the digest ────────────


def test_interest_score_points():
    window_start = T0 + DAY
    score, why = anomalies.interest_score(
        _emitter(name="unnamed", name_source="unnamed", first_seen=T0 + DAY + H, duty_overall=0.01),
        {"new_emitter", "gone_quiet"}, window_start)
    assert score == 3 + 3 + 2 + 1
    assert why[0] == "unnamed" and "gone quiet" in why
    fm = anomalies.interest_score(_emitter(class_id="broadcast_fm", name_source="unnamed"), set(), window_start)
    spur = anomalies.interest_score(_emitter(artifact_suspect=True, artifact_reason="tile_grid"), set(),
                                    window_start)
    assert spur[0] < fm[0] < 0


def test_signal_of_the_day_is_deterministic_and_prefers_the_primary():
    now = T0 + 2 * DAY
    roles = {"v3-01": TRUSTED, "v4-01": UNTRUSTED}
    a = _emitter(emitter_id="v4-01:150000", dongle_id="v4-01", name_source="unnamed", last_seen=now - H)
    b = _emitter(emitter_id="v3-01:150000", name_source="unnamed", last_seen=now - H)
    for order in ([a, b], [b, a]):
        pick = anomalies.signal_of_the_day(order, [], roles, now)
        assert pick.emitter.emitter_id == "v3-01:150000"
    assert anomalies.signal_of_the_day([b], [], roles, now + 3 * DAY) is None


def test_digest_is_silent_when_nothing_was_swept():
    assert anomalies.digest(None, [], {}, "2026-10-07") is None
    assert anomalies.digest(None, [], {"v3-01": 0}, "2026-10-07") is None


def test_digest_counts_each_kind_and_keeps_low_confidence_apart():
    e = _emitter(name="unnamed", center_hz=140_550_000)
    pick = anomalies.Pick(e, 6, "unnamed at 140.550 MHz on v3-01: unnamed, new")
    found = [Anomaly("new_emitter", "v3-01", "x", 1, "n", T0, "normal", ""),
             Anomaly("louder_than_usual", "v3-01", "y", 1, "n", T0, "normal", ""),
             Anomaly("louder_than_usual", "v4-01", "y", 1, "n", T0, "low", "")]
    title, body = anomalies.digest(pick, found, {"v3-01": 23, "v4-01": 12}, "2026-10-09")
    assert title == "rf_luv 2026-10-09: signal of the day 140.550 MHz"
    assert "Signal of the day: unnamed at 140.550 MHz on v3-01: unnamed, new." in body
    assert ("Last 24 h: new emitter 1, gone quiet 0, louder than usual 1, broken pattern 0, "
            "new TETRA carrier 0.") in body
    assert "Low confidence (untrusted levels): louder than usual 1." in body
    assert "Swept: v3-01 23 h, v4-01 12 h." in body


# ─── The fixture, end to end ────────────────────────────────


def _kinds(found):
    return {(a.kind, a.dongle_id, a.center_hz, a.confidence) for a in found}


def test_right_after_the_gap_day_nothing_is_quiet_or_odd():
    for hour in (0, 3, 6):
        found, _, _, _ = world.detect(FIXTURE, world.at(3, hour))
        assert not [a for a in found if a.kind in ("gone_quiet", "broken_pattern")]


def test_end_of_day_3():
    found, pick, elist, coverage = world.detect(FIXTURE, world.at(4))
    new_emitters = {(d, f) for k, d, f, _ in _kinds(found) if k == "new_emitter"}
    assert new_emitters == {("v3-01", synth.NEW_ON_DAY3)}
    quiet = [a for a in found if a.kind == "gone_quiet"]
    assert [(a.dongle_id, a.center_hz) for a in quiet] == [("v3-01", synth.STOPS_ON_DAY3)]
    assert quiet[0].event_at < int((synth.STOPS_AT + timedelta(hours=7)).timestamp())
    assert pick.emitter.center_hz == synth.NEW_ON_DAY3
    assert pick.reason.startswith("unnamed at 140.550 MHz on v3-01: unnamed, new")


def test_end_of_day_4():
    found, pick, _, coverage = world.detect(FIXTURE, world.at(5))
    k = _kinds(found)
    assert ("louder_than_usual", "v3-01", synth.COAST_STATION, "normal") in k
    assert ("louder_than_usual", "v4-01", synth.COAST_STATION, "low") in k
    assert [a.center_hz for a in found if a.kind == "louder_than_usual"] == [synth.COAST_STATION] * 2
    broken = [(a.dongle_id, a.center_hz) for a in found if a.kind == "broken_pattern"]
    assert broken == [("v3-01", synth.MARINE_CH16)]          # not the V4, it never swept 08:00 before
    tetra = [(a.dongle_id, a.center_hz) for a in found if a.kind == "new_tetra_carrier"]
    assert tetra == [("v3-01", synth.TETRA_OUTSIDE)]
    assert not [a for a in found if a.kind in ("new_emitter", "gone_quiet")]
    title, body = anomalies.digest(pick, found, anomalies.swept_hours(coverage, emitters.recent_split(world.at(5))),
                                   "2026-10-09")
    assert "louder than usual 1" in body and "Low confidence (untrusted levels): louder than usual 1." in body


def test_no_digest_for_a_day_with_no_sweeps():
    found, pick, _, coverage = world.detect(FIXTURE, world.at(3))
    window_start = emitters.recent_split(world.at(3))
    assert anomalies.digest(pick, found, anomalies.swept_hours(coverage, window_start), "2026-10-07") is None


def test_anomaly_rows_key_on_the_local_event_day():
    found, _, _, _ = world.detect(FIXTURE, world.at(4))
    rows = anomalies.anomaly_rows(found, world.at(4))
    new = [r for r in rows if r["kind"] == "new_emitter"][0]
    assert new["event_day"] == "2026-10-08"
    assert new["event_at"].startswith("2026-10-08 08:")
