-- 020: Dongle A/B comparison view
--
-- Non-materialized view producing per-hour, per-1-MHz-tile aggregate stats
-- side by side for v3-01 and v4-01. Used during the A/B filter-evaluation
-- week to quantify the FM bandstop filter's effect on noise floor, clip
-- rate, and peak discovery.
--
-- Returns all-NULL v4 columns while V4 is absent — FULL OUTER JOIN on
-- (hour, freq_mhz_tile) preserves V3 rows when v4_stats is empty.
--
-- Not materialized because:
--   * It's a forensic tool for a ~1-week window, not a live dashboard.
--   * The FULL OUTER JOIN + four CTEs is cheap at our data volume.
--   * If needed later we can promote to a materialized target; schema
--     is stable.
--
-- Frequency tile is 1 MHz (freq_hz / 1_000_000). Narrow enough to see
-- band-level differences (FM vs airband vs UHF) but coarse enough that
-- per-tile aggregates are robust.
--
-- noise_floor_estimate is the 10th percentile of power_dbfs per tile
-- per hour. Not the true noise floor (which would require no-signal bins
-- only), but close enough for A/B comparison where we care about relative
-- change, not absolute value.

CREATE OR REPLACE VIEW spectrum.dongle_comparison_view AS
WITH
    v3_stats AS (
        SELECT
            toStartOfHour(timestamp) AS hour,
            toUInt32(freq_hz / 1000000) AS freq_mhz_tile,
            avg(power_dbfs) AS avg_power,
            max(power_dbfs) AS peak_power,
            quantile(0.1)(power_dbfs) AS noise_floor_estimate
        FROM spectrum.scans
        WHERE dongle_id = 'v3-01'
        GROUP BY hour, freq_mhz_tile
    ),
    v4_stats AS (
        SELECT
            toStartOfHour(timestamp) AS hour,
            toUInt32(freq_hz / 1000000) AS freq_mhz_tile,
            avg(power_dbfs) AS avg_power,
            max(power_dbfs) AS peak_power,
            quantile(0.1)(power_dbfs) AS noise_floor_estimate
        FROM spectrum.scans
        WHERE dongle_id = 'v4-01'
        GROUP BY hour, freq_mhz_tile
    ),
    v3_clips AS (
        SELECT
            toStartOfHour(timestamp) AS hour,
            sum(clipped_captures) AS clip_count
        FROM spectrum.sweep_health
        WHERE dongle_id = 'v3-01'
        GROUP BY hour
    ),
    v4_clips AS (
        SELECT
            toStartOfHour(timestamp) AS hour,
            sum(clipped_captures) AS clip_count
        FROM spectrum.sweep_health
        WHERE dongle_id = 'v4-01'
        GROUP BY hour
    )
SELECT
    coalesce(v3.hour, v4.hour) AS hour,
    coalesce(v3.freq_mhz_tile, v4.freq_mhz_tile) AS freq_mhz_tile,
    v3.avg_power AS v3_avg_power_dbfs,
    v3.peak_power AS v3_peak_power_dbfs,
    v3.noise_floor_estimate AS v3_noise_floor_dbfs,
    v4.avg_power AS v4_avg_power_dbfs,
    v4.peak_power AS v4_peak_power_dbfs,
    v4.noise_floor_estimate AS v4_noise_floor_dbfs,
    (v4.noise_floor_estimate - v3.noise_floor_estimate) AS delta_noise_floor_db,
    v3c.clip_count AS v3_clip_count_per_hour,
    v4c.clip_count AS v4_clip_count_per_hour
FROM v3_stats v3
FULL OUTER JOIN v4_stats v4
    ON v3.hour = v4.hour AND v3.freq_mhz_tile = v4.freq_mhz_tile
LEFT JOIN v3_clips v3c
    ON coalesce(v3.hour, v4.hour) = v3c.hour
LEFT JOIN v4_clips v4c
    ON coalesce(v3.hour, v4.hour) = v4c.hour;
