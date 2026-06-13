#!/usr/bin/env python3
"""
spectrum-acars-feedback — promote ACARS decode confirmations into
spectrum.listening_log so the classifier treats those freqs as
operator-confirmed at confidence 1.0.

Reads and writes the one shared ClickHouse server (HTTP 8123) without using
ClickHouse's `remote()` function. That keeps the dependency graph explicit
(one process owns the cross-DB write) and stdlib-only. The cross-DB read works
on a single connection because the spectrum user holds SELECT on acars.* (the
one cross-grant in the bootstrap), so the fully-qualified acars.messages read
and the spectrum.listening_log write both run as user=spectrum.

Reads:    acars.messages aggregate           (HTTP 8123, user=spectrum)
Writes:   spectrum.listening_log INSERT      (HTTP 8123, user=spectrum)

A row is written for each (freq_mhz) that has had at least
ACARS_FEEDBACK_MIN_MESSAGES decoded messages in the lookback window
(default 24h, configurable via ACARS_FEEDBACK_LOOKBACK_HOURS).
Re-running the script appends new rows; the classifier picks the
newest per-freq via `ORDER BY timestamp DESC LIMIT 1` matching, so
stale entries are harmless until 365-day TTL ages them out.

Idempotency: a hash of (today_utc, freq_mhz) is stored in `notes` so
the same freq isn't re-confirmed twice on the same day. Inserts skip
if a row with that hash already exists.

Designed for the ops/spectrum-acars-feedback/ systemd timer (hourly).
"""

from __future__ import annotations

import os
import sys
import json
import logging
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("spectrum-acars-feedback")

# ─── Config ─────────────────────────────────────────────────

# Post-consolidation: one ClickHouse server on :8123 holds every database.
# The acars read is a fully-qualified cross-DB SELECT run as the spectrum
# user (which has the SELECT-on-acars cross-grant), so there is no separate
# acars endpoint or acars credential anymore.
SPECTRUM_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
SPECTRUM_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
SPECTRUM_DB = os.environ.get("CLICKHOUSE_DB", "spectrum")
SPECTRUM_USER = os.environ.get("CLICKHOUSE_USER", "spectrum")
SPECTRUM_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "spectrum_local")

LOOKBACK_HOURS = int(os.environ.get("ACARS_FEEDBACK_LOOKBACK_HOURS", "24"))
MIN_MESSAGES = int(os.environ.get("ACARS_FEEDBACK_MIN_MESSAGES", "10"))
DRY_RUN = os.environ.get("ACARS_FEEDBACK_DRY_RUN", "0") == "1"


# ─── HTTP helpers (mirrors spectrum/db.py + acars/migrate.py) ─

def _ch_query(host: str, port: str, db: str, user: str, password: str,
              sql: str, *, data: str | None = None, timeout: int = 30) -> str:
    """Always-POST ClickHouse HTTP query (DDL/SELECT in body, INSERT with payload in body)."""
    base = f"http://{host}:{port}/?database={db}&user={user}&password={password}"
    if data is not None:
        url = f"{base}&query={quote(sql)}"
        body = data.encode("utf-8")
    else:
        url = base
        body = sql.encode("utf-8")
    req = Request(url, data=body)
    req.add_header("Content-Type", "text/plain")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        log.error(f"ClickHouse HTTP {e.code} on {host}:{port}: {err_body[:300]}")
        raise


def acars_query(sql: str) -> list[dict]:
    # Runs on the shared server as user=spectrum (cross-grant SELECT on acars.*).
    # The caller passes fully-qualified acars.messages so the spectrum database
    # context on the connection is irrelevant.
    text = _ch_query(SPECTRUM_HOST, SPECTRUM_PORT, SPECTRUM_DB, SPECTRUM_USER, SPECTRUM_PASSWORD,
                     sql + " FORMAT JSONEachRow")
    return [json.loads(line) for line in text.splitlines() if line]


def spectrum_query(sql: str) -> list[dict]:
    text = _ch_query(SPECTRUM_HOST, SPECTRUM_PORT, SPECTRUM_DB, SPECTRUM_USER, SPECTRUM_PASSWORD,
                     sql + " FORMAT JSONEachRow")
    return [json.loads(line) for line in text.splitlines() if line]


def spectrum_insert(table: str, rows: list[dict]) -> None:
    if not rows:
        return
    payload = "\n".join(json.dumps(r) for r in rows)
    _ch_query(SPECTRUM_HOST, SPECTRUM_PORT, SPECTRUM_DB, SPECTRUM_USER, SPECTRUM_PASSWORD,
              f"INSERT INTO {table} FORMAT JSONEachRow", data=payload)


# ─── Main ───────────────────────────────────────────────────

def main() -> None:
    log.info(
        f"Reading acars.messages (lookback={LOOKBACK_HOURS}h, "
        f"min_messages={MIN_MESSAGES}, dry_run={DRY_RUN})"
    )

    # Query acars.messages directly (NOT freq_activity). The MV in 001_init.sql
    # uses ReplacingMergeTree(last_seen) with count() in the SELECT — that
    # counts the per-batch insert (typically 1 row), not cumulative across
    # all messages. Behaviour discovered 2026-05-02 when a 30-message
    # pipeline showed message_count=1 in freq_activity. Fixing the MV needs
    # AggregatingMergeTree + countState() (follow-up migration); for now,
    # this script bypasses the MV entirely and aggregates from messages.
    try:
        candidates = acars_query(
            "SELECT freq_mhz, dongle_id, count() AS message_count, max(timestamp) AS last_seen "
            "FROM acars.messages "
            f"WHERE timestamp > now() - INTERVAL {LOOKBACK_HOURS} HOUR "
            "GROUP BY freq_mhz, dongle_id "
            f"HAVING count() >= {MIN_MESSAGES} "
            "ORDER BY message_count DESC"
        )
    except (URLError, HTTPError) as e:
        # The acars database may not exist yet (acars pipeline not deployed on
        # the shared server). Soft no-op so the timer can land before acars.
        log.warning(f"Could not read acars.messages; acars pipeline likely not deployed yet ({e})")
        return

    if not candidates:
        log.info("No qualifying ACARS freqs (pipeline idle or below threshold). No-op.")
        return

    log.info(f"Found {len(candidates)} qualifying (freq, dongle) pair(s):")
    for c in candidates:
        log.info(f"  {c['freq_mhz']:.3f} MHz [{c['dongle_id']}]: "
                 f"{c['message_count']} msgs since {c['last_seen']}")

    # De-dupe per-freq across dongles (we want one listening_log entry per
    # freq, not one per dongle — listening_log is signal-space, not dongle-space).
    by_freq: dict[float, dict] = {}
    for c in candidates:
        f = float(c["freq_mhz"])
        if f not in by_freq or c["message_count"] > by_freq[f]["message_count"]:
            by_freq[f] = c

    # Skip freqs we already confirmed today — listening_log is append-only;
    # the classifier picks the newest match. Daily cadence is enough.
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = spectrum_query(
        "SELECT toFloat32(freq_mhz) AS freq_mhz "
        "FROM spectrum.listening_log "
        "WHERE class_id = 'acars_downlink' "
        "  AND notes LIKE 'acars-feedback%' "
        f"  AND notes LIKE '%{today_utc}%' "
    )
    already_today = {float(r["freq_mhz"]) for r in existing}
    if already_today:
        log.info(f"Already confirmed today: {sorted(already_today)}")

    rows_to_insert: list[dict] = []
    for freq_mhz, cand in by_freq.items():
        if freq_mhz in already_today:
            continue
        rows_to_insert.append({
            "freq_mhz":          freq_mhz,
            "mode":              "AM-MSK",
            "heard":             "ACARS automated decode",
            "class_id":          "acars_downlink",
            "language":          "",
            "notes":             (
                f"acars-feedback {today_utc} "
                f"dongle={cand['dongle_id']} msgs={cand['message_count']}"
            ),
            "confirmed_freq_hz": int(round(freq_mhz * 1_000_000)),
        })

    if not rows_to_insert:
        log.info("All qualifying freqs already confirmed today. No-op.")
        return

    if DRY_RUN:
        for r in rows_to_insert:
            log.info(f"  [dry-run] would INSERT {r}")
        return

    spectrum_insert("listening_log", rows_to_insert)
    log.info(
        f"Wrote {len(rows_to_insert)} confirmation row(s) to spectrum.listening_log "
        f"(class_id=acars_downlink); next classifier run will pick them up."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("acars_feedback failed")
        sys.exit(1)
