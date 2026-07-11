#!/usr/bin/env python3
"""
RDS → ClickHouse Ingest Worker

Reads JSON lines from stdin (piped from rds_reader.py) and batch-inserts
decoded RDS group records into rds.messages. Stdlib-only HTTP (urllib),
verbatim shape of ism/ism_ingest.py: clickhouse_query(), insert_batch(),
wait_for_clickhouse(), BATCH_SIZE / FLUSH_INTERVAL_SECONDS, SIGTERM flush.

The reader emits one JSON object per decoded group, e.g.:
  {"pi":4660,"group_type":"0A","tp":1,"pty":10,"ta":0,"ms":0,
   "ps":"KOSMOS","radiotext":"","clock_utc":null,"clock_offset_min":0,
   "block_errors":0,"raw_group":"1234 0408 E0E0 4B4F",
   "freq_hz":99600000,"dongle_id":"v4-01","timestamp":"2026-07-11 09:12:00.123"}

The only field rename is pi -> pi_code (the ClickHouse column name). The
ingest never imports numpy; the reader never imports urllib (the shell pipe
in entrypoint.sh is the IPC, ism-style).
"""

import os
import sys
import json
import time
import signal
import logging
from urllib.parse import quote
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ─── Config ──────────────────────────────────────────────

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CH_DB = os.environ.get("CLICKHOUSE_DB", "rds")
CH_USER = os.environ.get("CLICKHOUSE_USER", "rds")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "rds_local")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))
FLUSH_INTERVAL = int(os.environ.get("FLUSH_INTERVAL_SECONDS", "5"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rds-ingest")

# ─── Graceful shutdown ───────────────────────────────────

running = True


def handle_signal(signum, frame):
    global running
    log.info(f"Received signal {signum}, shutting down...")
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# ─── ClickHouse insertion ────────────────────────────────

CH_URL = f"http://{CH_HOST}:{CH_PORT}/"


def clickhouse_query(query: str, data: str = "") -> str:
    """Execute a ClickHouse query via HTTP (always POST; mirrors ism/spectrum).

    - SELECT / DDL:        clickhouse_query(sql)            → SQL in body
    - INSERT with payload: clickhouse_query(sql, data=rows) → SQL in URL, payload in body
    """
    base_params = f"database={CH_DB}&user={CH_USER}&password={CH_PASSWORD}"
    if data:
        url = f"{CH_URL}?{base_params}&query={quote(query)}"
        body = data.encode("utf-8")
    else:
        url = f"{CH_URL}?{base_params}"
        body = query.encode("utf-8")
    req = Request(url, data=body)
    req.add_header("Content-Type", "text/plain")
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        log.error(f"ClickHouse HTTP {e.code}: {err_body[:400]}")
        raise
    except URLError as e:
        log.error(f"ClickHouse query failed: {e}")
        raise


def insert_batch(rows: list[dict]) -> int:
    """Insert a batch of RDS group rows into ClickHouse."""
    if not rows:
        return 0

    payload = "\n".join(json.dumps(row) for row in rows)
    query = "INSERT INTO messages FORMAT JSONEachRow"

    try:
        clickhouse_query(query, payload)
        return len(rows)
    except Exception as e:
        log.error(f"Failed to insert batch of {len(rows)}: {e}")
        return 0


# ─── Field mapping ───────────────────────────────────────

def to_row(data: dict) -> dict | None:
    """Map a decoded-group JSON object to a ClickHouse row (rds.messages).

    Only rename is pi -> pi_code; unknown/missing fields fall back to column
    defaults. clock_utc stays null (Nullable(DateTime)) when absent.
    """
    if "pi" not in data and "pi_code" not in data:
        return None

    row = {
        "pi_code": int(data.get("pi", data.get("pi_code", 0))),
        "group_type": data.get("group_type", ""),
        "tp": int(data.get("tp", 0)),
        "ta": int(data.get("ta", 0)),
        "pty": int(data.get("pty", 0)),
        "ms": int(data.get("ms", 0)),
        "ps": data.get("ps", ""),
        "radiotext": data.get("radiotext", ""),
        "clock_utc": data.get("clock_utc"),   # may be None → SQL NULL
        "clock_offset_min": int(data.get("clock_offset_min", 0)),
        "block_errors": int(data.get("block_errors", 0)),
        "dongle_id": data.get("dongle_id", ""),
        "raw_group": data.get("raw_group", ""),
    }
    if data.get("freq_hz") is not None:
        row["freq_hz"] = int(data["freq_hz"])
    if data.get("timestamp"):
        row["timestamp"] = data["timestamp"]
    return row


# ─── Main loop ───────────────────────────────────────────

def wait_for_clickhouse(max_retries: int = 30, delay: int = 2):
    for i in range(max_retries):
        try:
            clickhouse_query("SELECT 1 FORMAT TabSeparated")
            log.info("ClickHouse is ready")
            return
        except Exception:
            log.info(f"Waiting for ClickHouse... ({i + 1}/{max_retries})")
            time.sleep(delay)
    log.error("ClickHouse not available after retries, starting anyway")


def main():
    global running

    wait_for_clickhouse()

    total_inserted = 0
    total_decoded = 0
    batch: list[dict] = []
    last_flush = time.monotonic()

    log.info("Reading JSON lines from stdin (rds_reader pipe)")

    for line in sys.stdin:
        if not running:
            break

        line = line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        row = to_row(data)
        if row is None:
            continue

        total_decoded += 1
        batch.append(row)

        if len(batch) >= BATCH_SIZE:
            count = insert_batch(batch)
            total_inserted += count
            if count > 0:
                log.info(f"Flushed {count} rows (batch) | decoded: {total_decoded} | inserted: {total_inserted}")
            batch.clear()
            last_flush = time.monotonic()

        elapsed = time.monotonic() - last_flush
        if batch and elapsed >= FLUSH_INTERVAL:
            count = insert_batch(batch)
            total_inserted += count
            if count > 0:
                log.info(f"Flushed {count} rows (timer) | decoded: {total_decoded} | inserted: {total_inserted}")
            batch.clear()
            last_flush = time.monotonic()

    if batch:
        count = insert_batch(batch)
        total_inserted += count
        log.info(f"Final flush: {count} rows | total inserted: {total_inserted}")

    log.info(f"Shutdown complete. Decoded: {total_decoded}, Inserted: {total_inserted}")


if __name__ == "__main__":
    main()
