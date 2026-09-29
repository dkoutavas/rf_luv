#!/usr/bin/env python3
"""
POCSAG multimon-ng -> ClickHouse ingest worker.

Reads multimon-ng's text lines from stdin (piped from rtl_fm | multimon-ng)
and batch-inserts decoded pages into ClickHouse. One page per line, e.g.:

  POCSAG1200: Address: 1234567  Function: 3  Alpha:   ON CALL TEAM B
  POCSAG512: Address:  123456  Function: 0  Numeric: 12345
  POCSAG1200: Address: 1234567  Function: 0            (tone-only page)

multimon-ng emits plain text, not JSON, so this parses the line itself. The
batch/flush/ClickHouse-HTTP machinery is copied from ism/ism_ingest.py.

PRIVACY: POCSAG is plaintext and can carry private messages (hospital pagers
and the like). Receiving is legal; the decoded text is stored only in the
local `pocsag` ClickHouse DB and must never be committed, screenshotted into a
shared place, or pushed off-host. See pocsag/README.md.
"""

import os
import re
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
CH_DB = os.environ.get("CLICKHOUSE_DB", "pocsag")
CH_USER = os.environ.get("CLICKHOUSE_USER", "pocsag")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "pocsag_local")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "50"))
FLUSH_INTERVAL = int(os.environ.get("FLUSH_INTERVAL_SECONDS", "10"))
# The tuned channel and dongle are stamped onto every row; multimon-ng itself
# does not know them.
FREQ_HZ = int(os.environ.get("POCSAG_FREQ_HZ", "0"))
DONGLE_ID = os.environ.get("POCSAG_DONGLE_ID", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pocsag-ingest")

# ─── Graceful shutdown ───────────────────────────────────

running = True


def handle_signal(signum, frame):
    global running
    log.info(f"Received signal {signum}, shutting down...")
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# ─── multimon-ng line parsing ────────────────────────────

# POCSAG<baud>: Address: <capcode>  Function: <0-3>  [Alpha|Numeric|Tone: <text>]
_POCSAG_RE = re.compile(
    r"^(POCSAG\d+):\s+Address:\s*(\d+)\s+Function:\s*(\d+)"
    r"(?:\s+(Alpha|Numeric|Tone):\s?(.*))?\s*$"
)


def parse_line(line: str, freq_hz: int = 0, dongle_id: str = "") -> dict | None:
    """Parse one multimon-ng stdout line into a ClickHouse row, or None.

    Non-POCSAG lines (multimon-ng banners, other decoders) return None.
    A page with no content section (a tone-only page) parses with an empty
    message and msg_type 'tone'.
    """
    m = _POCSAG_RE.match(line.strip())
    if not m:
        return None
    protocol, capcode, function, section, text = m.groups()
    msg_type = section.lower() if section else "tone"
    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "freq_hz": freq_hz,
        "protocol": protocol,
        "capcode": int(capcode),
        "function": int(function),
        "msg_type": msg_type,
        "message": (text or "").strip(),
        "raw_line": line.strip(),
        "dongle_id": dongle_id,
    }


# ─── ClickHouse insertion ────────────────────────────────

CH_URL = f"http://{CH_HOST}:{CH_PORT}/"


def clickhouse_query(query: str, data: str = "") -> str:
    """Execute a ClickHouse query via the HTTP interface (always POST).

    Mirrors ism/ism_ingest.py: SQL in the URL when there is a payload, else in
    the body. HTTPError captures the response body so ClickHouse's actual
    complaint is visible in logs instead of a bare HTTP 500.
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
    """Insert a batch of POCSAG rows into ClickHouse."""
    if not rows:
        return 0
    payload = "\n".join(json.dumps(row) for row in rows)
    try:
        clickhouse_query("INSERT INTO messages FORMAT JSONEachRow", payload)
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
    total_decoded = 0
    batch: list[dict] = []
    last_flush = time.monotonic()

    log.info(f"Reading multimon-ng lines from stdin (freq_hz={FREQ_HZ}, dongle={DONGLE_ID})")

    for line in sys.stdin:
        if not running:
            break
        row = parse_line(line, FREQ_HZ, DONGLE_ID)
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
