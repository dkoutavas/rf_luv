#!/usr/bin/env bash
# Daily heartbeat — confirms the alerting pipe still reaches the owner's phone.
# Runs once a day (09:00 UTC by default); sends an INFO ntfy with a one-line
# status: dongle freshness, recovery actions in last 24h, last reboot age.
#
# Reads state from the same files the freshness probe and escalator write.
# If those files don't exist yet, sends a minimal heartbeat anyway (trip-day-1
# baseline before either probe has fired).

set -euo pipefail

ESCALATOR_STATE=/var/lib/rtl-tcp-escalator/state.json
FRESHNESS_STATE=/var/lib/spectrum-monitor/freshness.json

# uptime in seconds since last reboot
uptime_s=$(awk '{print int($1)}' /proc/uptime)
uptime_human=$(printf '%dd%dh' $((uptime_s/86400)) $(( (uptime_s%86400)/3600 )))

# Dongle freshness from probe state file. Falls back to "?" if missing.
freshness_line="?"
if [ -r "$FRESHNESS_STATE" ]; then
    freshness_line=$(python3 -c '
import json, sys
try:
    s = json.load(open("'"$FRESHNESS_STATE"'"))
    parts = []
    for d, v in sorted(s.get("dongles", {}).items()):
        parts.append(f"{d}={v.get(\"stale_sec\", \"?\")}s")
    print(" ".join(parts) if parts else "no-data")
except Exception as e:
    print(f"err:{e}")
')
fi

# Unwedge counts in last 24h, per serial
unwedges_line="?"
if [ -r "$ESCALATOR_STATE" ]; then
    unwedges_line=$(python3 -c '
import json, time
try:
    s = json.load(open("'"$ESCALATOR_STATE"'"))
    now = time.time()
    parts = []
    for d, ts_list in sorted(s.get("unwedges", {}).items()):
        recent = [t for t in ts_list if now - t < 86400]
        parts.append(f"{d}={len(recent)}")
    last_reboot = s.get("last_reboot_ts", 0.0)
    age_h = ((now - last_reboot) / 3600) if last_reboot else None
    print(" ".join(parts) + (f" reboot_ago={age_h:.1f}h" if age_h else ""))
except Exception as e:
    print(f"err:{e}")
')
fi

msg="freshness: $freshness_line | unwedges_24h: $unwedges_line | uptime: $uptime_human"

exec /usr/local/bin/rf-notify INFO "rf_luv heartbeat" -m "$msg" --force
