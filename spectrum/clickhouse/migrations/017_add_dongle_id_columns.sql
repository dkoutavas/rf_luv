-- 017: Add dongle_id column to scanner-owned tables (phase 1 of dual-dongle support)
--
-- This is the first of four migrations (017–020) preparing leap for a second
-- RTL-SDR dongle. The brief's "single file" design was split into four so each
-- phase has its own schema_migrations checkpoint — a 150-line monolithic
-- migration leaves the runner in an inconsistent state if it fails midway.
--
-- Scope of 017 — additive only, safe with running scanner:
--   * ADD COLUMN dongle_id LowCardinality(String) DEFAULT 'v3-01' to:
--       scans, peaks, events, sweep_health, scan_runs, compression_events
--   * MODIFY ORDER BY on scans to append dongle_id (only legal ALTER form in CH 24.3).
--   * Backfill existing rows to 'v3-01' via ALTER UPDATE so the column is
--     materialized on disk rather than relying on the default at read time.
--
-- NOT in this migration:
--   * peak_features and signal_classifications — those are ReplacingMergeTree
--     and need the dongle_id column folded into ORDER BY so V3+V4 rows for
--     the same freq_hz don't collapse on merge. That requires table
--     recreation, done in 018.
--   * hourly_baseline MV — needs GROUP BY update and re-aggregation. 019.
--   * dongle_comparison_view — 020.
--
-- Precondition: none. Scanner can keep running.
-- Rollback: ALTER TABLE ... DROP COLUMN dongle_id across all six tables.
-- The default makes the old scanner's writes continue to land correctly.

ALTER TABLE spectrum.scans
    ADD COLUMN IF NOT EXISTS dongle_id LowCardinality(String) DEFAULT 'v3-01' AFTER run_id;

ALTER TABLE spectrum.peaks
    ADD COLUMN IF NOT EXISTS dongle_id LowCardinality(String) DEFAULT 'v3-01' AFTER run_id;

ALTER TABLE spectrum.events
    ADD COLUMN IF NOT EXISTS dongle_id LowCardinality(String) DEFAULT 'v3-01' AFTER run_id;

ALTER TABLE spectrum.sweep_health
    ADD COLUMN IF NOT EXISTS dongle_id LowCardinality(String) DEFAULT 'v3-01' AFTER run_id;

ALTER TABLE spectrum.scan_runs
    ADD COLUMN IF NOT EXISTS dongle_id LowCardinality(String) DEFAULT 'v3-01' AFTER run_id;

ALTER TABLE spectrum.compression_events
    ADD COLUMN IF NOT EXISTS dongle_id LowCardinality(String) DEFAULT 'v3-01' AFTER detector_version;

-- Append dongle_id to scans ORDER BY. This is the only table where the brief
-- calls for dongle_id in the sort key (scans is the high-volume table where
-- per-dongle query pruning matters). Appending is the only legal form — CH
-- does not allow prepending to an existing ORDER BY without a full table
-- recreation, which would require a scanner-stop window this session won't
-- afford. For pure WHERE dongle_id='v3-01' queries the LowCardinality
-- skip-index handles most of the selectivity; combined filters like
-- WHERE freq_hz=X AND dongle_id=Y will now use the full sort prefix.
ALTER TABLE spectrum.scans
    MODIFY ORDER BY (freq_hz, timestamp, dongle_id);

-- Materialize the default on existing rows. ClickHouse mutations are async
-- (they complete on merges); the migration returns immediately and the actual
-- column writes finish in the background. Until mutations settle, reads of
-- dongle_id on unmodified parts will still return 'v3-01' via the default.
ALTER TABLE spectrum.scans                UPDATE dongle_id = 'v3-01' WHERE dongle_id = '';
ALTER TABLE spectrum.peaks                UPDATE dongle_id = 'v3-01' WHERE dongle_id = '';
ALTER TABLE spectrum.events               UPDATE dongle_id = 'v3-01' WHERE dongle_id = '';
ALTER TABLE spectrum.sweep_health         UPDATE dongle_id = 'v3-01' WHERE dongle_id = '';
ALTER TABLE spectrum.scan_runs            UPDATE dongle_id = 'v3-01' WHERE dongle_id = '';
ALTER TABLE spectrum.compression_events   UPDATE dongle_id = 'v3-01' WHERE dongle_id = '';
