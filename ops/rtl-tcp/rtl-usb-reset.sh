#!/usr/bin/env bash
set -euo pipefail

# Re-enumerates one RTL-SDR dongle by unbind/bind via /sys/bus/usb/drivers/usb.
# Software equivalent of physically replugging. Needs root (wired through
# sudoers for the watchdog — see sudoers-rtl-usb-reset).
#
# Usage:
#   rtl-usb-reset              — reset ALL RTL-SDR dongles (legacy single-dongle mode)
#   rtl-usb-reset <serial>     — reset only the dongle with matching serial
#
# Per-serial mode is required once two dongles are on the same host — the
# legacy "reset all matches" mode would hard-bounce the healthy dongle
# whenever the other one hiccups.
#
# Installed as /usr/local/sbin/rtl-usb-reset on leap; wired via sudoers so
# the watchdog can invoke without a password.

VID="0bda"
PID="2838"
WANT_SERIAL="${1:-}"

if [ "$EUID" -ne 0 ]; then
    echo "rtl-usb-reset: must run as root" >&2
    exit 1
fi

reset_device() {
    local name="$1"
    echo "rtl-usb-reset: unbind/rebind $name"
    echo "$name" > /sys/bus/usb/drivers/usb/unbind
    sleep 1
    echo "$name" > /sys/bus/usb/drivers/usb/bind
    # Give udev time to recreate /dev/bus/usb node and ACLs
    sleep 2
}

matched=0
for dev in /sys/bus/usb/devices/*; do
    [ -f "$dev/idVendor" ] || continue
    v=$(cat "$dev/idVendor")
    p=$(cat "$dev/idProduct")
    [ "$v" = "$VID" ] && [ "$p" = "$PID" ] || continue

    if [ -n "$WANT_SERIAL" ]; then
        # Match by serial — only reset the dongle whose EEPROM serial matches.
        if [ ! -f "$dev/serial" ]; then
            continue  # serial attribute missing → not the dongle we want
        fi
        devserial=$(cat "$dev/serial")
        if [ "$devserial" != "$WANT_SERIAL" ]; then
            continue
        fi
    fi

    name=$(basename "$dev")
    echo "rtl-usb-reset: match $name ($v:$p, serial=${WANT_SERIAL:-any})"
    reset_device "$name"
    matched=1

    # In per-serial mode, only one match is expected; stop here so udev's
    # rename doesn't race with further iterations of the loop.
    if [ -n "$WANT_SERIAL" ]; then
        echo "rtl-usb-reset: done (serial=$WANT_SERIAL)"
        exit 0
    fi
done

if [ "$matched" -eq 0 ]; then
    if [ -n "$WANT_SERIAL" ]; then
        echo "rtl-usb-reset: no device $VID:$PID with serial '$WANT_SERIAL' found" >&2
    else
        echo "rtl-usb-reset: no device $VID:$PID found" >&2
    fi
    exit 2
fi

echo "rtl-usb-reset: done (all $VID:$PID devices reset)"
