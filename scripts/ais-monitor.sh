#!/usr/bin/env bash
set -euo pipefail

# AIS Ship Tracking — Piraeus / Saronic Gulf
#
# Decodes AIS (Automatic Identification System) messages from ships.
# Two channels: 161.975 MHz (Ch 87B) and 162.025 MHz (Ch 88B)
#
# From Polygono with antenna toward south/southwest, you should pick up
# traffic in the Saronic Gulf, Piraeus port, and possibly further.
#
# Usage:
#   ./scripts/ais-monitor.sh              # live decoded output
#   ./scripts/ais-monitor.sh log          # append JSON to file
#
# Requires: rtl_fm + multimon-ng, OR rtl_ais
# Antenna: vertical dipole, arms ~45 cm, facing toward Piraeus (SW)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$PROJECT_DIR/recordings"
mkdir -p "$OUTPUT_DIR"

GAIN="${GAIN:-40}"
MODE="${1:-console}"
LOG_FILE="$OUTPUT_DIR/ais_$(date +%Y%m%d).log"

# AIS uses two frequencies — we need to alternate or use rtl_ais which handles both
AIS_FREQ1="161975000"  # Channel 87B
AIS_FREQ2="162025000"  # Channel 88B

echo "╔══════════════════════════════════════════════════╗"
echo "║  AIS Ship Tracking"
echo "║  Ch 87B: 161.975 MHz / Ch 88B: 162.025 MHz"
echo "║  Gain: $GAIN dB"
echo "║  Tip: point antenna SW toward Piraeus"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# Prefer rtl_ais if available (handles dual-channel natively)
if command -v rtl_ais &>/dev/null; then
    echo "Using rtl_ais (dual-channel decoder)"
    echo "Listening... (Ctrl+C to stop)"
    echo ""

    case "$MODE" in
        console)
            rtl_ais -g "$GAIN" -p 0
            ;;
        log)
            echo "Logging to: $LOG_FILE"
            rtl_ais -g "$GAIN" -p 0 -n 2>&1 | tee -a "$LOG_FILE"
            ;;
    esac
else
    # Fallback: rtl_fm piped to multimon-ng (single channel only)
    echo "Using rtl_fm + multimon-ng (single channel: 161.975 MHz)"
    echo "  For dual-channel, install rtl_ais"
    echo "Listening... (Ctrl+C to stop)"
    echo ""

    case "$MODE" in
        console)
            rtl_fm -M fm -f "$AIS_FREQ1" -s 12500 -g "$GAIN" - 2>/dev/null | \
                multimon-ng -t raw -a AIS -
            ;;
        log)
            echo "Logging to: $LOG_FILE"
            rtl_fm -M fm -f "$AIS_FREQ1" -s 12500 -g "$GAIN" - 2>/dev/null | \
                multimon-ng -t raw -a AIS - 2>&1 | tee -a "$LOG_FILE"
            ;;
    esac
fi
