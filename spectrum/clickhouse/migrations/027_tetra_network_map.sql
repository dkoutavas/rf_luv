-- 027: The TETRA network map
--
-- Where every TETRA carrier in the 88-470 MHz sweep sits, how often it is
-- on, and at what hours. Occupancy from sweep data only: center, bandwidth,
-- duty cycle and time-of-day pattern. TETRA is encrypted. Nothing here
-- captures, demodulates or decodes it, and rf.py autopsy refuses any capture
-- that would include one of these carriers.
--
-- spectrum/emitters.py decides, per emitter:
--   * tetra_evidence: comma list of class (the classifier says tetra),
--     allocation (inside a TETRA range of allocations or signal_catalog),
--     named (a listening_log or known_frequencies entry within one bin says
--     tetra). Empty means not TETRA.
--   * tetra_network: public_safety (380-400 MHz), civil (410-430 MHz) or
--     other. "other" is a finding: TETRA evidence outside both bands.
--   * tetra_mode: continuous (on in at least half of the sweeps) or bursty.
--   * diurnal_hits / diurnal_sweeps: 24 counts by local hour of day (Europe/
--     Athens), of sweeps with a peak and of sweeps that covered the
--     frequency since first_seen. Hours the dongle never swept have 0 sweeps,
--     and the views show NULL there, not 0 % activity.
--
-- The views only present those numbers. Artifact suspects are left out: a
-- tile-grid spur with a tetra label is not a carrier. The primary dongle
-- (dongle_roles, the V3) is the source of truth. The secondary appears next
-- to it but never in a count, and no view here exposes a level.
--
-- Precondition: 026. Additive.
-- Rollback: DROP the three tetra_* views, then DROP the five columns.

ALTER TABLE spectrum.emitters ADD COLUMN IF NOT EXISTS tetra_network LowCardinality(String) DEFAULT '' AFTER artifact_reason;
ALTER TABLE spectrum.emitters ADD COLUMN IF NOT EXISTS tetra_mode LowCardinality(String) DEFAULT '' AFTER tetra_network;
ALTER TABLE spectrum.emitters ADD COLUMN IF NOT EXISTS tetra_evidence String DEFAULT '' AFTER tetra_mode;
ALTER TABLE spectrum.emitters ADD COLUMN IF NOT EXISTS diurnal_hits Array(UInt32) AFTER tetra_evidence;
ALTER TABLE spectrum.emitters ADD COLUMN IF NOT EXISTS diurnal_sweeps Array(UInt32) AFTER diurnal_hits;

-- Recreated so its column list includes the new columns.
CREATE OR REPLACE VIEW spectrum.emitters_current AS
SELECT *
FROM spectrum.emitters FINAL
WHERE run_at = (SELECT max(run_at) FROM spectrum.emitters);

-- One row per TETRA carrier per dongle. other_dongle_sees_it: a carrier on
-- the other role's dongle overlaps this one.
CREATE OR REPLACE VIEW spectrum.tetra_carriers AS
WITH
    primary_dongles AS (
        SELECT dongle_id FROM spectrum.dongle_roles FINAL WHERE is_primary
    ),
    c AS (
        SELECT *, dongle_id IN (SELECT dongle_id FROM primary_dongles) AS is_primary
        FROM spectrum.emitters_current
        WHERE tetra_network != '' AND NOT artifact_suspect
    ),
    partners AS (
        SELECT a.emitter_id AS emitter_id, count() AS n
        FROM c AS a CROSS JOIN c AS b
        WHERE a.is_primary != b.is_primary
          AND toInt64(b.lo_hz) - 50000 <= toInt64(a.hi_hz) + 50000
          AND toInt64(b.hi_hz) + 50000 >= toInt64(a.lo_hz) - 50000
        GROUP BY a.emitter_id
    )
SELECT
    c.tetra_network AS network,
    if(c.is_primary, 'primary', 'secondary') AS role,
    c.dongle_id AS dongle_id,
    c.emitter_id AS emitter_id,
    c.center_hz AS center_hz,
    round(c.center_hz / 1000000.0, 3) AS center_mhz,
    c.lo_hz AS lo_hz,
    c.hi_hz AS hi_hz,
    c.bandwidth_hz AS bandwidth_hz,
    if(c.tetra_mode = 'continuous', 'continuous (likely control channel)', 'bursty') AS mode,
    c.duty_overall AS duty_overall,
    c.duty_24h AS duty_24h,
    c.hours_observed AS hours_observed,
    c.first_seen AS first_seen,
    c.last_seen AS last_seen,
    c.diurnal_hits AS diurnal_hits,
    c.diurnal_sweeps AS diurnal_sweeps,
    arrayMap((h, s) -> if(s = 0, NULL, round(h / s, 3)), c.diurnal_hits, c.diurnal_sweeps) AS diurnal_duty,
    NOT has(splitByChar(',', c.tetra_evidence), 'allocation') AS outside_known_allocation,
    c.tetra_evidence AS evidence,
    c.name AS name,
    c.name_source AS name_source,
    p.n > 0 AS other_dongle_sees_it
FROM c
LEFT JOIN partners AS p ON p.emitter_id = c.emitter_id;

-- Per network, primary dongle only. diurnal_duty pools the carriers: the
-- network's sweeps with a peak over its covering sweeps, by hour of day.
CREATE OR REPLACE VIEW spectrum.tetra_networks AS
SELECT
    network,
    countIf(role = 'primary') AS carriers,
    countIf(role = 'primary' AND mode != 'bursty') AS likely_control_channels,
    countIf(role = 'primary' AND mode = 'bursty') AS bursty,
    countIf(role = 'primary' AND outside_known_allocation) AS outside_known_allocation,
    countIf(role = 'primary' AND other_dongle_sees_it) AS also_on_secondary,
    countIf(role = 'secondary' AND NOT other_dongle_sees_it) AS secondary_only,
    maxIf(hours_observed, role = 'primary') AS hours_observed,
    arrayMap((h, s) -> if(s = 0, NULL, round(h / s, 3)),
             sumForEachIf(diurnal_hits, role = 'primary'),
             sumForEachIf(diurnal_sweeps, role = 'primary')) AS diurnal_duty
FROM spectrum.tetra_carriers
GROUP BY network;

-- The same activity in long form, one row per network and hour of day, for
-- the dashboard's network-by-hour heatmap.
CREATE OR REPLACE VIEW spectrum.tetra_network_hourly AS
SELECT
    network,
    hour_of_day,
    carriers,
    if(s = 0, NULL, round(h / s, 3)) AS duty
FROM (
    SELECT
        network,
        count() AS carriers,
        sumForEach(diurnal_hits) AS hits,
        sumForEach(diurnal_sweeps) AS sweeps
    FROM spectrum.tetra_carriers
    WHERE role = 'primary'
    GROUP BY network
)
ARRAY JOIN range(24) AS hour_of_day, hits AS h, sweeps AS s;
