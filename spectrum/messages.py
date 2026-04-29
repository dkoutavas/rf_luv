"""JSON-line message contract between scanner.py and scan_ingest.py.

Each marker is a top-level boolean that scanner.py sets on the JSON line to
identify the message type; scan_ingest.py dispatches on that marker and
routes the row to the matching ClickHouse table. Defining the marker names
here gives both sides one source of truth — a typo on either becomes an
ImportError at startup instead of silently dropping data.

Untagged JSON lines (no marker set) are spectrum bins and go to UNTAGGED_TABLE.

run_update and run_end use ALTER TABLE UPDATE on scan_runs (not INSERT), so
they're not in INSERT_TABLE. flush is a control message — produces no row.
"""
from __future__ import annotations

RUN_START = "run_start"
RUN_UPDATE = "run_update"
RUN_END = "run_end"
FLUSH = "flush"
PEAK = "peak"
EVENT = "event"
HEALTH = "health"

UNTAGGED_TABLE = "scans"

INSERT_TABLE: dict[str, str] = {
    PEAK: "peaks",
    EVENT: "events",
    HEALTH: "sweep_health",
    RUN_START: "scan_runs",
}

ALL_MARKERS: tuple[str, ...] = (
    RUN_START, RUN_UPDATE, RUN_END, FLUSH, PEAK, EVENT, HEALTH,
)
