#!/usr/bin/env bash
set -euo pipefail
#
# install-host.sh — one-shot host onboarding for an rf_luv station with one or
# two RTL-SDR dongles on native Linux.
#
# It orchestrates the existing per-component installers (which stay the source
# of truth) and fills in the glue that had to be typed by hand on the first
# two-dongle bring-up (2026-09-19/20): the DVB blacklist, the real per-dongle
# env files with the live device index, enabling the units, and an
# uncommented backup target. Every step is idempotent; re-running is safe.
#
# Usage:
#   bash ops/install-host.sh --scanner v4-01 [--ghost v3-01] [--gain 12]
#                            [--backup-dir /data/rf-clickhouse-backups]
#                            [--dry-run] [--verify-only]
#
#   --scanner SERIAL   the dongle that runs the spectrum scanner (rtl_tcp :1234)
#   --ghost   SERIAL   optional second dongle for the ghost pipeline (rtl_tcp :1235)
#   --gain    N        SCAN_GAIN for new env files (default 12; 20 clips in Athens)
#   --backup-dir DIR   enable daily ClickHouse backups to DIR (put it on another disk)
#   --dry-run          print the mutating commands instead of running them
#   --verify-only      skip install, just run the PASS/FAIL checks
#
# What it does NOT do (physical or one-time, see RESTORE.md):
#   - write EEPROM serials (rtl_eeprom -s) — needs one dongle on the bus at a time
#   - bring up the Docker data layer (bash infra/up.sh) — run that first
#   - mount antennas or screw the FM bandstop onto the scanner dongle
#
# Needs sudo for /etc and /usr/local; run it in a terminal, not headless.

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[ok]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }
die()  { err "$*"; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_DIR=/etc/rtl-scanner
SCANNER_PORT=1234
GHOST_PORT=1235

SCANNER=""; GHOST=""; GAIN=12; BACKUP_DIR=""; DRY_RUN=0; VERIFY_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --scanner)    SCANNER="$2"; shift 2 ;;
        --ghost)      GHOST="$2"; shift 2 ;;
        --gain)       GAIN="$2"; shift 2 ;;
        --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
        --dry-run)    DRY_RUN=1; shift ;;
        --verify-only) VERIFY_ONLY=1; shift ;;
        -h|--help)    awk 'NR>=3 && !/^#/ {exit} NR>=3 {sub(/^# ?/,""); print}' "$0"; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done
[ -n "$SCANNER" ] || die "--scanner SERIAL is required (e.g. --scanner v4-01)"

# run: echo-and-execute, or just echo under --dry-run. Only mutating commands
# go through it; read-only probes always run so the plan reflects the host.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then echo "  [dry-run] $*"; else "$@"; fi
}

# ── 0. distro + tools ────────────────────────────────────────────────────────
# Only the package manager hint is distro-specific; everything else is plain
# systemd + udev. The udev group is chosen inside ops/rtl-tcp/install.sh.
DISTRO_ID="$(. /etc/os-release 2>/dev/null && echo "${ID:-unknown}")"
case "$DISTRO_ID" in
    opensuse*|sles) PKG_HINT="sudo zypper install rtl-sdr docker docker-compose python3-numpy" ;;
    debian|ubuntu)  PKG_HINT="sudo apt install rtl-sdr docker.io docker-compose-v2 python3-numpy" ;;
    fedora)         PKG_HINT="sudo dnf install rtl-sdr docker docker-compose python3-numpy" ;;
    arch)           PKG_HINT="sudo pacman -S rtl-sdr docker docker-compose python-numpy" ;;
    *)              PKG_HINT="install: rtl-sdr (rtl_tcp/rtl_eeprom), docker + compose, python3 numpy" ;;
esac

preflight() {
    step "Preflight ($DISTRO_ID)"
    local missing=0
    for t in rtl_tcp rtl_eeprom rtl_test docker python3; do
        if command -v "$t" >/dev/null; then info "$t: $(command -v "$t")"; else err "$t missing"; missing=1; fi
    done
    if python3 -c 'import numpy' 2>/dev/null; then info "python3 numpy present"; else err "python3 numpy missing"; missing=1; fi
    [ "$missing" -eq 0 ] || die "install the missing tools first:  $PKG_HINT"
    if id -nG | tr ' ' '\n' | grep -qx docker; then info "$USER in docker group"; else warn "$USER not in docker group (backups + infra need it): sudo usermod -aG docker $USER, then re-login"; fi
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx clickhouse; then info "ClickHouse container up"; else warn "ClickHouse not running — run 'bash infra/up.sh' first (backups + scanner writes need it)"; fi
}

# ── 1. dongles present ───────────────────────────────────────────────────────
# Identity is the EEPROM serial: the udev rule makes /dev/rtl_sdr_<serial> and
# rtl_tcp starts with -d <serial>. No USB index is recorded anywhere.
check_dongles() {
    step "Dongles on the bus"
    if [ -e /dev/rtl_sdr_00000001 ]; then
        die "a dongle still has the factory serial 00000001. Write one with ONLY that dongle plugged in:  rtl_eeprom -d 0 -s $SCANNER   then physically replug (see RESTORE.md step 3)"
    fi
    local s
    for s in $SCANNER $GHOST; do
        if [ -e "/dev/rtl_sdr_$s" ]; then
            info "serial '$s' present (/dev/rtl_sdr_$s)"
        else
            warn "serial '$s' not on the bus now; udev starts rtl-tcp@$s on plug. If it IS plugged in: DVB driver? (sudo modprobe -r dvb_usb_rtl28xxu), udev rule? (ops/rtl-tcp/install.sh)"
        fi
    done
}

# ── 2. DVB blacklist ─────────────────────────────────────────────────────────
dvb_blacklist() {
    step "DVB kernel driver blacklist"
    if [ -f /etc/modprobe.d/blacklist-rtlsdr.conf ]; then
        info "/etc/modprobe.d/blacklist-rtlsdr.conf present"
    else
        run bash -c "printf 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\n' | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf >/dev/null"
        info "wrote /etc/modprobe.d/blacklist-rtlsdr.conf"
    fi
    run bash -c "sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true"
}

# ── 3. component installers (unchanged, still the source of truth) ───────────
component_installers() {
    step "ops/rtl-tcp/install.sh (units, wrapper, watchdog, udev, sudoers, linger)"
    run bash "$REPO/ops/rtl-tcp/install.sh"
    step "ops/rtl-scanner/install.sh (scanner template unit)"
    run bash "$REPO/ops/rtl-scanner/install.sh"
    step "rf-mode (dongle mode switcher)"
    run sudo install -m 0755 "$REPO/ops/rf-mode" /usr/local/bin/rf-mode
}

# ── 4. per-dongle env files ──────────────────────────────────────────────────
# set_kv FILE KEY VALUE — replace an existing KEY= line or append one.
set_kv() {
    local f="$1" k="$2" v="$3"
    if grep -q "^$k=" "$f"; then sed -i "s|^$k=.*|$k=$v|" "$f"; else printf '%s=%s\n' "$k" "$v" >> "$f"; fi
}

write_env() {   # write_env SERIAL PORT ROLE
    local serial="$1" port="$2" role="$3"
    local dst="$ENV_DIR/$serial.env"
    if [ -f "$dst" ]; then info "$dst exists — kept"; return; fi
    local tmp; tmp="$(mktemp)"
    {
        local src="$REPO/ops/rtl-scanner/env.$serial.example"
        [ -f "$src" ] || src="$REPO/ops/rtl-scanner/env.v4-01.example"   # generic base
        cp "$src" "$tmp"
        set_kv "$tmp" SCAN_DONGLE_ID "$serial"
        set_kv "$tmp" RTL_TCP_HOST 127.0.0.1
        set_kv "$tmp" RTL_TCP_PORT "$port"
        set_kv "$tmp" SCAN_GAIN "$GAIN"
        info "$dst created ($role, port $port, gain $GAIN)"
    }
    run sudo install -m 0644 "$tmp" "$dst"
    rm -f "$tmp"
}

env_files() {
    step "Per-dongle env files in $ENV_DIR"
    run sudo install -d -m 0755 "$ENV_DIR"
    write_env "$SCANNER" "$SCANNER_PORT" scanner
    [ -n "$GHOST" ] && write_env "$GHOST" "$GHOST_PORT" ghost
    # Escalator watches every serial we manage.
    local tmp; tmp="$(mktemp)"
    if [ -f "$ENV_DIR/escalator.env" ]; then cp "$ENV_DIR/escalator.env" "$tmp"; else cp "$REPO/ops/rtl-tcp/escalator.env.example" "$tmp"; fi
    set_kv "$tmp" SERIALS "$(echo "$SCANNER${GHOST:+,$GHOST}")"
    run sudo install -m 0644 "$tmp" "$ENV_DIR/escalator.env"; rm -f "$tmp"
    info "$ENV_DIR/escalator.env SERIALS=$SCANNER${GHOST:+,$GHOST}"
}

# ── 5. per-instance device drop-ins ──────────────────────────────────────────
# The rtl-tcp@ template cannot name its own udev device unit: %i is "v4-01"
# but the device unit escapes the dash (dev-rtl_sdr_v4\x2d01.device). So one
# drop-in per serial. BindsTo+After stops rtl_tcp on unplug and keeps it from
# restart-looping while the dongle is absent; udev's SYSTEMD_USER_WANTS
# (ops/udev/99-rtl-sdr.rules) starts it again on plug. rtl-scanner@ has
# Requires=rtl-tcp@, so it follows the scanner dongle down; the Wants= drop-in
# brings it back up with the dongle.
device_dropins() {
    step "Per-instance device drop-ins"
    local s dev d
    for s in $SCANNER $GHOST; do
        dev="$(systemd-escape -p --suffix=device "/dev/rtl_sdr_$s")"
        d="$HOME/.config/systemd/user/rtl-tcp@$s.service.d"
        run mkdir -p "$d"
        run bash -c "printf '[Unit]\nBindsTo=%s\nAfter=%s\n' '$dev' '$dev' > '$d/10-device.conf'"
        info "$d/10-device.conf (BindsTo the udev device unit)"
    done
    d="$HOME/.config/systemd/user/rtl-tcp@$SCANNER.service.d"
    run bash -c "printf '[Unit]\nWants=rtl-scanner@%s.service\n' '$SCANNER' > '$d/20-scanner.conf'"
    info "$d/20-scanner.conf Wants=rtl-scanner@$SCANNER.service"
    run systemctl --user daemon-reload
}

# ── 6. enable + start ────────────────────────────────────────────────────────
enable_units() {
    step "Enable + start units"
    # An absent dongle makes --now fail (BindsTo= an inactive device unit);
    # the unit stays enabled and udev starts it on plug.
    for s in $SCANNER $GHOST; do
        run systemctl --user enable "rtl-tcp@$s" "rtl-tcp-watchdog@$s.timer"
        run systemctl --user start "rtl-tcp@$s" "rtl-tcp-watchdog@$s.timer" || warn "rtl-tcp@$s not started (dongle absent?)"
        info "rtl-tcp@$s + watchdog timer"
    done
    run systemctl --user enable "rtl-scanner@$SCANNER"
    run systemctl --user start "rtl-scanner@$SCANNER" || warn "rtl-scanner@$SCANNER not started (dongle absent?)"
    info "rtl-scanner@$SCANNER"
    run systemctl --user enable --now rtl-reset-failed.timer
    info "rtl-reset-failed.timer (StartLimitBurst safety net)"
}

# ── 7. backups ───────────────────────────────────────────────────────────────
backups() {
    [ -n "$BACKUP_DIR" ] || { step "Backups"; warn "no --backup-dir given; skipping (do this before collecting data you care about)"; return; }
    step "ClickHouse backups → $BACKUP_DIR"
    local dst="$ENV_DIR/clickhouse-backup.env" tmp; tmp="$(mktemp)"
    if [ -f "$dst" ]; then cp "$dst" "$tmp"; else cp "$REPO/ops/clickhouse-backup/clickhouse-backup.env.example" "$tmp"; fi
    # The example ships BACKUP_DIR commented out; the installer sources this
    # file, so it must be a live (uncommented) assignment.
    set_kv "$tmp" BACKUP_DIR "$BACKUP_DIR"
    run sudo install -d -m 0755 "$ENV_DIR"
    run sudo install -m 0644 "$tmp" "$dst"; rm -f "$tmp"
    run mkdir -p "$BACKUP_DIR"
    run bash "$REPO/ops/clickhouse-backup/install.sh"
}

# ── 8. verify ────────────────────────────────────────────────────────────────
FAILS=0
check() { if eval "$2" >/dev/null 2>&1; then info "PASS $1"; else err "FAIL $1"; FAILS=$((FAILS+1)); fi; }
verify() {
    step "Verify"
    for s in $SCANNER $GHOST; do
        local port; [ "$s" = "$SCANNER" ] && port=$SCANNER_PORT || port=$GHOST_PORT
        check "BindsTo drop-in for $s"        "grep -q BindsTo $HOME/.config/systemd/user/rtl-tcp@$s.service.d/10-device.conf"
        if [ ! -e "/dev/rtl_sdr_$s" ]; then
            # Plug and play: an absent dongle must leave its unit stopped, not looping.
            check "$s absent: rtl-tcp@$s inactive" "! systemctl --user is-active --quiet rtl-tcp@$s"
            continue
        fi
        check "rtl-tcp@$s active"            "systemctl --user is-active --quiet rtl-tcp@$s"
        check "rtl_tcp listening on :$port"  "ss -tln | grep -q ':$port '"
        check "watchdog timer @$s active"     "systemctl --user is-active --quiet rtl-tcp-watchdog@$s.timer"
        check "/dev/rtl_sdr_$s symlink"       "test -e /dev/rtl_sdr_$s"
        check "device unit for $s active"     "systemctl --user is-active --quiet \"\$(systemd-escape -p --suffix=device /dev/rtl_sdr_$s)\""
    done
    if [ -e "/dev/rtl_sdr_$SCANNER" ]; then
        check "rtl-scanner@$SCANNER active" "systemctl --user is-active --quiet rtl-scanner@$SCANNER"
    else
        check "$SCANNER absent: rtl-scanner@$SCANNER inactive" "! systemctl --user is-active --quiet rtl-scanner@$SCANNER"
    fi
    check "DVB driver not loaded"       "! lsmod | grep -q dvb_usb_rtl28xxu"
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx clickhouse; then
        check "spectrum.scans rows from $SCANNER in last 10 min" \
          "[ \"\$(docker exec clickhouse clickhouse-client --user spectrum --password spectrum_local --query \"SELECT count() FROM spectrum.scans WHERE dongle_id='$SCANNER' AND timestamp > now() - INTERVAL 10 MINUTE\")\" -gt 0 ]"
    else
        warn "ClickHouse not running; skipping data-flow check"
    fi
    local bdir="$BACKUP_DIR"
    [ -z "$bdir" ] && [ -f "$ENV_DIR/clickhouse-backup.env" ] && bdir="$(grep -E '^BACKUP_DIR=' "$ENV_DIR/clickhouse-backup.env" | tail -1 | cut -d= -f2)"
    if [ -n "$bdir" ]; then
        check "backup timer active"            "systemctl --user is-active --quiet clickhouse-backup.timer"
        check "backup snapshot in $bdir"       "test -e $bdir/spectrum/latest"
    fi
    echo
    if [ "$FAILS" -eq 0 ]; then info "all checks passed"; else err "$FAILS check(s) failed — see above; logs: journalctl --user -u rtl-tcp@$SCANNER -n 30"; exit 1; fi
}

# ── main ─────────────────────────────────────────────────────────────────────
if [ "$VERIFY_ONLY" -eq 1 ]; then verify; exit 0; fi
preflight
dvb_blacklist
component_installers
check_dongles
env_files
device_dropins
enable_units
backups
if [ "$DRY_RUN" -eq 1 ]; then
    step "Dry run complete"; info "nothing was changed; re-run without --dry-run to apply"; exit 0
fi
sleep 5
verify
