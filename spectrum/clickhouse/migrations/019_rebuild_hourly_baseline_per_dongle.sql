-- 019: Rebuild hourly_baseline as a per-dongle aggregate
--
-- hourly_baseline is the rolling per-frequency noise-floor model used by
-- detect_compression.py's sig_baseline flag. The current definition has a
-- global GROUP BY (hour, freq_hz), which is fine for one dongle but wrong
-- the moment V3 (unfiltered) and V4 (filtered) ingest into the same scans
-- table. The two dongles will have different noise regimes at most
-- frequencies — a combined baseline would misrepresent both and poison
-- compression detection for months.
--
-- Option (a) from the brief: rebuild with dongle_id in GROUP BY. Break
-- once, now, while V3 is still the only source.
--
-- Approach: explicit target table + TO-form MV. This avoids POPULATE (which
-- races with inserts during the populate window) and lets us backfill the
-- target in a single INSERT-SELECT that we control.
--
-- PRECONDITION: rtl-scanner@v3-01.service must be stopped during this
-- migration. The backfill INSERT-SELECT takes 30-60s on ~11.7M scan rows;
-- any scans rows arriving during that window will be missed by both the
-- backfill (the SELECT snapshot is already running) and the new MV (not
-- yet created), permanently leaving a 20% sample gap in one hour's
-- aggregate. Combine this migration's execution with the scanner cutover
-- downtime window. See spectrum/docs/dongle_cutover_runbook.md.
--
-- NOT idempotent: if the migration fails partway, manual cleanup is
-- required. The cutover runbook documents the recovery procedure.

-- Defensive cleanup of any half-applied state from a prior failed run.
-- Safe: these tables only exist if this migration has previously attempted
-- and failed.
DROP TABLE IF EXISTS spectrum.hourly_baseline_new;
DROP VIEW  IF EXISTS spectrum.hourly_baseline_mv;

-- Step 1: Create the new target table with dongle_id in ORDER BY.
-- freq_hz stays first (most queries pivot on frequency) and dongle_id
-- comes before hour so per-dongle queries over a frequency range get
-- the tightest compaction.
CREATE TABLE spectrum.hourly_baseline_new (
    hour         DateTime,
    freq_hz      UInt32,
    dongle_id    LowCardinality(String) DEFAULT 'v3-01',
    avg_power    AggregateFunction(avg, Float32),
    std_power    AggregateFunction(stddevPop, Float32),
    sample_count AggregateFunction(count)
) ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(hour)
ORDER BY (freq_hz, dongle_id, hour);

-- Step 2: Backfill the new table from scans. scans.dongle_id exists and
-- is populated (migration 017) so the GROUP BY picks it up correctly.
INSERT INTO spectrum.hourly_baseline_new
SELECT
    toStartOfHour(timestamp) AS hour,
    freq_hz,
    dongle_id,
    avgState(power_dbfs)     AS avg_power,
    stddevPopState(power_dbfs) AS std_power,
    countState()             AS sample_count
FROM spectrum.scans
GROUP BY hour, freq_hz, dongle_id;

-- Step 3: Drop the old MV. Because the old MV used inner storage (no TO
-- clause), this also drops the underlying .inner.* storage table.
DROP VIEW spectrum.hourly_baseline;

-- Step 4: Rename the new backfilled target into place.
RENAME TABLE spectrum.hourly_baseline_new TO spectrum.hourly_baseline;

-- Step 5: Create the incremental MV that writes to the renamed target.
-- From here on, new scans inserts fire this MV which aggregates
-- per (hour, freq_hz, dongle_id).
CREATE MATERIALIZED VIEW spectrum.hourly_baseline_mv
TO spectrum.hourly_baseline
AS SELECT
    toStartOfHour(timestamp) AS hour,
    freq_hz,
    dongle_id,
    avgState(power_dbfs)     AS avg_power,
    stddevPopState(power_dbfs) AS std_power,
    countState()             AS sample_count
FROM spectrum.scans
GROUP BY hour, freq_hz, dongle_id;

-- Followup: spectrum/analysis/detect_compression.py reads hourly_baseline
-- without a dongle_id filter. Until that filter is added, its queries will
-- span both dongles and return misleading aggregates once V4 ingests. See
-- spectrum/docs/followups/dongle_id_downstream.md for the specific changes.
