#!/usr/bin/env bash
set -euo pipefail

# Record a NOAA satellite pass for APT image decoding
#
# Usage:
#   ./scripts/satellite-pass.sh                  # NOAA 15 (default)
#   ./scripts/satellite-pass.sh noaa18           # NOAA 18
#   ./scripts/satellite-pass.sh noaa19           # NOAA 19
#   ./scripts/satellite-pass.sh meteor           # Meteor M2-3 (LRPT, different decoder)
#   DURATION=900 ./scripts/satellite-pass.sh     # override duration (seconds)
#
# A typical NOAA pass lasts 10-15 minutes. Default recording is 15 min.
#
# Requires: rtl_fm (from rtl-sdr), sox (for WAV conversion)
# Decoding: use noaa-apt or satdump after recording
#
# IMPORTANT: antenna must be outdoors (patio) with sky view!
# Use V-dipole configuration: arms at 120° angle, ~53 cm each,
# oriented roughly north-south, tilted ~30° from horizontal.
#
# Check pass times at: https://www.n2yo.com or use gpredict

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$PROJECT_DIR/recordings"
mkdir -p "$OUTPUT_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DURATION="${DURATION:-900}"  # 15 minutes default
GAIN="${GAIN:-40}"
SAMPLE_RATE="48000"  # APT signal is ~34 kHz wide, 48k sample rate works

# NOAA APT frequencies
declare -A SATELLITES=(
    ["noaa15"]="137620000"   # 137.620 MHz
    ["noaa18"]="137912500"   # 137.9125 MHz
    ["noaa19"]="137100000"   # 137.100 MHz
    ["meteor"]="137100000"   # 137.100 MHz (Meteor M2-3 LRPT)
)

declare -A SAT_NAMES=(
    ["noaa15"]="NOAA 15"
    ["noaa18"]="NOAA 18"
    ["noaa19"]="NOAA 19"
    ["meteor"]="Meteor M2-3"
)

SAT_KEY="${1:-noaa15}"

if [[ ! -v SATELLITES[$SAT_KEY] ]]; then
    echo "Unknown satellite: $SAT_KEY"
    echo "Available: ${!SATELLITES[*]}"
    exit 1
fi

FREQ="${SATELLITES[$SAT_KEY]}"
NAME="${SAT_NAMES[$SAT_KEY]}"
FREQ_MHZ=$(echo "scale=4; $FREQ / 1000000" | bc)

RAW_FILE="$OUTPUT_DIR/${SAT_KEY}_${TIMESTAMP}.raw"
WAV_FILE="$OUTPUT_DIR/${SAT_KEY}_${TIMESTAMP}.wav"

echo "╔══════════════════════════════════════════════════╗"
echo "║  Satellite Pass Recording"
echo "║  Satellite: $NAME"
echo "║  Frequency: ${FREQ_MHZ} MHz"
echo "║  Duration:  ${DURATION}s (~$((DURATION / 60)) min)"
echo "║  Output:    $WAV_FILE"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "  Make sure your antenna is on the patio with sky view!"
echo "  V-dipole: 53cm arms, 120° angle, tilted north-south"
echo ""
echo "  Recording starts in 3 seconds... (Ctrl+C to cancel)"
sleep 3

echo "Recording..."

if [[ "$SAT_KEY" == "meteor" ]]; then
    # Meteor M2-3 uses LRPT (digital) — needs wider bandwidth
    # Record raw IQ for satdump processing
    echo "  Note: Meteor uses LRPT digital mode. Recording raw IQ."
    echo "  Decode with: satdump live meteor_m2-3_lrpt $WAV_FILE"
    SAMPLE_RATE="192000"
fi

# rtl_fm demodulates FM and outputs raw audio
# -M wfm = wideband FM (for APT)
# -s = sample rate
# -f = frequency (with Doppler, the signal drifts ±3 kHz during a pass)
# -A fast = fast atan demod
# -l 0 = squelch off
# -E dc = DC offset removal (important for RTL-SDR)

timeout "$DURATION" rtl_fm \
    -M wfm \
    -f "$FREQ" \
    -s "$SAMPLE_RATE" \
    -g "$GAIN" \
    -A fast \
    -l 0 \
    -E dc \
    "$RAW_FILE" 2>/dev/null || true  # timeout returns 124, that's fine

echo ""
echo "Recording complete: $RAW_FILE"

# Convert raw audio to WAV for decoder compatibility
if command -v sox &>/dev/null; then
    echo "Converting to WAV..."
    sox -t raw -r "$SAMPLE_RATE" -e signed -b 16 -c 1 "$RAW_FILE" "$WAV_FILE"
    rm "$RAW_FILE"
    echo "WAV file: $WAV_FILE ($(du -h "$WAV_FILE" | cut -f1))"
else
    echo "sox not installed — raw file kept at $RAW_FILE"
    echo "Convert manually: sox -t raw -r $SAMPLE_RATE -e signed -b 16 -c 1 $RAW_FILE $WAV_FILE"
fi

echo ""
echo "Next steps:"
if [[ "$SAT_KEY" == "meteor" ]]; then
    echo "  Decode with satdump:"
    echo "    satdump live meteor_m2-3_lrpt $WAV_FILE --source file"
else
    echo "  Decode APT image with noaa-apt:"
    echo "    noaa-apt $WAV_FILE -o ${WAV_FILE%.wav}.png"
    echo ""
    echo "  Or with satdump:"
    echo "    satdump live noaa_apt $WAV_FILE --source file"
fi
