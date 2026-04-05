#!/usr/bin/env bash
set -uo pipefail

# Generate a frequency investigation checklist from spectrum scanner data.
# Queries ClickHouse for strong unknown signals and frequencies needing
# manual identification via SDR Console / SDR++.
#
# Usage:  ./investigate-freqs.sh
#
# Demod guide:
#   88-108 MHz  → WFM (broadcast FM)
#   108-137 MHz → AM (airband, 8.33 kHz spacing)
#   137-174 MHz → NFM (land mobile, 12.5/25 kHz)
#   174-230 MHz → (DVB-T/DAB — digital, no voice)
#   380-400 MHz → (TETRA — digital, encrypted)
#   433-446 MHz → NFM (ISM/PMR)

CH_HOST="${CLICKHOUSE_HOST:-localhost}"
CH_PORT="${CLICKHOUSE_PORT:-8126}"
CH_USER="${CLICKHOUSE_USER:-spectrum}"
CH_PASS="${CLICKHOUSE_PASSWORD:-spectrum_local}"
CH_URL="http://${CH_HOST}:${CH_PORT}/?user=${CH_USER}&password=${CH_PASS}"

query() { curl -s "$CH_URL" --data-binary "$1" 2>/dev/null; }

demod_for() {
    local f="$1"
    if   [ "$f" -ge 88  ] && [ "$f" -le 108 ]; then echo "WFM|150k|FM broadcast — music/talk?"
    elif [ "$f" -ge 108 ] && [ "$f" -le 137 ]; then echo "AM|8.33k|Airband — ATC voice? ATIS? Pilot?"
    elif [ "$f" -ge 137 ] && [ "$f" -le 144 ]; then echo "NFM|12.5k|Gov/military — voice? data bursts? repeater?"
    elif [ "$f" -ge 144 ] && [ "$f" -le 148 ]; then echo "NFM|12.5k|2m ham or military — voice? digital?"
    elif [ "$f" -ge 148 ] && [ "$f" -le 156 ]; then echo "NFM|12.5k|Military/business — dispatch? repeater?"
    elif [ "$f" -ge 156 ] && [ "$f" -le 163 ]; then echo "NFM|25k|Marine VHF — ship traffic? coast station?"
    elif [ "$f" -ge 163 ] && [ "$f" -le 174 ]; then echo "NFM|12.5k|Business/taxi — dispatch? repeater?"
    elif [ "$f" -ge 174 ] && [ "$f" -le 230 ]; then echo "(digital)|—|DVB-T/DAB — no voice, just confirm signal"
    elif [ "$f" -ge 380 ] && [ "$f" -le 400 ]; then echo "(digital)|25k|TETRA — buzzing, not decodable"
    elif [ "$f" -ge 430 ] && [ "$f" -le 450 ]; then echo "NFM|12.5k|ISM/PMR — weather station? walkie-talkie?"
    else echo "NFM|12.5k|Unknown — try AM and NFM"
    fi
}

# ─── Query data ─────────────────────────────────────────

UNKNOWNS=$(query "
WITH latest AS (
    SELECT sweep_id FROM spectrum.sweep_health
    WHERE preset = 'full' ORDER BY timestamp DESC LIMIT 1
),
matched AS (
    SELECT DISTINCT s.freq_hz
    FROM spectrum.scans s
    CROSS JOIN spectrum.known_frequencies kf
    WHERE s.sweep_id = (SELECT sweep_id FROM latest)
        AND abs(toInt64(s.freq_hz) - toInt64(kf.freq_hz))
            <= toInt64(greatest(kf.bandwidth_hz / 2, 50000))
)
SELECT
    round(freq_hz / 1000000, 1) AS freq_mhz,
    round(max(power_dbfs), 1) AS peak_power
FROM spectrum.scans s
WHERE s.sweep_id = (SELECT sweep_id FROM latest)
    AND s.power_dbfs > -25
    AND s.freq_hz NOT IN (SELECT freq_hz FROM matched)
    AND NOT (freq_hz BETWEEN 174000000 AND 230000000)
GROUP BY round(freq_hz / 1000000, 1)
ORDER BY peak_power DESC
LIMIT 30
FORMAT TabSeparated
")

AIRBAND=$(query "
SELECT
    round(freq_hz / 1000000, 3) AS freq_mhz,
    round(avg(power_dbfs), 1) AS avg_power,
    round(stddevPop(power_dbfs), 1) AS variability
FROM spectrum.scans
WHERE freq_hz BETWEEN 118000000 AND 137000000
    AND sweep_id LIKE 'full:%'
    AND run_id = (SELECT run_id FROM spectrum.scan_runs ORDER BY started_at DESC LIMIT 1)
GROUP BY freq_hz
HAVING avg(power_dbfs) > -30
ORDER BY avg(power_dbfs) DESC
LIMIT 12
FORMAT TabSeparated
")

RUN_INFO=$(query "
SELECT run_id, gain_db, antenna_position, antenna_arms_cm
FROM spectrum.scan_runs ORDER BY started_at DESC LIMIT 1
FORMAT TabSeparated
")

# ─── Output header ──────────────────────────────────────

cat << 'HEADER'
# Frequency Investigation Checklist

## How to use
1. Open SDR++ (or SDR Console) on Windows
2. Source: RTL-SDR, sample rate 2.048 MHz
3. For each frequency below:
   - Tune to the listed frequency
   - Set the demodulation mode as indicated
   - Listen for 30-60 seconds (some signals are bursty)
   - Note what you hear in the "Heard" column
   - Mark the checkbox when done

## Demod quick reference
| Mode | Use for | Bandwidth |
|------|---------|-----------|
| **AM** | Airband voice (118-137 MHz) | 8.33 kHz |
| **NFM** | Land mobile, marine, business | 12.5 or 25 kHz |
| **WFM** | FM broadcast (88-108 MHz) | 150 kHz |

HEADER

echo "## Scanner config"
echo '```'
echo "Run:     $(echo "$RUN_INFO" | cut -f1)"
echo "Gain:    $(echo "$RUN_INFO" | cut -f2) dB"
echo "Antenna: $(echo "$RUN_INFO" | cut -f3), $(echo "$RUN_INFO" | cut -f4) cm arms"
echo "Date:    $(date -u '+%Y-%m-%d %H:%M UTC')"
echo '```'
echo ""

# ─── Section 1: Strong unknowns ─────────────────────────

echo "---"
echo ""
echo "## 1. Strong Unknown Signals (auto-detected)"
echo ""
echo "Loudest signals not in the known_frequencies table. DVB-T band (174-230) excluded."
echo ""
echo "| # | Freq (MHz) | Power | Demod | BW | What to listen for | Heard |"
echo "|---|-----------|-------|-------|-----|-------------------|-------|"

N=1
while IFS=$'\t' read -r freq power; do
    [ -z "$freq" ] && continue
    freq_int=${freq%%.*}
    info=$(demod_for "$freq_int")
    demod=$(echo "$info" | cut -d'|' -f1)
    bw=$(echo "$info" | cut -d'|' -f2)
    hint=$(echo "$info" | cut -d'|' -f3)
    printf "| %d | **%s** | %s | %s | %s | %s | |\n" \
        "$N" "$freq" "$power" "$demod" "$bw" "$hint"
    N=$((N + 1))
done <<< "$UNKNOWNS"

echo ""

# ─── Section 2: Airband verification ────────────────────

echo "---"
echo ""
echo "## 2. Airband Verification (confirm real ATC)"
echo ""
echo "These frequencies are strong in the airband. We believe they are real"
echo "Athens ATC signals (not IMD artifacts). Tune AM, 8.33 kHz."
echo ""
echo "| # | Freq (MHz) | Avg Power | Variability | Expected | Heard |"
echo "|---|-----------|-----------|-------------|----------|-------|"

N=1
while IFS=$'\t' read -r freq power var; do
    [ -z "$freq" ] && continue
    if awk "BEGIN{exit ($var > 2.0) ? 0 : 1}" 2>/dev/null; then
        hint="Bursty voice (pilot/controller)"
    else
        hint="Semi-continuous (ATIS/VOLMET?)"
    fi
    printf "| %d | **%s** | %s | %s dB | %s | |\n" \
        "$N" "$freq" "$power" "$var" "$hint"
    N=$((N + 1))
done <<< "$AIRBAND"

echo ""

# ─── Section 3: Known signals to verify ─────────────────

cat << 'KNOWN'
---

## 3. Known Signals — Quick Verify

Already in the database but worth a quick listen to confirm.

| # | Freq (MHz) | Name | Demod | BW | What to confirm | OK? |
|---|-----------|------|-------|-----|----------------|-----|
| 1 | **136.125** | Athens ATIS | AM | 8.33k | Automated weather in English? | |
| 2 | **144.775** | Greek 2m Ham | NFM | 12.5k | Greek amateur voice? | |
| 3 | **148.440** | Military/Gov VHF | NFM | 12.5k | Greek military? Encrypted? Repeater? | |
| 4 | **150.490** | Military/Gov VHF | NFM | 12.5k | Same as above or different service? | |
| 5 | **152.540** | Business Radio | NFM | 12.5k | Commercial dispatch? Taxi? Security? | |
| 6 | **156.800** | Marine Ch16 | NFM | 25k | Coast guard? Securite calls? | |
| 7 | **156.650** | Marine Ch13 | NFM | 25k | Piraeus port? Bridge-to-bridge? | |

---

## 4. Baseline Mysteries

Flagged in the April 5 baseline report — strong, unidentified.

| # | Freq (MHz) | Power | Demod | BW | Hypothesis | Heard |
|---|-----------|-------|-------|-----|-----------|-------|
| 1 | **164.730** | -13.1 | NFM | 12.5k | Taxi / business dispatch (strong, bursty) | |
| 2 | **166.770** | -13.9 | NFM | 12.5k | Same service as 164.73? Related? | |
| 3 | **168.820** | -13.9 | NFM | 12.5k | EU 169 MHz business allocation | |
| 4 | **156.030** | -15.1 | NFM | 25k | Marine Ch1 — Piraeus port ops? | |
| 5 | **158.080** | -15.7 | NFM | 25k | Marine coast station (Piraeus Radio?) | |
| 6 | **160.130** | -15.9 | NFM | 25k | Coast station duplex TX freq | |
| 7 | **160.730** | -16.7 | NFM | 25k | Piraeus coast radio repeater? | |

---

## Reporting Template

After investigating, copy this for each signal:

```
Freq:     ___ MHz
Heard:    (voice / data bursts / tone / silence / digital noise)
Language: (Greek / English / N/A)
Content:  (what was said, or type of traffic)
Pattern:  (continuous / bursty / periodic / silence)
ID:       (proposed name)
Category: (gov / business / marine / airband / ham / unknown)
```
KNOWN

echo ""
echo "---"
echo "*Generated $(date -u '+%Y-%m-%d %H:%M UTC') | Run: $(echo "$RUN_INFO" | cut -f1)*"
