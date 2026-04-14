"""Tests for detect_transients — sweep-to-sweep signal change detection."""

import pytest
import scanner
from tests.helpers import make_bins
from scanner import detect_transients


@pytest.fixture(autouse=True)
def reset_prev_sweep():
    """Reset global transient state before each test."""
    scanner._prev_sweep = {}
    yield
    scanner._prev_sweep = {}


def test_first_sweep_no_events():
    """First sweep has no previous data → no events, but state is populated."""
    bins = make_bins([(100_000_000, -50.0), (200_000_000, -40.0)])

    events = detect_transients(bins)

    assert events == []
    assert scanner._prev_sweep == {100_000_000: -50.0, 200_000_000: -40.0}


def test_appeared_signal():
    """+20 dB jump (above 15 dB threshold) → 'appeared' event."""
    detect_transients(make_bins([(100_000_000, -60.0)]))

    events = detect_transients(make_bins([(100_000_000, -40.0)]))

    assert len(events) == 1
    assert events[0]["event_type"] == "appeared"
    assert events[0]["freq_hz"] == 100_000_000
    assert events[0]["power_dbfs"] == -40.0
    assert events[0]["prev_power"] == -60.0
    assert events[0]["delta_db"] == pytest.approx(20.0, abs=0.1)


def test_disappeared_signal():
    """-20 dB drop → 'disappeared' event, delta_db stored as positive."""
    detect_transients(make_bins([(100_000_000, -30.0)]))

    events = detect_transients(make_bins([(100_000_000, -50.0)]))

    assert len(events) == 1
    assert events[0]["event_type"] == "disappeared"
    assert events[0]["delta_db"] == pytest.approx(20.0, abs=0.1)


def test_below_threshold_no_event():
    """+10 dB change (below 15 dB threshold) → no event."""
    detect_transients(make_bins([(100_000_000, -50.0)]))

    events = detect_transients(make_bins([(100_000_000, -40.0)]))

    assert events == []


def test_exact_threshold_triggers():
    """+15 dB (exactly at threshold) → event triggered (>= condition)."""
    detect_transients(make_bins([(100_000_000, -50.0)]))

    events = detect_transients(make_bins([(100_000_000, -35.0)]))

    assert len(events) == 1
    assert events[0]["event_type"] == "appeared"


def test_new_freq_no_baseline():
    """A frequency appearing only in the second sweep has no prev → no event."""
    detect_transients(make_bins([(100_000_000, -50.0)]))

    events = detect_transients(make_bins([
        (100_000_000, -50.0),  # unchanged
        (200_000_000, -30.0),  # new freq, no prev
    ]))

    assert events == []


def test_prev_sweep_updated():
    """_prev_sweep should reflect the most recent call's bins."""
    detect_transients(make_bins([(100_000_000, -50.0)]))
    detect_transients(make_bins([(100_000_000, -30.0), (200_000_000, -45.0)]))

    assert scanner._prev_sweep == {100_000_000: -30.0, 200_000_000: -45.0}


def test_output_dict_keys():
    """Each event dict should have the expected keys."""
    detect_transients(make_bins([(100_000_000, -60.0)]))
    events = detect_transients(make_bins([(100_000_000, -40.0)]))

    assert len(events) == 1
    e = events[0]
    assert e["event"] is True
    assert "freq_hz" in e
    assert "event_type" in e
    assert "power_dbfs" in e
    assert "prev_power" in e
    assert "delta_db" in e
