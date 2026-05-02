#!/usr/bin/env bash
set -euo pipefail
#
# noaa-pass-scheduler installer — user-systemd timers for NOAA/Meteor
# pass scheduling + TLE refresh.
#
# What this installs:
#   ~/.config/systemd/user/noaa-pass-scheduler.{service,timer}   hourly
#   ~/.config/systemd/user/noaa-tle-refresh.{service,timer}      weekly
#   /var/lib/noaa/                                               state dir
#
# Prerequisites:
#   - noaa/ pipeline ClickHouse stack running (docker compose up -d in noaa/)
#   - python3.11 + orbit-predictor pip package installed:
#       pip install --user orbit-predictor
#
# Run on leap after `git pull`:
#   bash ops/noaa-pass-scheduler/install.sh
#
# Note: keeps NOAA_DRY_RUN=1 in the .service file, so the scheduler will
# only PREDICT passes and INSERT pending rows; it will NOT yet fire
# `systemd-run` to launch recorders. Flip to NOAA_DRY_RUN=0 in
# /etc/rtl-scanner/noaa-scheduler.env when ready to actually record
# (requires recorder.py orchestration to be implemented; see noaa/README.md).

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
REPO_DIR="$(cd "$SRC_DIR/../.." && pwd)"
SCHEDULER_PY="$REPO_DIR/noaa/scheduler.py"
TLE_REFRESH_SH="$REPO_DIR/noaa/tle_refresh.sh"

if [ ! -f "$SCHEDULER_PY" ]; then
    err "scheduler.py not found at $SCHEDULER_PY"
    exit 1
fi
if [ ! -f "$TLE_REFRESH_SH" ]; then
    err "tle_refresh.sh not found at $TLE_REFRESH_SH"
    exit 1
fi
chmod +x "$TLE_REFRESH_SH"

step "State directory"
# Don't hardcode -g "$USER" — openSUSE Leap uses a shared "users" group
# (gid 100), not per-user groups like Debian. Letting install determine
# the primary group via -o $USER alone keeps it portable.
sudo install -d -m 0755 -o "$USER" /var/lib/noaa
info "/var/lib/noaa (TLE cache + state, owned by $USER)"

step "Install user systemd units"
mkdir -p "$USER_UNIT_DIR"
install -m 0644 "$SRC_DIR/noaa-pass-scheduler.service" "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/noaa-pass-scheduler.timer"   "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/noaa-tle-refresh.service"    "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/noaa-tle-refresh.timer"      "$USER_UNIT_DIR/"
systemctl --user daemon-reload
info "$USER_UNIT_DIR"

step "Check orbit-predictor installed"
if python3.11 -c "import orbit_predictor" 2>/dev/null; then
    info "orbit-predictor present"
else
    warn "orbit-predictor not installed; scheduler.py will warn and exit cleanly until you run:"
    warn "  pip3 install --user orbit-predictor"
fi

step "Enable + start timers"
systemctl --user enable --now noaa-tle-refresh.timer
systemctl --user enable --now noaa-pass-scheduler.timer
info "noaa-tle-refresh.timer + noaa-pass-scheduler.timer active"

step "Trigger TLE refresh now (one-shot)"
if systemctl --user start --wait noaa-tle-refresh.service; then
    info "TLE refresh OK"
    if [ -r /var/lib/noaa/tles.txt ]; then
        n=$(grep -c "^NOAA\|^METEOR" /var/lib/noaa/tles.txt || true)
        info "  → $n TLE entries in /var/lib/noaa/tles.txt"
    fi
else
    warn "TLE refresh failed — check journalctl --user -u noaa-tle-refresh"
fi

step "Done"
info "Hourly schedule:  *:05 — see 'systemctl --user list-timers noaa-pass-scheduler.timer'"
info "Weekly TLE pull:  Mon 03:00 UTC + on boot"
info ""
info "While NOAA_DRY_RUN=1, the scheduler will INSERT pending pass rows but"
info "NOT fire any systemd-run recordings. Flip to 0 only after recorder.py"
info "rtl-tcp orchestration is implemented (see noaa/README.md)."
info ""
info "Inspect output:   journalctl --user -u noaa-pass-scheduler -n 50"
