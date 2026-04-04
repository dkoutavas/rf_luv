#!/usr/bin/env python3
"""
Spectrum Scan → ClickHouse Ingest Worker

Reads JSON lines from stdin (piped from scanner.py) and batch-inserts
power measurements into ClickHouse.

Each JSON line: {"freq_hz": 88050000, "power_dbfs": -42.3, "sweep_id": "2026-04-04T12:00:00.000"}
"""

import os
import sys
import json
import time
import signal
import logging
from urllib.parse import quote
from urllib.request import urlopen, Request
from urllib.error import URLError

# ─── Config ──────────────────────────────────────────────

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CH_DB = os.environ.get("CLICKHOUSE_DB", "spectrum")
CH_USER = os.environ.get("CLICKHOUSE_USER", "spectrum")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "spectrum_local")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "500"))
FLUSH_INTERVAL = int(os.environ.get("FLUSH_INTERVAL_SECONDS", "5"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("scan-ingest")

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
    params = f"database={CH_DB}&user={CH_USER}&password={CH_PASSWORD}&query={quote(query)}"
    url = f"{CH_URL}?{params}"
    req = Request(url, data=data.encode("utf-8") if data else None)
    req.add_header("Content-Type", "text/plain")
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except URLError as e:
        log.error(f"ClickHouse query failed: {e}")
        raise


def insert_batch(rows: list[dict]) -> int:
    if not rows:
        return 0

    payload = "\n".join(json.dumps(row) for row in rows)
    query = "INSERT INTO scans FORMAT JSONEachRow"

    try:
        clickhouse_query(query, payload)
        return len(rows)
    except Exception as e:
        log.error(f"Failed to insert batch of {len(rows)}: {e}")
        return 0


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
    total_read = 0
    batch: list[dict] = []
    last_flush = time.monotonic()

    log.info("Reading JSON lines from stdin (scanner pipe)")

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

        # Flush marker from scanner.py — flush remaining batch
        if data.get("flush"):
            if batch:
                count = insert_batch(batch)
                total_inserted += count
                if count > 0:
                    log.info(f"Flushed {count} rows (sweep end) | read: {total_read} | inserted: {total_inserted}")
                batch.clear()
                last_flush = time.monotonic()
            continue

        # Map fields
        row = {
            "timestamp": data.get("sweep_id", ""),
            "freq_hz": data.get("freq_hz", 0),
            "power_dbfs": data.get("power_dbfs", 0.0),
            "sweep_id": data.get("sweep_id", ""),
        }

        total_read += 1
        batch.append(row)

        if len(batch) >= BATCH_SIZE:
            count = insert_batch(batch)
            total_inserted += count
            if count > 0:
                log.info(f"Flushed {count} rows (batch) | read: {total_read} | inserted: {total_inserted}")
            batch.clear()
            last_flush = time.monotonic()

        elapsed = time.monotonic() - last_flush
        if batch and elapsed >= FLUSH_INTERVAL:
            count = insert_batch(batch)
            total_inserted += count
            if count > 0:
                log.info(f"Flushed {count} rows (timer) | read: {total_read} | inserted: {total_inserted}")
            batch.clear()
            last_flush = time.monotonic()

    if batch:
        count = insert_batch(batch)
        total_inserted += count
        log.info(f"Final flush: {count} rows | total inserted: {total_inserted}")

    log.info(f"Shutdown complete. Read: {total_read}, Inserted: {total_inserted}")


if __name__ == "__main__":
    main()
