-- 026: Emitters, one row per transmitter per dongle (M2)
--
-- spectrum/emitters.py clusters persistent peaks into emitters: adjacent
-- 100 kHz bins that peak across many sweeps on one dongle. It rebuilds the
-- whole list every 15 minutes from the peaks history and writes one row per
-- emitter per run, all with the same run_at. Per-bin tables (peak_features,
-- signal_classifications) are unchanged. Emitters are a layer on top.
--
-- ReplacingMergeTree(run_at) ORDER BY (dongle_id, emitter_id): an emitter's
-- latest run replaces its older rows on merge. An emitter that a run no
-- longer produces (merged into a neighbor, or aged out with its peaks) keeps
-- its last row until the TTL. Read through emitters_current, which keeps only
-- the latest run.
--
-- emitter_id is stable across runs. emitters.py carries it forward by
-- footprint overlap. See assign_ids() for the method.
--
-- Column notes:
--   * lo_hz / hi_hz are the lowest and highest member bin labels. A peak
--     belongs to the emitter when it lies within half a bin (50 kHz) of them.
--   * bandwidth_hz is at bin resolution: hi - lo + one bin. An upper bound.
--   * duty_overall = sweeps with a peak / sweeps that covered the frequency
--     since first_seen. duty_24h is the same over the last 24 h and is NULL
--     when the dongle swept nothing in that window.
--   * hours_observed = clock hours since first_seen in which the dongle swept
--     the frequency. The evidence behind the duty numbers.
--   * power_* mix gains and include clipped sweeps. For orientation only.
--     anomalies compare levels per gain setting instead.
--   * name_source: listening_log | known_frequencies | classifier |
--     allocation | signal_catalog | unnamed.
--   * artifact_reason: comma list of tile_grid, clipped_only, intermod.
--
-- Precondition: none. Additive, the scanner keeps running.
-- Rollback: DROP the three views, then the two tables.

CREATE TABLE IF NOT EXISTS spectrum.emitters (
    emitter_id          String,
    dongle_id           LowCardinality(String),
    center_hz           UInt32,
    lo_hz               UInt32,
    hi_hz               UInt32,
    bandwidth_hz        UInt32,
    first_seen          DateTime64(3),
    last_seen           DateTime64(3),
    sweeps_hit          UInt32,
    duty_overall        Float32,
    duty_24h            Nullable(Float32),
    hours_observed      UInt32,
    power_max_dbfs      Float32,
    power_mean_dbfs     Float32,
    class_id            LowCardinality(String),
    service             LowCardinality(String),
    name                String,
    name_source         LowCardinality(String),
    name_note           String DEFAULT '',
    artifact_suspect    Bool,
    artifact_reason     String DEFAULT '',
    run_at              DateTime
) ENGINE = ReplacingMergeTree(run_at)
ORDER BY (dongle_id, emitter_id)
TTL run_at + INTERVAL 90 DAY;

-- Which dongle is the source of truth, and whose levels to believe. Read by
-- the views below, by the TETRA map and by anomalies.py. When hardware
-- changes, insert a new row for the dongle. The latest updated_at wins.
CREATE TABLE IF NOT EXISTS spectrum.dongle_roles (
    dongle_id       LowCardinality(String),
    is_primary      Bool,
    levels_trusted  Bool,
    note            String DEFAULT '',
    updated_at      DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY dongle_id;

INSERT INTO spectrum.dongle_roles (dongle_id, is_primary, levels_trusted, note)
SELECT * FROM (
    SELECT 'v3-01' AS dongle_id, true AS is_primary, true AS levels_trusted,
           'RTL-SDR Blog V3, patio dipole with 67 cm arms, no FM bandstop. Primary since 2026-09-26.' AS note
    UNION ALL SELECT 'v4-01', false, false,
           'RTL-SDR Blog V4, water-damaged: 16.5 dB short of the V3 at FM, 3.5 dB at UHF, 7 to 9 dB at 1090 MHz. Window antenna. Do not trust its absolute levels.'
) AS seed
WHERE (SELECT count() FROM spectrum.dongle_roles) = 0;

-- The latest run only. FINAL collapses each emitter to its newest row, and
-- the run_at filter drops emitters the latest run did not produce.
CREATE OR REPLACE VIEW spectrum.emitters_current AS
SELECT *
FROM spectrum.emitters FINAL
WHERE run_at = (SELECT max(run_at) FROM spectrum.emitters);

-- Both dongles in one list, for display. Emitters are per dongle. This view
-- pairs each primary emitter with the nearest secondary emitter whose
-- footprint touches it, and lists secondary emitters with no primary partner
-- on their own rows. The primary's numbers lead. The secondary contributes
-- only whether and how often it saw the same thing, never its levels.
CREATE OR REPLACE VIEW spectrum.emitters_merged AS
WITH
    primary_dongles AS (
        SELECT dongle_id FROM spectrum.dongle_roles FINAL WHERE is_primary
    ),
    p AS (
        SELECT * FROM spectrum.emitters_current
        WHERE dongle_id IN (SELECT dongle_id FROM primary_dongles)
    ),
    s AS (
        SELECT * FROM spectrum.emitters_current
        WHERE dongle_id NOT IN (SELECT dongle_id FROM primary_dongles)
    ),
    pairs AS (
        SELECT p.emitter_id AS primary_id,
               argMin(s.emitter_id, abs(toInt64(s.center_hz) - toInt64(p.center_hz))) AS secondary_id
        FROM p CROSS JOIN s
        WHERE toInt64(s.lo_hz) - 50000 <= toInt64(p.hi_hz) + 50000
          AND toInt64(s.hi_hz) + 50000 >= toInt64(p.lo_hz) - 50000
        GROUP BY primary_id
    )
SELECT
    p.emitter_id AS emitter_id,
    p.dongle_id AS dongle_id,
    true AS from_primary,
    p.center_hz AS center_hz,
    p.lo_hz AS lo_hz,
    p.hi_hz AS hi_hz,
    p.name AS name,
    p.name_source AS name_source,
    p.class_id AS class_id,
    p.artifact_suspect AS artifact_suspect,
    p.artifact_reason AS artifact_reason,
    p.first_seen AS first_seen,
    p.last_seen AS last_seen,
    p.duty_overall AS duty_overall,
    p.duty_24h AS duty_24h,
    p.hours_observed AS hours_observed,
    if(pairs.secondary_id = '', '', s.dongle_id) AS also_dongle_id,
    pairs.secondary_id AS also_emitter_id,
    if(pairs.secondary_id = '', NULL, s.duty_overall) AS also_duty_overall,
    if(pairs.secondary_id = '', NULL, s.last_seen) AS also_last_seen
FROM p
LEFT JOIN pairs ON pairs.primary_id = p.emitter_id
LEFT JOIN s ON s.emitter_id = pairs.secondary_id
UNION ALL
SELECT
    s.emitter_id, s.dongle_id, false, s.center_hz, s.lo_hz, s.hi_hz, s.name, s.name_source,
    s.class_id, s.artifact_suspect, s.artifact_reason, s.first_seen, s.last_seen,
    s.duty_overall, s.duty_24h, s.hours_observed,
    '', '', CAST(NULL AS Nullable(Float32)), CAST(NULL AS Nullable(DateTime64(3)))
FROM s
WHERE s.emitter_id NOT IN (SELECT secondary_id FROM pairs);
