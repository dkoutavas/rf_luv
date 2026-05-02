#!/usr/bin/env python3
"""
ACARS acarsdec → ClickHouse Ingest Worker

Listens for JSON datagrams on a UDP socket (sent by acarsdec via -j /
OUTPUT_SERVER) and batch-inserts decoded ACARS messages into ClickHouse.

UDP transport mirrors the AIS pipeline (AIS-catcher → ais_ingest.py).
acarsdec emits one JSON object per datagram in MODE=J / OUTPUT_SERVER_MODE=udp,
e.g.:
  {"timestamp": 1764691234.567, "channel": 0, "freq": "131.525",
   "level": "-25.5", "error": 0, "mode": "2", "label": "H1",
   "tail": "N12345", "flight": "AAL123", "msgno": "S40A",
   "text": "FREE TEXT MESSAGE", "end": 1}

Quirks handled:
  - `freq` and `level` are emitted as strings (printf-formatted), parsed to floats
  - `ack` may be bool or single-char string, stringified uniformly
  - `libacars` is a nested object — stored as JSON string under libacars_json
  - `flight` is downlink-only; uplinks have only `tail`
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

# ─── Config ──────────────────────────────────────────────

UDP_PORT = int(os.environ.get("ACARS_UDP_PORT", "5550"))
CH_HOST = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CH_DB = os.environ.get("CLICKHOUSE_DB", "acars")
CH_USER = os.environ.get("CLICKHOUSE_USER", "acars")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "acars_local")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "100"))
FLUSH_INTERVAL = int(os.environ.get("FLUSH_INTERVAL_SECONDS", "10"))

# Tagged onto every row so cross-pipeline queries can slice by source dongle.
DONGLE_ID = os.environ.get("ACARS_DONGLE_ID", "v4-01")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("acars-ingest")

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
    """Execute a ClickHouse query via HTTP. Mirrors spectrum/db.py:
       - SELECT:               clickhouse_query(sql)
       - INSERT with payload:  clickhouse_query(sql, data=rows_jsonl)
    Both shapes POST. ClickHouse 24.3 rejects mutating DDL via GET.
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
    if not rows:
        return 0
    payload = "\n".join(json.dumps(row) for row in rows)
    try:
        clickhouse_query("INSERT INTO messages FORMAT JSONEachRow", payload)
        return len(rows)
    except Exception as e:
        log.error(f"Failed to insert batch of {len(rows)}: {e}")
        return 0


# ─── Field extraction ────────────────────────────────────

def _str_or_empty(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


def _float_or_zero(val) -> float:
    """Parse acarsdec's stringified freq/level fields, falling back to 0.0."""
    if val is None or val == "":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _format_ts(ts_val) -> str:
    """Convert acarsdec's float-seconds timestamp to ClickHouse DateTime64(3)."""
    if isinstance(ts_val, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(ts_val), tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
        except (ValueError, OSError):
            pass
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def extract_fields(data: dict) -> dict | None:
    """Map acarsdec JSON object to a ClickHouse row. Returns None if invalid."""
    has_payload = any(data.get(k) for k in ("tail", "flight", "label", "text"))
    if not has_payload:
        return None

    libacars_obj = data.get("libacars")
    libacars_json_str = ""
    libacars_app_name = ""
    if isinstance(libacars_obj, dict):
        libacars_json_str = json.dumps(libacars_obj)
        if libacars_obj:
            libacars_app_name = next(iter(libacars_obj.keys()))

    app_obj = data.get("app")
    if isinstance(app_obj, dict) and not libacars_app_name:
        libacars_app_name = _str_or_empty(app_obj.get("name"))

    row = {
        "timestamp":     _format_ts(data.get("timestamp")),
        "freq_mhz":      _float_or_zero(data.get("freq")),
        "channel":       int(data.get("channel") or 0),
        "level_db":      _float_or_zero(data.get("level")),
        "err_count":     int(data.get("error") or 0),
        "mode":          _str_or_empty(data.get("mode")),
        "label":         _str_or_empty(data.get("label")),
        "block_id":      _str_or_empty(data.get("block_id")),
        "ack":           _str_or_empty(data.get("ack")),
        "tail":          _str_or_empty(data.get("tail")).strip(),
        "flight":        _str_or_empty(data.get("flight")).strip(),
        "msgno":         _str_or_empty(data.get("msgno")),
        "text":          _str_or_empty(data.get("text")),
        "msg_end":       1 if data.get("end") else 0,

        "depa":          _str_or_empty(data.get("depa")),
        "dsta":          _str_or_empty(data.get("dsta")),
        "eta":           _str_or_empty(data.get("eta")),
        "gtout":         _str_or_empty(data.get("gtout")),
        "gtin":          _str_or_empty(data.get("gtin")),
        "wloff":         _str_or_empty(data.get("wloff")),
        "wlin":          _str_or_empty(data.get("wlin")),

        "sublabel":      _str_or_empty(data.get("sublabel")),
        "mfi":           _str_or_empty(data.get("mfi")),
        "libacars_app":  libacars_app_name,
        "libacars_json": libacars_json_str,

        "dongle_id":     DONGLE_ID,
        "station_id":    _str_or_empty(data.get("station_id")),

        "raw_json":      json.dumps(data, default=str),
    }
    return row


# ─── Main loop ───────────────────────────────────────────

def wait_for_clickhouse(max_retries: int = 30, delay: int = 2) -> None:
    for i in range(max_retries):
        try:
            clickhouse_query("SELECT 1 FORMAT TabSeparated")
            log.info("ClickHouse is ready")
            return
        except Exception:
            log.info(f"Waiting for ClickHouse... ({i + 1}/{max_retries})")
            time.sleep(delay)
    log.error("ClickHouse not available after retries, starting anyway")


def maybe_flush(batch: list[dict], last_flush: float, total_inserted: int,
                total_decoded: int, reason: str) -> tuple[float, int]:
    """Insert and clear `batch` if anything's there. Returns (new_last_flush, new_total_inserted)."""
    if not batch:
        return last_flush, total_inserted
    count = insert_batch(batch)
    total_inserted += count
    if count > 0:
        log.info(
            f"Flushed {count} rows ({reason}) | decoded: {total_decoded} | inserted: {total_inserted}"
        )
    batch.clear()
    return time.monotonic(), total_inserted


def main() -> None:
    global running
    wait_for_clickhouse()

    total_inserted = 0
    total_decoded = 0
    total_skipped = 0
    batch: list[dict] = []
    last_flush = time.monotonic()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", UDP_PORT))
    sock.settimeout(1.0)
    log.info(f"Listening for acarsdec JSON on UDP :{UDP_PORT}, dongle_id={DONGLE_ID}")

    while running:
        try:
            data, _addr = sock.recvfrom(8192)
        except socket.timeout:
            elapsed = time.monotonic() - last_flush
            if batch and elapsed >= FLUSH_INTERVAL:
                last_flush, total_inserted = maybe_flush(
                    batch, last_flush, total_inserted, total_decoded, "timer"
                )
            continue
        except OSError:
            break

        # Each datagram is typically one JSON object; tolerate newline-joined too.
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            continue

        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                total_skipped += 1
                continue
            row = extract_fields(obj)
            if row is None:
                total_skipped += 1
                continue
            total_decoded += 1
            batch.append(row)

        if len(batch) >= BATCH_SIZE:
            last_flush, total_inserted = maybe_flush(
                batch, last_flush, total_inserted, total_decoded, "batch"
            )

        elapsed = time.monotonic() - last_flush
        if batch and elapsed >= FLUSH_INTERVAL:
            last_flush, total_inserted = maybe_flush(
                batch, last_flush, total_inserted, total_decoded, "timer"
            )

    last_flush, total_inserted = maybe_flush(
        batch, last_flush, total_inserted, total_decoded, "shutdown"
    )
    sock.close()
    log.info(
        f"Shutdown complete. Decoded: {total_decoded}, "
        f"Inserted: {total_inserted}, Skipped: {total_skipped}"
    )


if __name__ == "__main__":
    main()
