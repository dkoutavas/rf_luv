#!/usr/bin/env bash
set -euo pipefail

# Re-enumerates the RTL-SDR dongle by unbind/bind via /sys/bus/usb/drivers/usb.
# Software equivalent of physically replugging. Needs root (wired through
# sudoers for the watchdog — see sudoers-rtl-usb-reset).

VID="0bda"
PID="2838"

if [ "$EUID" -ne 0 ]; then
    echo "rtl-usb-reset: must run as root" >&2
    exit 1
fi

for dev in /sys/bus/usb/devices/*; do
    [ -f "$dev/idVendor" ] || continue
    v=$(cat "$dev/idVendor")
    p=$(cat "$dev/idProduct")
    if [ "$v" = "$VID" ] && [ "$p" = "$PID" ]; then
        name=$(basename "$dev")
        echo "rtl-usb-reset: unbind/rebind $name ($v:$p)"
        echo "$name" > /sys/bus/usb/drivers/usb/unbind
        sleep 1
        echo "$name" > /sys/bus/usb/drivers/usb/bind
        # Give udev time to recreate /dev/bus/usb node and ACLs
        sleep 2
        echo "rtl-usb-reset: done"
        exit 0
    fi
done

echo "rtl-usb-reset: no device $VID:$PID found" >&2
exit 2
