# Live signature detection — design

**Status: design only. No code to merge yet. See `spectrum/analysis/detect_compression.py` for the Phase-2 reference implementation that Phase 3 mode (a) will reuse.**

This document specifies three live detection modes the pipeline needs beyond the existing `detect_peaks`/`detect_transients` in `scanner.py`:

| Mode | What it flags | Where it lives |
|---|---|---|
| (a) Compression event | LNA/ADC compression from a strong pulsed emitter | **Sidecar** running every 5 min at `*:15` |
| (b) Narrow persistent carrier emergence | A bin that transitions noise-floor → clearly-above-floor and stays there | **Sidecar** reusing classifier cadence (`*:30`) |
| (c) Transient narrow burst | A narrow "appeared" event that isn't a compression artifact and isn't a known class | **View over existing `events` + `signal_classifications` — no detector code** |

## Why sidecar, not scanner

`spectrum/scanner.py` is time-sensitive (must sweep the band every 5 min, respond to rtl_tcp latency, stay stdlib-only for deployment simplicity). Adding stateful detection logic there grows surface area and couples detection to scanner uptime. A sidecar reading from ClickHouse:

- Can use numpy freely (already a dep for the scanner image, not needed for feature/classifier stack).
- Runs at whatever cadence fits (5 min, 1 min, on-demand).
- Has access to the full current-plus-history picture in SQL.
- Acceptable latency: ~3 min from sweep-end to event-detected is fine for a hobby project.

The operator-confirmation flow (`listening_log`) already demonstrates that delayed-by-minutes is fine for this pipeline.

## Mode (a) — Compression event detector

**Reference implementation**: `spectrum/analysis/detect_compression.py` (Phase 2 archaeology). The live detector is the same code, run on each new full sweep instead of the whole history.

### Signature summary
Three independent sub-flags, aggregated into `match_tier ∈ {none, low, medium, high}` by count:

| Sub-flag | Fires when |
|---|---|
| `sig_spur` | ≥10 consecutive tiles share argmax offset within 30 kHz stdev, absolute offset ≥ 100 kHz (excludes DC spike), median peak power ≥ -15 dBFS (excludes weak baseline spurs) |
| `sig_baseline` | Median attenuation of known-strong carrier bins (hourly_baseline > -35 dBFS, FM/DVB-T primarily) vs previous-hour baseline ≥ 5 dB |
| `sig_clip` | `sweep_health.worst_clip_freq_hz` outside 170-230 MHz AND `clipped_captures > 0` |

### Features stored
Schema: `spectrum.compression_events` (migration 012). Columns include `estimated_emitter_freq_hz`, `estimated_emitter_power_dbfs`, `spur_offset_hz`, `spur_block_tile_lo/hi/count`, `spur_offset_stddev_khz`, `baseline_depression_db`, all three `sig_*` flags, and `match_tier`.

### Deployment
- Install as `~/ops/spectrum-compression-detector/spectrum-compression-detector.{service,timer}` mirroring the existing classifier timer pattern.
- Schedule: `OnCalendar=*:00/5:15` (runs 15 s after each full-sweep emitter target) — 3-4 min after the latest full sweep completes, so no race with `scan_ingest.py` flush.
- Default `--min-tier medium` so only convincing events persist to the table; run with `--min-tier low --dry-run` manually to calibrate.

### Grafana panel
Add to `spectrum-overview.json`:
```sql
SELECT timestamp AS time, estimated_emitter_freq_hz/1e6 AS "emitter_mhz",
       estimated_emitter_power_dbfs AS dbfs, match_tier
FROM compression_events FINAL
WHERE $__timeFilter(timestamp)
ORDER BY timestamp
```
Panel type: table with bar-vis on `dbfs`. Alternative: state-timeline colored by `match_tier`.

### FP rate estimate
From 2,430 full sweeps over 16 days (Phase 2 backfill running at time of writing) and the Apr 21 calibration:

- `sig_spur` alone fires on ~2% of sweeps (baseline spurs that clear the MIN_POWER=-15 dBFS cutoff after strong transient broadcasts).
- `sig_clip` alone is common — any worst-clip outside 170-230 (usually 99 MHz FM) fires it.
- `sig_baseline` alone is rare — requires a specific measurable drop in known carriers.
- **All three together (`high`)**: expected 0-5 events in 16 days.
- **Two together (`medium`)**: expected 5-15 events in 16 days.

If backfill proves this wrong, tune `MIN_SPUR_BLOCK_TILES` and `DEPRESSION_MIN_DB` accordingly. Don't aggregate sub-flags into a single "probability" — keep them independently readable (see §design discipline at end).

## Mode (b) — Narrow persistent carrier emergence

**What it catches**: a frequency that was at noise floor for ≥24h and now shows a persistent above-floor signal. Distinguishable from both mode (a) (which catches wide pulsed events) and mode (c) (which catches transients).

### Signature

For each `freq_hz` in `peak_features` updated this round:

1. **Duty-cycle emergence** — `duty_cycle_1h > 0.1` AND `duty_cycle_7d < 0.02` (a bin that was silent for a week now shows activity in the last hour).
2. **Power margin above baseline** — `power_p95_dbfs` > (hourly_baseline for this freq in the last-quiet-period) + 10 dB.
3. **Hysteresis** — `duty_cycle_24h > 0.05` (avoids flapping on a single burst; requires some persistence).
4. **Not already classified** — `class_id` in `signal_classifications` is `unknown_bursty` or `unknown_continuous` (if it's already `dvbt_mux` or `broadcast_fm`, not an emergence — known signal intermittently weak).

### Where
Extend `spectrum/classifier.py` with a new `detect_emergence()` pass that runs after the main classification loop, or add `spectrum/detectors/carrier_emergence.py` as a standalone sidecar reusing the classifier's systemd unit model (runs at `*:00/5:30` after classifier proper). Recommended: extend classifier so one commit adds the new row-type and it shares the feature load.

### Features stored

New table `spectrum.emergence_events` (migration TBD):
```
freq_hz                UInt32
first_seen_at          DateTime64(3)
last_silent_at         DateTime64(3)  -- 24h quiet before emergence
duty_cycle_1h_trigger  Float32
power_p95_dbfs_trigger Float32
baseline_power_dbfs    Float32        -- hourly_baseline prior to emergence
persistence_24h        Float32        -- how often it's been active since trigger
notes                  String DEFAULT ''
detected_at            DateTime64(3)
```

### Grafana panel
Add to `listening-playbook.json`: "New Signals — Worth a Listen." Columns: freq_mhz (link to SDR++ suggested demod), first_seen_at, duty_cycle_1h, power, and a "confirmed" button calling the listening_log form.

### FP rate estimate
Hourly baseline takes 1-2 days to stabilize for a new bin, so emergence triggers on ~0 legit signals in "steady state." When baseline is still maturing (new antenna, new location, first 48h after a big event), false positives can spike to 10-20 per day. Mitigation: skip emergence triggers for bins with `sweeps_observed_24h < 50` (not enough history to judge).

## Mode (c) — Transient narrow burst

**What it catches**: a narrow bin that `appeared` in one sweep, `disappeared` in the next, isn't part of a compression event, and isn't a known class (AIS, ATC, etc.).

### This is a view, not a detector

We already have `spectrum.events` (populated by `scanner.py:341 detect_transients`), `spectrum.signal_classifications` (class_id per freq), and (after Phase 2) `spectrum.compression_events` (compression windows). Mode (c) is the join.

### Proposed migration `013_add_transient_burst_view.sql`

```sql
-- 013: Transient burst view
CREATE VIEW IF NOT EXISTS spectrum.transient_bursts AS
SELECT
    e.timestamp AS timestamp,
    e.freq_hz AS freq_hz,
    e.freq_hz / 1e6 AS freq_mhz,
    e.event_type AS event_type,
    e.power_dbfs AS power_dbfs,
    e.delta_db AS delta_db,
    ifNull(c.class_id, 'none') AS existing_class,
    -- presence in compression window (±1 sweep ≈ ±5 min)
    (SELECT count() FROM spectrum.compression_events ce
     WHERE ce.timestamp BETWEEN e.timestamp - INTERVAL 5 MINUTE
                              AND e.timestamp + INTERVAL 5 MINUTE
    ) AS in_compression_window
FROM spectrum.events e
LEFT JOIN (
    SELECT freq_hz, argMax(class_id, classified_at) AS class_id
    FROM spectrum.signal_classifications
    GROUP BY freq_hz
) c ON e.freq_hz = c.freq_hz
WHERE e.event_type = 'appeared'
  AND e.delta_db >= 15
  AND ifNull(c.class_id, 'unknown_bursty') IN ('unknown_bursty', 'unknown_continuous', 'none')
  AND in_compression_window = 0;
```

(Correlated subquery notes: ClickHouse 24.3 supports this via `GLOBAL IN` if it fails as-written. If this pattern fails, precompute `compression_windows` as a materialized view.)

### Where
Single SQL file migrated via migrate.py. No Python.

### Grafana panel
Add to `listening-playbook.json`: "Unclassified Transients." Same shape as existing "Mystery Signals" panel but with the compression-window exclusion baked in.

### FP rate estimate
Existing `events` table has ~200k rows over 16 days = ~12k/day transient events. After filtering for appeared + delta≥15dB + unknown class + not-in-compression, expect ~100-500/day — manageable as a feed for operator listening.

## Cross-cutting: verify rtl_tcp concurrency before Phase 5 builds on this

Before shipping Phase 5 (forensic capture), verify rtl_tcp on the leap box. The simplest test:

```bash
# Terminal 1
ssh dio_nysis@192.168.2.10 "nc -z 127.0.0.1 1234 && echo open && sleep 5"

# Terminal 2, while Terminal 1 is sleeping:
ssh dio_nysis@192.168.2.10 "nc -z 127.0.0.1 1234 && echo second-client-accepted"
```

If the second connection gets refused or receives a closed socket, Phase 5 must use a file lock on `/run/rtl_tcp.lock` to serialize access and pause the scanner's sweep loop during forensic capture. Even if rtl_tcp accepts concurrent clients, the lock is still worth having to avoid IQ-sample cross-contamination.

Belongs in Phase 3 (this) rather than Phase 5 because the result informs the Phase 5 architecture.

## Design discipline — stated honestly

- **No fabricated probabilities.** Sub-flags stored independently. `match_tier` is a count, not a calibrated confidence. The Apr 21 event is the only unambiguous training example; we don't pretend statistics we don't have.
- **Each detector has a verifiable false-positive story.** FP estimates above are budget items: if backfill shows 10× more events than predicted, we re-tune before shipping.
- **Each detector has a Grafana endpoint.** If you can't see it, you can't validate it.
- **No shared classifier state.** Compression/emergence/transient detectors run independently. No mode has the authority to suppress another's output.
