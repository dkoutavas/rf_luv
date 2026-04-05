#!/usr/bin/env bash
set -euo pipefail

# Export spectrum scanner data from ClickHouse for analysis
# Bundles scans, peaks, events, sweep_health, and known_frequencies
# into a timestamped directory, ready to feed to an analysis agent.
#
# Usage:
#   ./export-data.sh                          # last 1 hour
#   ./export-data.sh 2h                       # last 2 hours
#   ./export-data.sh 6h                       # last 6 hours
#   ./export-data.sh 1d                       # last 1 day
#   ./export-data.sh "2026-04-05 10:00:00" "2026-04-05 14:00:00"  # explicit range
#
# Output: exports/spectrum_<timestamp>/
#   scans.csv          - raw power measurements (one row per freq bin per sweep)
#   peaks.csv          - detected spectral peaks (bins above neighbors)
#   events.csv         - transient signals (appeared/disappeared between sweeps)
#   sweep_health.csv   - per-sweep metadata (clipping, timing)
#   known_freqs.csv    - reference table of known Athens frequencies
#   summary.txt        - row counts and time range
#
# Requires: curl, ClickHouse running on localhost:8126

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPORT_DIR="$SCRIPT_DIR/exports"

# ClickHouse connection
CH_HOST="${CLICKHOUSE_HOST:-localhost}"
CH_PORT="${CLICKHOUSE_PORT:-8126}"
CH_USER="${CLICKHOUSE_USER:-spectrum}"
CH_PASS="${CLICKHOUSE_PASSWORD:-spectrum_local}"
CH_URL="http://${CH_HOST}:${CH_PORT}/?user=${CH_USER}&password=${CH_PASS}"

# --- Parse time range ---

if [[ $# -eq 0 ]]; then
    # Default: last 1 hour
    INTERVAL="1 HOUR"
    LABEL="last_1h"
elif [[ $# -eq 1 ]]; then
    # Shorthand: 2h, 6h, 1d, 30m, etc.
    INPUT="$1"
    case "$INPUT" in
        *m) INTERVAL="${INPUT%m} MINUTE"; LABEL="last_${INPUT}" ;;
        *h) INTERVAL="${INPUT%h} HOUR";   LABEL="last_${INPUT}" ;;
        *d) INTERVAL="${INPUT%d} DAY";    LABEL="last_${INPUT}" ;;
        *)  echo "Error: unrecognized duration '$INPUT'. Use Nm, Nh, or Nd (e.g. 2h, 30m, 1d)." >&2; exit 1 ;;
    esac
elif [[ $# -eq 2 ]]; then
    # Explicit range: "2026-04-05 10:00:00" "2026-04-05 14:00:00"
    TIME_START="$1"
    TIME_END="$2"
    LABEL="range"
else
    echo "Usage: $0 [duration | start_time end_time]" >&2
    echo "  duration: 30m, 2h, 6h, 1d" >&2
    echo "  times:    '2026-04-05 10:00:00' '2026-04-05 14:00:00'" >&2
    exit 1
fi

# Build WHERE clause
if [[ -n "${TIME_START:-}" ]]; then
    TIME_FILTER="timestamp BETWEEN '${TIME_START}' AND '${TIME_END}'"
    RANGE_DESC="${TIME_START} → ${TIME_END}"
else
    TIME_FILTER="timestamp > now() - INTERVAL ${INTERVAL}"
    RANGE_DESC="last ${INTERVAL}"
fi

# --- Setup output directory ---

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUT="$EXPORT_DIR/spectrum_${TIMESTAMP}"
mkdir -p "$OUT"

echo "Exporting spectrum data (${RANGE_DESC})..."
echo "Output: $OUT/"
echo ""

# --- Helper to query ClickHouse → CSV ---

query_csv() {
    local outfile="$1"
    local sql="$2"
    local label="$3"

    if ! curl -sf "$CH_URL" --data-binary "${sql} FORMAT CSVWithNames" -o "$outfile" 2>/dev/null; then
        echo "  ✗ ${label}: query failed (is ClickHouse running on ${CH_HOST}:${CH_PORT}?)" >&2
        return 1
    fi

    # Count rows (subtract 1 for header)
    local rows
    rows=$(( $(wc -l < "$outfile") - 1 ))
    if [[ $rows -lt 0 ]]; then rows=0; fi
    echo "  ${label}: ${rows} rows"
    echo "${label}: ${rows} rows" >> "$OUT/summary.txt"
}

# --- Export tables ---

echo "Time range: ${RANGE_DESC}" > "$OUT/summary.txt"
echo "Exported at: $(date -Iseconds)" >> "$OUT/summary.txt"
echo "---" >> "$OUT/summary.txt"

query_csv "$OUT/scans.csv" \
    "SELECT timestamp, freq_hz, power_dbfs, sweep_id FROM spectrum.scans WHERE ${TIME_FILTER} ORDER BY timestamp, freq_hz" \
    "scans"

query_csv "$OUT/peaks.csv" \
    "SELECT timestamp, freq_hz, power_dbfs, prominence_db, sweep_id FROM spectrum.peaks WHERE ${TIME_FILTER} ORDER BY timestamp, freq_hz" \
    "peaks"

query_csv "$OUT/events.csv" \
    "SELECT timestamp, freq_hz, event_type, power_dbfs, prev_power, delta_db, sweep_id FROM spectrum.events WHERE ${TIME_FILTER} ORDER BY timestamp, freq_hz" \
    "events"

query_csv "$OUT/sweep_health.csv" \
    "SELECT timestamp, sweep_id, preset, bin_count, max_power, sweep_duration_ms FROM spectrum.sweep_health WHERE ${TIME_FILTER} ORDER BY timestamp" \
    "sweep_health"

# known_frequencies is a reference table — no time filter
query_csv "$OUT/known_freqs.csv" \
    "SELECT freq_hz, bandwidth_hz, name, category, modulation, notes FROM spectrum.known_frequencies ORDER BY freq_hz" \
    "known_freqs"

# --- Hourly baseline (aggregated, useful for anomaly context) ---

query_csv "$OUT/hourly_baseline.csv" \
    "SELECT
        hour,
        freq_hz,
        avgMerge(avg_power) AS avg_power_dbfs,
        stddevPopMerge(std_power) AS std_power_dbfs,
        countMerge(sample_count) AS samples
    FROM spectrum.hourly_baseline
    WHERE hour >= toStartOfHour(now() - INTERVAL 24 HOUR)
    GROUP BY hour, freq_hz
    ORDER BY hour, freq_hz" \
    "hourly_baseline (24h)"

echo ""
echo "--- Export complete ---"
echo ""
cat "$OUT/summary.txt"
echo ""
echo "Files:"
ls -lh "$OUT/"
echo ""
echo "To analyze with Claude Code:"
echo "  claude 'Analyze the spectrum data in $OUT/ — check peak detection accuracy, identify anomalies, and validate known frequency identification.'"
