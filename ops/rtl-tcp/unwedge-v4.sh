#!/usr/bin/env bash
# Recover a wedged V4 dongle on leap. Runs the full sequence proven on
# 2026-04-29 after V4 stopped producing IQ samples around 06:30 EEST:
#
#   1. sudo rtl-usb-reset v4-01           # NOPASSWD per-device port bounce
#   2. usb-controller-reset.sh            # full xHCI unbind/rebind
#   3. systemctl --user restart rtl-tcp@v4-01   # fresh libusb handle
#   4. watchdog probe → expect "recovered: <N>B in <T>s"
#
# Why both 1 AND 2: per-device reset re-inits the dongle but the R828D
# tuner i2c bus can still fail (rtlsdr_demod_*_reg / r82xx_write returning
# libusb -4). Bouncing the whole xHCI clears that. Step 3 is required
# because rtl_tcp holds a libusb handle that goes stale across the xHCI
# bounce — without it the new tuner state is unreachable.
#
# Use when V4 panels in Grafana go flat and the watchdog journal shows
# repeated "starvation: 0B in 2.0s" / "CIRCUIT_BREAKER_OPEN".
#
# Run from any host that can reach leap over Tailscale:
#     bash ops/rtl-tcp/unwedge-v4.sh
# Asks for sudo password on leap once (during step 2 — controller unbind
# isn't NOPASSWD by design).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> step 1/4: per-device USB reset for v4-01 (NOPASSWD)"
ssh scanner 'sudo /usr/local/sbin/rtl-usb-reset v4-01'

echo
echo "==> step 2/4: xHCI controller reset (will prompt for sudo on leap)"
bash "$HERE/usb-controller-reset.sh"

echo
echo "==> step 3/4: restart rtl-tcp@v4-01 (fresh libusb handle)"
ssh scanner 'systemctl --user restart rtl-tcp@v4-01 && sleep 4 && systemctl --user is-active rtl-tcp@v4-01'

echo
echo "==> step 4/4: wait for next watchdog probe (up to 60s)"
ssh scanner 'for i in $(seq 1 20); do
    last=$(journalctl --user -u rtl-tcp-watchdog@v4-01 --since "now-1 min" --no-pager 2>/dev/null | tail -3)
    if echo "$last" | grep -q "recovered:"; then
        echo "[GREEN] watchdog confirms samples are flowing:"
        echo "$last" | sed "s/^/    /"
        exit 0
    fi
    if echo "$last" | grep -q "starvation:"; then
        echo "[STILL WEDGED] watchdog still sees 0 bytes — manual investigation needed:"
        echo "$last" | sed "s/^/    /"
        exit 1
    fi
    sleep 3
done
echo "[TIMEOUT] no watchdog probe in 60s — check manually:"
echo "    journalctl --user -u rtl-tcp-watchdog@v4-01 -n 10 --no-pager"
exit 2'

echo
echo "==> v4 unwedged. Grafana V4 panels resume within one sweep cycle (≤5 min)."
