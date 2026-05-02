#!/usr/bin/env bash
set -euo pipefail
#
# tle_refresh.sh — pull TLEs for the satellites we care about from celestrak,
# write them to /var/lib/noaa/tles.txt for noaa/scheduler.py.
#
# Why CATNR-based fetching, not group fetching:
#   The classic NOAA 15/18/19 satellites used to live in celestrak's
#   GROUP=weather feed, but as of 2026 they've been moved out (only
#   modern polar sats — NOAA 20/21 JPSS, METEOR-M2 series — show up there).
#   Fetching each by NORAD catalog number is more robust against celestrak's
#   group reorganizations.
#
# Format: 3-line groups (NAME, line1, line2). orbit-predictor's
# get_predictor_from_tle_lines() expects this exact shape.
#
# Idempotent. If any per-sat fetch fails, others continue; final exit
# code is 0 unless ZERO sats were fetched (likely network-down) — in
# that case keep the old file untouched and exit non-zero so systemd retries.
#
# Run once:
#     bash noaa/tle_refresh.sh
# Or via systemd timer (see ../ops/noaa-pass-scheduler/).

OUTPUT="${NOAA_TLE_PATH:-/var/lib/noaa/tles.txt}"

# Satellite name → NORAD catalog number. Verified against celestrak 2026-05.
# Add more here when extending coverage.
declare -A CATNR=(
    ["NOAA 15"]=25338
    ["NOAA 18"]=28654
    ["NOAA 19"]=33591
    ["METEOR-M2 3"]=57166
    ["METEOR-M2 4"]=59051
)

URL_BASE="${CELESTRAK_URL_BASE:-https://celestrak.org/NORAD/elements/gp.php}"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

success=0
failed=()
for name in "${!CATNR[@]}"; do
    catnr="${CATNR[$name]}"
    url="${URL_BASE}?CATNR=${catnr}&FORMAT=tle"
    if curl -fsSL --max-time 20 --retry 2 --retry-delay 3 "$url" -o "$TMP.one" 2>/dev/null; then
        if [ -s "$TMP.one" ] && grep -q "^[12] " "$TMP.one"; then
            cat "$TMP.one" >> "$TMP"
            success=$((success + 1))
        else
            failed+=("$name (empty/malformed response)")
        fi
    else
        failed+=("$name (HTTP fetch failed)")
    fi
    rm -f "$TMP.one"
done

if [ "$success" -eq 0 ]; then
    echo "[$(date -u +%FT%TZ)] tle_refresh: all $((${#CATNR[@]})) fetches failed; keeping existing $OUTPUT"
    for f in "${failed[@]}"; do echo "  - $f"; done
    exit 1
fi

# Atomic install
mkdir -p "$(dirname "$OUTPUT")"
mv "$TMP" "$OUTPUT"
chmod 0644 "$OUTPUT"
trap - EXIT  # mv consumed the tmpfile

echo "[$(date -u +%FT%TZ)] tle_refresh: wrote $success / ${#CATNR[@]} TLE entries to $OUTPUT"
if [ "${#failed[@]}" -gt 0 ]; then
    for f in "${failed[@]}"; do echo "  warning: $f"; done
    exit 0  # partial success — still useful, don't trigger systemd retry
fi
