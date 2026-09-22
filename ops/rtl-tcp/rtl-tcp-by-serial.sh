#!/usr/bin/env bash
set -euo pipefail

# rtl-tcp wrapper: start rtl_tcp on the dongle with the given serial.
#
# rtl_tcp's -d flag accepts a serial string, not only an index (librtlsdr
# verbose_device_search: exact serial match, then suffix match). It reads
# the USB string descriptors without claiming the interface, so the lookup
# works even while the sibling dongle is held by another rtl_tcp. Tested on
# rtl-sdr 2.0.3 on 2026-09-22. Identity therefore never depends on USB
# enumeration order, which shifts whenever a dongle is unplugged.
#
# If the serial is not on the bus rtl_tcp prints "No matching devices found."
# and exits 1. The per-instance systemd drop-in (BindsTo= the udev device
# unit, written by ops/install-host.sh) keeps the unit from restarting in a
# loop until the dongle returns.
#
# Usage:
#   rtl-tcp-by-serial <SERIAL> [<rtl_tcp args...>]

if [ "$#" -lt 1 ]; then
    echo "usage: $(basename "$0") <serial> [rtl_tcp args...]" >&2
    exit 2
fi

SERIAL="$1"
shift

echo "rtl-tcp-by-serial: $SERIAL (rtl_tcp -d by serial)" >&2
exec rtl_tcp -d "$SERIAL" "$@"
