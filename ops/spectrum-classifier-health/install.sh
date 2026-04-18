#!/usr/bin/env bash
set -euo pipefail

# spectrum-classifier-health installer — user-systemd timer that runs the
# classifier health monitor 45 s after every classifier pass on leap.
#
# Idempotent. Mirrors ops/spectrum-classifier/install.sh layout.
#
# Run on leap after `git pull`:
#   bash ops/spectrum-classifier-health/install.sh

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
REPO_DIR="$(cd "$SRC_DIR/../.." && pwd)"
SCRIPT="$REPO_DIR/spectrum/classifier_health.py"

if [ ! -f "$SCRIPT" ]; then
    err "classifier_health.py not found at $SCRIPT"
    exit 1
fi

step "Install user systemd units"
mkdir -p "$USER_UNIT_DIR"
install -m 0644 "$SRC_DIR/spectrum-classifier-health.service" "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/spectrum-classifier-health.timer"   "$USER_UNIT_DIR/"
systemctl --user daemon-reload
info "$USER_UNIT_DIR"

step "Enable + start timer"
systemctl --user enable --now spectrum-classifier-health.timer
info "spectrum-classifier-health.timer active"

step "Smoke test: run health monitor once, now"
if systemctl --user start --wait spectrum-classifier-health.service; then
    info "one-shot run completed"
else
    warn "one-shot run failed — check logs:"
    warn "  journalctl --user -u spectrum-classifier-health -n 50"
fi

step "Status"
systemctl --user list-timers --no-pager spectrum-classifier-health.timer || true
echo
systemctl --user --no-pager status spectrum-classifier-health.service | head -10 || true

echo
echo "Logs:   journalctl --user -u spectrum-classifier-health -f"
echo "Manual: /usr/bin/python3.11 $SCRIPT"
