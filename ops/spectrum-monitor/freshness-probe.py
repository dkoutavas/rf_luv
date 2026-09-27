#!/usr/bin/env python3
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
    unplugged dongle → ABSENT, no alert (collection is session-based)
    rtl_tcp held by another client (SDR++) → IN_USE, no alert

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
    "CH_URL": "http://127.0.0.1:8123",
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
    "EXPECTED_DONGLES": "v4-01",
    "DEV_DIR": "/dev",
    "DONGLE_ENV_DIR": "/etc/rtl-scanner",
}

# Our own clients of rtl_tcp. The scanner is the data source this probe
# checks; the watchdog connects for ~2 s to test an idle server.
OWN_CLIENTS = ("scanner.py", "rtl-tcp-watchdog")


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


def rtl_tcp_port(cfg: dict, dongle: str) -> int | None:
    """RTL_TCP_PORT from the dongle's scanner env file, or None."""
    try:
        with open(os.path.join(cfg["DONGLE_ENV_DIR"], f"{dongle}.env")) as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key == "RTL_TCP_PORT":
                    return int(value)
    except (OSError, ValueError):
        pass
    return None


def _read_tcp_table() -> list[list[str]]:
    rows = []
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                next(f)  # header
                rows += [line.split() for line in f]
        except OSError:
            continue
    return rows


def _socket_owners() -> dict[str, str]:
    """Socket inode -> command line of the process holding it."""
    owners = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            for fd in os.listdir(f"/proc/{pid}/fd"):
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
                if link.startswith("socket:["):
                    owners[link[8:-1]] = cmdline
        except OSError:
            continue  # process exited or is not ours to read
    return owners


def foreign_clients(port: int) -> list[str]:
    """Clients of rtl_tcp on `port` that are not our own scanner or watchdog.

    rtl_tcp serves one client at a time. When SDR++ (or anything else) holds
    the port, the scanner cannot collect, so missing data is expected rather
    than a fault. The scanner itself is excluded on purpose: if it is
    connected but no rows arrive, the ingest path is broken and that must
    still alert.

    Each connection shows up twice in /proc/net/tcp: the server side (local
    port = rtl_tcp's port) and the client side (remote port = rtl_tcp's port).
    The client side's inode leads to the process. A server-side peer with no
    client side in this network namespace (a Docker decoder) is foreign too.
    """
    port_hex = f"{port:04X}"
    ESTABLISHED = "01"
    rows = [r for r in _read_tcp_table() if len(r) > 9 and r[3] == ESTABLISHED]
    peers = {r[2] for r in rows if r[1].rsplit(":", 1)[-1] == port_hex}
    if not peers:
        return []
    client_inodes = {r[1]: r[9] for r in rows
                     if r[2].rsplit(":", 1)[-1] == port_hex and r[1] in peers}
    owners = _socket_owners() if client_inodes else {}
    foreign = []
    for peer in peers:
        cmdline = owners.get(client_inodes.get(peer, ""), "")
        if not any(own in cmdline for own in OWN_CLIENTS):
            foreign.append(cmdline.split(" ")[0].rsplit("/", 1)[-1] or f"unknown peer {peer}")
    return foreign


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
        prev_level = prev.get(d, {}).get("level", "OK")

        # Collection is session-based: the dongles are unplugged between
        # sessions. udev's /dev/rtl_sdr_<serial> symlink exists only while the
        # dongle is on the bus (the watchdog uses the same test). An unplugged
        # dongle is not a fault, so it gets no alert.
        dev_link = os.path.join(cfg["DEV_DIR"], f"rtl_sdr_{d}")
        try:
            plugged_for = int(now - os.lstat(dev_link).st_mtime)
        except FileNotFoundError:
            new_dongles[d] = {"stale_sec": fresh.get(d), "level": "ABSENT"}
            if prev_level != "ABSENT":
                log_action(cfg, "dongle_absent", dongle=d)
            continue

        # Listening sessions: SDR++ holds the dongle's rtl_tcp, so the
        # scanner cannot collect. Expected, so no alert.
        port = rtl_tcp_port(cfg, d)
        clients = foreign_clients(port) if port else []
        if clients:
            new_dongles[d] = {"stale_sec": fresh.get(d), "level": "IN_USE"}
            if prev_level != "IN_USE":
                log_action(cfg, "dongle_in_use", dongle=d, clients=clients)
            continue

        stale = fresh.get(d)
        if stale is None:
            # No rows in last 6h. Treat as critical.
            stale = 6 * 3600
        # udev creates the symlink at plug-in, so its mtime is the plug time.
        # Data cannot be expected from before the plug, so staleness is
        # capped at the time since then; a new session starts at OK.
        stale = min(stale, plugged_for)
        # Same cap after a listening session: the last row is from before it,
        # so count from when the other client let go. The scanner then gets
        # the normal warn window to reconnect.
        in_use_ended = now if prev_level == "IN_USE" else prev.get(d, {}).get("in_use_ended")
        if in_use_ended and now - in_use_ended < crit_s:
            stale = min(stale, int(now - in_use_ended))
        else:
            in_use_ended = None
        level = classify(stale, warn_s, crit_s)
        new_dongles[d] = {"stale_sec": stale, "level": level}
        if in_use_ended:
            new_dongles[d]["in_use_ended"] = in_use_ended

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

    # Surface unexpected dongles only if they look ACTIVE (fresh rows in the
    # last warn_s window). The "actively scanning but not in EXPECTED_DONGLES"
    # case is the alarm we want — it means a third dongle was wired in without
    # updating the env file. Conversely, a stale unexpected dongle is almost
    # always a previously-monitored dongle that's been reassigned to another
    # pipeline (e.g. v4-01 became the ACARS dongle on 2026-05-02). Logging it
    # repeatedly as CRITICAL pollutes the tick log without adding signal.
    for d, stale in fresh.items():
        if d in expected:
            continue
        s = int(stale)
        if s >= warn_s:
            continue
        new_dongles[d] = {"stale_sec": s,
                          "level": classify(s, warn_s, crit_s),
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
