#!/usr/bin/env bash
set -euo pipefail

# Wideband spectrum scan using rtl_power
# Produces CSV output that can be visualized with heatmap.py
#
# Usage:
#   ./scripts/spectrum-scan.sh              # full VHF/UHF scan (default)
#   ./scripts/spectrum-scan.sh fm           # FM broadcast band only
#   ./scripts/spectrum-scan.sh airband      # aviation band
#   ./scripts/spectrum-scan.sh marine       # marine VHF + AIS
#   ./scripts/spectrum-scan.sh full         # 24 MHz to 1.7 GHz (takes a while)
#   ./scripts/spectrum-scan.sh custom 400M 500M  # custom range
#
# Requires: rtl_power (from rtl-sdr package)
# Note: needs direct USB access (usbipd) or won't work through rtl_tcp
#
# Output: recordings/scan_<name>_<timestamp>.csv
# Visualize: python3 ~/.local/bin/heatmap.py <csv_file> <output.png>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$PROJECT_DIR/recordings"
mkdir -p "$OUTPUT_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Scan duration in seconds (default: 5 minutes)
DURATION="${SCAN_DURATION:-300}"

# Integration interval (how often to write a line — shorter = more time resolution)
INTERVAL="${SCAN_INTERVAL:-10}"

# Gain (auto = -1, or specify dB value)
GAIN="${SCAN_GAIN:-40}"

# Bin size in Hz (frequency resolution per pixel)
BIN_SIZE="10k"

scan_band() {
    local name="$1"
    local freq_start="$2"
    local freq_end="$3"
    local output="$OUTPUT_DIR/scan_${name}_${TIMESTAMP}.csv"

    echo "╔══════════════════════════════════════════════════╗"
    echo "║  Spectrum Scan: $name"
    echo "║  Range: $freq_start → $freq_end"
    echo "║  Duration: ${DURATION}s | Interval: ${INTERVAL}s"
    echo "║  Output: $output"
    echo "╚══════════════════════════════════════════════════╝"
    echo ""

    # rtl_power arguments:
    # -f start:end:bin_size  — frequency range and resolution
    # -i interval            — integration time per sweep line
    # -g gain                — tuner gain in dB
    # -e duration            — total scan duration
    # -1                     — single scan mode (exit after one complete sweep)

    rtl_power \
        -f "$freq_start":"$freq_end":"$BIN_SIZE" \
        -i "$INTERVAL" \
        -g "$GAIN" \
        -e "$DURATION" \
        "$output"

    echo ""
    echo "Scan complete: $output"
    echo "File size: $(du -h "$output" | cut -f1)"
    echo ""
    echo "To visualize:"
    echo "  python3 ~/.local/bin/heatmap.py $output ${output%.csv}.png"
    echo ""
    echo "To analyze in Python:"
    echo "  import pandas as pd"
    echo "  # Columns: date, time, freq_low, freq_high, bin_step, num_samples, dB values..."
    echo "  df = pd.read_csv('$output', header=None)"
}

case "${1:-default}" in
    fm)
        scan_band "fm_broadcast" "88M" "108M"
        ;;
    airband)
        scan_band "airband" "118M" "137M"
        ;;
    marine)
        scan_band "marine_vhf" "156M" "163M"
        ;;
    adsb)
        # Narrow scan around 1090 MHz — useful to see signal strength
        BIN_SIZE="100k"
        scan_band "adsb_band" "1080M" "1100M"
        ;;
    ism)
        scan_band "ism_433" "430M" "440M"
        ;;
    tetra)
        scan_band "tetra" "380M" "400M"
        ;;
    full)
        DURATION="${SCAN_DURATION:-600}"
        BIN_SIZE="50k"
        scan_band "full_spectrum" "24M" "1700M"
        ;;
    custom)
        if [ $# -lt 3 ]; then
            echo "Usage: $0 custom <start_freq> <end_freq>"
            echo "Example: $0 custom 400M 500M"
            exit 1
        fi
        scan_band "custom" "$2" "$3"
        ;;
    default)
        # Default: most interesting VHF/UHF range for Athens
        scan_band "vhf_uhf" "80M" "500M"
        ;;
    *)
        echo "Unknown band: $1"
        echo "Available: fm, airband, marine, adsb, ism, tetra, full, custom, default"
        exit 1
        ;;
esac
