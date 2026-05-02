#!/usr/bin/env python3
"""
ADS-B SBS BaseStation → ClickHouse Ingest Worker

Connects to readsb's SBS output (port 30003), parses position messages,
and batch-inserts into ClickHouse. Reconnects automatically on failures.

SBS BaseStation format (comma-separated):
MSG,msg_type,session_id,aircraft_id,hex_ident,flight_id,
date_gen,time_gen,date_log,time_log,
callsign,altitude,ground_speed,track,lat,lon,
vertical_rate,squawk,alert,emergency,spi,is_on_ground
"""

import os
import sys
import csv
import json
import time
import socket
import signal
import logging
from io import StringIO
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ─── Config ──────────────────────────────────────────────

SBS_HOST = os.environ.get("SBS_HOST", "readsb")
SBS_PORT = int(os.environ.get("SBS_PORT", "30003"))
CH_HOST = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CH_DB = os.environ.get("CLICKHOUSE_DB", "adsb")
CH_USER = os.environ.get("CLICKHOUSE_USER", "adsb")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "adsb_local")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "500"))
FLUSH_INTERVAL = int(os.environ.get("FLUSH_INTERVAL_SECONDS", "5"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("adsb-ingest")

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

    Always POSTs (mirrors spectrum/db.py + acars/acars_ingest.py):
      - SELECT / DDL:        clickhouse_query(sql)               → SQL in body
      - INSERT with payload: clickhouse_query(sql, data=rows)    → SQL in URL, payload in body

    ClickHouse 24.3 forbids DDL via GET ("readonly mode"). HTTPError
    captures the response body so future schema mismatches surface in logs.
    Also quote()s the query for URL safety (ADS-B previously interpolated raw).
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
    """Insert a batch of position rows into ClickHouse using JSONEachRow format."""
    if not rows:
        return 0

    payload = "\n".join(json.dumps(row) for row in rows)
    query = "INSERT INTO positions FORMAT JSONEachRow"

    try:
        clickhouse_query(query, payload)
        return len(rows)
    except Exception as e:
        log.error(f"Failed to insert batch of {len(rows)}: {e}")
        return 0

# ─── SBS message parsing ────────────────────────────────

def parse_sbs_line(line: str) -> dict | None:
    """
    Parse an SBS BaseStation format line into a position dict.
    Only processes MSG type messages (position/identification reports).
    """
    line = line.strip()
    if not line:
        return None

    parts = line.split(",")
    if len(parts) < 22:
        return None

    if parts[0] != "MSG":
        return None

    msg_type = parts[1].strip()
    hex_ident = parts[4].strip()

    if not hex_ident:
        return None

    # Parse timestamp from date_generated + time_generated
    date_gen = parts[6].strip()
    time_gen = parts[7].strip()

    try:
        if date_gen and time_gen:
            ts = datetime.strptime(f"{date_gen} {time_gen}", "%Y/%m/%d %H:%M:%S.%f")
        else:
            ts = datetime.now(timezone.utc)
    except ValueError:
        ts = datetime.now(timezone.utc)

    row = {
        "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "hex_ident": hex_ident,
        "msg_type": int(msg_type) if msg_type.isdigit() else 0,
    }

    # Callsign (MSG type 1)
    callsign = parts[10].strip()
    if callsign:
        row["callsign"] = callsign

    # Altitude
    alt = parts[11].strip()
    if alt:
        try:
            row["altitude"] = int(alt)
        except ValueError:
            pass

    # Ground speed
    spd = parts[12].strip()
    if spd:
        try:
            row["ground_speed"] = float(spd)
        except ValueError:
            pass

    # Track (heading)
    trk = parts[13].strip()
    if trk:
        try:
            row["track"] = float(trk)
        except ValueError:
            pass

    # Position
    lat_s = parts[14].strip()
    lon_s = parts[15].strip()
    if lat_s and lon_s:
        try:
            row["lat"] = float(lat_s)
            row["lon"] = float(lon_s)
        except ValueError:
            pass

    # Vertical rate
    vr = parts[16].strip()
    if vr:
        try:
            row["vertical_rate"] = int(vr)
        except ValueError:
            pass

    # Squawk
    squawk = parts[17].strip()
    if squawk:
        row["squawk"] = squawk

    # On ground
    gnd = parts[21].strip()
    if gnd:
        row["is_on_ground"] = 1 if gnd == "-1" or gnd.lower() == "true" else 0

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
    batch: list[dict] = []
    last_flush = time.monotonic()

    while running:
        try:
            log.info(f"Connecting to readsb SBS at {SBS_HOST}:{SBS_PORT}...")
            sock = socket.create_connection((SBS_HOST, SBS_PORT), timeout=10)
            sock.settimeout(1.0)  # non-blocking reads for graceful shutdown
            sbs_file = sock.makefile("r", encoding="utf-8", errors="replace")
            log.info("Connected to readsb SBS output")

            while running:
                try:
                    line = sbs_file.readline()
                except socket.timeout:
                    # Check if it's time to flush
                    elapsed = time.monotonic() - last_flush
                    if batch and elapsed >= FLUSH_INTERVAL:
                        count = insert_batch(batch)
                        total_inserted += count
                        if count > 0:
                            log.info(f"Flushed {count} rows (timer) | total: {total_inserted}")
                        batch.clear()
                        last_flush = time.monotonic()
                    continue

                if not line:
                    log.warning("SBS connection closed")
                    break

                row = parse_sbs_line(line)
                if row:
                    batch.append(row)

                # Flush on batch size
                if len(batch) >= BATCH_SIZE:
                    count = insert_batch(batch)
                    total_inserted += count
                    if count > 0:
                        log.info(f"Flushed {count} rows (batch) | total: {total_inserted}")
                    batch.clear()
                    last_flush = time.monotonic()

                # Flush on timer
                elapsed = time.monotonic() - last_flush
                if batch and elapsed >= FLUSH_INTERVAL:
                    count = insert_batch(batch)
                    total_inserted += count
                    if count > 0:
                        log.info(f"Flushed {count} rows (timer) | total: {total_inserted}")
                    batch.clear()
                    last_flush = time.monotonic()

        except (ConnectionRefusedError, socket.error, OSError) as e:
            log.warning(f"Connection error: {e} — retrying in 5s...")
            time.sleep(5)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    # Final flush
    if batch:
        count = insert_batch(batch)
        total_inserted += count
        log.info(f"Final flush: {count} rows | total: {total_inserted}")

    log.info(f"Shutdown complete. Total rows inserted: {total_inserted}")


if __name__ == "__main__":
    main()
