#!/usr/bin/env python3.11
# Pinned to 3.11 because leap's default `python3` is 3.6 (Leap 15.6).
"""Signal-quality probe — catches "deaf scanner" failure mode.

The freshness probe is sibling: it asks "are rows landing in spectrum.scans?"
and alerts when the answer is no. That misses a class of failure where the
scanner keeps emitting JSON (rows land, freshness stays green) but the RF
front-end has gone silent — e.g. a coax disconnect, a loose F-connector on
an inline filter, or an antenna that fell off its tripod. The data flows
but every sweep reports near-noise-floor power and zero detected peaks.

Observed concretely on 2026-04-29: V3's full-sweep `max_power` dropped
30 dB at 18:15 UTC (from -14 dBFS to -43 dBFS) without any other
indicator of failure. The freshness probe stayed OK for the full hour
the issue persisted.

This probe runs every 5 min, queries spectrum.sweep_health for `max(max_power)`
over the past SIGNAL_WINDOW_S seconds (full sweeps only — airband has fewer
strong signals and is normally ~10 dB quieter), and notifies on transitions:
    healthy → WARN at max_power < SIGNAL_WARN_FLOOR_DBFS sustained over the window
    healthy → CRITICAL at max_power < SIGNAL_CRIT_FLOOR_DBFS sustained
    any → recovered when max_power rises above SIGNAL_WARN_FLOOR_DBFS

State persisted at /var/lib/spectrum-monitor/signal_quality.json so transitions
are detected across runs.

Tuning notes:
    * Defaults assume a moderately strong RF environment with at least one
      reliable carrier above -35 dBFS in the 88-470 MHz scan band (FM
      broadcast, DVB-T, marine, TETRA repeaters). For RF-quiet locations
      (rural / heavily-attenuated indoor) raise SIGNAL_WARN_FLOOR_DBFS
      and SIGNAL_CRIT_FLOOR_DBFS — otherwise the probe will WARN constantly.
    * SIGNAL_MIN_SWEEPS guards against false alerts in the first few
      minutes after install / restart. A full sweep fires every ~5 min,
      so 5 sweeps means >25 min of data needed before WARN can trigger.
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULTS = {
    "CH_URL": "http://127.0.0.1:8126",
    "CH_USER": "spectrum",
    "CH_PASSWORD": "spectrum_local",
    "CH_DATABASE": "spectrum",
    "CH_TABLE": "sweep_health",
    "CH_DONGLE_COL": "dongle_id",
    "CH_PRESET": "full",            # only consider full sweeps
    "SIGNAL_WINDOW_S": "1800",       # 30 min
    "SIGNAL_WARN_FLOOR_DBFS": "-35", # max_pwr below this → WARN (sustained)
    "SIGNAL_CRIT_FLOOR_DBFS": "-40", # max_pwr below this → CRITICAL (sustained)
    "SIGNAL_MIN_SWEEPS": "5",        # ignore window if fewer sweeps captured
    "STATE_FILE": "/var/lib/spectrum-monitor/signal_quality.json",
    "ACTION_LOG": "/var/log/rtl-recovery.log",
    "NOTIFY_BIN": "/usr/local/bin/rf-notify",
    "EXPECTED_DONGLES": "v3-01,v4-01",
}


def load_env(path: str = "/etc/rtl-scanner/signal-quality-probe.env") -> dict:
    out = dict(DEFAULTS)
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def log_action(cfg: dict, action: str, **fields) -> None:
    entry = {"ts": round(time.time(), 3), "source": "signal-quality-probe",
             "action": action, **fields}
    line = json.dumps(entry, sort_keys=True)
    print(line, file=sys.stderr, flush=True)
    try:
        Path(cfg["ACTION_LOG"]).parent.mkdir(parents=True, exist_ok=True)
        with open(cfg["ACTION_LOG"], "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[signal] action log write failed: {e}", file=sys.stderr)


def notify(cfg: dict, level: str, title: str, message: str = "") -> None:
    import subprocess
    try:
        subprocess.run([cfg["NOTIFY_BIN"], level, title, "-m", message],
                       check=False, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[signal] notify failed: {e}", file=sys.stderr)


def query_signal_levels(cfg: dict) -> dict:
    """Return {dongle_id: {'max_pwr': float, 'sweeps': int}} or {'_error': str}."""
    sql = (
        f"SELECT {cfg['CH_DONGLE_COL']}, "
        f"  max(max_power) AS max_pwr, "
        f"  count() AS sweeps "
        f"FROM {cfg['CH_DATABASE']}.{cfg['CH_TABLE']} "
        f"WHERE timestamp > now() - INTERVAL {int(cfg['SIGNAL_WINDOW_S'])} SECOND "
        f"  AND preset = '{cfg['CH_PRESET']}' "
        f"GROUP BY {cfg['CH_DONGLE_COL']} "
        f"FORMAT JSON"
    )
    qs = urllib.parse.urlencode({
        "user": cfg["CH_USER"],
        "password": cfg["CH_PASSWORD"],
    })
    url = f"{cfg['CH_URL']}/?{qs}"
    req = urllib.request.Request(url, data=sql.encode("utf-8"), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = json.load(r)
    except (OSError, json.JSONDecodeError) as e:
        return {"_error": str(e)}
    out = {}
    for row in payload.get("data", []):
        d = row.get(cfg["CH_DONGLE_COL"])
        m = row.get("max_pwr")
        s = row.get("sweeps")
        if d is None or m is None or s is None:
            continue
        out[str(d)] = {"max_pwr": float(m), "sweeps": int(s)}
    return out


def classify(max_pwr: float, sweeps: int, warn_floor: float,
             crit_floor: float, min_sweeps: int) -> str:
    """Return OK / WARN / CRITICAL based on signal level over the window.

    Returns OK if not enough data to judge — protects against false alerts
    immediately after install/restart, before the window has filled in.
    """
    if sweeps < min_sweeps:
        return "OK"
    if max_pwr < crit_floor:
        return "CRITICAL"
    if max_pwr < warn_floor:
        return "WARN"
    return "OK"


def load_state(cfg: dict) -> dict:
    try:
        with open(cfg["STATE_FILE"]) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(cfg: dict, state: dict) -> None:
    p = Path(cfg["STATE_FILE"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, indent=2))
    tmp.replace(p)


def main():
    cfg = load_env()
    warn_floor = float(cfg["SIGNAL_WARN_FLOOR_DBFS"])
    crit_floor = float(cfg["SIGNAL_CRIT_FLOOR_DBFS"])
    min_sweeps = int(cfg["SIGNAL_MIN_SWEEPS"])
    window_s = int(cfg["SIGNAL_WINDOW_S"])
    expected = [s.strip() for s in cfg["EXPECTED_DONGLES"].split(",") if s.strip()]

    levels = query_signal_levels(cfg)
    state = load_state(cfg)
    prev = state.get("dongles", {})
    now = time.time()

    if "_error" in levels:
        log_action(cfg, "ch_query_failed", error=levels["_error"])
        # Don't alert here — the freshness probe (sibling) handles
        # ClickHouse-unreachable. Avoid double-paging on the same root cause.
        state.setdefault("dongles", {})
        state["last_check_ts"] = now
        state["last_error"] = levels["_error"]
        save_state(cfg, state)
        return

    state["last_error"] = ""

    new_dongles = {}
    for d in expected:
        row = levels.get(d)
        if row is None:
            # No full sweeps in the window. Could be brand-new install or
            # the freshness probe will catch a real outage. Stay OK here.
            new_dongles[d] = {"max_pwr": None, "sweeps": 0, "level": "OK"}
            continue

        max_pwr = row["max_pwr"]
        sweeps = row["sweeps"]
        level = classify(max_pwr, sweeps, warn_floor, crit_floor, min_sweeps)
        prev_level = prev.get(d, {}).get("level", "OK")
        new_dongles[d] = {"max_pwr": round(max_pwr, 1), "sweeps": sweeps, "level": level}

        if level != prev_level:
            log_action(cfg, "signal_quality_transition", dongle=d,
                       from_level=prev_level, to_level=level,
                       max_pwr_dbfs=round(max_pwr, 1), sweeps=sweeps,
                       window_s=window_s)
            if level == "WARN":
                notify(cfg, "WARN", f"rf_luv: {d} signal weak (deaf scanner?)",
                       message=(f"max_power {round(max_pwr,1)} dBFS over last "
                                f"{window_s//60}min ({sweeps} full sweeps), "
                                f"floor is {warn_floor} dBFS. Check antenna/coax/filter."))
            elif level == "CRITICAL":
                notify(cfg, "CRITICAL", f"rf_luv: {d} signal CRITICAL (likely RF disconnect)",
                       message=(f"max_power {round(max_pwr,1)} dBFS over last "
                                f"{window_s//60}min ({sweeps} sweeps), "
                                f"floor is {crit_floor} dBFS. RF chain physically broken."))
            elif level == "OK" and prev_level in ("WARN", "CRITICAL"):
                notify(cfg, "INFO", f"rf_luv: {d} signal recovered",
                       message=f"max_power back to {round(max_pwr,1)} dBFS (was {prev_level})")

    state["dongles"] = new_dongles
    state["last_check_ts"] = now
    save_state(cfg, state)

    summary = " ".join(
        f"{d}:{v['max_pwr']}dBFS({v['level']},n={v['sweeps']})"
        if v["max_pwr"] is not None else f"{d}:no-data({v['level']})"
        for d, v in sorted(new_dongles.items())
    )
    print(f"[signal-quality] tick: {summary}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
