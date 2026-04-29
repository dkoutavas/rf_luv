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

import db
import messages

# ─── Config ──────────────────────────────────────────────

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "500"))
FLUSH_INTERVAL = int(os.environ.get("FLUSH_INTERVAL_SECONDS", "5"))

# Fallback dongle_id when the incoming JSON line lacks the field. Matches
# scanner.py's default. A WARN is logged on fallback so a scanner that
# forgot to emit dongle_id is loudly visible in logs.
DEFAULT_DONGLE_ID = "v3-01"
_dongle_warn_emitted = False

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


def insert_batch(rows: list[dict], table: str = "scans") -> int:
    """Wraps db.insert with the silent-error semantics this loop needs.

    db.insert raises on HTTP error; the ingest loop tolerates a transient
    insert failure (next batch will catch up — scanner.py keeps producing).
    Returning 0 on failure lets the metrics counter stay accurate.
    """
    if not rows:
        return 0
    try:
        db.insert(table, rows, timeout=10)
        return len(rows)
    except Exception as e:
        log.error(f"Failed to insert {len(rows)} into {table}: {e}")
        return 0


def dongle_id_from(data: dict) -> str:
    """Extract dongle_id from a scanner JSON line, falling back to the default
    with a one-shot WARN log so misconfigured scanners are visible but not noisy."""
    global _dongle_warn_emitted
    val = data.get("dongle_id")
    if val:
        return val
    if not _dongle_warn_emitted:
        log.warning(
            f"incoming JSON line lacks dongle_id, defaulting to '{DEFAULT_DONGLE_ID}' "
            f"— check scanner.py version / SCAN_DONGLE_ID env"
        )
        _dongle_warn_emitted = True
    return DEFAULT_DONGLE_ID


# ─── Main loop ───────────────────────────────────────────

def wait_for_clickhouse(max_retries: int = 30, delay: int = 2):
    for i in range(max_retries):
        try:
            db.query("SELECT 1 FORMAT TabSeparated", timeout=10)
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
        if data.get(messages.RUN_START):
            try:
                dongle_id = dongle_id_from(data)
                db.insert("scan_runs", [{
                    "run_id": data["run_id"],
                    "started_at": data["started_at"],
                    "gain_db": data["gain_db"],
                    "antenna_position": data.get("antenna_position", ""),
                    "antenna_arms_cm": data.get("antenna_arms_cm", 0),
                    "antenna_orientation_deg": data.get("antenna_orientation_deg", 0),
                    "antenna_height_m": data.get("antenna_height_m", 0),
                    "notes": data.get("notes", ""),
                    "dongle_id": dongle_id,
                }], timeout=10)
                log.info(f"Run started: {data['run_id']} (dongle={dongle_id}, gain={data['gain_db']}, pos={data.get('antenna_position')})")
            except Exception as e:
                log.error(f"Failed to insert run_start: {e}")
            continue

        if data.get(messages.RUN_UPDATE):
            try:
                rid = data["run_id"]
                nf = data["noise_floor_dbfs"]
                ps = data["peak_signal_dbfs"]
                pf = data["peak_signal_freq_hz"]
                db.query(
                    f"ALTER TABLE scan_runs UPDATE "
                    f"noise_floor_dbfs = {nf}, peak_signal_dbfs = {ps}, "
                    f"peak_signal_freq_hz = {pf} WHERE run_id = '{rid}'",
                    timeout=10,
                )
                log.info(f"Run updated: noise_floor={nf}, peak={ps} dBFS")
            except Exception as e:
                log.error(f"Failed to update run: {e}")
            continue

        if data.get(messages.RUN_END):
            try:
                db.query(
                    f"ALTER TABLE scan_runs UPDATE "
                    f"ended_at = '{data['ended_at']}' "
                    f"WHERE run_id = '{data['run_id']}'",
                    timeout=10,
                )
                log.info(f"Run ended: {data['run_id']}")
            except Exception as e:
                log.error(f"Failed to update run_end: {e}")
            continue

        # Flush marker from scanner.py — flush all batches
        if data.get(messages.FLUSH):
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

        # Prefer explicit timestamp field; fall back to parsing sweep_id
        sweep_id = data.get("sweep_id", "")
        ts = data.get("timestamp") or (sweep_id.split(":", 1)[1] if ":" in sweep_id else sweep_id)

        # Every non-control line carries a dongle_id; pull it once per line.
        dongle_id = dongle_id_from(data)

        # Route to appropriate table based on marker flags
        if data.get(messages.PEAK):
            peak_batch.append({
                "timestamp": ts,
                "freq_hz": data.get("freq_hz", 0),
                "power_dbfs": data.get("power_dbfs", 0.0),
                "prominence_db": data.get("prominence_db", 0.0),
                "sweep_id": sweep_id,
                "run_id": data.get("run_id", ""),
                "dongle_id": dongle_id,
            })
            continue

        if data.get(messages.EVENT):
            event_batch.append({
                "timestamp": ts,
                "freq_hz": data.get("freq_hz", 0),
                "event_type": data.get("event_type", ""),
                "power_dbfs": data.get("power_dbfs", 0.0),
                "prev_power": data.get("prev_power", 0.0),
                "delta_db": data.get("delta_db", 0.0),
                "sweep_id": sweep_id,
                "run_id": data.get("run_id", ""),
                "dongle_id": dongle_id,
            })
            continue

        if data.get(messages.HEALTH):
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
                "dongle_id": dongle_id,
            })
            continue

        # Regular scan bin
        row = {
            "timestamp": ts,
            "freq_hz": data.get("freq_hz", 0),
            "power_dbfs": data.get("power_dbfs", 0.0),
            "sweep_id": sweep_id,
            "run_id": data.get("run_id", ""),
            "dongle_id": dongle_id,
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
