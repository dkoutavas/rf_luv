# Followup — calibrate detector thresholds from data, not from one event

**Opened**: 2026-04-21

## Context
Current signature thresholds in `detect_compression.py` are anchored to the single verified Apr-21 compression event:
- `MIN_SPUR_BLOCK_TILES = 10` (Apr 21 had 27)
- `SPUR_OFFSET_STDDEV_MAX_HZ = 30_000`
- `SPUR_OFFSET_MIN_ABS_HZ = 100_000`
- `SPUR_MIN_MEDIAN_POWER_DBFS = −15.0`
- `DEPRESSION_MIN_DB = 5.0` (Apr 21 at 4.8 dB just misses — intentionally)

Today's v2 fixes change the detector semantics (outlier-tolerant spur blocks, constrained emitter search). The thresholds above should be re-validated against the new semantics. Also: thresholds set from one training example are likely wrong in both directions — too tight for weaker real events, too loose for artifacts we haven't seen yet.

## Question
Can we set each threshold at a principled quantile of the observed distribution over non-anomalous sweeps, rather than by hand from a single event?

## Why it matters
- The Apr-21 event's 4.8 dB depression is right at the threshold. A principled calibration might pick 4.0 dB (p99.9 of non-anomalous) or 6.5 dB (p99.99). The *right* answer is determined by the noise distribution, not by whether we think the Apr-21 event "should" fire.
- False-positive rate becomes deterministic: "we expect ~1 sweep per quarter to accidentally fire each sub-signal alone" — a real SLO instead of guessing.

## Approach
1. **Gate on fix #1 first.** Re-run archaeology with the v2 detector to get new non-anomalous / compression splits. Old v1 counts are invalidated by the emitter-estimator fix and shouldn't feed calibration.
2. For each signature:
   - Compute the metric across all sweeps flagged `none` tier (after v2).
   - Plot the distribution (even just a histogram — 2,400 sweeps).
   - Pick a high quantile (p99.5 or p99.9) as the threshold.
3. Re-run detect_compression.py with the calibrated thresholds. Compare events to the current run.
4. Decision: the Apr-21 event *either* lands above or below the new threshold. Don't adjust to force it — document whatever the data says.

## Notes
- Per-signature independence matters: calibrate each sub-signal on its own distribution. Don't try to calibrate the combined tier.
- The spur-block-length threshold is the hardest because it's bimodal (normal sweeps have short runs at +26 kHz DC spike, compressed sweeps have long runs at other offsets). May need to carry per-offset-bucket stats.
- Do NOT bake this into `detect_compression.py` immediately. Write a one-shot calibration script in `spectrum/analysis/calibrate_thresholds.py` and have it print the proposed thresholds; then humans decide if they're reasonable.

## Expected outcome
A short analysis doc comparing current thresholds to quantile-calibrated ones. If the calibration suggests changes, a follow-up patch adjusts `detect_compression.py` constants.
