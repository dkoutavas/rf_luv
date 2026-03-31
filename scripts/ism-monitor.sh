#!/usr/bin/env bash
set -euo pipefail

# ISM Band Device Monitor — rtl_433
#
# Listens on 433.92 MHz (EU ISM band) and decodes signals from:
# - Weather stations (temperature, humidity, wind, rain)
# - Car key fobs / tire pressure sensors (TPMS)
# - Wireless doorbells, remotes
# - Smart home sensors
# - Garage door openers
# - Utility meters (in some areas)
#
# This is one of the most immediately rewarding things to run —
# you'll be surprised how many devices are transmitting near you.
#
# Usage:
#   ./scripts/ism-monitor.sh              # live console output
#   ./scripts/ism-monitor.sh log          # append JSON lines to file
#   ./scripts/ism-monitor.sh mqtt         # publish to MQTT (for home automation)
#
# Requires: rtl_433, direct USB access

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$PROJECT_DIR/recordings"
mkdir -p "$OUTPUT_DIR"

GAIN="${GAIN:-40}"
FREQ="${FREQ:-433.92M}"
LOG_FILE="$OUTPUT_DIR/ism_devices_$(date +%Y%m%d).jsonl"

MODE="${1:-console}"

echo "╔══════════════════════════════════════════════════╗"
echo "║  ISM Band Device Monitor (rtl_433)"
echo "║  Frequency: $FREQ"
echo "║  Gain: $GAIN dB"
echo "║  Mode: $MODE"
echo "╚══════════════════════════════════════════════════╝"
echo ""

case "$MODE" in
    console)
        # Pretty-print decoded signals to terminal
        # -F json outputs structured data, but for console we use default
        echo "Listening... (Ctrl+C to stop)"
        echo ""
        rtl_433 -g "$GAIN" -f "$FREQ" -M time:utc -M level
        ;;

    log)
        # Append JSON lines to daily log file
        echo "Logging to: $LOG_FILE"
        echo "Listening... (Ctrl+C to stop)"
        echo ""

        # -F json = one JSON object per line per decoded signal
        # Tee to both console and file
        rtl_433 -g "$GAIN" -f "$FREQ" -M time:utc -M level -F json 2>/dev/null | \
            tee -a "$LOG_FILE"
        ;;

    mqtt)
        # Publish to MQTT broker (if you have one running, e.g., EMQX/Mosquitto)
        MQTT_HOST="${MQTT_HOST:-localhost}"
        MQTT_PORT="${MQTT_PORT:-1883}"
        MQTT_TOPIC="${MQTT_TOPIC:-rtl_433}"

        echo "Publishing to MQTT: ${MQTT_HOST}:${MQTT_PORT}/${MQTT_TOPIC}"
        echo "Listening... (Ctrl+C to stop)"
        echo ""

        rtl_433 -g "$GAIN" -f "$FREQ" -M time:utc -M level \
            -F "mqtt://${MQTT_HOST}:${MQTT_PORT},events=${MQTT_TOPIC}/events,devices=${MQTT_TOPIC}/devices"
        ;;

    analyze)
        # Run for a set time, then summarize what was found
        DURATION="${DURATION:-300}"
        TEMP_FILE=$(mktemp)

        echo "Scanning for ${DURATION}s, then analyzing..."
        echo ""

        timeout "$DURATION" rtl_433 -g "$GAIN" -f "$FREQ" -M time:utc -F json 2>/dev/null > "$TEMP_FILE" || true

        DEVICE_COUNT=$(jq -s '[.[].model] | unique | length' "$TEMP_FILE" 2>/dev/null || echo "0")
        TOTAL_MSGS=$(wc -l < "$TEMP_FILE")

        echo ""
        echo "═══ Scan Summary ═══"
        echo "Duration: ${DURATION}s"
        echo "Total messages decoded: $TOTAL_MSGS"
        echo "Unique device types: $DEVICE_COUNT"
        echo ""

        if [ "$TOTAL_MSGS" -gt 0 ]; then
            echo "Devices found:"
            jq -rs '[.[] | {model, id}] | group_by(.model) | .[] | {model: .[0].model, count: length, ids: [.[].id] | unique}' "$TEMP_FILE" 2>/dev/null || \
                echo "  (install jq for detailed analysis: sudo zypper install jq)"
        else
            echo "No devices decoded. Try:"
            echo "  - Higher gain (GAIN=50 ./scripts/ism-monitor.sh analyze)"
            echo "  - Moving antenna to window"
            echo "  - Waiting longer (DURATION=600)"
        fi

        rm -f "$TEMP_FILE"
        ;;

    *)
        echo "Unknown mode: $MODE"
        echo "Available: console, log, mqtt, analyze"
        exit 1
        ;;
esac
