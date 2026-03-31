#!/usr/bin/env bash
set -euo pipefail

# Athens Aviation Band Listener
#
# Listen to ATC (Air Traffic Control) communications around Athens.
# Aviation uses AM modulation (not FM like most other services).
#
# Usage:
#   ./scripts/airband-listen.sh                # Athens Approach (default)
#   ./scripts/airband-listen.sh approach       # Athens Approach
#   ./scripts/airband-listen.sh tower          # Athens Tower
#   ./scripts/airband-listen.sh ground         # Athens Ground
#   ./scripts/airband-listen.sh atis           # ATIS (weather/runway info)
#   ./scripts/airband-listen.sh <freq_mhz>    # custom frequency
#
# Requires: rtl_fm + aplay (or sox play)
# Antenna: vertical dipole, arms ~36 cm (airband quarter wave)
#
# Note: from Polygono you're well-positioned to hear approach traffic.
# ATC is in English (ICAO standard). Perfectly legal to listen.

GAIN="${GAIN:-38}"

# Athens ATC frequencies (may change — verify on current charts)
declare -A FREQS=(
    ["approach"]="118.575"      # Athens Approach (primary)
    ["approach2"]="119.1"       # Athens Approach (secondary)
    ["tower"]="118.1"           # Athens Tower
    ["ground"]="121.75"         # Athens Ground
    ["atis"]="136.125"          # ATIS (automated weather/info)
    ["delivery"]="118.575"      # Clearance Delivery
    ["emergency"]="121.5"       # Guard (international emergency)
)

TARGET="${1:-approach}"

# Check if it's a named frequency or a raw number
if [[ -v FREQS[$TARGET] ]]; then
    FREQ="${FREQS[$TARGET]}"
    NAME="Athens ${TARGET^}"
else
    # Assume it's a raw frequency in MHz
    FREQ="$TARGET"
    NAME="Custom ($FREQ MHz)"
fi

FREQ_HZ=$(echo "$FREQ * 1000000" | bc | cut -d. -f1)

echo "╔══════════════════════════════════════════════════╗"
echo "║  Aviation Band Listener"
echo "║  $NAME: $FREQ MHz"
echo "║  Modulation: AM (airband standard)"
echo "║  Gain: $GAIN dB"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "  Listening... (Ctrl+C to stop)"
echo "  Available channels: ${!FREQS[*]}"
echo ""

# Aviation uses AM modulation
# -M am = AM demodulation
# -s 12500 = 12.5 kHz sample rate (sufficient for 8.33/25 kHz channel spacing)
# -l 0 = no squelch (hear everything including silence)
#   change to -l 50 for squelch (only hear active transmissions)

if command -v aplay &>/dev/null; then
    rtl_fm -M am -f "$FREQ_HZ" -s 12500 -g "$GAIN" -l 0 - 2>/dev/null | \
        aplay -r 12500 -f S16_LE -t raw -c 1 -q
elif command -v play &>/dev/null; then
    # sox's play command
    rtl_fm -M am -f "$FREQ_HZ" -s 12500 -g "$GAIN" -l 0 - 2>/dev/null | \
        play -t raw -r 12500 -e signed -b 16 -c 1 -
else
    echo "No audio player found. Install: sudo zypper install alsa-utils"
    echo "Or record to file instead:"
    echo "  rtl_fm -M am -f $FREQ_HZ -s 12500 -g $GAIN - > airband.raw"
    exit 1
fi
