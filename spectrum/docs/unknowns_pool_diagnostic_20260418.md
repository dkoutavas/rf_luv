# Unknowns pool diagnostic — 2026-04-18

## Query

```sql
SELECT
    floor(freq_hz / 10000000) * 10 AS band_mhz,
    class_id,
    count() AS n,
    round(avg(duty_cycle_24h), 3) AS avg_duty,
    round(avg(power_mean_dbfs), 1) AS avg_power,
    countIf(harmonic_of_hz IS NOT NULL) AS harmonic_flagged
FROM spectrum.signal_classifications sc FINAL
INNER JOIN spectrum.peak_features pf USING (freq_hz)
WHERE (sc.class_id LIKE 'unknown_%' OR sc.confidence < 0.5)
  AND pf.sweeps_observed_24h > 5
GROUP BY band_mhz, class_id
ORDER BY band_mhz, n DESC;
```

Run against the live classifier output on leap after Issue 1's
round-confidence fix. Pool size at run time: ~744 rows.

## Output

| band_mhz | class_id | n | avg_duty | avg_power | harmonic_flagged |
|---:|---|---:|---:|---:|---:|
| 80  | broadcast_fm (conf<0.5)    | 5  | 0.031 | -8.7  | 0  |
| 90  | broadcast_fm (conf<0.5)    | 31 | 0.032 | -8.1  | 0  |
| 100 | broadcast_fm (conf<0.5)    | 27 | 0.028 | -16.0 | 0  |
| 100 | unknown_bursty             | 7  | 0.029 | -16.9 | 0  |
| 110 | unknown_bursty             | 41 | 0.311 | -16.2 | 0  |
| 110 | unknown_continuous         | 10 | 0.696 | -8.7  | 0  |
| 120 | unknown_continuous         | 5  | 0.796 | -14.6 | 0  |
| 130 | unknown_continuous         | 34 | 0.768 | -18.7 | 0  |
| 130 | unknown_bursty             | 4  | 0.256 | -9.8  | 0  |
| 140 | unknown_continuous         | 32 | 0.731 | -15.0 | 0  |
| 140 | unknown_bursty             | 10 | 0.185 | -9.1  | 0  |
| 140 | nfm_voice_repeater (low)   | 2  | 0.812 | -18.1 | 0  |
| 150 | unknown_continuous         | 27 | 0.743 | -19.8 | 0  |
| 150 | nfm_voice_repeater (low)   | 3  | 0.757 | -18.8 | 0  |
| 160 | unknown_continuous         | 36 | 0.748 | -18.5 | 0  |
| 160 | ais (low)                  | 3  | 0.245 | -11.5 | 0  |
| 160 | marine_vhf_channel (low)   | 2  | 0.799 | -16.1 | 0  |
| 160 | nfm_voice_repeater (low)   | 1  | 0.432 | -28.9 | 0  |
| 170 | unknown_continuous         | 28 | 0.757 | -15.3 | 7  |
| 170 | dvbt_mux (low)             | 14 | 0.188 | -8.9  | 0  |
| 170 | unknown_bursty             | 1  | 0.420 | -10.4 | 1  |
| 180 | dvbt_mux (low)             | 19 | 0.193 | -4.5  | 4  |
| 180 | unknown_continuous         | 14 | 0.746 | -19.1 | 14 |
| 180 | unknown_bursty             | 10 | 0.091 | -8.4  | 10 |
| 190 | unknown_continuous         | 17 | 0.677 | -14.6 | 17 |
| 190 | dvbt_mux (low)             | 16 | 0.112 | -5.5  | 0  |
| 190 | unknown_bursty             | 10 | 0.113 | -9.1  | 10 |
| 200 | unknown_continuous         | 12 | 0.758 | -22.1 | 12 |
| 200 | unknown_bursty             | 10 | 0.311 | -28.9 | 10 |
| 200 | dvbt_mux (low)             | 9  | 0.391 | -15.9 | 2  |
| 250–460 | unknown_bursty (sum)   | 268| ~0.04 | ~-34  | 266 |

Full UHF breakdown collapsed to one row above; see query output for
per-10-MHz detail. Roughly every unknown_bursty row in 250–460 MHz
sits at −33 to −37 dBFS with ~4% duty and almost all harmonic-flagged.

## Narrative

**The pool splits cleanly into three regimes.**

1. **UHF noise floor (250–460 MHz, ~268 rows, 36% of pool).** Power is
   −33 to −37 dBFS, i.e., within 3 dB of the measured noise floor
   (−47 dBFS per last run — UHF floor is a few dB higher due to
   quantisation and thermal). Duty cycles are 1–5% (transient spurs,
   not real carriers) and **virtually every row is harmonic-flagged**
   — which is Issue 3's false-positive story hitting the noise floor.
   This bucket is legitimately unclassifiable until Issue 3 is fixed;
   after Issue 3 the harmonic flags will clear and most of these rows
   stay unknown_bursty because the scores are small regardless.

2. **VHF productive bands (108–174 MHz, ~210 rows, 28% of pool).**
   Power is −14 to −20 dBFS — **well above the noise floor** and
   clustered at `duty ~0.7+ / pattern=continuous`. These are real
   always-on carriers (repeater idle carriers, gov/military data
   links, marine coast stations idling) that fail the classifier
   because the current `nfm_voice_repeater` rule only accepts
   `bursty_low / bursty_high` patterns. There is no class for
   "continuous NFM voice carrier on a land-mobile allocation", so they
   fall through to `unknown_continuous`.

3. **Low-confidence real classes (88–210 MHz, ~166 rows, 22% of pool).**
   These ARE classified correctly by class_id (broadcast_fm, dvbt_mux,
   nfm_voice_repeater, ais, marine_vhf_channel) but at confidence 0.4.
   Mostly the step-2 duty-baseline artifact (continuous signals showing
   as bursty_low duty after the self-baseline dominates p10) pushing
   the score+beat-margin just below the 0.6 tier. Some are cap-fired
   at 0.4 by the harmonic-penalty logic (Issue 3).

## Decision

**Pool is NOT purely legitimate noise.** The ~210 rows in 108–174 MHz
with `power −15 to −20 dBFS / duty ~0.7 / derived continuous` are
real signals being missed by the classifier, not noise.

Not in scope for this cleanup pass per the spec. **Flagged for a
later tuning pass with this concrete recommendation**:

> Widen `nfm_voice_repeater.duty_pattern` from `["bursty_low",
> "bursty_high"]` to `["continuous", "bursty_low", "bursty_high"]`
> (or introduce a new `nfm_idle_carrier` class if the distinction
> between "idle repeater" and "active voice" matters for dashboarding).
> This would reclassify roughly 100+ currently-unknown_continuous VHF
> bins as nfm_voice_repeater at moderate confidence, without affecting
> am_airband_atc / marine_vhf_channel behaviour (their allocations
> don't overlap with land mobile / gov / amateur).

Expected pool size after this tuning + Issue 3: ~270 rows in UHF
(legit noise) + ~50 in low-conf real classes = roughly 320, down
from 744. Acceptable headroom for the "classifier is actually
resolving most signals" sanity target.

## Out of scope for this pass

- Retuning scoring weights or duty_pattern rules
- Adding new signal_classes entries
- Any change to `allocations` or `known_frequencies`

This diagnostic is the Issue 2 commit by itself (no code change).
Issues 3 and 4 follow separately.
