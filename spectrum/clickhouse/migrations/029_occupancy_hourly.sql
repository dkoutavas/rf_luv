-- 029: M1, the occupancy atlas
--
-- For every 1 MHz band, every hour and every dongle: in what share of the
-- sweeps that measured the band did the peak detector fire there? Think of
-- a spectrogram turned into a duty-cycle map, one cell per MHz per hour.
-- Only swept time counts. An hour with no sweep has no cell at all, so a
-- session gap shows as a hole, never as 0 %.
--
-- Two materialized views fill one AggregatingMergeTree table at insert
-- time, so no dashboard query ever scans raw scans:
--   * occupancy_hourly_swept_mv, from scans: bins measured per band, hour
--     and preset. A cell with bins > 0 means that preset swept the band in
--     that hour.
--   * occupancy_hourly_active_mv, from peaks: the set of sweeps with a peak
--     in the band (uniq state, exact at these sizes, a few dozen sweeps an
--     hour).
-- The view occupancy_hourly turns them into a percentage, with the sweep
-- counts from sweep_health as the denominator.
--
-- Peaks are counted raw. A tile-grid DC spike or the 116.82 MHz
-- intermodulation product makes its band look busier than it is. The
-- emitters table, not this atlas, is where artifacts are told apart. And the
-- peak detector needs 5 bins on each side, so the first and last 5 bins of a
-- sweep never peak: the bands at 88, 118, 136 and 469 MHz read a little low.
--
-- Backfill: the views are created first, then the history is inserted.
-- A scans or peaks row that lands in between can reach the table twice.
-- That is harmless here: bins is only tested for > 0, and the uniq state of
-- sweep_ids is a set, where a second copy changes nothing.
--
-- Precondition: none. Additive. The backfill reads scans once (3.4 M rows on
-- the host on 2026-10-14, a few seconds).
-- Rollback: DROP the view, both MVs, then the table.

CREATE TABLE IF NOT EXISTS spectrum.occupancy_hourly_cells (
    hour        DateTime,
    dongle_id   LowCardinality(String),
    band_mhz    UInt16,
    preset      LowCardinality(String),
    bins        SimpleAggregateFunction(sum, UInt64),
    active      AggregateFunction(uniq, String)
) ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMM(hour)
ORDER BY (dongle_id, hour, band_mhz, preset)   -- the dashboard asks: one dongle, a time range
TTL hour + INTERVAL 180 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS spectrum.occupancy_hourly_swept_mv
TO spectrum.occupancy_hourly_cells
AS SELECT
    toStartOfHour(timestamp) AS hour,
    dongle_id,
    toUInt16(intDiv(freq_hz, 1000000)) AS band_mhz,
    splitByChar(':', sweep_id)[1] AS preset,
    toUInt64(count()) AS bins
FROM spectrum.scans
GROUP BY hour, dongle_id, band_mhz, preset;

CREATE MATERIALIZED VIEW IF NOT EXISTS spectrum.occupancy_hourly_active_mv
TO spectrum.occupancy_hourly_cells
AS SELECT
    toStartOfHour(timestamp) AS hour,
    dongle_id,
    toUInt16(intDiv(freq_hz, 1000000)) AS band_mhz,
    splitByChar(':', sweep_id)[1] AS preset,
    uniqState(sweep_id) AS active
FROM spectrum.peaks
GROUP BY hour, dongle_id, band_mhz, preset;

INSERT INTO spectrum.occupancy_hourly_cells (hour, dongle_id, band_mhz, preset, bins)
SELECT
    toStartOfHour(timestamp) AS hour,
    dongle_id,
    toUInt16(intDiv(freq_hz, 1000000)) AS band_mhz,
    splitByChar(':', sweep_id)[1] AS preset,
    toUInt64(count()) AS bins
FROM spectrum.scans
GROUP BY hour, dongle_id, band_mhz, preset;

INSERT INTO spectrum.occupancy_hourly_cells (hour, dongle_id, band_mhz, preset, active)
SELECT
    toStartOfHour(timestamp) AS hour,
    dongle_id,
    toUInt16(intDiv(freq_hz, 1000000)) AS band_mhz,
    splitByChar(':', sweep_id)[1] AS preset,
    uniqState(sweep_id) AS active
FROM spectrum.peaks
GROUP BY hour, dongle_id, band_mhz, preset;

-- Band by hour, percentage of swept time active. Sweeps of different
-- presets are different sweeps, so their counts add up.
CREATE OR REPLACE VIEW spectrum.occupancy_hourly AS
WITH
    cells AS (
        SELECT hour, dongle_id, band_mhz, preset, sum(bins) AS bins, uniqMerge(active) AS active
        FROM spectrum.occupancy_hourly_cells
        GROUP BY hour, dongle_id, band_mhz, preset
    ),
    sweeps AS (
        SELECT toStartOfHour(timestamp) AS hour, dongle_id, preset, count() AS sweeps
        FROM spectrum.sweep_health
        GROUP BY hour, dongle_id, preset
    )
SELECT
    c.hour AS hour,
    c.dongle_id AS dongle_id,
    c.band_mhz AS band_mhz,
    sum(s.sweeps) AS swept_sweeps,
    sum(c.active) AS active_sweeps,
    round(100 * active_sweeps / swept_sweeps, 1) AS active_pct
FROM cells AS c
INNER JOIN sweeps AS s ON s.hour = c.hour AND s.dongle_id = c.dongle_id AND s.preset = c.preset
WHERE c.bins > 0
GROUP BY c.hour, c.dongle_id, c.band_mhz;
