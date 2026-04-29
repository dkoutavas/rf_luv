#!/usr/bin/env bash
# Trip-hardening installer: layers escalator + freshness probe + ntfy alerting
# on top of the existing rtl_tcp watchdog stack.
#
# What this installs (all root-level):
#   /usr/local/bin/rtl-tcp-escalator         escalator past the watchdog CB
#   /usr/local/bin/rf-freshness-probe        ClickHouse-level liveness check
#   /usr/local/bin/rf-notify                 ntfy.sh sender (stdlib urllib)
#   /usr/local/bin/rf-heartbeat              daily heartbeat
#   /etc/systemd/system/rtl-tcp-escalator.{service,timer}    every 5 min
#   /etc/systemd/system/rf-freshness-probe.{service,timer}   every 5 min
#   /etc/systemd/system/rf-heartbeat.{service,timer}         daily at 09:00 UTC
#   /etc/rtl-scanner/notify.env              ntfy topic config (placeholder)
#   /etc/rtl-scanner/escalator.env           escalator overrides (optional)
#   /etc/rtl-scanner/freshness-probe.env     freshness overrides (optional)
#   /etc/logrotate.d/rtl-recovery            logrotate for /var/log/rtl-recovery.log
#   /var/lib/rtl-tcp-escalator/              state dir
#   /var/lib/spectrum-monitor/               state dir
#   /var/log/rtl-recovery.log                action log (touch + chmod)
#
# What it does NOT do:
#   - Configure NTFY_TOPIC. Operator does that interactively after install.
#   - Start any timers if --no-enable is passed.
#
# Idempotent: re-runnable. Sudo prompt once.
#
# Usage:
#   bash ops/install-trip-hardening.sh           # full install + enable
#   bash ops/install-trip-hardening.sh --dry-run # show what would happen
#   bash ops/install-trip-hardening.sh --no-enable

set -euo pipefail

DRY=0
ENABLE=1
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        --no-enable) ENABLE=0 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown arg: $arg"; exit 2 ;;
    esac
done

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*"; }
step() { echo -e "\n${GREEN}===${NC} $* ${GREEN}===${NC}"; }
run()  { if [ "$DRY" -eq 1 ]; then echo "DRY: $*"; else "$@"; fi; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
RTL_TCP_DIR="$SRC_DIR/rtl-tcp"
SPEC_DIR="$SRC_DIR/spectrum-monitor"
NOTIFY_DIR="$SRC_DIR/notify"

for f in \
    "$RTL_TCP_DIR/rtl-tcp-escalator.py" \
    "$RTL_TCP_DIR/rtl-tcp-escalator.service" \
    "$RTL_TCP_DIR/rtl-tcp-escalator.timer" \
    "$SPEC_DIR/freshness-probe.py" \
    "$SPEC_DIR/freshness-probe.service" \
    "$SPEC_DIR/freshness-probe.timer" \
    "$NOTIFY_DIR/notify.py" \
    "$NOTIFY_DIR/notify.env.example" \
    "$NOTIFY_DIR/heartbeat.sh" \
    "$NOTIFY_DIR/heartbeat.service" \
    "$NOTIFY_DIR/heartbeat.timer"; do
    if [ ! -f "$f" ]; then err "missing: $f"; exit 1; fi
done

step "Install scripts"
run sudo install -m 0755 "$RTL_TCP_DIR/rtl-tcp-escalator.py"   /usr/local/bin/rtl-tcp-escalator
run sudo install -m 0755 "$SPEC_DIR/freshness-probe.py"        /usr/local/bin/rf-freshness-probe
run sudo install -m 0755 "$NOTIFY_DIR/notify.py"               /usr/local/bin/rf-notify
run sudo install -m 0755 "$NOTIFY_DIR/heartbeat.sh"            /usr/local/bin/rf-heartbeat
info "scripts installed"

step "Install systemd units"
run sudo install -m 0644 "$RTL_TCP_DIR/rtl-tcp-escalator.service"  /etc/systemd/system/rtl-tcp-escalator.service
run sudo install -m 0644 "$RTL_TCP_DIR/rtl-tcp-escalator.timer"    /etc/systemd/system/rtl-tcp-escalator.timer
run sudo install -m 0644 "$SPEC_DIR/freshness-probe.service"      /etc/systemd/system/rf-freshness-probe.service
run sudo install -m 0644 "$SPEC_DIR/freshness-probe.timer"        /etc/systemd/system/rf-freshness-probe.timer
run sudo install -m 0644 "$NOTIFY_DIR/heartbeat.service"          /etc/systemd/system/rf-heartbeat.service
run sudo install -m 0644 "$NOTIFY_DIR/heartbeat.timer"            /etc/systemd/system/rf-heartbeat.timer
info "units installed"

step "Create state dirs + action log"
run sudo install -d -m 0755 /etc/rtl-scanner
run sudo install -d -m 0755 /var/lib/rtl-tcp-escalator
run sudo install -d -m 0755 /var/lib/spectrum-monitor
run sudo touch /var/log/rtl-recovery.log
run sudo chmod 0644 /var/log/rtl-recovery.log
info "/var/lib/* + /var/log/rtl-recovery.log ready"

step "Install env-file placeholder (notify.env)"
if [ ! -f /etc/rtl-scanner/notify.env ]; then
    run sudo install -m 0640 "$NOTIFY_DIR/notify.env.example" /etc/rtl-scanner/notify.env
    warn "Edit /etc/rtl-scanner/notify.env and set NTFY_TOPIC before leaving."
else
    info "/etc/rtl-scanner/notify.env exists; not overwriting"
fi

step "Install logrotate config"
LOGROTATE=/etc/logrotate.d/rtl-recovery
if [ "$DRY" -eq 0 ]; then
    sudo tee "$LOGROTATE" >/dev/null <<'LR'
/var/log/rtl-recovery.log {
    weekly
    rotate 4
    size 10M
    missingok
    notifempty
    compress
    delaycompress
    create 0644 root root
}
LR
    info "$LOGROTATE"
else
    echo "DRY: would write $LOGROTATE"
fi

step "Mask deferred legacy units (rtl-tcp-refresh.{service,timer})"
# Per spectrum/docs/followups/20260423_hardening_followups.md item 1.
for u in rtl-tcp-refresh.service rtl-tcp-refresh.timer; do
    target=/etc/systemd/system/$u
    if [ -e "$target" ]; then
        if [ -L "$target" ] && [ "$(readlink "$target")" = "/dev/null" ]; then
            info "$u already masked"
        else
            run sudo ln -sf /dev/null "$target"
            info "masked $u"
        fi
    else
        info "$u not present; nothing to mask"
    fi
done

step "Reload systemd"
run sudo systemctl daemon-reload

if [ "$ENABLE" -eq 1 ]; then
    step "Enable + start timers"
    run sudo systemctl enable --now rtl-tcp-escalator.timer
    run sudo systemctl enable --now rf-freshness-probe.timer
    run sudo systemctl enable --now rf-heartbeat.timer
    info "timers enabled"
else
    warn "--no-enable: timers installed but NOT started"
fi

step "Verify"
run sudo systemctl list-timers --no-pager 'rtl-tcp-escalator.timer' 'rf-freshness-probe.timer' 'rf-heartbeat.timer'

echo
info "Install complete."
echo
echo "Next steps:"
echo "  1. Edit /etc/rtl-scanner/notify.env and set NTFY_TOPIC=<your-topic>"
echo "  2. Test the alert pipe:  rf-notify INFO 'install test' -m 'pipe alive' --force"
echo "  3. One-shot dry-run of escalator:  sudo /usr/local/bin/rtl-tcp-escalator --dry-run"
echo "  4. One-shot freshness:             sudo /usr/local/bin/rf-freshness-probe"
echo "  5. Tail action log:                sudo tail -f /var/log/rtl-recovery.log"
