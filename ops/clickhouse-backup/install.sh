#!/usr/bin/env bash
set -euo pipefail
#
# clickhouse-backup installer: daily off-host logical backups of the rf_luv
# ClickHouse databases. Mirrors the ops/noaa-pass-scheduler install style.
#
# What this installs:
#   ~/.config/systemd/user/clickhouse-backup.{service,timer}   daily 04:17 UTC
#   /etc/rtl-scanner/clickhouse-backup.env                      (from .example, if absent)
#   $BACKUP_DIR                                                 (snapshot target)
#
# Prerequisites:
#   - The pipeline ClickHouse stacks running (docker compose up -d per pipeline)
#   - The invoking user in the `docker` group (the timer runs `docker exec`)
#   - `rf-notify` installed (ops/install-trip-hardening.sh) for failure alerts
#     [optional: backup still runs without it]
#
# Run on leap after `git pull`:
#   bash ops/clickhouse-backup/install.sh

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[ok]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
ENV_DST="/etc/rtl-scanner/clickhouse-backup.env"
BACKUP_DIR_DEFAULT="/var/backups/rf-clickhouse"

chmod +x "$SRC_DIR/backup.sh" "$SRC_DIR/restore.sh"

step "Docker group check"
if id -nG | tr ' ' '\n' | grep -qx docker; then
    info "$USER is in the docker group"
else
    warn "$USER is NOT in the docker group. The timer runs 'docker exec' and will"
    warn "fail until you run:  sudo usermod -aG docker $USER  (then re-login)"
fi

step "Config file"
if [ -f "$ENV_DST" ]; then
    info "$ENV_DST already exists; leaving it untouched"
else
    sudo install -d -m 0755 /etc/rtl-scanner
    sudo install -m 0644 "$SRC_DIR/clickhouse-backup.env.example" "$ENV_DST"
    warn "Installed $ENV_DST from the example."
    warn "EDIT IT: set BACKUP_DIR to OFF-HOST storage (external drive / NAS /"
    warn "rclone mount). A backup on the same disk is not a backup."
fi

step "Backup target directory"
# Read BACKUP_DIR from the env file if set, else use the default.
BACKUP_DIR="$(. "$ENV_DST" 2>/dev/null; echo "${BACKUP_DIR:-$BACKUP_DIR_DEFAULT}")"
if [ -d "$BACKUP_DIR" ]; then
    info "$BACKUP_DIR exists"
else
    sudo install -d -m 0755 -o "$USER" "$BACKUP_DIR"
    info "created $BACKUP_DIR (owned by $USER)"
fi
case "$BACKUP_DIR" in
    /var/backups/*|/home/*|/root/*)
        warn "BACKUP_DIR=$BACKUP_DIR looks like LOCAL storage. If this is the same"
        warn "physical disk as the ClickHouse volumes, it will not survive a disk"
        warn "failure. Point it at an external/remote mount." ;;
esac

step "Install user systemd units"
mkdir -p "$USER_UNIT_DIR"
install -m 0644 "$SRC_DIR/clickhouse-backup.service" "$USER_UNIT_DIR/"
install -m 0644 "$SRC_DIR/clickhouse-backup.timer"   "$USER_UNIT_DIR/"
systemctl --user daemon-reload
info "$USER_UNIT_DIR"

step "Enable + start timer"
systemctl --user enable --now clickhouse-backup.timer
info "clickhouse-backup.timer active (daily 04:17 UTC)"

step "Run one backup now (one-shot smoke test)"
if systemctl --user start --wait clickhouse-backup.service; then
    info "first backup OK"
    for d in "$BACKUP_DIR"/*/latest; do
        [ -e "$d" ] || continue
        db="$(basename "$(dirname "$d")")"
        info "  $db -> $(readlink -f "$d")"
    done
else
    err "first backup failed, check: journalctl --user -u clickhouse-backup -n 50"
fi

step "Done"
info "Schedule:   systemctl --user list-timers clickhouse-backup.timer"
info "Restore:    bash ops/clickhouse-backup/restore.sh --db spectrum --latest"
info "Inspect:    journalctl --user -u clickhouse-backup -n 50"
