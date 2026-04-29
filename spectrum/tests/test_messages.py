"""Contract tests for spectrum/messages.py — the scanner ↔ ingest message schema."""

import messages


def test_all_markers_unique():
    assert len(messages.ALL_MARKERS) == len(set(messages.ALL_MARKERS))


def test_all_markers_are_lowercase_snake_case():
    for m in messages.ALL_MARKERS:
        assert m == m.lower()
        assert " " not in m
        assert m.replace("_", "").isalpha()


def test_insert_table_keys_are_known_markers():
    """INSERT_TABLE only contains markers that ingest routes to a table."""
    for marker in messages.INSERT_TABLE.keys():
        assert marker in messages.ALL_MARKERS, (
            f"INSERT_TABLE references unknown marker {marker!r}"
        )


def test_insert_table_covers_expected_routes():
    """Stay in sync with scan_ingest.py's dispatch chain — bins → scans (untagged),
    peak → peaks, event → events, health → sweep_health, run_start → scan_runs."""
    assert messages.INSERT_TABLE[messages.PEAK] == "peaks"
    assert messages.INSERT_TABLE[messages.EVENT] == "events"
    assert messages.INSERT_TABLE[messages.HEALTH] == "sweep_health"
    assert messages.INSERT_TABLE[messages.RUN_START] == "scan_runs"
    assert messages.UNTAGGED_TABLE == "scans"


def test_run_update_and_run_end_are_not_inserts():
    """run_update / run_end are ALTER TABLE UPDATEs, not INSERTs — must be
    absent from INSERT_TABLE so a future refactor doesn't accidentally route
    them into a row insert."""
    assert messages.RUN_UPDATE not in messages.INSERT_TABLE
    assert messages.RUN_END not in messages.INSERT_TABLE


def test_flush_is_a_control_message():
    """flush is emitted by scanner at sweep boundaries; ingest treats it as
    a control message (drain batches) and writes no row."""
    assert messages.FLUSH not in messages.INSERT_TABLE


def test_marker_strings_match_constant_names():
    """Each marker constant's value should match its name lowercased — a
    consistency check so a typo on either end becomes obvious."""
    assert messages.RUN_START == "run_start"
    assert messages.RUN_UPDATE == "run_update"
    assert messages.RUN_END == "run_end"
    assert messages.FLUSH == "flush"
    assert messages.PEAK == "peak"
    assert messages.EVENT == "event"
    assert messages.HEALTH == "health"
