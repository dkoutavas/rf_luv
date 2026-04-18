#!/usr/bin/env bash
set -euo pipefail

# spectrum-classifier installer — user-systemd timer that runs the
# rule-based classifier 30 s after every feature extractor pass on leap.
#
# Idempotent. Mirrors ops/spectrum-features/install.sh layout.
#
# Run on leap after `git pull`:
#   bash ops/spectrum-classifier/install.sh

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
REPO_DIR="$(cd "$SRC_DIR/../.." && pwd)"
SCRIPT="$REPO_DIR/spectrum/classifier.py"

if [ ! -f "$SCRIPT" ]; then
    err "classifier.py not found at $SCRIPT"
    exit 1
fi

step "Install user systemd units"
mkdir -p "$USER_UNIT_DIR"
install -m 0644 "$SRC_DIR/spectrum-classifier.service" "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/spectrum-classifier.timer"   "$USER_UNIT_DIR/"
systemctl --user daemon-reload
info "$USER_UNIT_DIR"

step "Enable + start timer"
systemctl --user enable --now spectrum-classifier.timer
info "spectrum-classifier.timer active"

step "Smoke test: run classifier once, now"
if systemctl --user start --wait spectrum-classifier.service; then
    info "one-shot run completed"
else
    warn "one-shot run failed — check logs:"
    warn "  journalctl --user -u spectrum-classifier -n 50"
fi

step "Status"
systemctl --user list-timers --no-pager spectrum-classifier.timer || true
echo
systemctl --user --no-pager status spectrum-classifier.service | head -10 || true

echo
echo "Logs:   journalctl --user -u spectrum-classifier -f"
echo "Manual: /usr/bin/python3.11 $SCRIPT"
