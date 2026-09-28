#!/usr/bin/env bash
set -euo pipefail

# Build rtl_tcp with the disconnect-race fix (rtl_tcp-disconnect-race.patch).
#
# Stock rtl_tcp aborts, or sometimes hangs, when a client closes the
# connection while a retune is still being applied: the USB teardown in
# rtlsdr_read_async() races the command thread's control transfer (libusb
# "usbi_mutex_destroy: Assertion ... failed"). Upstream osmocom and the
# RTL-SDR Blog fork have the same code, so the fix is carried here. The patch
# also wakes the sender when a session ends (the next client no longer waits
# out a 5 s timeout) and makes SIGTERM end the process cleanly with status 0.
#
# The build fetches rtl_tcp.c and its helpers at RTL_SDR_TAG and links them
# against the system librtlsdr, so the tag must match the installed package
# (`rpm -q rtl-sdr`). Rebuild after an rtl-sdr update. No extra packages:
# gcc and the system librtlsdr.so.0 are enough.
#
# Usage:
#   bash ops/rtl-tcp/build-rtl-tcp.sh           # prints the install command
#   sudo install -m 0755 <build dir>/rtl_tcp /usr/local/bin/rtl_tcp
#   systemctl --user restart rtl-tcp@v3-01 rtl-tcp@v4-01
# /usr/local/bin precedes /usr/bin in PATH, so rtl-tcp-by-serial picks it up.
# Roll back with: sudo rm /usr/local/bin/rtl_tcp, then restart the units.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG="${RTL_SDR_TAG:-v2.0.3}"
BUILD="${BUILD_DIR:-$(mktemp -d)}"
URL="https://raw.githubusercontent.com/osmocom/rtl-sdr/$TAG"

installed="$(rpm -q --qf '%{VERSION}' rtl-sdr 2>/dev/null || true)"
if [ -n "$installed" ] && [ "v$installed" != "$TAG" ]; then
    echo "warning: installed rtl-sdr is $installed, building rtl_tcp from $TAG" >&2
fi

mkdir -p "$BUILD/src/convenience" "$BUILD/include"
for f in src/rtl_tcp.c src/convenience/convenience.c src/convenience/convenience.h \
         include/rtl-sdr.h include/rtl-sdr_export.h; do
    curl -sfL "$URL/$f" -o "$BUILD/$f"
done
patch -d "$BUILD" -p1 < "$HERE/rtl_tcp-disconnect-race.patch"
gcc -O2 -I"$BUILD/include" -I"$BUILD/src" -o "$BUILD/rtl_tcp" \
    "$BUILD/src/rtl_tcp.c" "$BUILD/src/convenience/convenience.c" \
    -l:librtlsdr.so.0 -lpthread -lm

echo "built $BUILD/rtl_tcp ($TAG + disconnect-race patch). Install with:"
echo "  sudo install -m 0755 $BUILD/rtl_tcp /usr/local/bin/rtl_tcp"
echo "  systemctl --user restart rtl-tcp@v3-01 rtl-tcp@v4-01"
