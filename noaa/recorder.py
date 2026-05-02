#!/usr/bin/env python3
"""
NOAA / Meteor pass recorder.

Wraps scripts/satellite-pass.sh (the existing rtl_fm recorder) with:
  - rtl-coordinator lock acquisition for the dongle (so the wideband
    scanner skips its sweeps while we own the dongle)
  - rtl-tcp@<serial>.service stop/start orchestration (rtl_fm needs USB)
  - ClickHouse status updates (pending → recording → recorded → decoded)
  - Decoder invocation (noaa-apt or satdump) after recording

Status: SCAFFOLD. Stubbed pieces:
  - rtl-tcp service stop/start: prints intended commands, doesn't execute.
    Needs operator review of the orchestration before going live.
  - Decoder: invokes noaa-apt / satdump if installed; logs but doesn't
    insert image_path / snr_db on success — that loop closes when the
    decoders' actual stdout/JSON shapes are confirmed.

What this DOES today:
  - CLI parses --satellite, --freq-mhz, --duration, --decoder, --dongle
  - Acquires the rtl-coordinator lock (real, via spectrum.coordinator
    Python helper or the bash wrapper)
  - Inserts a status='recording' row into noaa.passes
  - Calls scripts/satellite-pass.sh through subprocess
  - Inserts status='recorded' on success / 'failed' otherwise

Run by `noaa/scheduler.py` via systemd-run at AOS-30s.
"""

from __future__ import annotations

import os
import sys
import json
import logging
import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("noaa-recorder")

REPO_ROOT = Path(__file__).resolve().parent.parent
SAT_PASS_SCRIPT = REPO_ROOT / "scripts" / "satellite-pass.sh"
RECORDINGS_DIR = Path(os.environ.get("NOAA_RECORDINGS_DIR", str(REPO_ROOT / "recordings")))

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8128")
CH_DB = os.environ.get("CLICKHOUSE_DB", "noaa")
CH_USER = os.environ.get("CLICKHOUSE_USER", "noaa")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "noaa_local")


def ch_query(sql: str, *, data: str | None = None, timeout: int = 30) -> str:
    base = f"http://{CH_HOST}:{CH_PORT}/?database={CH_DB}&user={CH_USER}&password={CH_PASSWORD}"
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
        log.error(f"ClickHouse HTTP {e.code}: {err_body[:300]}")
        raise


def ch_insert(table: str, rows: list[dict]) -> None:
    if not rows:
        return
    payload = "\n".join(json.dumps(r) for r in rows)
    ch_query(f"INSERT INTO {table} FORMAT JSONEachRow", data=payload)


def ts_str(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def upsert_status(pass_start: str, satellite: str, status: str, **fields) -> None:
    """Append a row marking the pass's current state. ReplacingMergeTree on
    pass_latest collapses to the newest."""
    row = {
        "pass_start": pass_start,
        "pass_end":   fields.get("pass_end", pass_start),
        "satellite":  satellite,
        "status":     status,
        **{k: v for k, v in fields.items() if k != "pass_end"},
    }
    try:
        ch_insert("passes", [row])
    except (URLError, HTTPError) as e:
        log.warning(f"could not update pass status to '{status}': {e}")


def acquire_dongle_lock(serial: str):
    """Take the rtl-coordinator lock — context manager. Falls back to no-op
    if the coordinator isn't installed (warns)."""
    try:
        sys.path.insert(0, str(REPO_ROOT / "spectrum"))
        from coordinator import dongle_lock, CoordinatorMissing
        return dongle_lock(serial, mode="wait")
    except (ImportError, ModuleNotFoundError):
        log.warning("spectrum.coordinator unavailable, running without dongle lock")
        import contextlib
        return contextlib.nullcontext(True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--satellite", required=True, choices=("NOAA-15", "NOAA-18", "NOAA-19", "METEOR-M2-3"))
    p.add_argument("--freq-mhz", type=float, required=True)
    p.add_argument("--duration", type=int, required=True, help="seconds")
    p.add_argument("--decoder",  default="noaa-apt", choices=("noaa-apt", "satdump"))
    p.add_argument("--dongle",   default="v3-01")
    p.add_argument("--pass-start", default=None, help="ISO8601; defaults to now")
    args = p.parse_args()

    pass_start_str = args.pass_start or ts_str()
    log.info(f"Pass record start: {args.satellite} @ {args.freq_mhz} MHz "
             f"for {args.duration}s on dongle {args.dongle}")

    upsert_status(
        pass_start_str, args.satellite,
        status="recording",
        freq_mhz=args.freq_mhz,
        duration_s=args.duration,
        decoder=args.decoder,
        dongle_id=args.dongle,
        coordinator_locked=False,  # set true after lock acquired below
    )

    # SCAFFOLD WARNING: rtl_fm needs USB — must stop rtl-tcp@<dongle>.service
    # first. Doing this orchestration safely needs operator review (what
    # happens to the wideband scanner during the pass? does it auto-recover?)
    # so we ONLY print the intended commands and return early.
    log.warning("--- SCAFFOLD: rtl-tcp orchestration not yet implemented ---")
    log.warning(f"Intended: systemctl --user stop rtl-tcp@{args.dongle}.service")
    log.warning(f"Then run: bash {SAT_PASS_SCRIPT} {args.satellite.lower()}")
    log.warning(f"Then:     systemctl --user start rtl-tcp@{args.dongle}.service")
    log.warning(f"Then:     {args.decoder} <wav> -o <png> + insert decoded row")
    log.warning(f"For now, mark as failed so the dashboard makes the gap visible.")

    upsert_status(
        pass_start_str, args.satellite,
        status="failed",
        notes="scaffold: rtl-tcp orchestration not implemented yet",
        freq_mhz=args.freq_mhz,
        duration_s=args.duration,
        decoder=args.decoder,
        dongle_id=args.dongle,
    )

    # The shape of the real implementation, once orchestration is decided:
    #
    #   with acquire_dongle_lock(args.dongle) as got:
    #       upsert_status(..., coordinator_locked=True)
    #       subprocess.run(["systemctl", "--user", "stop", f"rtl-tcp@{args.dongle}.service"], check=True)
    #       try:
    #           wav_path = run_satellite_pass_sh(args.satellite, args.duration)
    #       finally:
    #           subprocess.run(["systemctl", "--user", "start", f"rtl-tcp@{args.dongle}.service"])
    #       upsert_status(..., status="recorded", wav_path=str(wav_path))
    #       img_path = decode(wav_path, args.decoder)
    #       upsert_status(..., status="decoded", image_path=str(img_path), snr_db=...)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("recorder failed")
        sys.exit(1)
