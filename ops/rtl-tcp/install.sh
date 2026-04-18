#!/usr/bin/env bash
set -euo pipefail

# rtl_tcp reliability stack installer.
#   (1) systemd user service for rtl_tcp with Restart=always
#   (2) health-probe watchdog (timer + oneshot) that detects the
#       "TCP-accepts-but-no-samples" failure mode we hit 2026-04-18
#   (3) passwordless sudoers entry for /usr/local/sbin/rtl-usb-reset so the
#       watchdog can unbind/rebind the USB device as a replug equivalent
#
# Run on the spectrum host (e.g. 192.168.2.10):
#   bash ops/rtl-tcp/install.sh
#
# Requires: rtl_tcp in PATH, sudo access for the installing user.

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
WATCHDOG_BIN="/usr/local/bin/rtl-tcp-watchdog.py"
RESET_BIN="/usr/local/sbin/rtl-usb-reset"
SUDOERS_PATH="/etc/sudoers.d/rtl-usb-reset"

if ! command -v rtl_tcp >/dev/null 2>&1; then
    err "rtl_tcp not in PATH"; exit 1
fi

step "Stop any manual rtl_tcp"
pkill -TERM rtl_tcp 2>/dev/null || true
sleep 1

step "Install user systemd units"
mkdir -p "$USER_UNIT_DIR"
install -m 0644 "$SRC_DIR/rtl_tcp.service"          "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/rtl-tcp-watchdog.service" "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/rtl-tcp-watchdog.timer"   "$USER_UNIT_DIR/"
systemctl --user daemon-reload
info "$USER_UNIT_DIR"

step "Install watchdog + USB-reset helper"
sudo install -m 0755 "$SRC_DIR/rtl-tcp-watchdog.py" "$WATCHDOG_BIN"
sudo install -m 0755 "$SRC_DIR/rtl-usb-reset.sh"    "$RESET_BIN"
info "$WATCHDOG_BIN, $RESET_BIN"

step "Install sudoers entry"
TMP=$(mktemp)
sed "s/__USER__/$USER/g" "$SRC_DIR/sudoers-rtl-usb-reset" > "$TMP"
if sudo visudo -cf "$TMP" >/dev/null; then
    sudo install -m 0440 "$TMP" "$SUDOERS_PATH"
    info "$SUDOERS_PATH"
else
    err "sudoers snippet failed visudo -cf; not installed"
    rm -f "$TMP"; exit 1
fi
rm -f "$TMP"

step "Enable linger (services persist without login)"
if loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
    info "linger already enabled"
else
    sudo loginctl enable-linger "$USER"
    info "linger enabled"
fi

step "Enable and start services"
systemctl --user enable --now rtl_tcp.service
systemctl --user enable --now rtl-tcp-watchdog.timer
info "rtl_tcp.service + rtl-tcp-watchdog.timer active"

step "Status"
systemctl --user --no-pager status rtl_tcp.service | head -10 || true
echo
systemctl --user list-timers --no-pager rtl-tcp-watchdog.timer || true

echo
echo "Logs:"
echo "  journalctl --user -u rtl_tcp -f"
echo "  journalctl --user -u rtl-tcp-watchdog -f"
echo
echo "Manual test:  /usr/local/bin/rtl-tcp-watchdog.py ; echo exit=\$?"
