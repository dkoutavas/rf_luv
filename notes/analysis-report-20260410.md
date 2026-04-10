# RF Weather Station Analysis Report — 2026-04-10

## Summary

5-day dataset (April 5-10), 1.1M rows, 1,299 sweeps across 4 scanner runs.
Scanner on Dell Inspiron 7537, antenna on patio window exterior (~2m), gain 12 dB.

---

## 1. Bug Status

### Bug 1: dB-domain averaging — FIXED

**Problem:** `compute_power_spectrum()` returned dB values, then `sweep()` averaged
those dB values. `downsample_bins()` also averaged in dB. Jensen's inequality
means this underestimates bursty signals by 0.5-3 dB.

**Fix applied to scanner.py:**
- Split `compute_power_spectrum()` into `compute_linear_power()` + `linear_to_db()`
- `sweep()` now accumulates LINEAR power, converts to dB after averaging
- `downsample_bins()` now converts dB→linear, averages, converts back

**Expected impact:** Noise floor shifts up ~0.5-1 dB. Strong signals negligible change.
New run will capture the discontinuity via run tracking.

### Bug 2: Post-full-sweep airband warmup — FIXED

**Problem:** 65,536 bytes warmup (~16ms) insufficient for PLL settle after jumping
from 470 MHz (end of full sweep) to 118 MHz (airband start). First airband sweep
after each full sweep showed ~10 dB suppression.

**Fix applied to scanner.py:**
- Added throwaway tune step: tunes to sweep start frequency before discarding
- Increased warmup to 131,072 bytes (~32ms) + 10ms sleep = ~42ms total settle
- Airband panel description updated to remove PLL bug caveat

---

## 2. Accuracy Validation

### 2.1 ATIS Stability (136.125 MHz)

Reference signal — always-on AM weather broadcast from Athens airport.

| Metric | Value | Assessment |
|--------|-------|------------|
| Hourly avg range | -32.4 to -39.3 dBFS | Moderate variation |
| Peak range | -15.3 to -26.8 dBFS | Consistent detection |
| Typical stddev | 8-10 dB | Expected for AM modulation |
| Hours with data | 19/19 (100%) | No gaps |

The ~7 dB spread in hourly averages is partly from AM modulation envelope (ATIS
transmits in bursts of weather info then pauses), partly from multi-bin averaging.
Peak power is consistent at -15 to -27 dBFS, confirming the signal is reliably
present. After the dB-averaging fix, averages should tighten slightly.

### 2.2 Noise Floor Consistency (400-470 MHz UHF)

| Metric | Value | Assessment |
|--------|-------|------------|
| P10 range | -50.4 to -51.0 dBFS | Rock solid |
| Median range | -48.8 to -49.4 dBFS | Excellent stability |
| Drift | < 0.6 dB over 18 hours | No thermal drift |

The noise floor is exceptionally stable. The Dell's thermal environment is not
causing gain drift. P10 = -50.8 dBFS is the effective noise floor (slightly higher
than the -52.8 reported from April 5, likely due to the dB-averaging bug inflating
the previous measurement).

### 2.3 Sweep Timing Regularity

| Preset | Avg Duration | Stddev | Min | Max | Count |
|--------|-------------|--------|-----|-----|-------|
| full | 2253.2 ms | 0.9 ms | 2251 | 2260 | 223 |
| airband | 134.3 ms | 1.1 ms | 124 | 142 | 1053 |

Sub-millisecond jitter on full sweeps. The Dell handles the workload with zero
contention. USB throughput is stable, no sample drops.

---

## 3. Temporal Patterns

### 3.1 Diurnal Activity

| Signal Group | Day Avg (dBFS) | Night Avg (dBFS) | Pattern |
|-------------|---------------|-----------------|---------|
| ATC Tower/Approach | -34.5 to -36.1 | -35.1 to -35.9 | Flat — 24/7 airport |
| Marine VHF | -32.3 to -34.6 | -32.4 to -33.2 | Flat — 24/7 port |
| Military/Gov VHF | -33.4 to -35.5 | -33.8 to -34.7 | Flat — continuous carriers |
| Unknown 165-169 | -34.8 to -37.4 | -35.2 to -36.4 | Flat — 24/7 infrastructure |

Key finding: **All four signal groups operate 24/7 with no meaningful diurnal
variation.** Athens airport and Piraeus port run around the clock. The military VHF
repeaters are continuous carriers. The unknown 165-169 cluster matches this
24/7 infrastructure pattern — ruling out business-hours-only dispatch.

### 3.2 Signal Persistence Classification

From 224 full sweeps over 72 hours:

**always_on (100% presence):** 150+ frequency bins
- DVB-T multiplexes (177-200 MHz) — dominant, -10 to -22 dBFS
- Military/Gov VHF repeaters (146.4, 148.1, 148.4 MHz) — -13 to -17 dBFS
- Marine coast stations (159-163 MHz) — -15 to -21 dBFS
- Unknown cluster (164.7, 165.3, 168.9 MHz) — -16 to -21 dBFS
- Ham/business (144.3, 152.5 MHz) — -14 to -18 dBFS

**frequent (>50%):** Marine channels, ATIS, additional VHF
**intermittent (10-50%):** ATC approach/tower (bursty AM voice)
**rare (<10%):** Transient signals in various bands

### 3.3 Burst Detection

Top burst signals (peak - avg power):

| Freq (MHz) | Burst Amplitude | Character | Likely Source |
|-----------|----------------|-----------|---------------|
| 199.9 | 37.9 dB | extreme_burst | DVB-T edge/SFN artifact |
| 173.3 | 33.9 dB | extreme_burst | DVB-T Band III edge |
| 131.8 | 32.8 dB | extreme_burst | Airband (approach freq?) |
| 169.1 | 32.5 dB | extreme_burst | Unknown cluster burst |
| 140.6 | 32.0 dB | extreme_burst | VHF — unidentified |

Burst amplitudes >30 dB are remarkable. The dB-averaging bug was compressing
these — after the fix, burst detection should improve by 1-3 dB.

### 3.4 Cross-Signal Correlation

| Pair | Correlation | Interpretation |
|------|------------|----------------|
| Unknown↔Marine | **0.387** | Moderate positive — likely related |
| Unknown↔Military | 0.063 | None |
| Unknown↔ATC | -0.087 | None |
| ATC↔Marine | -0.058 | None |
| ATC↔Military | 0.030 | None |
| Marine↔Military | -0.011 | None |

**The unknown 165-169 cluster correlates significantly only with marine VHF.**
All other pairs show no correlation. This strongly suggests the unknown signals
are port/maritime-adjacent infrastructure.

---

## 4. Signal Discovery

### 4.1 Rare Events (Spikes >15 dB above average)

| Freq (MHz) | Avg Power | Peak Power | Spike (dB) | Notes |
|-----------|-----------|-----------|-----------|-------|
| 148.6 | -47.3 | -9.9 | 37.4 | Military VHF burst |
| 168.4 | -47.7 | -13.2 | 34.5 | Unknown cluster burst |
| 173.3 | -41.4 | -7.5 | 33.9 | Band III edge |
| 144.5 | -49.1 | -15.6 | 33.5 | Ham/military? |
| 140.6 | -47.0 | -14.0 | 33.0 | Unidentified VHF |

All spike signals are in the VHF range (125-175 MHz). No rare spikes detected
in UHF, confirming the 380-470 MHz range is essentially dead from this location.

### 4.2 Transient Events (Top by count)

| Freq (MHz) | Events | Avg Delta | Type |
|-----------|--------|-----------|------|
| 167.07 | 101 (51 dis + 50 app) | 24 dB | Unknown cluster — most active |
| 158.58 | 89 | 22.7 dB | Marine coast station area |
| 166.87 | 87 | 22.0 dB | Unknown cluster |
| 166.37 | 85 | 24.0 dB | Unknown cluster |
| 175.37 | 82 | 21.5 dB | Band III edge |
| 198.64 | 73 | 31.9 dB | DVB-T edge — extreme swings |

The 166-167 MHz range dominates transient activity. These frequencies turn on
and off constantly with 20-37 dB swings — consistent with voice or data bursts
from a dispatch system.

---

## 5. Unknown Cluster Analysis (164.73 / 166.77 / 168.82 MHz)

### Evidence Summary

| Property | Value | Interpretation |
|---------|-------|----------------|
| Spacing | ~2.05 MHz even intervals | Channelized system |
| Activity | 24/7, no diurnal pattern | Infrastructure, not business |
| Character | Highly bursty (20-37 dB swings) | Voice or data dispatch |
| Correlation | 0.387 with Marine VHF | Port/maritime related |
| Persistence | always_on (100%) at some bins | Carrier + bursty overlay |
| Location | 164-169 MHz VHF band | Greek land mobile allocation |

### Likely Identity

**Piraeus Port Authority or maritime logistics dispatch.**

The 164-169 MHz range in Greece is allocated to land mobile services, including
port authorities and maritime support operations. The evidence points to this:

1. **24/7 operation** matches port/harbor operations (Piraeus is Greece's busiest port)
2. **Correlation with marine VHF** (0.387) suggests these activate when ships
   communicate on marine channels — consistent with port coordination
3. **Even 2.05 MHz spacing** suggests a channelized dispatch system with multiple
   talk groups (operations, security, logistics)
4. **Bursty nature** (167.07 MHz has 101 transient events in 72h) matches voice
   dispatch — keying up a radio, talking, releasing

To confirm: tune SDR++ to 166.77 MHz in NFM mode and listen. If you hear Greek
voice traffic discussing port operations, that's confirmation.

---

## 6. Infrastructure Status

### Scanner Pipeline
- Dell box running 24/7, stable for 5 days
- No sample drops, no USB errors
- Sweep timing jitter < 1ms
- Zero clipped sweeps at gain 12 in current run

### ClickHouse Remote Access
- **Already accessible** from laptop at `http://192.168.2.10:8126`
- No firewall changes needed
- Query directly: `curl "http://192.168.2.10:8126/?user=spectrum&password=spectrum_local" --data-binary "QUERY"`
- Or use Python: `clickhouse_driver.Client('192.168.2.10', port=9000, user='spectrum', password='spectrum_local')`

### Known Frequencies Updated
- Added 6 marine/military entries (Ch1, coast stations, VHF repeaters)
- Replaced 3 broad DVB-T entries with 5 specific multiplex channels
- Total: 32 known frequencies (was 27)

### Dashboard Improvements
- Power spectrum: fixed color from gradient rainbow to solid dark-blue
- Airband panel: removed PLL bug caveat
- 3 new panels: Signal Persistence, Burst Event Summary, 24-Hour Activity Pattern

---

## 7. Recommendations

### Priority 1: Deploy and validate bug fixes
Rebuild the scanner container on the Dell and restart. Wait for 2 full sweeps,
then compare noise floor and ATIS readings to pre-fix values. The run tracking
system will capture the discontinuity automatically.

```bash
cd ~/rf_luv/spectrum
docker compose build spectrum-scanner
docker compose up -d spectrum-scanner
docker compose restart grafana-spectrum
```

### Priority 2: Confirm unknown cluster identity
Tune SDR++ to 166.77 MHz NFM and listen. If it's port dispatch, add these
frequencies to known_frequencies with proper names. The 2.05 MHz channel spacing
and correlation with marine traffic strongly suggest Piraeus Port Authority.

### Priority 3: Let data accumulate post-fix
After deploying the linear averaging fix, let the scanner run for 48+ hours
before re-running this analysis. The dB-averaging bug compressed bursty signals
by 0.5-3 dB. Post-fix data will show:
- Slightly higher noise floor (~0.5-1 dB)
- Better burst detection sensitivity
- More accurate ATIS power readings
- Potentially new signals emerging from the improved dynamic range

### Priority 4: HF exploration
The VHF/UHF spectrum is well-characterized now. Consider:
- Adding a second RTL-SDR for HF direct sampling (3-30 MHz)
- Long wire antenna from patio for shortwave reception
- UVB-76 monitoring at 4.625 MHz (best after sunset)

### Priority 5: Add DVB-T individual channel monitoring
The DVB-T multiplexes (177-206 MHz) are the strongest signals in the environment.
Individual channel signal strength tracking could detect propagation changes or
transmitter issues on Hymettus.
