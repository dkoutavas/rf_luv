#!/usr/bin/env python3
"""
ISM Band rtl_433 → ClickHouse Ingest Worker

Reads JSON lines from stdin (piped from rtl_433) and batch-inserts
decoded device events into ClickHouse.

rtl_433 outputs one JSON object per decoded transmission, e.g.:
  {"time":"2026-04-04 12:00:00","model":"Acurite-Tower","id":1234,
   "channel":"A","battery_ok":1,"temperature_C":22.3,"humidity":45}

We map known fields to typed columns and store the full JSON
in raw_json so nothing is lost from the 200+ supported protocols.
"""

import os
import sys
import json
import time
import signal
import logging
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ─── Config ──────────────────────────────────────────────

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CH_DB = os.environ.get("CLICKHOUSE_DB", "ism")
CH_USER = os.environ.get("CLICKHOUSE_USER", "ism")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "ism_local")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "200"))
FLUSH_INTERVAL = int(os.environ.get("FLUSH_INTERVAL_SECONDS", "10"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ism-ingest")

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
    """Execute a ClickHouse query via HTTP interface.

    Always POSTs (mirrors spectrum/db.py + acars/migrate.py):
      - SELECT / DDL:        clickhouse_query(sql)               → SQL in body
      - INSERT with payload: clickhouse_query(sql, data=rows)    → SQL in URL, payload in body

    ClickHouse 24.3 forbids DDL via GET ("readonly mode"), so always-POST
    keeps the function future-proof if ISM ever runs DDL through it
    (currently it only does INSERTs; init.sql runs at clickhouse boot).
    HTTPError captures the response body so ClickHouse's actual complaint
    is visible in logs instead of a bare HTTP 500.
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
    """Insert a batch of ISM event rows into ClickHouse."""
    if not rows:
        return 0

    payload = "\n".join(json.dumps(row) for row in rows)
    query = "INSERT INTO events FORMAT JSONEachRow"

    try:
        clickhouse_query(query, payload)
        return len(rows)
    except Exception as e:
        log.error(f"Failed to insert batch of {len(rows)}: {e}")
        return 0


# ─── Field extraction ───────────────────────────────────

# rtl_433 JSON field names → ClickHouse column names
_FIELD_MAP = {
    "temperature_C": "temperature_c",
    "humidity": "humidity",
    "pressure_hPa": "pressure_hpa",
    "wind_avg_km_h": "wind_avg_km_h",
    "wind_max_km_h": "wind_max_km_h",
    "wind_dir_deg": "wind_dir_deg",
    "rain_mm": "rain_mm",
    "battery_ok": "battery_ok",
    "rssi": "rssi",
    "snr": "snr",
}


def extract_fields(data: dict) -> dict | None:
    """Extract known fields from rtl_433 JSON into a ClickHouse row."""
    model = data.get("model")
    if not model:
        return None

    # Parse timestamp from rtl_433 or use current UTC
    time_str = data.get("time")
    if time_str:
        try:
            ts = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            ts_out = ts.strftime("%Y-%m-%d %H:%M:%S.000")
        except ValueError:
            ts_out = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    else:
        ts_out = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    row = {
        "timestamp": ts_out,
        "model": model,
        "raw_json": json.dumps(data),
    }

    # Device ID — rtl_433 uses "id" as int or string depending on protocol
    device_id = data.get("id")
    if device_id is not None:
        row["device_id"] = str(device_id)

    # Channel
    channel = data.get("channel")
    if channel is not None:
        row["channel"] = str(channel)

    # Map known numeric fields
    for json_key, ch_col in _FIELD_MAP.items():
        val = data.get(json_key)
        if val is not None:
            try:
                row[ch_col] = float(val)
            except (ValueError, TypeError):
                pass

    return row


# ─── Main loop ───────────────────────────────────────────

def wait_for_clickhouse(max_retries: int = 30, delay: int = 2):
    """Wait for ClickHouse to be ready."""
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

    log.info("Reading JSON lines from stdin (rtl_433 pipe)")

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

        row = extract_fields(data)
        if row is None:
            continue

        total_decoded += 1
        batch.append(row)

        # Flush on batch size
        if len(batch) >= BATCH_SIZE:
            count = insert_batch(batch)
            total_inserted += count
            if count > 0:
                log.info(f"Flushed {count} rows (batch) | decoded: {total_decoded} | inserted: {total_inserted}")
            batch.clear()
            last_flush = time.monotonic()

        # Flush on timer
        elapsed = time.monotonic() - last_flush
        if batch and elapsed >= FLUSH_INTERVAL:
            count = insert_batch(batch)
            total_inserted += count
            if count > 0:
                log.info(f"Flushed {count} rows (timer) | decoded: {total_decoded} | inserted: {total_inserted}")
            batch.clear()
            last_flush = time.monotonic()

    # Final flush
    if batch:
        count = insert_batch(batch)
        total_inserted += count
        log.info(f"Final flush: {count} rows | total inserted: {total_inserted}")

    log.info(f"Shutdown complete. Decoded: {total_decoded}, Inserted: {total_inserted}")


if __name__ == "__main__":
    main()
