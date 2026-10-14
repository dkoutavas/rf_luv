#!/usr/bin/env bash
set -euo pipefail

# spectrum-anomalies installer: two user-systemd timers.
#   spectrum-anomalies  hourly, writes spectrum.anomalies and the signal of the day
#   spectrum-digest     daily at 21:00, the same run plus a notification through
#                       ops/notify/notify.py (a desktop popup unless ntfy is set up)
#
# Idempotent. Mirrors ops/spectrum-classifier/install.sh. Needs migrations 026
# to 028 (`bash infra/up.sh`) and the emitter timer (ops/spectrum-emitters).
#
# Run on the host after `git pull`:
#   bash ops/spectrum-anomalies/install.sh

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
REPO_DIR="$(cd "$SRC_DIR/../.." && pwd)"
SCRIPT="$REPO_DIR/spectrum/anomalies.py"

if [ ! -f "$SCRIPT" ]; then
    err "anomalies.py not found at $SCRIPT"
    exit 1
fi

step "Install user systemd units"
mkdir -p "$USER_UNIT_DIR"
for unit in spectrum-anomalies.service spectrum-anomalies.timer \
            spectrum-digest.service spectrum-digest.timer; do
    install -m 0644 "$SRC_DIR/$unit" "$USER_UNIT_DIR/"
done
systemctl --user daemon-reload
info "$USER_UNIT_DIR"

step "Enable + start timers"
systemctl --user enable --now spectrum-anomalies.timer spectrum-digest.timer
info "spectrum-anomalies.timer and spectrum-digest.timer active"

step "Smoke test: run anomaly detection once, now (no digest)"
if systemctl --user start --wait spectrum-anomalies.service; then
    info "one-shot run completed"
else
    warn "one-shot run failed, check the logs:"
    warn "  journalctl --user -u spectrum-anomalies -n 50"
fi

step "Status"
systemctl --user list-timers --no-pager spectrum-anomalies.timer spectrum-digest.timer || true

echo
echo "Logs:    journalctl --user -u spectrum-anomalies -u spectrum-digest -f"
echo "Preview: /usr/bin/python3 $SCRIPT --digest --dry-run"
