# V3 self-A/B comparison — design

## Goal

Quantify the effect of an inline FM bandstop filter on V3, by comparing
V3 scans **before vs after** the filter-install timestamp. Output: a
defensible yes/no decision on whether the filter stays inline permanently.

## Why self-A/B and not dual-dongle A/B (history)

The original plan (`91d3860`) was a dual-dongle A/B: V4 with filter, V3
stock, both running 7 days, compare via `dongle_comparison_view`. That
plan implicitly required a splitter so the two dongles saw the same
antenna — only the filter would differ.

Hardware reality (2026-04-22): two dongles, two antennas, no splitter.
V3 sits on a rooftop tripod with a 57 cm dipole; V4 sits at the patio
window with a 5 cm vertical whip. Their antenna positions, lengths, and
mounting heights are all different, so a dual-dongle comparison would
mix the filter effect with the antenna effects and the threshold-based
decision framework would silently lose meaning.

We pivoted to self-A/B on V3:
- Same dongle (R820T2), same antenna (rooftop tripod, 57 cm dipole),
  same gain (12 dB) before and after the filter install.
- The only thing that changed at `2026-04-22 14:35:12 UTC` is the
  filter going inline.
- We have weeks of pre-install V3 data already in `spectrum.scans`
  (TTL is 180 days), so the "pre" sample is large.

V4's role is now antenna diversity, not A/B control. `dongle_comparison_view`
still exists but its semantics changed (see below).

## Setup

- **V3**: rooftop tripod, 57 cm dipole, gain 12 dB, FM bandstop filter
  inline since `2026-04-22 14:35:12 UTC`. Config in
  `/etc/rtl-scanner/v3-01.env` on leap. The env file's `SCAN_NOTES` is
  the source-of-truth for "filter is inline".
- **V4** (informational, NOT the A/B control): patio window, 5 cm
  vertical whip, gain ~10 dB (adaptive), no filter. Different RF
  environment, useful for diversity but not directly comparable.
- The cutoff timestamp is the only piece of state the analysis depends
  on. It's also recorded in
  `~/.claude/projects/-home-dio-nysi-dev-rf-luv/memory/project_fm_filter_v3_install.md`.

## Minimum data window

- **24 hours** post-install minimum, to cover one full diurnal cycle —
  FM transmitters and airband traffic both vary by time-of-day.
- **48 hours** preferred. The first 24h gives us a noisy answer because
  weekday vs weekend FM programming, evening vs morning ATC traffic,
  etc. all shift the per-band averages.

The pre-install window should cover the same hours-of-day as the
post-install window (e.g., compare same 24h interval one week apart),
otherwise diurnal variation appears in the delta as filter effect.

## Data sources

Primary: `spectrum.scans` filtered by `dongle_id='v3-01'` and split by
the `2026-04-22 14:35:12 UTC` cutoff. Filter `freq_hz` to the band of
interest (FM, airband, etc.).

Secondary: `spectrum.sweep_health` filtered the same way, for clip-rate
deltas.

`spectrum.dongle_comparison_view` is **not** the primary tool here. With
two different antennas it shows antenna-position effects masquerading as
filter effects. It's still useful as a sanity check on V4-vs-V3
diversity (e.g. which dongle saw a signal the other missed) but its
`delta_noise_floor_db` column is no longer "what the filter did".

If migration 021 lands later (adds `scan_runs.filter` column), prefer
that over the timestamp cutoff. Until then, the 14:35:12 timestamp is
canonical.

## Decision framework

The filter stays inline on V3 permanently if **all four** criteria hold,
measured over ≥24 h post-install (ideally 48 h, ideally same hours-of-day
as the pre-install window):

| # | Criterion | Threshold | Why |
|---|---|---|---|
| 1 | FM band noise floor drop | `delta_avg_power_dbfs ≤ -3.0` for 88-108 MHz | If the filter doesn't drop FM by 3 dB, it isn't doing meaningful work |
| 2 | Airband insertion loss | `delta_avg_power_dbfs ≥ -1.0` for 118-137 MHz | More than 1 dB hit on airband = filter is eating signals we want |
| 3 | Clip rate reduction in FM | `post/pre ≤ 0.2` for `sweep_health.max_clip_fraction` in FM band | Front-end no longer saturating on Lycabettus/Hymettus FM |
| 4 | No unknown signal loss | Manual peaks review | Signals that were present pre and absent post, outside 88-108 MHz, must all be explainable (FM harmonics, propagation variance) |

Where `delta = post_filter - pre_filter`. Negative = post is lower.

If all four hold → keep filter, document outcome in
`spectrum/docs/ab_comparison_results.md`.

If any fail → either remove filter (likely if #2 or #4 fails) or
research a different filter (likely if #1 fails — current one is too
weak). Either way, document the failing metric with data.

## Query templates

### Criterion 1 + 2: noise floor delta per band

```sql
WITH cutoff AS (SELECT toDateTime64('2026-04-22 14:35:12', 3, 'UTC') AS t)
SELECT
    CASE
        WHEN freq_hz BETWEEN 88000000  AND 108000000 THEN 'fm'
        WHEN freq_hz BETWEEN 118000000 AND 137000000 THEN 'airband'
        WHEN freq_hz BETWEEN 174000000 AND 230000000 THEN 'dvbt'
        WHEN freq_hz BETWEEN 225000000 AND 400000000 THEN 'mil_uhf'
        WHEN freq_hz BETWEEN 432000000 AND 434000000 THEN 'ism'
    END AS band,
    (timestamp < (SELECT t FROM cutoff)) AS pre_filter,
    count() AS n,
    round(avg(power_dbfs), 2) AS avg_power_dbfs,
    round(quantile(0.1)(power_dbfs), 2) AS noise_floor_q10
FROM spectrum.scans
WHERE dongle_id = 'v3-01'
  AND timestamp > toDateTime64('2026-04-15 00:00:00', 3, 'UTC')  -- 7 days back
  AND band IS NOT NULL
GROUP BY band, pre_filter
ORDER BY band, pre_filter DESC
```

Then compute deltas off-line: `post.avg_power_dbfs - pre.avg_power_dbfs`
per band. Criterion 1 passes if `fm` delta ≤ -3.0; criterion 2 passes if
`airband` delta ≥ -1.0.

### Criterion 3: clip rate in FM band

```sql
WITH cutoff AS (SELECT toDateTime64('2026-04-22 14:35:12', 3, 'UTC') AS t)
SELECT
    (timestamp < (SELECT t FROM cutoff)) AS pre_filter,
    count() AS sweeps,
    round(avg(max_clip_fraction), 4) AS avg_clip,
    sum(clipped_captures) AS total_clipped_captures
FROM spectrum.sweep_health
WHERE dongle_id = 'v3-01'
  AND worst_clip_freq_hz BETWEEN 88000000 AND 108000000
  AND timestamp > toDateTime64('2026-04-15 00:00:00', 3, 'UTC')
GROUP BY pre_filter
ORDER BY pre_filter DESC
```

Criterion 3 passes if `post.avg_clip / pre.avg_clip ≤ 0.2`. Watch out
for divide-by-zero if pre had no clips (good problem to have, treat as
pass).

### Criterion 4: peaks present pre but absent post (outside FM)

```sql
WITH cutoff AS (SELECT toDateTime64('2026-04-22 14:35:12', 3, 'UTC') AS t)
SELECT
    toUInt32(freq_hz / 100000) AS freq_100khz,    -- 100 kHz tiles
    countIf(timestamp < (SELECT t FROM cutoff)) AS pre_count,
    countIf(timestamp >= (SELECT t FROM cutoff)) AS post_count
FROM spectrum.peaks
WHERE dongle_id = 'v3-01'
  AND timestamp > toDateTime64('2026-04-15 00:00:00', 3, 'UTC')
GROUP BY freq_100khz
HAVING pre_count >= 5 AND post_count = 0
   AND (freq_100khz < 880 OR freq_100khz > 1080)   -- exclude FM band
ORDER BY freq_100khz
```

Each row is a candidate "lost signal". Manually triage:
- Known FM harmonic (e.g. 199.2 MHz = 99.6 × 2)? Expected loss, OK.
- Strong narrowband at the same frequency in `known_frequencies`?
  Concern — investigate.
- Otherwise? Note it, then check the dongle_comparison_view to see if
  V4 is still seeing it (rules out band death vs filter cause).

## Antenna diversity (V3 vs V4) — no longer the A/B, but still useful

Now that V3 (rooftop, 57 cm dipole, FM-filtered) and V4 (patio, 5 cm
whip, stock) run in parallel, `dongle_comparison_view` becomes a
diversity panel: "what does the system see from one position that the
other misses". Useful for:

- Cross-validating peaks: a signal in `peaks` for both dongles is
  almost certainly real RF.
- Spotting local interference: a strong V4-only signal at the patio
  window often means a household device near the patio (a charger, a
  wifi router, etc.).
- Sky-vs-street comparisons: V3 at rooftop should win for satellite,
  ADS-B, distant ATC; V4 at patio window may win for ground-level
  emitters and short-range signals.

Treat the view's `delta_noise_floor_db` as "antenna diversity delta",
not "filter delta". Negative numbers mean V4 sees a quieter floor at
that frequency, which can be:
- V3's antenna picking up more environmental noise (likely above ~150 MHz
  where the rooftop dipole's collecting area helps but also hurts)
- The 5 cm whip's poor sub-GHz match attenuating signal (and noise)
  proportionally
- An actual asymmetric noise source

Don't read too much into a single tile. Look for patterns across bands.

## Post-A/B cleanup

After the decision is made:

- Document conclusion in `spectrum/docs/ab_comparison_results.md` (this
  file is the design; that file is the outcome).
- If filter stays: nothing to do — V3 already runs filtered. Mark it in
  the env file's `SCAN_NOTES`. Optionally land migration 021 to add
  `scan_runs.filter` column for future cleaner queries.
- If filter is removed: physically uninstall, restart `rtl-scanner@v3-01`,
  the baseline tables will re-derive from new data within ~24 h.
- Either way: V4 stays as a diversity feed. Possibly retune V4 to a
  specialist band (e.g. mil UHF 225-400 MHz, ADS-B 1090 MHz) — its short
  whip is poorly matched at VHF anyway. Edit `/etc/rtl-scanner/v4-01.env`
  for the retune.
