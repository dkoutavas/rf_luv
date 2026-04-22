#!/usr/bin/env bash
set -euo pipefail

# rtl-scanner installer — user-systemd template for the native-Python
# spectrum scanner (scanner.py | scan_ingest.py pipeline). One systemd
# instance per dongle; instance name = dongle serial.
#
# Idempotent. Mirrors ops/rtl-tcp/install.sh conventions:
#   - user systemd units under ~/.config/systemd/user/
#   - linger assumed enabled (rtl-tcp installer takes care of that)
#   - per-instance env files land under /etc/rtl-scanner/<instance>.env
#
# What this script does:
#   (1) Install rtl-scanner@.service template
#   (2) Install example env files under /etc/rtl-scanner/ (as *.env.example —
#       NOT as real env files; real ones must be placed by the operator)
#   (3) Reload systemd user daemon
#
# What this script does NOT do:
#   - Start any instance (the cutover runbook does that explicitly)
#   - Overwrite an existing real env file
#   - Remove the old docker-compose spectrum-scanner service (that's a
#     post-cutover cleanup step in the followups)
#
# Run on leap after `git pull`:
#   bash ops/rtl-scanner/install.sh

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SRC_DIR/../.." && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
ENV_DIR="/etc/rtl-scanner"
SCANNER_PY="$REPO_DIR/spectrum/scanner.py"
INGEST_PY="$REPO_DIR/spectrum/scan_ingest.py"

if [ ! -f "$SCANNER_PY" ] || [ ! -f "$INGEST_PY" ]; then
    err "scanner.py / scan_ingest.py missing under $REPO_DIR/spectrum/"
    exit 1
fi

if ! command -v python3.11 >/dev/null 2>&1; then
    err "python3.11 not in PATH (matches spectrum-features / classifier path)"
    exit 1
fi

step "Install template unit"
mkdir -p "$USER_UNIT_DIR"
install -m 0644 "$SRC_DIR/rtl-scanner@.service" "$USER_UNIT_DIR/"
systemctl --user daemon-reload
info "$USER_UNIT_DIR/rtl-scanner@.service"

step "Install env.*.example references"
sudo mkdir -p "$ENV_DIR"
sudo install -m 0644 "$SRC_DIR/env.v3-01.example" "$ENV_DIR/v3-01.env.example"
sudo install -m 0644 "$SRC_DIR/env.v4-01.example" "$ENV_DIR/v4-01.env.example"
info "$ENV_DIR/v3-01.env.example, $ENV_DIR/v4-01.env.example"

step "Check real env files"
for serial in v3-01 v4-01; do
    real="$ENV_DIR/$serial.env"
    if [ -f "$real" ]; then
        info "$real exists"
    else
        warn "$real missing — copy from $ENV_DIR/$serial.env.example and edit"
    fi
done

step "Check linger"
if loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
    info "linger enabled"
else
    warn "linger NOT enabled — scanner will stop on logout."
    warn "Run: sudo loginctl enable-linger $USER"
fi

echo
echo "Next steps:"
echo "  1. Copy the example and populate the real env file, then:"
echo "       sudo install -m 0644 $ENV_DIR/v3-01.env.example $ENV_DIR/v3-01.env"
echo "       sudo \$EDITOR $ENV_DIR/v3-01.env"
echo "  2. Install the sibling rtl-tcp@ units first:  bash ops/rtl-tcp/install.sh"
echo "  3. Enable + start (cutover runbook):"
echo "       systemctl --user enable --now rtl-tcp@v3-01 rtl-scanner@v3-01"
echo "  4. Verify one sweep reaches ClickHouse (see preflight checklist)."
echo
echo "Logs:   journalctl --user -u rtl-scanner@v3-01 -f"
