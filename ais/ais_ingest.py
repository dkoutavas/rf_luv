#!/usr/bin/env python3
"""
AIS NMEA → ClickHouse Ingest Worker

Listens for AIVDM NMEA sentences on a UDP socket (sent by AIS-catcher),
decodes them using ais_decoder, and batch-inserts into ClickHouse.

UDP is used because AIS-catcher's -u flag sends NMEA as UDP datagrams —
one sentence per packet, no framing needed. Simpler than TCP for this use case.
"""

import os
import sys
import json
import time
import socket
import signal
import logging
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from ais_decoder import NMEAAssembler, decode_nmea

# ─── Config ──────────────────────────────────────────────

UDP_PORT = int(os.environ.get("AIS_UDP_PORT", "10110"))
CH_HOST = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CH_DB = os.environ.get("CLICKHOUSE_DB", "ais")
CH_USER = os.environ.get("CLICKHOUSE_USER", "ais")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "ais_local")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "500"))
FLUSH_INTERVAL = int(os.environ.get("FLUSH_INTERVAL_SECONDS", "5"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ais-ingest")

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

    ClickHouse 24.3 forbids DDL via GET ("readonly mode"). HTTPError captures
    the response body so future schema mismatches surface in logs.
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
    """Insert a batch of decoded AIS rows into ClickHouse."""
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

    assembler = NMEAAssembler(timeout=30.0)
    total_inserted = 0
    total_decoded = 0
    batch: list[dict] = []
    last_flush = time.monotonic()

    # Bind UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", UDP_PORT))
    sock.settimeout(1.0)  # non-blocking for graceful shutdown
    log.info(f"Listening for NMEA on UDP :{UDP_PORT}")

    while running:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            # Check if it's time to flush
            elapsed = time.monotonic() - last_flush
            if batch and elapsed >= FLUSH_INTERVAL:
                count = insert_batch(batch)
                total_inserted += count
                if count > 0:
                    log.info(f"Flushed {count} rows (timer) | decoded: {total_decoded} | inserted: {total_inserted}")
                batch.clear()
                last_flush = time.monotonic()
            continue
        except OSError:
            break

        # Each UDP datagram may contain one or more NMEA sentences
        lines = data.decode("ascii", errors="replace").strip().split("\n")
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        for line in lines:
            line = line.strip()
            if not line:
                continue

            decoded = decode_nmea(line, assembler)
            if decoded is None:
                continue

            total_decoded += 1

            # Add timestamp
            decoded["timestamp"] = now_str
            batch.append(decoded)

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

    sock.close()
    log.info(f"Shutdown complete. Decoded: {total_decoded}, Inserted: {total_inserted}")


if __name__ == "__main__":
    main()
