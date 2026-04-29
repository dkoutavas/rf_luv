#!/usr/bin/env python3.11
# Pinned to 3.11 because leap's default `python3` is 3.6 (Leap 15.6).
"""ClickHouse-level freshness probe for the spectrum pipeline.

The user-level rtl_tcp watchdog is TCP-aware: it knows IQ samples flow out of
rtl_tcp on port 1234. It does not know whether the spectrum-scanner Docker
container, the ClickHouse server, or the ingest path are actually wiring those
samples into the spectrum.scans table. A failure anywhere downstream of
rtl_tcp (e.g. Docker died, ClickHouse OOM'd, ingest broke) would leave the
watchdog reporting healthy while the database goes silent for days.

This probe runs every 5 min, asks ClickHouse for max(timestamp) per dongle,
and notifies on state transitions:
    healthy → WARN at >FRESHNESS_WARN_S stale
    healthy → CRITICAL at >FRESHNESS_CRITICAL_S stale
    any → recovered when stale drops below FRESHNESS_WARN_S

State persisted at /var/lib/spectrum-monitor/freshness.json so transitions
are detected across runs.
"""

import json
import os
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
    "CH_TABLE": "scans",
    "CH_DONGLE_COL": "dongle_id",
    "FRESHNESS_WARN_S": "600",      # 10 min
    "FRESHNESS_CRITICAL_S": "1500", # 25 min
    "STATE_FILE": "/var/lib/spectrum-monitor/freshness.json",
    "ACTION_LOG": "/var/log/rtl-recovery.log",
    "NOTIFY_BIN": "/usr/local/bin/rf-notify",
    "EXPECTED_DONGLES": "v3-01,v4-01",
}


def load_env(path: str = "/etc/rtl-scanner/freshness-probe.env") -> dict:
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
    entry = {"ts": round(time.time(), 3), "source": "freshness-probe",
             "action": action, **fields}
    line = json.dumps(entry, sort_keys=True)
    print(line, file=sys.stderr, flush=True)
    try:
        Path(cfg["ACTION_LOG"]).parent.mkdir(parents=True, exist_ok=True)
        with open(cfg["ACTION_LOG"], "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[freshness] action log write failed: {e}", file=sys.stderr)


def notify(cfg: dict, level: str, title: str, message: str = "") -> None:
    import subprocess
    try:
        subprocess.run([cfg["NOTIFY_BIN"], level, title, "-m", message],
                       check=False, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[freshness] notify failed: {e}", file=sys.stderr)


def query_freshness(cfg: dict) -> dict:
    """Return {dongle_id: stale_sec} or {} if query fails."""
    sql = (
        f"SELECT {cfg['CH_DONGLE_COL']}, "
        f"dateDiff('second', max(timestamp), now()) AS stale_sec "
        f"FROM {cfg['CH_DATABASE']}.{cfg['CH_TABLE']} "
        f"WHERE timestamp > now() - INTERVAL 6 HOUR "
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
        s = row.get("stale_sec")
        if d is None or s is None:
            continue
        out[str(d)] = int(s)
    return out


def classify(stale_sec: int, warn_s: int, crit_s: int) -> str:
    if stale_sec >= crit_s:
        return "CRITICAL"
    if stale_sec >= warn_s:
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
    warn_s = int(cfg["FRESHNESS_WARN_S"])
    crit_s = int(cfg["FRESHNESS_CRITICAL_S"])
    expected = [s.strip() for s in cfg["EXPECTED_DONGLES"].split(",") if s.strip()]

    fresh = query_freshness(cfg)
    state = load_state(cfg)
    prev = state.get("dongles", {})
    now = time.time()

    if "_error" in fresh:
        log_action(cfg, "ch_query_failed", error=fresh["_error"])
        # Treat ClickHouse-unreachable as a CRITICAL itself — the alerting
        # path doesn't go through ClickHouse so this still reaches the user.
        notify(cfg, "CRITICAL", "rf_luv: ClickHouse unreachable",
               message=f"freshness probe: {fresh['_error']}")
        state.setdefault("dongles", {})
        state["last_check_ts"] = now
        state["last_error"] = fresh["_error"]
        save_state(cfg, state)
        return

    state["last_error"] = ""

    # Walk expected dongles even if missing from query (no rows in 6h = stale).
    new_dongles = {}
    for d in expected:
        stale = fresh.get(d)
        if stale is None:
            # No rows in last 6h. Treat as critical.
            stale = 6 * 3600
        level = classify(stale, warn_s, crit_s)
        prev_level = prev.get(d, {}).get("level", "OK")
        new_dongles[d] = {"stale_sec": stale, "level": level}

        # State transition?
        if level != prev_level:
            log_action(cfg, "freshness_transition", dongle=d,
                       from_level=prev_level, to_level=level, stale_sec=stale)
            if level == "WARN":
                notify(cfg, "WARN", f"rf_luv: {d} freshness WARN",
                       message=f"{stale}s stale (> {warn_s}s)")
            elif level == "CRITICAL":
                notify(cfg, "CRITICAL", f"rf_luv: {d} freshness CRITICAL",
                       message=f"{stale}s stale (> {crit_s}s)")
            elif level == "OK" and prev_level in ("WARN", "CRITICAL"):
                notify(cfg, "INFO", f"rf_luv: {d} freshness recovered",
                       message=f"now {stale}s stale (was {prev_level})")

    # Also surface any unexpected dongle reported by ClickHouse (e.g., a third
    # one was added without updating EXPECTED_DONGLES).
    for d, stale in fresh.items():
        if d in expected:
            continue
        new_dongles[d] = {"stale_sec": int(stale),
                          "level": classify(int(stale), warn_s, crit_s),
                          "unexpected": True}

    state["dongles"] = new_dongles
    state["last_check_ts"] = now
    save_state(cfg, state)

    summary = " ".join(
        f"{d}:{v['stale_sec']}s({v['level']})"
        for d, v in sorted(new_dongles.items())
    )
    print(f"[freshness] tick: {summary}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
