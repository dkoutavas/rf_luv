#!/usr/bin/env bash
set -euo pipefail

# spectrum-acars-feedback installer — user-systemd timer that promotes
# ACARS decode confirmations into spectrum.listening_log so the classifier
# picks them up as operator-confirmation overrides at confidence 1.0.
#
# Idempotent. Mirrors ops/spectrum-classifier/install.sh layout.
#
# Prerequisites:
#   - acars/ pipeline deployed and decoding (otherwise the script no-ops cleanly)
#   - spectrum/ migration 022 applied (`bash ops/spectrum-classifier/install.sh`
#     re-runs migrations as a side effect, or run migrate.py directly)
#
# Run on leap after `git pull`:
#   bash ops/spectrum-acars-feedback/install.sh

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
REPO_DIR="$(cd "$SRC_DIR/../.." && pwd)"
SCRIPT="$REPO_DIR/spectrum/acars_feedback.py"

if [ ! -f "$SCRIPT" ]; then
    err "acars_feedback.py not found at $SCRIPT"
    exit 1
fi

step "Install user systemd units"
mkdir -p "$USER_UNIT_DIR"
install -m 0644 "$SRC_DIR/spectrum-acars-feedback.service" "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/spectrum-acars-feedback.timer"   "$USER_UNIT_DIR/"
systemctl --user daemon-reload
info "$USER_UNIT_DIR"

step "Enable + start timer"
systemctl --user enable --now spectrum-acars-feedback.timer
info "spectrum-acars-feedback.timer active"

step "Smoke test: dry-run once, now"
ACARS_FEEDBACK_DRY_RUN=1 systemctl --user start --wait spectrum-acars-feedback.service || \
    warn "Dry-run failed — check 'journalctl --user -u spectrum-acars-feedback'"

step "Done"
info "Hourly schedule: *:15 — see 'systemctl --user list-timers spectrum-acars-feedback.timer'"
info "Inspect output: journalctl --user -u spectrum-acars-feedback -n 50"
