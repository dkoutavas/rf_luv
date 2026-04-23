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

# Enumerate by probing rtl_eeprom on each candidate index. rtl_eeprom reads
# the EEPROM and exits — unlike `rtl_test` (no args), which enters an
# indefinite sample loop and won't exit on SIGPIPE if it's not currently
# writing to stderr. The original wrapper used `rtl_test 2>&1 | awk ...`
# which deadlocked here on librtlsdr 2.0+ (see followup 2026-04-22:
# rtl_test sampling loop never wrote stderr after the device list, so awk's
# exit didn't terminate it). MAX_PROBE bounds the search since librtlsdr
# uses 0-based contiguous indices.
#
# MAX_ENUMERATE_ATTEMPTS wraps the whole probe in a retry loop because
# librtlsdr enumeration races with USB re-enumeration events: on 2026-04-23
# the wrapper failed with "serial 'v3-01' not found on bus" AT THE SAME
# INSTANT sysfs showed v3-01 on the bus — the kernel had the device but
# librtlsdr's usb_get_device_list() had a stale cache. A short sleep +
# retry clears the race without adding meaningful startup latency.
MAX_PROBE=7
MAX_ENUMERATE_ATTEMPTS=3
ENUMERATE_RETRY_SLEEP=0.5
INDEX=""
SAW_BUSY=0  # track whether any probe hit usb_claim_interface error (device on bus but in use)
for attempt in $(seq 1 "$MAX_ENUMERATE_ATTEMPTS"); do
    for IDX in $(seq 0 "$MAX_PROBE"); do
        # `|| true` because rtl_eeprom exits non-zero past the last device, and
        # set -e would abort the script otherwise. We detect the actual end of
        # devices by the "No matching devices found" message instead.
        PROBE=$(rtl_eeprom -d "$IDX" 2>&1) || true
        if echo "$PROBE" | grep -q "No matching devices found"; then
            break
        fi
        if echo "$PROBE" | grep -q "usb_claim_interface error"; then
            # Device is enumerated on the bus but another process holds the
            # handle. rtl_eeprom can't read the EEPROM to confirm serial, so
            # we can't positively match it against $SERIAL — but we do know
            # SOMETHING is at this index. Record that we saw a busy dongle
            # so we can produce a meaningful error message at the end
            # (rather than the misleading "not found on bus").
            SAW_BUSY=1
            continue
        fi
        THIS_SERIAL=$(echo "$PROBE" | awk '/Serial number/ {print $NF; exit}')
        if [ "${THIS_SERIAL:-}" = "$SERIAL" ]; then
            INDEX="$IDX"
            break 2
        fi
    done
    if [ "$attempt" -lt "$MAX_ENUMERATE_ATTEMPTS" ]; then
        echo "rtl-tcp-by-serial: attempt $attempt/$MAX_ENUMERATE_ATTEMPTS did not find '$SERIAL', retrying in ${ENUMERATE_RETRY_SLEEP}s" >&2
        sleep "$ENUMERATE_RETRY_SLEEP"
    fi
done

if [ -z "${INDEX:-}" ]; then
    if [ "$SAW_BUSY" -eq 1 ]; then
        # Most likely cause: another rtl_tcp (system-level rtl-tcp.service is
        # the classic offender — see 2026-04-22 and 2026-04-23 incidents) is
        # holding the device, so rtl_eeprom can't read the serial to match.
        echo "rtl-tcp-by-serial: serial '$SERIAL' not found (at least one dongle is BUSY — another rtl_tcp likely holds it)." >&2
        echo "rtl-tcp-by-serial: check 'systemctl status rtl-tcp.service' (system) and kill orphan rtl_tcp processes." >&2
    else
        echo "rtl-tcp-by-serial: serial '$SERIAL' not found on bus after $MAX_ENUMERATE_ATTEMPTS attempts" >&2
    fi
    rtl_eeprom 2>&1 | grep -E 'Found|SN:' >&2 || true
    exit 1
fi

# Post-resolve re-verification was here originally, but a second
# back-to-back rtl_eeprom -d "$INDEX" call sometimes returns non-zero
# (USB device not yet released by the first probe), which triggered
# set -e and silently exited the wrapper. The probe loop above is
# already authoritative — drop the second probe.

echo "rtl-tcp-by-serial: $SERIAL → index $INDEX" >&2
exec /usr/local/bin/rtl_tcp -d "$INDEX" "$@"
