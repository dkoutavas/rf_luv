# Extended Dataset Analysis — 2026-04-10

RF spectrum scanner dataset analysis after 5 days of operation across multiple configurations.

## 1. Dataset Overview

| Metric | Value |
|--------|-------|
| Total rows | 1,349,870 |
| Total sweeps | 1,578 |
| Total runs | 14 |
| Earliest data | 2026-04-05 12:32 |
| Latest data | 2026-04-10 13:34 |
| Hours of data | 121 |

## 2. Run History

14 runs total, but data is concentrated in a few meaningful runs:

| Run | Started | Gain | Position | Height | Noise Floor | Peak | Sweeps | Notes |
|-----|---------|------|----------|--------|-------------|------|--------|-------|
| run_20260405_122805 | Apr 5 12:28 | 12 | rooftop_tripod | 4m | -48.5 | -24.2 | 5 | Pre-fix (dB-avg + warmup bugs) |
| run_20260409_155519 | Apr 9 15:55 | 12 | rooftop_tripod | 4m | -49.8 | -1.1 | 206 | **Main dataset** — post-fix, 24h+ |
| run_20260410_082608 | Apr 10 08:26 | 12 | rooftop_tripod | 4m | -50.8 | -3.4 | 24 | Morning continuation |
| run_20260410_101944 | Apr 10 10:19 | 12 | patio_window | 2m | -48.8 | 3.3 | 3 | Window position test |
| run_20260410_103346 | Apr 10 10:33 | 9 | patio_window | 2m | -48.9 | 2.9 | — | Gain 9 test (no improvement) |
| run_20260410_104644 | Apr 10 10:46 | 12 | patio_window | 2m | -48.8 | 3.3 | 20 | Back to gain 12, window |
| run_20260410_122907 | Apr 10 12:29 | 12 | rooftop_tripod | 3m | -48.7 | 5.6 | 11 | Rooftop 3m, clipping onset |
| run_20260410_131740 | Apr 10 13:17 | 12 | rooftop_tripod | 3m | -48.7 | 4.5 | 4 | Current run, clipping |

Runs not listed had <3 full sweeps (config-change restarts).

## 3. Signal Inventory — Current Best Configuration

From run_20260409_155519 (206 sweeps, rooftop tripod 4m, gain 12, post-fix). Noise floor reference: p10 at -50.8 dBFS in UHF quiet zone.

### Top 30 Signals by Average Power

| Freq (MHz) | Avg Power | Peak | SNR (dB) | Variability | Character | Likely ID |
|------------|-----------|------|----------|-------------|-----------|-----------|
| 187.15 | -10.1 | -3.8 | 40.7 | 3.93 | semi-stable | DVB-T Mux Hymettus |
| 185.11 | -10.6 | -3.5 | 40.2 | 4.61 | bursty | DVB-T Mux Hymettus |
| 189.20 | -11.0 | -3.1 | 39.8 | 6.78 | bursty | DVB-T Mux Hymettus |
| 192.50 | -11.5 | -3.9 | 39.3 | 3.65 | semi-stable | DVB-T Mux Hymettus |
| 179.76 | -12.0 | -7.0 | 38.8 | 4.09 | bursty | DVB-T Mux Hymettus |
| 177.71 | -12.4 | -7.1 | 38.4 | 5.65 | bursty | DVB-T Mux Hymettus |
| 146.39 | -13.5 | -6.9 | 37.3 | 3.92 | semi-stable | Military VHF 146.39 |
| 144.35 | -13.8 | -6.5 | 37.0 | 4.45 | bursty | Unidentified gov/military |
| 162.68 | -14.7 | -6.0 | 36.1 | 4.60 | bursty | Marine coast station |
| 148.44 | -14.9 | -7.1 | 35.9 | 5.76 | bursty | Military/Gov VHF |
| 160.63 | -15.1 | -6.4 | 35.7 | 5.68 | bursty | Marine coast station |
| 146.09 | -15.2 | -8.9 | 35.6 | 4.11 | bursty | Military VHF |
| 164.73 | -15.9 | -7.1 | 34.9 | 7.09 | bursty | Land mobile / port |
| 161.78 | -16.0 | -8.9 | 34.8 | 4.15 | bursty | Marine VHF |
| 163.28 | -17.9 | -8.6 | 32.9 | 5.47 | bursty | Land mobile / port |
| 161.23 | -18.0 | -7.4 | 32.8 | 5.46 | bursty | Marine VHF |

**Key observations:**
- DVB-T dominates the top 10 — the Hymettus multiplexes are by far the strongest signals
- Military VHF at 146.39/148.44 MHz is the strongest non-broadcast signal (-13.5 dBFS avg)
- Marine VHF cluster (159-163 MHz) shows multiple active channels
- Land mobile 163-165 MHz shows bursty activity — likely Piraeus port operations
- Nearly all non-broadcast signals are "bursty" — indicating intermittent voice traffic

## 4. Diurnal Patterns

From the 206-sweep run covering hours 0-8 and 15-23 (gap during 9-14 between runs).

| Band | Night (0-5) Avg | Day (15-21) Avg | Delta | Notes |
|------|-----------------|-----------------|-------|-------|
| Airband (118-137) | -37.8 | -37.3 | +0.5 | Essentially flat — ATC is 24/7 |
| Military VHF (146-153) | -34.4 | -34.6 | -0.2 | No day/night pattern |
| Marine VHF (156-163) | -33.0 | -33.4 | -0.4 | Slightly stronger at night |
| Land Mobile (164-170) | -35.5 | -36.1 | -0.6 | Marginally stronger at night |
| TETRA (380-400) | -49.0 | -48.9 | +0.1 | At noise floor, no variation |

**Findings:**
- No significant diurnal variation in any band — delta is within measurement noise (1-2 dB)
- Athens airport operates 24/7, explaining flat airband
- Marine traffic (Piraeus/Saronic) shows slight nighttime increase — possibly fishing fleet
- TETRA is effectively at noise floor from this antenna position

## 5. Cross-Run Band Evolution

How each band performed across configurations:

### Airband (118-137 MHz)

| Run | Position | Gain | Avg | Peak | NF | Sweeps |
|-----|----------|------|-----|------|----|--------|
| Apr 5 (pre-fix) | rooftop 4m | 12 | -33.5 | -24.4 | -38.0 | 5 |
| Apr 9 (post-fix) | rooftop 4m | 12 | -37.7 | -7.5 | -48.6 | 206 |
| Apr 10 AM | rooftop 4m | 12 | -38.1 | -10.4 | -48.9 | 24 |
| Apr 10 window | patio 2m | 12 | -24.9 | -0.3 | -44.7 | 20 |
| Apr 10 rooftop 3m | rooftop 3m | 12 | -15.9 | 2.3 | -35.0 | 11 |

### VHF (137-174 MHz)

| Run | Position | Avg | Peak | NF |
|-----|----------|-----|------|----|
| Apr 9 (post-fix) | rooftop 4m | -35.0 | -6.0 | -48.1 |
| Apr 10 window | patio 2m | -23.5 | 4.7 | -43.1 |
| Apr 10 rooftop 3m | rooftop 3m | -13.5 | 5.8 | -24.8 |

### UHF (380-470 MHz)

| Run | Position | Avg | Peak | NF |
|-----|----------|-----|------|----|
| Apr 9 (post-fix) | rooftop 4m | -49.3 | -40.4 | -50.7 |
| Apr 10 window | patio 2m | -47.3 | -30.1 | -48.8 |
| Apr 10 rooftop 3m | rooftop 3m | -46.4 | -23.5 | -47.9 |

**Key insight:** The recent rooftop 3m runs show dramatically elevated readings compared to the stable Apr 9 run. The VHF noise floor went from -48.1 to -24.8 — this is the broadband clipping event, not a real signal improvement. The Apr 9 run at 4m height is the most reliable dataset.

## 6. Measurement Accuracy — Pre-Fix vs Post-Fix

UHF quiet zone (400-470 MHz) noise statistics:

| Run | Position | Gain | Noise Floor (p10) | Noise Std Dev | Sweeps |
|-----|----------|------|-------------------|---------------|--------|
| Apr 5 (pre-fix) | rooftop 4m | 12 | **-46.8** | **1.815** | 5 |
| Apr 9 (post-fix) | rooftop 4m | 12 | **-50.8** | **1.052** | 206 |
| Apr 10 AM | rooftop 4m | 12 | -50.8 | 0.961 | 24 |
| Apr 10 window | patio 2m | 12 | -48.9 | 1.042 | 3 |
| Apr 10 window | patio 2m | 12 | -48.9 | 1.098 | 20 |
| Apr 10 rooftop 3m | rooftop 3m | 12 | -47.9 | 1.575 | 11 |

**Bug fix impact:**
- Noise floor improved by **4.0 dB** (from -46.8 to -50.8)
- Noise variability reduced by **42%** (from 1.815 to 1.052 stddev)
- These improvements came from fixing the dB-averaging bug (was averaging linear power instead of dB values) and the PLL warmup bug (first samples after retune were noisy)

## 7. UHF Deep Dive

No signals above -40 dBFS detected in the 230-470 MHz range in the 206-sweep run. The UHF band is effectively at noise floor (-50.7 dBFS) from the rooftop position with this antenna.

**Why:**
- The dipole arms (57 cm) are tuned for VHF (~130 MHz quarter-wave). At UHF frequencies (400+ MHz), the antenna is severely mismatched
- TETRA (380-400 MHz) is digital and encrypted — bursts exist but average power stays near noise floor
- PMR446 / ISM433 require very close proximity to transmitters
- The thick stone walls block UHF from indoor sources

**To improve UHF reception:** A dedicated UHF antenna (quarter-wave arms ~18 cm for TETRA, ~7 cm for ISM) or a discone antenna would be needed.

## 8. Unidentified Signals

Strong signals not matching known_frequencies entries (from the 206-sweep run):

| Freq (MHz) | Avg Power | Character | Likely Category |
|------------|-----------|-----------|-----------------|
| 144.35 | -13.8 | bursty | Gov/military VHF — persistent repeater |
| 144.05 | -15.5 | bursty | Ham 2m band or gov overlap |
| 142.30 | -16.1 | bursty | Gov/military VHF |
| 143.45 | -17.6 | bursty | Gov/military VHF |
| 145.49 | -17.4 | bursty | Ham 2m / military |
| 149.24 | -17.9 | bursty | Military/gov VHF |
| 147.19 | -17.8 | semi-stable | Military/gov VHF |
| 162.68 | -14.7 | bursty | Marine coast station (unlogged channel) |
| 160.63 | -15.1 | bursty | Marine coast station (unlogged channel) |
| 159.73 | -16.1 | bursty | Marine VHF (unlogged channel) |
| 164.73 | -15.9 | bursty | Land mobile — port operations? |
| 163.83 | -17.5 | bursty | Land mobile — port operations? |

**Recommendations:**
- The 142-149 MHz cluster has multiple strong unidentified signals. These are likely Greek military or government VHF repeaters. Tune with NFM to characterize.
- The 159-163 MHz unlogged marine channels should be added to known_frequencies once identified via listening sessions.
- The 163-165 MHz land mobile signals are consistent with Piraeus port dispatch — confirm via listening.

## 9. Key Findings and Recommendations

### What we learned
1. **The bug fixes made a real difference:** 4 dB better noise floor, 42% less measurement noise. The pre-fix data is unreliable for absolute comparisons.
2. **DVB-T is the dominant signal source** and the primary cause of ADC stress. An FM notch filter would help (FM is excluded but still hits the ADC), but a DVB-T bandstop would have more impact.
3. **No meaningful diurnal variation** in any band — Athens RF environment is essentially 24/7 constant. This means time-of-day isn't a factor for listening sessions.
4. **UHF is invisible** with this antenna. The VHF dipole at 57 cm is simply the wrong antenna for 380-470 MHz.
5. **The 140-150 MHz military/gov band** is the richest source of unidentified signals — multiple active repeaters worth investigating with SDR++.
6. **Broadband clipping events** began around 12:15 on Apr 10 and persist. Likely caused by increased DVB-T transmitter power or atmospheric conditions.

### Next steps
- **FM notch filter** (88-108 MHz bandstop) — would reduce ADC load and may eliminate clipping
- **Add unidentified marine channels** to known_frequencies after listening session confirmation
- **Investigate 142-149 MHz cluster** — tune each frequency with NFM in SDR++
- **Consider a 24h+ run with current rooftop config** once clipping is resolved, to get a complete diurnal dataset
- **UHF requires a dedicated antenna** — discone or dedicated quarter-wave for TETRA/ISM if those bands are of interest
