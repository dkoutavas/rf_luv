# Spectrum Scanner — Baseline Analysis & Pipeline Validation Report

**Date:** 2026-04-05
**Data window:** 06:35 – 07:45 UTC (~1h10m)
**Config:** 57cm dipole arms (patio, outdoor), gain=20 dB, 88-470 MHz, 100 kHz bins
**Sweeps:** 13 full, 63 airband | **Total rows:** 63,011

---

## Pipeline Health Summary

| Check | Status | Key Number |
|-------|--------|-----------|
| Bin count consistency | PASS (perfect) | 3917 full / 195 airband, zero variation |
| Frequency coverage | PASS | 88.05-469.978 MHz, 97.5 kHz spacing |
| Power distribution | PASS | Noise floor -50 dBFS, peaks to -5.4, no clipping |
| Sweep timing | PASS | 2251 +/- 1ms full, 133 +/- 2ms airband |
| Sweep-to-sweep stability | PASS | 1.9-2.5 dB stddev on stable carriers |
| DC spike check | PASS | No tuning-center artifacts |
| Edge bin check | PASS | Smooth, no stale buffer spikes |
| Post-full airband | BUG | First airband after full: ~10 dB suppression (100%) |

**Overall confidence: HIGH** — measurements are fundamentally sound.

---

## Bugs Found

### 1. dB-domain averaging (scanner.py)
Both FFT averaging (8 captures) and bin downsampling (50 FFT bins -> 1 output bin) average dB values instead of linear power. Underestimates by ~0.5-1 dB near noise floor.

### 2. Post-full-sweep suppression (scanner.py)
65536-byte warmup discard insufficient for PLL settling after 470->118 MHz band change. First airband sweep shows -47 dBFS vs normal -34 dBFS.

---

## RF Environment Baseline

### Noise Floor
- Q25: -50.9 dBFS (UHF 400-470 MHz band)
- Median: -50.3 dBFS
- Spread (Q25-Q75): 1.2 dB

### Band Summary

| Band | Avg (dBFS) | Peak (dBFS) | P10 | P90 | Variability |
|------|-----------|-------------|-----|-----|-------------|
| FM 88-108 | -40.8 | -33.3 | -45.0 | -36.2 | 4.09 |
| Airband 108-137 | -35.2 | -14.3 | -51.6 | -24.3 | 9.16 |
| VHF Gov 137-156 | -29.6 | -7.3 | -36.1 | -19.5 | 6.72 |
| Marine 156-174 | -29.0 | -7.9 | -36.9 | -18.6 | 7.18 |
| DVB-T 174-230 | -33.5 | -5.4 | -41.2 | -21.1 | 8.02 |
| Mid UHF 230-380 | -44.2 | -33.7 | -49.9 | -38.2 | 4.38 |
| TETRA 380-400 | -50.4 | -47.4 | -51.8 | -49.1 | 1.00 |
| ISM/PMR 400-470 | -50.3 | -46.2 | -51.8 | -49.1 | 1.01 |

### Strongest Signals

| Freq MHz | Avg dBFS | Type | ID |
|----------|---------|------|-----|
| 148.44 | -10.8 | semi-stable | Military/Gov VHF repeater |
| 146.39 | -11.6 | bursty | Military/Gov VHF |
| 150.49 | -11.8 | semi-stable | Military/Gov VHF repeater |
| 152.54 | -12.2 | bursty | Business Radio repeater |
| 195.35 | -12.5 | bursty | DVB-T (Hymettus) |
| 182.11 | -12.7 | semi-stable | DVB-T |
| 191.25 | -12.9 | semi-stable | DVB-T |
| 186.21 | -13.0 | semi-stable | DVB-T |
| 193.30 | -13.0 | bursty | DVB-T |
| 164.73 | -13.1 | bursty | Unknown — business/taxi? |
| 202.74 | -13.2 | semi-stable | DVB-T Band III |
| 166.77 | -13.9 | bursty | Unknown — VHF business |
| 168.82 | -13.9 | bursty | Unknown — near DAB band |

### Airband Top Catches (60s resolution)

| Freq MHz | Avg | Peak | Burst Amp | Likely ID |
|----------|-----|------|-----------|-----------|
| 124.394 | -41.4 | -1.8 | 39.6 dB | Athens TMA / Military approach |
| 128.490 | -41.2 | -1.9 | 39.3 dB | Athens ACC sector |
| 132.586 | -39.6 | -1.9 | 37.7 dB | ACC or military |
| 129.090 | -40.5 | -4.5 | 36.0 dB | Active ATC frequency |
| 118.850 | -40.8 | -4.8 | 36.0 dB | Near Athens Tower (118.1) |

### Unknown Signals to Investigate

| Freq MHz | Avg dBFS | Notes |
|----------|---------|-------|
| 164.73 | -13.1 | Strong bursty — taxi/business dispatch? |
| 166.77 | -13.9 | VHF business band |
| 168.82 | -13.9 | Near 169 MHz business allocation |
| 156.03 | -15.1 | Marine Ch1 (port ops) — add to known_freqs |
| 158.08 | -15.7 | Marine coast station repeater |
| 160.13 | -15.9 | Marine coast station duplex TX |
| 160.73 | -16.7 | Piraeus coast radio |

---

## Sweep Health

| Metric | Full Sweep | Airband Sweep |
|--------|-----------|---------------|
| Count | 13 | 63 |
| Duration | 2,251 +/- 1 ms | 133 +/- 2 ms |
| Interval | ~283s (cfg: 280) | ~67s (cfg: 60) |
| Max power | -5.4 to -9.5 | -1.8 to -48.4 |
| Clipping | None | None |
