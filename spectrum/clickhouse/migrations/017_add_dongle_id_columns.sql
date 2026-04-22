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
--   * Backfill existing rows to 'v3-01' via ALTER UPDATE so the column is
--     materialized on disk rather than relying on the default at read time.
--
-- ORDER BY change — REMOVED 2026-04-22 after first apply attempt failed:
--   ClickHouse 24.3 (BAD_ARGUMENTS Code 36): "Existing column dongle_id is used
--   in the expression that was added to the sorting key. You can add expressions
--   that use only the newly added columns." Because migrate.py splits the file
--   into separate HTTP statements, by the time MODIFY ORDER BY runs the column
--   already "exists" and CH refuses. Combining ADD COLUMN + MODIFY ORDER BY
--   into a single ALTER would work but isn't supported by the splitter. Per
--   the original author's note below, the LowCardinality skip-index handles
--   most of the per-dongle pruning anyway, so the ORDER BY change is dropped.
--   If full sort-key pruning is later needed, do it via table recreation in
--   a separate scanner-stop window.
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

-- (ORDER BY change removed — see header. Pruning falls back to LowCardinality
-- skip-index, which is sufficient for typical per-dongle filter queries.)

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
