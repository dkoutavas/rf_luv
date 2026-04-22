#!/usr/bin/env bash
set -euo pipefail

# rtl-tcp wrapper that resolves an RTL-SDR serial to its current librtlsdr
# index, verifies the device at that index is actually the one we asked for,
# and then execs rtl_tcp with the resolved index.
#
# Why: with two RTL-SDR dongles on one host, USB enumeration order is
# non-deterministic. rtl_tcp's `-d <index>` binds by position, not identity,
# so on reboots or USB rebinds the wrong dongle's samples can land in the
# wrong pipeline. This wrapper converts serial (stable) → index (volatile)
# on every invocation.
#
# Usage:
#   rtl-tcp-by-serial <SERIAL> [<rtl_tcp args...>]
#
# Example (systemd):
#   ExecStart=/usr/local/bin/rtl-tcp-by-serial %i -a 0.0.0.0 -p 1234 -s 2048000
#   (where %i is the systemd instance name, e.g. v3-01)
#
# Exits:
#   1 if the serial isn't found on the bus
#   1 if the index resolves but post-open verification finds a different serial
#   (whatever rtl_tcp exits with, if we get that far)

if [ "$#" -lt 1 ]; then
    echo "usage: $(basename "$0") <serial> [rtl_tcp args...]" >&2
    exit 2
fi

SERIAL="$1"
shift

# rtl_test prints to stderr. The format (librtlsdr 0.6+):
#   Found 2 device(s):
#     0:  Realtek, RTL2838UHIDIR, SN: v3-01
#     1:  Realtek, RTL2838UHIDIR, SN: v4-01
#
# Extract the leading index of the line whose SN field matches $SERIAL.
INDEX=$(rtl_test 2>&1 \
    | awk -v s="$SERIAL" '
        match($0, /^  ([0-9]+):/, m) && $0 ~ ("SN: " s "$") {
            print m[1]
            exit
        }
    ')

if [ -z "${INDEX:-}" ]; then
    echo "rtl-tcp-by-serial: serial '$SERIAL' not found on bus" >&2
    rtl_test 2>&1 | grep -E 'Found|SN:' >&2 || true
    exit 1
fi

# Post-resolve verification: ask rtl_eeprom what serial actually sits at that
# index, and compare. Guards against races where the USB bus re-enumerated
# between the rtl_test call above and now (rare, but not zero).
ACTUAL=$(rtl_eeprom -d "$INDEX" 2>&1 | awk '/Serial number/ {print $NF; exit}')
if [ "${ACTUAL:-}" != "$SERIAL" ]; then
    echo "rtl-tcp-by-serial: index $INDEX now holds serial '${ACTUAL:-unknown}', expected '$SERIAL' — aborting" >&2
    exit 1
fi

echo "rtl-tcp-by-serial: $SERIAL → index $INDEX" >&2
exec /usr/local/bin/rtl_tcp -d "$INDEX" "$@"
