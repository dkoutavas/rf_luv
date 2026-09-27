"""Test that scan_ingest records run_end even when it gets SIGTERM first.

The regression this guards: systemd sends SIGTERM to both ends of the
`scanner.py | scan_ingest.py` pipe. The scanner finishes its sweep and prints
run_end after the signal. The ingest used to stop reading on SIGTERM, so
run_end was never processed and scan_runs.ended_at stayed empty for every
run (1,514 of 1,514 on 2026-09-27).
"""

import json
import os
import signal

import messages
import scan_ingest


def _stdin_with_sigterm_before_run_end():
    """Yield scanner output, sending SIGTERM to ourselves before run_end,
    the order in which it happens when systemd stops the unit."""
    yield json.dumps({"freq_hz": 100_000_000, "power_dbfs": -50.0,
                      "sweep_id": "full:2026-09-27 07:00:00.000",
                      "run_id": "run_test", "dongle_id": "v3-01"}) + "\n"
    yield json.dumps({messages.FLUSH: True}) + "\n"
    os.kill(os.getpid(), signal.SIGTERM)
    yield json.dumps({messages.RUN_END: True, "run_id": "run_test",
                      "dongle_id": "v3-01",
                      "ended_at": "2026-09-27 07:00:40.000"}) + "\n"


def test_run_end_is_recorded_after_sigterm(monkeypatch):
    queries = []
    inserted = []
    monkeypatch.setattr(scan_ingest, "wait_for_clickhouse", lambda: None)
    monkeypatch.setattr(scan_ingest.db, "query", lambda sql, **kw: queries.append(sql))
    monkeypatch.setattr(scan_ingest, "insert_batch",
                        lambda rows, table="scans": inserted.append((table, len(rows))) or len(rows))
    monkeypatch.setattr(scan_ingest.sys, "stdin", _stdin_with_sigterm_before_run_end())

    scan_ingest.main()

    assert ("scans", 1) in inserted
    assert any("ended_at = '2026-09-27 07:00:40.000'" in q and "run_test" in q for q in queries), queries
