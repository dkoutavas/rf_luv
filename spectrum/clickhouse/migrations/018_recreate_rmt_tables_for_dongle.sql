-- 018: Recreate ReplacingMergeTree tables with dongle_id in the sort key
--
-- peak_features, signal_classifications, and compression_events are all
-- ReplacingMergeTree with the sort key as their dedup identity. Adding
-- dongle_id as a plain column (migration 017 did this for compression_events)
-- is not enough — once V4 starts ingesting, rows keyed by the same freq_hz
-- or sweep_id from the two dongles would collapse to one row per merge.
-- V4's classifications would silently overwrite V3's, or vice versa.
--
-- Fix: recreate each table with dongle_id folded into ORDER BY. The tables
-- are short-lived (peak_features and signal_classifications rebuild every
-- 5 min on their respective timers) so data-preservation matters only for
-- the ~5 min transition after migration.
--
-- Approach: atomic swap via _new tables + INSERT-SELECT + RENAME + DROP.
-- If the scanner/classifier/feature_extractor writes a row with only the old
-- columns during the INSERT-SELECT window, the DEFAULT 'v3-01' on dongle_id
-- means it lands correctly in the new table once RENAME completes.
--
-- Precondition: 017 applied. Scanner can keep running but will briefly
-- see the old table schema disappear; writes during the rename window
-- will fail and be retried by the client.
--
-- NOT idempotent: if this migration fails partway through, manual cleanup
-- is required (DROP the _new tables, re-run). See
-- spectrum/docs/dongle_cutover_runbook.md for recovery.

-- ── peak_features ───────────────────────────────────────────────
-- ReplacingMergeTree(computed_at) ORDER BY freq_hz → ORDER BY (freq_hz, dongle_id)
-- computed_at is the replacing version column, unchanged.

DROP TABLE IF EXISTS spectrum.peak_features_new;

CREATE TABLE spectrum.peak_features_new (
    freq_hz             UInt32,
    bandwidth_hz        UInt32,
    duty_cycle_1h       Float32,
    duty_cycle_24h      Float32,
    duty_cycle_7d       Float32,
    burst_p50_s         Nullable(Float32),
    burst_p95_s         Nullable(Float32),
    diurnal_pattern     Array(Float32),
    weekday_pattern     Array(Float32),
    harmonic_of_hz      Nullable(UInt32),
    power_mean_dbfs     Float32,
    power_p95_dbfs      Float32,
    power_std_db        Float32,
    sweeps_observed_24h UInt32,
    computed_at         DateTime DEFAULT now(),
    dongle_id           LowCardinality(String) DEFAULT 'v3-01'
) ENGINE = ReplacingMergeTree(computed_at)
ORDER BY (freq_hz, dongle_id);

INSERT INTO spectrum.peak_features_new
SELECT
    freq_hz, bandwidth_hz,
    duty_cycle_1h, duty_cycle_24h, duty_cycle_7d,
    burst_p50_s, burst_p95_s,
    diurnal_pattern, weekday_pattern,
    harmonic_of_hz,
    power_mean_dbfs, power_p95_dbfs, power_std_db,
    sweeps_observed_24h,
    computed_at,
    'v3-01' AS dongle_id
FROM spectrum.peak_features FINAL;

RENAME TABLE
    spectrum.peak_features     TO spectrum.peak_features_old,
    spectrum.peak_features_new TO spectrum.peak_features;

DROP TABLE spectrum.peak_features_old;

-- ── signal_classifications ──────────────────────────────────────
-- ReplacingMergeTree(classified_at) ORDER BY freq_hz → ORDER BY (freq_hz, dongle_id)

DROP TABLE IF EXISTS spectrum.signal_classifications_new;

CREATE TABLE spectrum.signal_classifications_new (
    freq_hz            UInt32,
    class_id           String,
    confidence         Float32,
    reasoning          String,
    features_snapshot  String,
    classified_at      DateTime DEFAULT now(),
    dongle_id          LowCardinality(String) DEFAULT 'v3-01'
) ENGINE = ReplacingMergeTree(classified_at)
ORDER BY (freq_hz, dongle_id);

INSERT INTO spectrum.signal_classifications_new
SELECT
    freq_hz, class_id, confidence, reasoning, features_snapshot, classified_at,
    'v3-01' AS dongle_id
FROM spectrum.signal_classifications FINAL;

RENAME TABLE
    spectrum.signal_classifications     TO spectrum.signal_classifications_old,
    spectrum.signal_classifications_new TO spectrum.signal_classifications;

DROP TABLE spectrum.signal_classifications_old;

-- ── compression_events ──────────────────────────────────────────
-- ReplacingMergeTree(detected_at) ORDER BY (timestamp, sweep_id) →
-- ORDER BY (timestamp, dongle_id, sweep_id)
--
-- Currently the table has dongle_id as a plain column (added in 017). Moving
-- it into the sort key preserves per-dongle dedup. Without this, two scanners
-- producing the same sweep_id at identical millisecond timestamps (rare, but
-- possible on synchronised starts) would collapse their compression events
-- into a single row.

DROP TABLE IF EXISTS spectrum.compression_events_new;

CREATE TABLE spectrum.compression_events_new (
    sweep_id                     String,
    timestamp                    DateTime64(3),
    estimated_emitter_freq_hz    Nullable(UInt32),
    estimated_emitter_power_dbfs Nullable(Float32),
    spur_offset_hz               Int32,
    spur_block_tile_lo           UInt16,
    spur_block_tile_hi           UInt16,
    spur_block_tile_count        UInt16,
    spur_offset_stddev_khz       Float32,
    baseline_depression_db       Float32,
    baseline_bins_sampled        UInt32,
    worst_clip_freq_hz           UInt32,
    clipped_captures             UInt32,
    sig_spur                     UInt8,
    sig_baseline                 UInt8,
    sig_clip                     UInt8,
    sig_clip_fm                  UInt8 DEFAULT 0,
    match_tier                   Enum8('none'=0, 'low'=1, 'medium'=2, 'high'=3),
    detected_at                  DateTime64(3) DEFAULT now64(3),
    detector_version             String DEFAULT 'v1',
    dongle_id                    LowCardinality(String) DEFAULT 'v3-01'
) ENGINE = ReplacingMergeTree(detected_at)
ORDER BY (timestamp, dongle_id, sweep_id)
TTL toDateTime(timestamp) + INTERVAL 365 DAY
SETTINGS index_granularity = 8192;

INSERT INTO spectrum.compression_events_new
SELECT
    sweep_id, timestamp,
    estimated_emitter_freq_hz, estimated_emitter_power_dbfs,
    spur_offset_hz,
    spur_block_tile_lo, spur_block_tile_hi, spur_block_tile_count,
    spur_offset_stddev_khz,
    baseline_depression_db, baseline_bins_sampled,
    worst_clip_freq_hz, clipped_captures,
    sig_spur, sig_baseline, sig_clip, sig_clip_fm,
    match_tier,
    detected_at, detector_version,
    'v3-01' AS dongle_id
FROM spectrum.compression_events FINAL;

RENAME TABLE
    spectrum.compression_events     TO spectrum.compression_events_old,
    spectrum.compression_events_new TO spectrum.compression_events;

DROP TABLE spectrum.compression_events_old;
