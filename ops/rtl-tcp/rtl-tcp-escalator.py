#!/usr/bin/env python3.11
# Pinned to 3.11 because leap's default `python3` is 3.6 (Leap 15.6) and the
# code uses PEP 604 `dict | None` annotations.
"""Escalator past the rtl_tcp watchdog circuit breaker.

The user-level watchdog gives up at MAX_CONSECUTIVE_FAILURES (10) and just
logs CIRCUIT_BREAKER_OPEN every 30s. That stops it from hammering the USB
subsystem in a reset storm — but it also means a single sustained failure
becomes silent permanent failure.

This script runs as root every 5 min, reads the watchdog state files, and
escalates anything that has been CB-open for ≥CB_OPEN_DURATION_S to a full
unwedge sequence (the proven manual recipe from ops/rtl-tcp/unwedge-v4.sh):

    1. Per-device USB unbind/rebind for the stuck dongle (rtl-usb-reset)
    2. Full xHCI controller bounce on PCI 0000:00:14.0 (clears stuck tuner i2c)
    3. Restart rtl-tcp@<serial>.service in the user manager (linger required)
    4. Record the attempt; back off UNWEDGE_COOLDOWN_S before retrying

If we've done REBOOT_AFTER_UNWEDGES on a single serial in 24h, OR both serials
have been CB-open for ≥REBOOT_BOTH_CB_S, trigger systemctl reboot.
Rate-limited to one reboot per REBOOT_COOLDOWN_S (default 6h).

All actions append a JSON line to /var/log/rtl-recovery.log. State transitions
(CB opened, CB cleared, unwedge attempt, reboot) emit ntfy alerts via rf-notify.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Defaults (overridable via /etc/rtl-scanner/escalator.env, KEY=VALUE).
DEFAULTS = {
    "TARGET_USER": "dio_nysis",
    "TARGET_UID": "1000",
    "SERIALS": "v3-01,v4-01",
    "WATCHDOG_STATE_DIR": "/run/user/1000",
    "XHCI_PCI": "0000:00:14.0",
    "RTL_USB_RESET": "/usr/local/sbin/rtl-usb-reset",
    "RTL_SERVICE_TEMPLATE": "rtl-tcp@%s.service",
    "CB_FAILURE_THRESHOLD": "10",     # mirrors watchdog MAX_CONSECUTIVE_FAILURES
    "CB_OPEN_DURATION_S": "600",      # 10 min CB-open before escalator acts
    "UNWEDGE_COOLDOWN_S": "3600",     # 1h between unwedges of the same serial
    "REBOOT_AFTER_UNWEDGES": "3",     # reboot if 3 unwedges/24h on one serial
    "REBOOT_BOTH_CB_S": "1800",       # reboot if both serials CB ≥30 min
    "REBOOT_COOLDOWN_S": "21600",     # 6h between reboots
    "STATE_FILE": "/var/lib/rtl-tcp-escalator/state.json",
    "ACTION_LOG": "/var/log/rtl-recovery.log",
    "NOTIFY_BIN": "/usr/local/bin/rf-notify",
}


def load_env(path: str = "/etc/rtl-scanner/escalator.env") -> dict:
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
    """Append a JSON line to the recovery log + stderr."""
    entry = {"ts": round(time.time(), 3), "action": action, **fields}
    line = json.dumps(entry, sort_keys=True)
    print(line, file=sys.stderr, flush=True)
    try:
        Path(cfg["ACTION_LOG"]).parent.mkdir(parents=True, exist_ok=True)
        with open(cfg["ACTION_LOG"], "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[escalator] action log write failed: {e}", file=sys.stderr)


def notify(cfg: dict, level: str, title: str, message: str = "", force: bool = False) -> None:
    """Fire-and-forget ntfy via rf-notify CLI. Never raises."""
    cmd = [cfg["NOTIFY_BIN"], level, title, "-m", message]
    if force:
        cmd.append("--force")
    try:
        subprocess.run(cmd, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[escalator] notify failed: {e}", file=sys.stderr)


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


def read_watchdog_state(cfg: dict, serial: str) -> dict | None:
    """Read user-level watchdog state file. Returns None if missing/corrupt."""
    path = Path(cfg["WATCHDOG_STATE_DIR"]) / f"rtl-tcp-watchdog-{serial}.state"
    try:
        with open(path) as f:
            wd = json.load(f)
        wd["_path"] = str(path)
        try:
            wd["_mtime"] = path.stat().st_mtime
        except OSError:
            wd["_mtime"] = 0.0
        return wd
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def restart_user_unit(cfg: dict, serial: str) -> int:
    """Restart rtl-tcp@<serial>.service in the target user's systemd manager.

    The user has linger enabled, so its systemd manager runs at boot. We talk
    to it via --machine=<user>@.host (systemd's documented mechanism for
    cross-user systemctl from root).
    """
    unit = cfg["RTL_SERVICE_TEMPLATE"] % serial
    cmd = ["systemctl", f"--machine={cfg['TARGET_USER']}@.host",
           "--user", "restart", unit]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[escalator] {' '.join(cmd)} -> {r.returncode}: "
              f"{(r.stderr or '').strip()}", file=sys.stderr)
    return r.returncode


def xhci_bounce(cfg: dict) -> bool:
    """Unbind + rebind the xHCI host controller. Affects every USB device on
    that controller for ~10s. Returns True on success.
    """
    pci = cfg["XHCI_PCI"]
    drv = "/sys/bus/pci/drivers/xhci_hcd"
    try:
        with open(f"{drv}/unbind", "w") as f:
            f.write(pci)
        time.sleep(3)
        with open(f"{drv}/bind", "w") as f:
            f.write(pci)
        time.sleep(5)
        return True
    except OSError as e:
        print(f"[escalator] xhci_bounce failed: {e}", file=sys.stderr)
        return False


def unwedge(cfg: dict, serial: str, dry_run: bool) -> dict:
    """Run the full per-serial unwedge sequence. Returns a result dict."""
    result = {"serial": serial, "steps": []}

    # Step 1: per-device unbind/rebind
    cmd1 = [cfg["RTL_USB_RESET"], serial]
    if dry_run:
        result["steps"].append({"step": "rtl-usb-reset", "dry_run": True})
    else:
        r = subprocess.run(cmd1, capture_output=True, text=True)
        result["steps"].append({
            "step": "rtl-usb-reset",
            "rc": r.returncode,
            "stdout_tail": (r.stdout or "").strip().splitlines()[-3:],
        })

    # Step 2: xHCI bounce — only escalate this far if step 1 alone wasn't enough.
    # Per the 2026-04-23 V4 history, per-device reset works ~60% of the time;
    # the other 40% need the controller bounce to clear the tuner i2c bus.
    # We do both unconditionally because we only get here when the watchdog
    # has already exhausted soft restarts AND a per-device hard reset.
    if dry_run:
        result["steps"].append({"step": "xhci_bounce", "dry_run": True})
    else:
        ok = xhci_bounce(cfg)
        result["steps"].append({"step": "xhci_bounce", "ok": ok})

    # Step 3: restart the user unit so rtl_tcp gets a fresh libusb handle
    if dry_run:
        result["steps"].append({"step": "restart_unit", "dry_run": True})
    else:
        rc = restart_user_unit(cfg, serial)
        result["steps"].append({"step": "restart_unit", "rc": rc})

    return result


def reboot(cfg: dict, reason: str, dry_run: bool) -> None:
    log_action(cfg, "reboot", reason=reason, dry_run=dry_run)
    notify(cfg, "CRITICAL", "rf_luv: REBOOT triggered",
           message=f"reason: {reason}", force=True)
    if not dry_run:
        # Brief pause so notify has a chance to flush before init kills us
        time.sleep(2)
        subprocess.run(["systemctl", "reboot"], check=False)


def evaluate_serial(cfg: dict, serial: str, state: dict, now: float,
                    dry_run: bool) -> dict:
    """Process one serial: detect CB transitions, decide whether to unwedge.

    Returns a summary dict for downstream reboot logic.
    """
    threshold = int(cfg["CB_FAILURE_THRESHOLD"])
    cb_open_duration_s = int(cfg["CB_OPEN_DURATION_S"])
    unwedge_cooldown = int(cfg["UNWEDGE_COOLDOWN_S"])

    state.setdefault("cb_first_seen", {})
    state.setdefault("unwedges", {})
    state["unwedges"].setdefault(serial, [])

    # Prune unwedges to last 24h
    state["unwedges"][serial] = [t for t in state["unwedges"][serial]
                                 if now - t < 86400]

    wd = read_watchdog_state(cfg, serial)
    if wd is None:
        return {"serial": serial, "watchdog": "missing"}

    fails = int(wd.get("consecutive_failures", 0))
    cb_open = fails > threshold

    cb_first = float(state["cb_first_seen"].get(serial, 0.0))
    cb_age = (now - cb_first) if cb_first else 0.0

    summary = {
        "serial": serial,
        "fails": fails,
        "cb_open": cb_open,
        "cb_age_s": round(cb_age, 1),
        "unwedges_24h": len(state["unwedges"][serial]),
    }

    # Detect transitions
    if cb_open and not cb_first:
        state["cb_first_seen"][serial] = now
        log_action(cfg, "cb_open", serial=serial, fails=fails)
        notify(cfg, "WARN", f"rf_luv: {serial} circuit breaker open",
               message=f"{serial} fails={fails}; escalator monitoring")
        summary["transition"] = "cb_open"
        cb_age = 0.0
    elif (not cb_open) and cb_first:
        log_action(cfg, "cb_cleared", serial=serial,
                   open_duration_s=round(cb_age, 1))
        notify(cfg, "INFO", f"rf_luv: {serial} recovered",
               message=f"{serial} CB cleared after {cb_age/60:.1f} min")
        state["cb_first_seen"][serial] = 0.0
        summary["transition"] = "cb_cleared"

    # Should we unwedge? Only if CB has been open long enough AND we haven't
    # tried recently. The cooldown protects against the 2026-04-23 reset-storm
    # pathology — even if a dongle is genuinely dead, we attempt at most once
    # per UNWEDGE_COOLDOWN_S. After REBOOT_AFTER_UNWEDGES attempts in 24h with
    # CB still open, we stop unwedging on this serial — the reboot path takes
    # over after all serials are evaluated.
    reboot_after = int(cfg["REBOOT_AFTER_UNWEDGES"])
    if cb_open and cb_age >= cb_open_duration_s:
        last = max(state["unwedges"][serial]) if state["unwedges"][serial] else 0.0
        cooldown_left = unwedge_cooldown - (now - last)
        if cooldown_left > 0:
            summary["unwedge"] = f"cooldown {cooldown_left:.0f}s left"
        elif len(state["unwedges"][serial]) >= reboot_after:
            # Tried REBOOT_AFTER_UNWEDGES times already in last 24h, none
            # restored samples. Don't burn another cycle; signal reboot.
            summary["unwedge"] = (
                f"skipped: {len(state['unwedges'][serial])} prior unwedges/24h "
                f"didn't restore samples"
            )
            summary["reboot_due"] = (
                f"{serial}={len(state['unwedges'][serial])}_unwedges_didnt_help"
            )
        else:
            log_action(cfg, "unwedge_start", serial=serial, fails=fails,
                       cb_age_s=round(cb_age, 1), dry_run=dry_run)
            notify(cfg, "CRITICAL",
                   f"rf_luv: unwedging {serial}",
                   message=f"CB open {cb_age/60:.1f} min; running rtl-usb-reset + xHCI bounce + restart")
            result = unwedge(cfg, serial, dry_run)
            log_action(cfg, "unwedge_done", **result)
            if not dry_run:
                state["unwedges"][serial].append(now)
            summary["unwedge"] = result

    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="evaluate state and decide actions but do NOT execute")
    ap.add_argument("--serial", action="append",
                    help="restrict to specific serial(s); default: all in env")
    args = ap.parse_args()

    cfg = load_env()
    serials = args.serial or [s.strip() for s in cfg["SERIALS"].split(",") if s.strip()]
    state = load_state(cfg)
    state.setdefault("cb_first_seen", {})
    state.setdefault("unwedges", {})
    state.setdefault("last_reboot_ts", 0.0)
    now = time.time()

    summaries = []
    for serial in serials:
        summaries.append(evaluate_serial(cfg, serial, state, now, args.dry_run))

    # Reboot logic — independent pass, after all serials processed.
    reboot_both_s = int(cfg["REBOOT_BOTH_CB_S"])
    reboot_cooldown = int(cfg["REBOOT_COOLDOWN_S"])
    last_reboot = float(state.get("last_reboot_ts", 0.0))

    # Reboot reasons gathered from per-serial evaluation (already CB-open AND
    # already attempted reboot_after times).
    reasons = []
    for s in summaries:
        if s.get("reboot_due"):
            reasons.append(s["reboot_due"])

    cb_durations = [
        now - float(state["cb_first_seen"].get(s, 0.0))
        for s in serials
        if state["cb_first_seen"].get(s, 0.0)
    ]
    if len(cb_durations) == len(serials) and len(serials) >= 2 \
            and all(d >= reboot_both_s for d in cb_durations):
        reasons.append(f"all_serials_cb_open>={reboot_both_s}s")

    if reasons and (now - last_reboot) >= reboot_cooldown:
        reason_str = ",".join(reasons)
        state["last_reboot_ts"] = now
        # Save BEFORE rebooting so the new boot sees the timestamp
        save_state(cfg, state)
        reboot(cfg, reason_str, args.dry_run)
        return
    elif reasons:
        cooldown_left = reboot_cooldown - (now - last_reboot)
        log_action(cfg, "reboot_skipped", reasons=reasons,
                   cooldown_left_s=round(cooldown_left, 0))

    save_state(cfg, state)

    # Always emit a final tick log so the journal is greppable for "did the
    # escalator run at all" without needing to enable verbose mode.
    summary_line = "; ".join(
        f"{s['serial']}:fails={s.get('fails','?')} cb={s.get('cb_open','?')}"
        f" age={s.get('cb_age_s','?')}s unwedges24h={s.get('unwedges_24h','?')}"
        for s in summaries
    )
    print(f"[escalator] tick: {summary_line}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
