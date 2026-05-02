#!/usr/bin/env python3
"""
NOAA / Meteor pass scheduler.

Predicts the next 12 hours of satellite passes from current TLEs, inserts
'pending' rows into noaa.passes, and queues a transient systemd timer for
each one to fire `recorder.py` at AOS.

Designed to be called hourly via systemd (ops/noaa-pass-scheduler/) — each
run is idempotent: it skips passes that already have a row in the table.

Status: SCAFFOLD. Stubbed pieces:
  - TLE source: reads from /var/lib/noaa/tles.txt (refreshed by tle_refresh.sh
    daily). If the file is missing, exits 0 with a warning — does not fetch.
  - Pass prediction: requires `orbit-predictor` (installed via setup/install-wsl.sh).
    If not present, exits 0 with a warning so the deploy doesn't crash before the
    operator has installed it.
  - Systemd queueing: prints `systemd-run --on-calendar=... --unit=noaa-pass-XXX
    /usr/local/bin/noaa-record-pass <args>` commands but doesn't execute them yet
    (NOAA_DRY_RUN=1 by default while the design is shaking out).

What this DOES today:
  - Loads existing passes from noaa.passes (via HTTP)
  - Fetches receiver lat/lon/alt from env (NOAA_RX_LAT, _LON, _ALT_M)
  - Tries to compute upcoming passes from TLE + receiver
  - INSERTs pending rows for new ones it finds (skipping duplicates)

What's TODO before this is production-ready:
  [ ] Implement systemd-run scheduling at AOS-30s (set NOAA_DRY_RUN=0)
  [ ] tle_refresh.sh that pulls from celestrak weekly + signs the source
  [ ] Tunable horizon (currently hardcoded 12h)
  [ ] Multi-satellite filtering (currently does all NOAA + METEOR)
  [ ] Logging/metrics for missed passes
"""

from __future__ import annotations

import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
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
log = logging.getLogger("noaa-scheduler")

# ─── Config ─────────────────────────────────────────────────

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8128")
CH_DB = os.environ.get("CLICKHOUSE_DB", "noaa")
CH_USER = os.environ.get("CLICKHOUSE_USER", "noaa")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "noaa_local")

TLE_PATH = Path(os.environ.get("NOAA_TLE_PATH", "/var/lib/noaa/tles.txt"))
HORIZON_HOURS = int(os.environ.get("NOAA_HORIZON_HOURS", "12"))
MIN_ELEVATION_DEG = float(os.environ.get("NOAA_MIN_ELEVATION_DEG", "20"))
DRY_RUN = os.environ.get("NOAA_DRY_RUN", "1") == "1"

RX_LAT = float(os.environ.get("NOAA_RX_LAT", "37.9838"))      # Athens default
RX_LON = float(os.environ.get("NOAA_RX_LON", "23.7275"))
RX_ALT_M = float(os.environ.get("NOAA_RX_ALT_M", "100"))

# Frequency table — keys MUST match celestrak's TLE name lines exactly
# (post-strip), so the scheduler can find a TLE by name. Verified against
# celestrak's CATNR-based responses 2026-05.
SATELLITES = {
    "NOAA 15":     {"freq_mhz": 137.620,  "decoder": "noaa-apt"},
    "NOAA 18":     {"freq_mhz": 137.9125, "decoder": "noaa-apt"},
    "NOAA 19":     {"freq_mhz": 137.100,  "decoder": "noaa-apt"},
    "METEOR-M2 3": {"freq_mhz": 137.100,  "decoder": "satdump"},
    "METEOR-M2 4": {"freq_mhz": 137.100,  "decoder": "satdump"},
}


# ─── ClickHouse helpers (mirrors acars/migrate.py + spectrum/db.py) ──

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


def ch_query_rows(sql: str) -> list[dict]:
    text = ch_query(sql + " FORMAT JSONEachRow")
    return [json.loads(line) for line in text.splitlines() if line]


def ch_insert(table: str, rows: list[dict]) -> None:
    if not rows:
        return
    payload = "\n".join(json.dumps(r) for r in rows)
    ch_query(f"INSERT INTO {table} FORMAT JSONEachRow", data=payload)


# ─── Pass prediction (orbit-predictor) ───────────────────────

def predict_passes() -> list[dict]:
    """Compute upcoming passes within HORIZON_HOURS for all known satellites.

    Returns list of dicts: {satellite, pass_start, pass_end, max_elevation, ...}
    """
    if not TLE_PATH.exists():
        log.warning(f"TLE file missing at {TLE_PATH} — run noaa/tle_refresh.sh first")
        return []

    try:
        from orbit_predictor.sources import get_predictor_from_tle_lines
        from orbit_predictor.locations import Location
    except ImportError:
        log.warning("orbit-predictor not installed (pip install orbit-predictor)")
        return []

    receiver = Location("rx", RX_LAT, RX_LON, RX_ALT_M)
    horizon_end = datetime.now(timezone.utc) + timedelta(hours=HORIZON_HOURS)

    # Read TLEs as 3-line groups (name, line1, line2)
    lines = TLE_PATH.read_text().splitlines()
    tles_by_name: dict[str, tuple[str, str]] = {}
    for i in range(0, len(lines) - 2, 3):
        name = lines[i].strip()
        if name in SATELLITES:
            tles_by_name[name] = (lines[i + 1].strip(), lines[i + 2].strip())

    out: list[dict] = []
    for sat_name, (l1, l2) in tles_by_name.items():
        try:
            predictor = get_predictor_from_tle_lines([sat_name, l1, l2])
            now = datetime.now(timezone.utc)
            while now < horizon_end:
                pass_obj = predictor.get_next_pass(receiver, when_utc=now)
                if pass_obj.aos > horizon_end:
                    break
                if pass_obj.max_elevation_deg < MIN_ELEVATION_DEG:
                    now = pass_obj.los + timedelta(seconds=1)
                    continue
                meta = SATELLITES[sat_name]
                out.append({
                    "satellite": sat_name,
                    "pass_start": pass_obj.aos.replace(tzinfo=timezone.utc),
                    "pass_end":   pass_obj.los.replace(tzinfo=timezone.utc),
                    "max_elevation": float(pass_obj.max_elevation_deg),
                    "freq_mhz": meta["freq_mhz"],
                    "decoder":  meta["decoder"],
                    "duration_s": int((pass_obj.los - pass_obj.aos).total_seconds()),
                })
                now = pass_obj.los + timedelta(seconds=1)
        except Exception as e:
            log.warning(f"prediction for {sat_name} failed: {e}")
    return sorted(out, key=lambda p: p["pass_start"])


# ─── Main ───────────────────────────────────────────────────

def already_scheduled(known: list[dict], pass_start, satellite: str) -> bool:
    for k in known:
        if k["satellite"] == satellite and k["pass_start"] == pass_start:
            return True
    return False


def main() -> None:
    log.info(f"Scheduler starting (rx={RX_LAT},{RX_LON} alt={RX_ALT_M}m, "
             f"horizon={HORIZON_HOURS}h, dry_run={DRY_RUN})")

    # noaa ClickHouse may not be deployed yet (the timer can land before
    # `cd noaa && docker compose up -d`). In that case, log a warning and
    # exit 0 — same posture as ops/spectrum-acars-feedback. The timer will
    # keep firing hourly and start working as soon as CH comes up.
    try:
        existing = ch_query_rows(
            "SELECT satellite, toString(pass_start) AS pass_start "
            "FROM noaa.pass_latest "
            "WHERE pass_start > now() - INTERVAL 1 HOUR"
        )
    except (URLError, HTTPError, OSError) as e:
        log.warning(f"could not reach noaa ClickHouse — pipeline likely not deployed yet ({e})")
        return
    log.info(f"{len(existing)} pass(es) already in noaa.pass_latest")

    upcoming = predict_passes()
    if not upcoming:
        log.info("No upcoming passes (TLE missing or orbit-predictor unavailable)")
        return

    new_rows: list[dict] = []
    queued_cmds: list[str] = []
    for p in upcoming:
        ts_str = p["pass_start"].strftime("%Y-%m-%d %H:%M:%S.000")
        if already_scheduled(existing, ts_str, p["satellite"]):
            continue

        new_rows.append({
            "pass_start":    p["pass_start"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "pass_end":      p["pass_end"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "satellite":     p["satellite"],
            "freq_mhz":      p["freq_mhz"],
            "max_elevation": p["max_elevation"],
            "duration_s":    p["duration_s"],
            "decoder":       p["decoder"],
            "status":        "pending",
            "dongle_id":     "v3-01",  # NOAA freqs in scanner band → V3 default
        })

        # AOS - 30s gives rtl_fm time to start before the satellite rises
        aos_unix = int(p["pass_start"].timestamp())
        cmd = (
            f"systemd-run --user --on-calendar='@{aos_unix - 30}' "
            f"--unit='noaa-pass-{aos_unix}.service' "
            f"/usr/local/bin/noaa-record-pass --satellite '{p['satellite']}' "
            f"--freq-mhz {p['freq_mhz']} --duration {p['duration_s']} "
            f"--decoder '{p['decoder']}'"
        )
        queued_cmds.append(cmd)

    if not new_rows:
        log.info("No new passes to schedule")
        return

    log.info(f"{len(new_rows)} new pass(es) to schedule:")
    for r in new_rows:
        log.info(f"  {r['satellite']:12s}  {r['pass_start']}  "
                 f"max_el={r['max_elevation']:.1f}°  {r['duration_s']}s")

    if DRY_RUN:
        log.info("[dry-run] not inserting and not queueing systemd-run")
        for cmd in queued_cmds:
            log.info(f"  [dry-run] {cmd}")
        return

    ch_insert("passes", new_rows)
    log.info(f"INSERTed {len(new_rows)} pending rows into noaa.passes")

    # systemd-run launching is intentionally NOT implemented yet — the design
    # needs operator review (per-pass user units vs system units, naming,
    # logging, etc.). For now, print so the operator can run by hand.
    log.info("Scheduler stops short of systemd-run; print commands instead:")
    for cmd in queued_cmds:
        print(cmd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("scheduler failed")
        sys.exit(1)
