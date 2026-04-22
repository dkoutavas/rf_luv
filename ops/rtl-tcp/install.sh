#!/usr/bin/env bash
set -euo pipefail

# rtl_tcp reliability stack installer (dual-dongle ready).
#
# Installs both the legacy singleton units (rtl_tcp.service / rtl-tcp-watchdog)
# AND the new template units (rtl-tcp@.service / rtl-tcp-watchdog@.service +
# .timer) side-by-side, so the cutover runbook can swap between them without
# a rebuild. Also installs the rtl-tcp-by-serial wrapper and the updated
# USB-reset helper that accepts a serial argument.
#
# What this script does:
#   (1) Install systemd user units (both singleton and template)
#   (2) Install the wrapper + watchdog + USB-reset helpers
#   (3) Install the udev rules for stable dongle symlinks
#   (4) Install the sudoers entry (allows per-serial USB resets)
#   (5) Enable linger + journal group (if not already)
#
# What this script does NOT do:
#   - Start/stop the singleton rtl_tcp.service (leave the running pipeline
#     alone — cutover runbook does that step explicitly)
#   - Enable the template instances (explicit during cutover)
#
# Run on leap after `git pull`:
#   bash ops/rtl-tcp/install.sh
#
# Requires: rtl_tcp + rtl_test + rtl_eeprom in PATH, sudo access.

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SRC_DIR/../.." && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
WATCHDOG_BIN="/usr/local/bin/rtl-tcp-watchdog"
WRAPPER_BIN="/usr/local/bin/rtl-tcp-by-serial"
RESET_BIN="/usr/local/sbin/rtl-usb-reset"
SUDOERS_PATH="/etc/sudoers.d/rtl-usb-reset"
UDEV_RULES_SRC="$REPO_DIR/ops/udev/99-rtl-sdr.rules"
UDEV_RULES_DEST="/etc/udev/rules.d/99-rtl-sdr.rules"

for tool in rtl_tcp rtl_test rtl_eeprom; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        err "$tool not in PATH"; exit 1
    fi
done

step "Install user systemd units (singleton + template)"
mkdir -p "$USER_UNIT_DIR"
# Singletons (legacy — preserved for backward compat during cutover)
install -m 0644 "$SRC_DIR/rtl_tcp.service"          "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/rtl-tcp-watchdog.service" "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/rtl-tcp-watchdog.timer"   "$USER_UNIT_DIR/"
# Template units
install -m 0644 "$SRC_DIR/rtl-tcp@.service"              "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/rtl-tcp-watchdog@.service"     "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/rtl-tcp-watchdog@.timer"       "$USER_UNIT_DIR/"
systemctl --user daemon-reload
info "$USER_UNIT_DIR (singletons + templates installed side by side)"

step "Install wrapper + watchdog + USB-reset helper"
sudo install -m 0755 "$SRC_DIR/rtl-tcp-by-serial.sh" "$WRAPPER_BIN"
sudo install -m 0755 "$SRC_DIR/rtl-tcp-watchdog.py"  "$WATCHDOG_BIN"
sudo install -m 0755 "$SRC_DIR/rtl-usb-reset.sh"     "$RESET_BIN"
# Preserve the old filename as a symlink so any external references still work.
# The file itself has the new per-serial semantics; no-arg mode is still
# supported as a fallback for single-dongle deployments.
sudo ln -sf "$WATCHDOG_BIN" /usr/local/bin/rtl-tcp-watchdog.py
info "$WRAPPER_BIN, $WATCHDOG_BIN (+ legacy .py symlink), $RESET_BIN"

step "Install udev rules"
if [ -f "$UDEV_RULES_SRC" ]; then
    sudo install -m 0644 "$UDEV_RULES_SRC" "$UDEV_RULES_DEST"
    sudo udevadm control --reload
    sudo udevadm trigger --subsystem-match=usb
    info "$UDEV_RULES_DEST (reload + trigger done)"
    if [ -e /dev/rtl_sdr_v3 ] || [ -e /dev/rtl_sdr_v4 ]; then
        info "/dev/rtl_sdr_v3 or /dev/rtl_sdr_v4 present — serials appear to match"
    else
        warn "no /dev/rtl_sdr_* symlinks — dongle serials may not yet match (v3-01 / v4-01)"
        warn "run 'rtl_test 2>&1 | grep SN' to confirm, and rtl_eeprom -s if needed"
    fi
else
    warn "$UDEV_RULES_SRC missing — skipping udev install"
fi

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

step "Grant journal read access to $USER"
if id -nG "$USER" | tr ' ' '\n' | grep -qx systemd-journal; then
    info "$USER already in systemd-journal"
else
    sudo usermod -aG systemd-journal "$USER"
    warn "$USER added to systemd-journal — log out and back in for journalctl --user to work"
fi

step "Status summary"
# Do NOT enable/start anything here — cutover is explicit. Just report what's
# currently active so the operator has a baseline.
echo "Currently active units matching rtl_tcp/rtl-tcp:"
systemctl --user list-units --no-pager --all 'rtl_tcp.service' 'rtl-tcp@*.service' \
    'rtl-tcp-watchdog*.service' 'rtl-tcp-watchdog*.timer' 2>/dev/null || true

echo
echo "Next steps:"
echo "  Cutover runbook:  spectrum/docs/dongle_cutover_runbook.md"
echo "  Sibling installer (scanner template): bash ops/rtl-scanner/install.sh"
echo
echo "Manual verify:  rtl_test 2>&1 | head -5"
echo "                ls -la /dev/rtl_sdr_v3 /dev/rtl_sdr_v4 2>/dev/null"
