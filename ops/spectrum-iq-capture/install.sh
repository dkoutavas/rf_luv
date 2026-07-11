#!/usr/bin/env bash
set -euo pipefail

# spectrum-iq-capture installer — user-systemd LONG-RUNNING daemon that polls
# spectrum.forensic_trigger and captures raw IQ (.cs8) from the V4 dongle,
# time-sharing it with the scanner via the flock coordinator.
#
# Unlike ops/spectrum-features (oneshot + timer), this is a Type=simple service
# because it polls continuously (the long-running precedent is rtl-tcp@.service).
#
# Idempotent. Mirrors ops/spectrum-features/install.sh conventions:
#   - user systemd unit under ~/.config/systemd/user/
#   - linger assumed enabled (rtl-tcp installer takes care of that)
#
# Run locally after `git pull`:
#   bash ops/spectrum-iq-capture/install.sh

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
REPO_DIR="$(cd "$SRC_DIR/../.." && pwd)"
SCRIPT="$REPO_DIR/spectrum/iq_capture.py"
IQ_DIR="/var/lib/spectrum/iq_captures"
COORD_DIR="/var/lib/rtl-coordinator"

if [ ! -f "$SCRIPT" ]; then
    err "iq_capture.py not found at $SCRIPT"
    exit 1
fi

step "Create capture directory"
# 0775 owned by the invoking user so the daemon (user systemd) can write; the
# archive/ subdir is immune to the 2 GB oldest-first rotation (non-recursive glob).
sudo install -d -m 0775 -o "$USER" "$IQ_DIR"
sudo install -d -m 0775 -o "$USER" "$IQ_DIR/archive"
info "$IQ_DIR (+ archive/)"

step "Check coordinator lock dir"
if [ -d "$COORD_DIR" ]; then
    info "$COORD_DIR present — real flock coordination with the scanner"
else
    warn "$COORD_DIR ABSENT — dongle_lock degrades to a warn-once no-op (no"
    warn "  real coordination with the scanner). Run first:"
    warn "    bash ops/rtl-coordinator/install.sh"
fi

step "Install user systemd unit"
mkdir -p "$USER_UNIT_DIR"
install -m 0644 "$SRC_DIR/spectrum-iq-capture.service" "$USER_UNIT_DIR/"
systemctl --user daemon-reload
info "$USER_UNIT_DIR"

step "Check linger"
if loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
    info "linger enabled"
else
    warn "linger NOT enabled — the daemon will stop on logout."
    warn "Run: sudo loginctl enable-linger $USER"
fi

step "Enable + start daemon"
systemctl --user enable --now spectrum-iq-capture.service
info "spectrum-iq-capture.service enabled"

step "Status"
systemctl --user --no-pager status spectrum-iq-capture.service | head -12 || true

echo
echo "Logs:      journalctl --user -u spectrum-iq-capture -f"
echo "Manual:    /usr/bin/python3 $SCRIPT --capture 99600000   # smoke test (Kosmos FM)"
echo "Trigger:   /usr/bin/python3 $SCRIPT --trigger 99600000 --source operator:cli"
