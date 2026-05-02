#!/usr/bin/env bash
set -euo pipefail
#
# rtl-coordinator installer — flock-based dongle access coordinator.
#
# What this installs:
#   /var/lib/rtl-coordinator/      — lock directory (root:root, 0775, world-writable
#                                    so unprivileged decoders can take locks)
#   /usr/local/bin/rtl-coordinator-run     — wrapper script (root, 0755)
#   /usr/local/bin/rtl-coordinator-status  — debug helper (root, 0755)
#   ~/.config/systemd/user/rtl-coordinator@.service — templated unit (optional)
#   /etc/rtl-coordinator/                  — per-instance env files (you create)
#
# Run on leap (one sudo prompt per binary):
#     bash ops/rtl-coordinator/install.sh

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
LOCK_DIR="/var/lib/rtl-coordinator"
ETC_DIR="/etc/rtl-coordinator"
BIN_DIR="/usr/local/bin"

step "Lock directory"
sudo install -d -m 0775 "$LOCK_DIR"
info "$LOCK_DIR"

step "Per-instance env file directory"
sudo install -d -m 0755 "$ETC_DIR"
info "$ETC_DIR (drop <serial>.env files here)"

step "Wrapper + status binaries"
sudo install -m 0755 "$SRC_DIR/rtl-coordinator-run"    "$BIN_DIR/"
sudo install -m 0755 "$SRC_DIR/rtl-coordinator-status" "$BIN_DIR/"
info "$BIN_DIR/rtl-coordinator-run, rtl-coordinator-status"

step "User systemd template (optional, for systemctl-based launches)"
mkdir -p "$USER_UNIT_DIR"
install -m 0644 "$SRC_DIR/rtl-coordinator@.service" "$USER_UNIT_DIR/"
systemctl --user daemon-reload
info "$USER_UNIT_DIR/rtl-coordinator@.service"

step "Smoke test"
out=$(rtl-coordinator-run v3-01 --nonblock -- /bin/true 2>&1) && exit_code=$? || exit_code=$?
if [ "$exit_code" -eq 0 ]; then
    info "Smoke test passed (acquired + released lock for v3-01)"
elif [ "$exit_code" -eq 75 ]; then
    warn "Smoke test: lock for v3-01 is currently HELD — that's OK if a real consumer is using it"
else
    err "Smoke test failed (exit $exit_code): $out"
    exit 1
fi

step "Done"
info "Status command: rtl-coordinator-status"
info "Per-instance env files (e.g. NOAA recordings on V3) go in $ETC_DIR/<serial>.env"
info "Action log: tail -f /var/log/rtl-recovery.log | jq"
