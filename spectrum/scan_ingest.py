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
    if data:
        # INSERT-style: query in URL param, data in POST body
        params = f"database={CH_DB}&user={CH_USER}&password={CH_PASSWORD}&query={quote(query)}"
        url = f"{CH_URL}?{params}"
        req = Request(url, data=data.encode("utf-8"))
    else:
        # All other queries: send as POST body (required for ALTER TABLE, etc.)
        params = f"database={CH_DB}&user={CH_USER}&password={CH_PASSWORD}"
        url = f"{CH_URL}?{params}"
        req = Request(url, data=query.encode("utf-8"))
    req.add_header("Content-Type", "text/plain")
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except URLError as e:
        log.error(f"ClickHouse query failed: {e}")
        raise


def insert_batch(rows: list[dict], table: str = "scans") -> int:
    if not rows:
        return 0

    payload = "\n".join(json.dumps(row) for row in rows)
    query = f"INSERT INTO {table} FORMAT JSONEachRow"

    try:
        clickhouse_query(query, payload)
        return len(rows)
    except Exception as e:
        log.error(f"Failed to insert {len(rows)} into {table}: {e}")
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
    peak_batch: list[dict] = []
    event_batch: list[dict] = []
    health_batch: list[dict] = []
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

        # Run tracking messages
        if data.get("run_start"):
            try:
                clickhouse_query(
                    "INSERT INTO scan_runs FORMAT JSONEachRow",
                    json.dumps({
                        "run_id": data["run_id"],
                        "started_at": data["started_at"],
                        "gain_db": data["gain_db"],
                        "antenna_position": data.get("antenna_position", ""),
                        "antenna_arms_cm": data.get("antenna_arms_cm", 0),
                        "antenna_orientation_deg": data.get("antenna_orientation_deg", 0),
                        "antenna_height_m": data.get("antenna_height_m", 0),
                        "notes": data.get("notes", ""),
                    })
                )
                log.info(f"Run started: {data['run_id']} (gain={data['gain_db']}, pos={data.get('antenna_position')})")
            except Exception as e:
                log.error(f"Failed to insert run_start: {e}")
            continue

        if data.get("run_update"):
            try:
                rid = data["run_id"]
                nf = data["noise_floor_dbfs"]
                ps = data["peak_signal_dbfs"]
                pf = data["peak_signal_freq_hz"]
                clickhouse_query(
                    f"ALTER TABLE scan_runs UPDATE "
                    f"noise_floor_dbfs = {nf}, peak_signal_dbfs = {ps}, "
                    f"peak_signal_freq_hz = {pf} WHERE run_id = '{rid}'"
                )
                log.info(f"Run updated: noise_floor={nf}, peak={ps} dBFS")
            except Exception as e:
                log.error(f"Failed to update run: {e}")
            continue

        if data.get("run_end"):
            try:
                clickhouse_query(
                    f"ALTER TABLE scan_runs UPDATE "
                    f"ended_at = '{data['ended_at']}' "
                    f"WHERE run_id = '{data['run_id']}'"
                )
                log.info(f"Run ended: {data['run_id']}")
            except Exception as e:
                log.error(f"Failed to update run_end: {e}")
            continue

        # Flush marker from scanner.py — flush all batches
        if data.get("flush"):
            if batch:
                count = insert_batch(batch, "scans")
                total_inserted += count
                if count > 0:
                    log.info(f"Flushed {count} scans (sweep end) | inserted: {total_inserted}")
                batch.clear()
            if peak_batch:
                insert_batch(peak_batch, "peaks")
                peak_batch.clear()
            if event_batch:
                insert_batch(event_batch, "events")
                event_batch.clear()
            if health_batch:
                insert_batch(health_batch, "sweep_health")
                health_batch.clear()
            last_flush = time.monotonic()
            continue

        # Extract timestamp from sweep_id (format: "preset:2026-04-04 ...")
        sweep_id = data.get("sweep_id", "")
        ts = sweep_id.split(":", 1)[1] if ":" in sweep_id else sweep_id

        # Route to appropriate table based on marker flags
        if data.get("peak"):
            peak_batch.append({
                "timestamp": ts,
                "freq_hz": data.get("freq_hz", 0),
                "power_dbfs": data.get("power_dbfs", 0.0),
                "prominence_db": data.get("prominence_db", 0.0),
                "sweep_id": sweep_id,
                "run_id": data.get("run_id", ""),
            })
            continue

        if data.get("event"):
            event_batch.append({
                "timestamp": ts,
                "freq_hz": data.get("freq_hz", 0),
                "event_type": data.get("event_type", ""),
                "power_dbfs": data.get("power_dbfs", 0.0),
                "prev_power": data.get("prev_power", 0.0),
                "delta_db": data.get("delta_db", 0.0),
                "sweep_id": sweep_id,
                "run_id": data.get("run_id", ""),
            })
            continue

        if data.get("health"):
            health_batch.append({
                "timestamp": ts,
                "sweep_id": sweep_id,
                "preset": data.get("preset", ""),
                "bin_count": data.get("bin_count", 0),
                "max_power": data.get("max_power", -100.0),
                "max_power_dvbt": data.get("max_power_dvbt", -100.0),
                "sweep_duration_ms": data.get("sweep_duration_ms", 0),
                "gain_db": data.get("gain_db", 0.0),
                "clipped": data.get("clipped", False),
                "max_clip_fraction": data.get("max_clip_fraction", 0.0),
                "worst_clip_freq_hz": data.get("worst_clip_freq_hz", 0),
                "clipped_captures": data.get("clipped_captures", 0),
                "total_captures": data.get("total_captures", 0),
                "run_id": data.get("run_id", ""),
            })
            continue

        # Regular scan bin
        row = {
            "timestamp": ts,
            "freq_hz": data.get("freq_hz", 0),
            "power_dbfs": data.get("power_dbfs", 0.0),
            "sweep_id": sweep_id,
            "run_id": data.get("run_id", ""),
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
