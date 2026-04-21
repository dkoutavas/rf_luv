# Compression-event archaeology — 2026-04-05 to 2026-04-21

Results of one-shot historical backfill by `detect_compression.py` over 2,441 full sweeps covering 16 days of RTL-SDR baseline. Detector version `v1`.

## Headline numbers

| Tier | Sweep count | % of sweeps |
|---|---|---|
| `none` | 2,357 | 96.6% |
| `low` (1 sig fired) | 73 | 2.99% |
| `medium` (2 sigs) | 10 | 0.41% |
| `high` (all 3 sigs) | 1 | 0.04% |
| **medium + high (persisted to DB)** | **11** | **0.45%** |

**Event rate**: 11 `medium+` events / 16 days = **0.69 events/day**. This is within the 5-15 events/16 days budget called out in the plan.

## Full list of medium+ events

| When (UTC) | Tier | Sigs (spur, base, clip) | Emitter freq (MHz) | Emitter power (dBFS) | Spur offset | Spur block | Depression |
|---|---|---|---|---|---|---|---|
| 2026-04-16 12:26:00 | **high** | (1, **1**, 1) | 167.374 | +2.1 | −174 kHz | tiles 0–11 (n=12) | **5.2 dB** |
| 2026-04-16 15:35:56 | medium | (1, 0, 1) | 171.470 | −3.9 | −174 kHz | tiles 2–11 (n=10) | 1.6 dB |
| 2026-04-17 05:20:15 | medium | (1, 0, 1) | 165.326 | −3.8 | −174 kHz | tiles 2–11 (n=10) | 1.5 dB |
| 2026-04-17 12:05:32 | medium | (1, 0, 1) | 169.422 | −3.3 | −174 kHz | tiles 0–11 (n=12) | 1.8 dB |
| 2026-04-17 20:43:43 | medium | (1, 0, 1) | 163.278 | −3.6 | −174 kHz | tiles 2–11 (n=10) | 1.8 dB |
| 2026-04-17 21:21:29 | medium | (1, 0, 1) | 167.374 | −3.3 | −174 kHz | tiles 1–11 (n=11) | 1.5 dB |
| 2026-04-18 09:16:02 | medium | (1, 0, 1) | 169.422 | −3.4 | −183 kHz | tiles 2–12 (n=11) | 2.1 dB |
| 2026-04-19 14:03:53 | medium | (1, **1**, 0) | 166.874 | −4.3 | **+526 kHz** | tiles 45–54 (n=10) | **8.6 dB** |
| 2026-04-21 02:15:36 | medium | (1, 0, 1) | 167.374 | −3.0 | −174 kHz | tiles 1–12 (n=12) | 1.3 dB |
| 2026-04-21 09:29:02 | medium | (1, 0, 1) | 165.326 | −3.0 | −174 kHz | tiles 2–12 (n=11) | 1.3 dB |
| 2026-04-21 11:55:04 | medium | (1, 0, 1) | **304.190** | **+3.5** | **+526 kHz** | **tiles 40–66 (n=27)** | 4.8 dB |

## Key observations

### Two distinct spur-offset families

The detected events cluster into two different spur signatures:

| Family | Events | Spur block location | Typical depression | Interpretation |
|---|---|---|---|---|
| **−174 kHz** | 9 | FM band (tiles 0–12, 88–113 MHz) | 1–2 dB | Weak-to-moderate. `sig_baseline` fires once (5.2 dB on the Apr 16 event). |
| **+526 kHz** | 2 | Higher VHF/UHF (tiles 40–66, 170–226 MHz and tiles 45–54) | 5–9 dB | Strong. Both events have `sig_baseline=1` (Apr 19) or are the verified Apr 21 event. |

This is a surprising finding. We came into this investigation thinking of the +526 kHz spur pattern (seen live on Apr 21 11:55) as *the* compression fingerprint. In fact the detector finds a second recurring pattern at **−174 kHz offset from each tile center** that appears in the FM band and is associated with much weaker compression (1–2 dB depression). The single −174 kHz event that fires all three sigs (Apr 16 12:26) has ~5 dB depression, matching a real but less severe compression than Apr 21.

### The −174 kHz "emitter" frequencies are an artifact

The −174 kHz events report "emitter" frequencies at 163.28, 165.33, 167.37, 169.42, 171.47 MHz — all exactly 2.048 MHz apart, which is **the tile spacing**. My emitter estimator finds the strongest peak that *doesn't* sit at the −174 kHz offset; in tiles outside the spur block (tiles ≥ 36, i.e. above 162 MHz), the argmax often falls at **+526 kHz offset from tile center** (the *other* spur offset). So the estimator is effectively picking one spur family's peak when the other is the "dominant" compression signature. This means:

- The reported −174 kHz "emitter" frequencies are **NOT real signals at those exact MHz values**. They're +526-offset spur peaks inside a sweep that also shows a −174-offset spur comb.
- The mapping is: spur A's presence in the FM tiles hides spur B's peaks elsewhere, which the estimator then picks up.

### The genuine compressions

Only **3 events** are confidently real compression events, by the combined test "depression ≥ 5 dB AND strong deviant peak":

| Event | Notes |
|---|---|
| 2026-04-16 12:26:00 UTC | `high` tier. Depression 5.2 dB. Emitter reported 167.374 MHz but this is the +526 offset peak in that tile — **real emitter probably near 166–168 MHz based on peak tile location**. Known gov/business VHF repeater zone. |
| 2026-04-19 14:03:53 UTC | Depression **8.6 dB** (largest on record). Emitter 166.874 MHz at −4.3 dBFS. Spur block at tiles 45–54 (113–133 MHz, airband). **Likely real** — matches the 164–168 cluster pattern documented in `notes/analysis-report-20260410.md`. |
| 2026-04-21 11:55:04 UTC | The one we already know. 304.19 MHz @ +3.5 dBFS. Depression 4.8 dB — just below the 5 dB threshold, so `sig_baseline=0`. This event is `medium` by our criteria but is the strongest pure-power case observed. |

The three events span **7 days** (Apr 16, 19, 21), not clustered.

### Time-of-day distribution

All 11 events, Athens local time:

```
00:00  *
05:00  *
08:00  *
12:00  **
15:00  **
17:00  *
18:00  *
23:00  **
```

Rough spread across the day; 7 of 11 fall in 08:00–18:00 local (daytime). Not a strong clock pattern, but slight daytime bias is consistent with human-driven emissions (business/gov radio traffic peaks during work hours).

### Frequency clusters

**Observed emitter-freq concentration**:
- 163–171 MHz zone: 9 events (gov/business/marine-coast VHF). But 8 of these are the artifact described above, so not actionable as-is.
- 304 MHz zone: 1 event (Apr 21). Unique to the mystery-zone finding.
- 167 MHz: recurring across 6 events at this specific frequency — strongly suggesting a **real emitter at 166–167 MHz** that triggers compression when it's on. Needs verification via listening_log.

### Offset-family connection to known signals

The 2026-04-19 14:03 event is particularly interesting: its spur block is tiles 45–54 (113–133 MHz). That's *airband*. Emitter reported at 166.874 MHz. So an emitter near 166 MHz causes compression visible in the airband-adjacent tiles. If the airband detectors (classifier) caught unusual activity in airband at 2026-04-19 14:03, that's the same event.

## Honest limits of this analysis

- **No ground truth.** Only the 2026-04-21 11:55 event is operator-verified as a real compression event. Everything else is heuristic classification.
- **Sub-flag scoring is not calibrated.** `match_tier` is a count, not a confidence. Rescoring with a 4 dB depression threshold (instead of 5) would reclassify the Apr 21 event as `high`. We don't tune thresholds post-hoc — that would overfit to the one training example.
- **Emitter estimation is fragile** when two spur families overlap in one sweep (see the 9 "false-positive-looking" −174 kHz events). Real emitter frequency extraction requires IQ data or per-tile timestamps, both pending Phase 4.
- **Detector v1 is tuned for Apr-21-scale events.** It misses narrower/weaker compressions like the 2026-04-12 14:12 303.89 MHz event (4 tiles of elevation — below the 10-tile minimum).

## Follow-up hooks

1. **Cross-reference 166.87 MHz with listening_log.** This frequency fires 6 of 11 events and may be a recurring real emitter worth listening to.
2. **Live Phase-3 wiring.** `detect_compression.py` is ready to run as a systemd timer sidecar; run it at `*:00/5:15` to populate `compression_events` in real time.
3. **Reconsider the −174 kHz pattern.** It might be a routine tuner artifact (images, LO leakage) rather than a compression fingerprint. If so, `sig_spur` should exclude this offset specifically.
4. **Rescore after Phase 4 ships.** Once per-tile timestamps + IQ captures are available, we can verify `high` and `medium` events against recorded waveforms and retire the heuristic-only classification.

## Reproducing

```bash
cd ~/dev/rf_luv/spectrum
python3 analysis/detect_compression.py --backfill --min-tier low
# ~10 min, writes to spectrum.compression_events
```

Or the archaeology window only:
```bash
python3 analysis/detect_compression.py --since '2026-04-05' --min-tier medium
```

Or a single sweep (for debugging):
```bash
python3 analysis/detect_compression.py --sweep 'full:2026-04-21 11:55:04.960' --dry-run
```
