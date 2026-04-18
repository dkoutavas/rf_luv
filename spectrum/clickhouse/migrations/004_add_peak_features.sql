-- 004: Add peak_features table (step 2 of 3)
--
-- One row per (freq_hz, computed_at) with features computed from rolling + historical
-- data. Populated by spectrum/feature_extractor.py on a systemd timer (~5 min cadence).
--
-- ReplacingMergeTree(computed_at) means repeat runs produce new rows that get
-- collapsed on merge to the most recent per freq_hz. Queries should use
-- `FROM peak_features FINAL` or `argMax(col, computed_at)` for authoritative reads.

CREATE TABLE IF NOT EXISTS spectrum.peak_features (
    freq_hz             UInt32,
    bandwidth_hz        UInt32,                 -- FWHM estimate; 999000000 sentinel = wider than measurable
    duty_cycle_1h       Float32,                -- fraction active (>baseline+6 dB) in last 1h
    duty_cycle_24h      Float32,                -- last 24h
    duty_cycle_7d       Float32,                -- last 7d
    burst_p50_s         Nullable(Float32),      -- NULL if <3 bursts in 24h
    burst_p95_s         Nullable(Float32),      -- NULL if <3 bursts in 24h
    diurnal_pattern     Array(Float32),         -- length 24, activity per hour-of-day over 7d (UTC)
    weekday_pattern     Array(Float32),         -- length 7, activity per weekday over 14d (UTC)
    harmonic_of_hz      Nullable(UInt32),       -- base freq if this is a 2x/3x/4x of stronger peak
    power_mean_dbfs     Float32,                -- mean over ACTIVE sweeps only
    power_p95_dbfs      Float32,                -- p95 over ACTIVE sweeps only
    power_std_db        Float32,                -- stddev over ACTIVE sweeps only
    sweeps_observed_24h UInt32,                 -- denominator for duty_cycle_24h (debug)
    computed_at         DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
ORDER BY freq_hz;
