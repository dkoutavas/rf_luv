# V3 vs V4 A/B comparison — design

## Goal

Quantify the effect of an inline FM bandstop filter on the wideband scanner,
using V4 (with filter) and V3 (without) running in parallel for one week.
Output: a defensible yes/no decision on whether the filter should migrate
permanently to V3.

## Setup during A/B week

- V3: stock RTL-SDR Blog V3, rooftop dipole, no filter. Existing config
  preserved via `/etc/rtl-scanner/v3-01.env` — identical to pre-A/B.
- V4: RTL-SDR Blog V4 with FM bandstop filter (88–108 MHz notch) inline.
  Same antenna as V3 if possible (via splitter); separate antenna otherwise.
  Same scan presets as V3 so `dongle_comparison_view` produces apples-to-apples
  deltas.
- Both scanners write into the same ClickHouse tables, tagged by
  `dongle_id`.
- Duration: **7 full days** (168 hours), starting the hour after both
  scanners are running cleanly.

## Data sources

The workhorse is `spectrum.dongle_comparison_view` (migration 020). It
aggregates `spectrum.scans` and `spectrum.sweep_health` by
`(hour, freq_mhz_tile, dongle_id)` and produces side-by-side columns:

| Column | Source | Interpretation |
|---|---|---|
| `v3_avg_power_dbfs` | avg(scans.power_dbfs) | Mean power across the tile per hour |
| `v3_peak_power_dbfs` | max(scans.power_dbfs) | Loudest bin in the tile per hour |
| `v3_noise_floor_dbfs` | quantile(0.1)(scans.power_dbfs) | 10th-percentile power — proxy for noise floor |
| `v4_avg_power_dbfs` … | (same, dongle_id='v4-01') | — |
| `delta_noise_floor_db` | v4 − v3 | Negative = V4 sees a lower noise floor (filter working) |
| `v3_clip_count_per_hour` | sum(sweep_health.clipped_captures) | ADC saturations in the hour |
| `v4_clip_count_per_hour` | (same, v4-01) | — |

Tile = `toUInt32(freq_hz / 1_000_000)` — 1 MHz buckets. Fine enough to see
in-band vs out-of-band effects; coarse enough that per-tile aggregates are
robust.

For peak-discovery comparisons (which signals V4 saw that V3 missed, and vice
versa), query `spectrum.peaks` directly:

```sql
SELECT freq_hz, dongle_id, count() AS peak_count
FROM spectrum.peaks
WHERE timestamp > now() - INTERVAL 7 DAY
GROUP BY freq_hz, dongle_id
ORDER BY freq_hz;
```

Then pivot to find signals with counts on one side but not the other.

## Panel priority (to build after A/B decision, NOT in this session)

The brief is clear that panels are a followup. This list is the "when we
decide to visualize the A/B, start here" plan. Ordered by decision value:

### 1. Noise-floor delta by frequency band (line chart, primary)

Primary decision input. One line per band over the 7-day window:

```sql
SELECT hour,
       avgIf(delta_noise_floor_db, freq_mhz_tile BETWEEN 88 AND 107) AS fm_band,
       avgIf(delta_noise_floor_db, freq_mhz_tile BETWEEN 118 AND 136) AS airband,
       avgIf(delta_noise_floor_db, freq_mhz_tile BETWEEN 174 AND 229) AS dvbt_band,
       avgIf(delta_noise_floor_db, freq_mhz_tile BETWEEN 225 AND 399) AS mil_uhf,
       avgIf(delta_noise_floor_db, freq_mhz_tile BETWEEN 432 AND 434) AS ism_433
FROM spectrum.dongle_comparison_view
GROUP BY hour
ORDER BY hour;
```

Expected: fm_band strongly negative (filter doing its job, 88–108 MHz),
other bands near zero (filter has no effect out of band). If airband or
UHF show significant negative deltas, V4's improved SNR from reduced LNA
overload is propagating — a secondary benefit.

### 2. Hourly clip-rate delta (bar chart)

```sql
SELECT hour,
       sum(v3_clip_count_per_hour) AS v3_clips,
       sum(v4_clip_count_per_hour) AS v4_clips,
       sum(v4_clip_count_per_hour) - sum(v3_clip_count_per_hour) AS delta
FROM spectrum.dongle_comparison_view
GROUP BY hour
ORDER BY hour;
```

Expected: V4 clips significantly less. If V4 clips more, the filter is not
reducing saturation — either not properly inline, or gain setting is wrong.

### 3. Side-by-side waterfalls (heatmap ×2)

Two Grafana heatmap panels, same time range, one filtered to `dongle_id='v3-01'`
and one to `dongle_id='v4-01'`. Visual sanity check — the FM band should look
"carved out" on the V4 panel.

Query pattern (per panel):
```sql
SELECT toStartOfInterval(timestamp, INTERVAL 1 MINUTE) AS t,
       freq_hz,
       avg(power_dbfs)
FROM spectrum.scans
WHERE dongle_id = 'v3-01'   -- or 'v4-01'
  AND timestamp > now() - INTERVAL 1 HOUR
GROUP BY t, freq_hz
ORDER BY t, freq_hz;
```

### 4. Signals-gained vs signals-lost (table)

Signals V4 saw that V3 didn't, and vice versa:

```sql
WITH v3_peaks AS (
    SELECT toUInt32(freq_hz / 100000) AS freq_100khz
    FROM spectrum.peaks
    WHERE dongle_id = 'v3-01' AND timestamp > now() - INTERVAL 7 DAY
    GROUP BY freq_100khz
    HAVING count() >= 5
),
v4_peaks AS (
    SELECT toUInt32(freq_hz / 100000) AS freq_100khz
    FROM spectrum.peaks
    WHERE dongle_id = 'v4-01' AND timestamp > now() - INTERVAL 7 DAY
    GROUP BY freq_100khz
    HAVING count() >= 5
)
SELECT
    v4.freq_100khz / 10 AS freq_mhz,
    'v4_only' AS status
FROM v4_peaks v4
LEFT ANTI JOIN v3_peaks v3 ON v4.freq_100khz = v3.freq_100khz
UNION ALL
SELECT
    v3.freq_100khz / 10,
    'v3_only'
FROM v3_peaks v3
LEFT ANTI JOIN v4_peaks v4 ON v3.freq_100khz = v4.freq_100khz
ORDER BY freq_mhz;
```

- `v4_only` peaks in passband (anywhere except 88–108 MHz) = signals V4's
  improved SNR revealed. Good.
- `v3_only` peaks in FM band (88–108 MHz) = signals the filter suppressed.
  Expected and intentional.
- `v3_only` peaks OUTSIDE FM = cause for concern — either V4 has lower
  sensitivity at that frequency (filter passband loss) or the dongles are
  not otherwise identical. Investigate case-by-case.

### 5. Peak-prominence at known frequencies (single-stat × N)

For each entry in `spectrum.known_frequencies`, show V3 and V4 peak power
side-by-side over the last 7 days. Decision-relevant because a ~1 dB drop
on a known airband frequency is significant even if it's lost in the
aggregate.

```sql
SELECT
    kf.name,
    kf.freq_hz / 1e6 AS mhz,
    avg(scans.power_dbfs) FILTER (WHERE dongle_id = 'v3-01') AS v3_power,
    avg(scans.power_dbfs) FILTER (WHERE dongle_id = 'v4-01') AS v4_power,
    (avg(scans.power_dbfs) FILTER (WHERE dongle_id = 'v4-01'))
      - (avg(scans.power_dbfs) FILTER (WHERE dongle_id = 'v3-01')) AS delta_db
FROM spectrum.known_frequencies kf
INNER JOIN spectrum.scans scans ON scans.freq_hz = kf.freq_hz
WHERE scans.timestamp > now() - INTERVAL 7 DAY
GROUP BY kf.name, kf.freq_hz
ORDER BY mhz;
```

(Actual dialect adjustments for ClickHouse FILTER may be needed; above is
illustrative.)

## Decision framework

The filter earns permanent V3 residency if **all** of the following hold
across the 7-day A/B window:

1. **Mean noise floor drop**: `avg(delta_noise_floor_db)` in the FM band
   (88–108 MHz) is ≤ -3 dB. The filter is supposed to attenuate FM power,
   and a 3 dB drop is the threshold below which the filter isn't doing
   meaningful work.

2. **No significant passband loss at critical bands**: `avg(delta_noise_floor_db)`
   in the airband (118–137 MHz) is ≥ -1 dB. More than 1 dB loss in a band we
   actively monitor is a meaningful sensitivity hit — the filter is eating
   into signals we care about.

3. **Clip rate reduction**: `sum(v4_clip_count_per_hour) / sum(v3_clip_count_per_hour)`
   is ≤ 0.2 (i.e., V4 clips ≤ 20% as often as V3). FM-carrier saturation is
   the primary overload mechanism in Athens; the filter's secondary purpose
   is to give the LNA dynamic-range headroom.

4. **No unknown signals lost in non-FM bands**: the `v3_only` peaks list
   (criterion 4 above) contains nothing outside 88–108 MHz that isn't
   explainable (e.g., a known FM harmonic at 199.2 MHz = 99.6 × 2, or a
   receiver intermod spur).

If all four hold, plan the filter migration (separate session). If any
fails, record the failing metric with data, and either:
- Decline the filter permanently (likely if #2 or #4 fails).
- Propose a different filter (more selective, less passband loss) and
  re-run A/B (likely if #1 fails, i.e., filter is weak).

## Post-A/B cleanup

Either way, at end of A/B week:

- Document conclusion in `spectrum/docs/ab_comparison_results.md` (this file
  is the design; that file is the outcome).
- If filter migrates: see `spectrum/docs/second_dongle_preflight.md`
  "Before FM filter install on V3" checklist. That migration triggers the
  hourly_baseline re-derivation discussion.
- V4 repurposes for specialist role (mil UHF 225–400 MHz, or ADS-B 1090 MHz).
  Rewrite `/etc/rtl-scanner/v4-01.env` accordingly; scheme allows narrow-band
  scanners without Further schema changes.
