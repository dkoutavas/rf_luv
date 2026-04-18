#!/usr/bin/env bash
set -euo pipefail

# spectrum-features installer — user-systemd timer that runs the feature
# extractor every 5 minutes on the spectrum host (leap).
#
# Idempotent. Mirrors ops/rtl-tcp/install.sh conventions:
#   - user systemd units under ~/.config/systemd/user/
#   - linger assumed enabled (rtl-tcp installer takes care of that)
#
# Run on leap after `git pull`:
#   bash ops/spectrum-features/install.sh

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
REPO_DIR="$(cd "$SRC_DIR/../.." && pwd)"
SCRIPT="$REPO_DIR/spectrum/feature_extractor.py"

if [ ! -f "$SCRIPT" ]; then
    err "feature_extractor.py not found at $SCRIPT"
    exit 1
fi

step "Install user systemd units"
mkdir -p "$USER_UNIT_DIR"
install -m 0644 "$SRC_DIR/spectrum-features.service" "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/spectrum-features.timer"   "$USER_UNIT_DIR/"
systemctl --user daemon-reload
info "$USER_UNIT_DIR"

step "Check linger"
if loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
    info "linger enabled"
else
    warn "linger NOT enabled — features will stop running on logout."
    warn "Run: sudo loginctl enable-linger $USER"
fi

step "Enable + start timer"
systemctl --user enable --now spectrum-features.timer
info "spectrum-features.timer active"

step "Smoke test: run feature_extractor once, now"
if systemctl --user start --wait spectrum-features.service; then
    info "one-shot run completed"
else
    warn "one-shot run failed — check logs:"
    warn "  journalctl --user -u spectrum-features -n 50"
fi

step "Status"
systemctl --user list-timers --no-pager spectrum-features.timer || true
echo
systemctl --user --no-pager status spectrum-features.service | head -10 || true

echo
echo "Logs:   journalctl --user -u spectrum-features -f"
echo "Manual: /usr/bin/python3.11 $SCRIPT"
