-- 023: Forensic IQ capture — trigger queue + capture results (feature D2)
--
-- Two tables backing spectrum/iq_capture.py, the standalone coordinator-shared
-- raw-IQ recorder. This is the time-domain layer that unblocks blind decoding
-- (D3): the scanner only ever stores FFT power, so a compression event or an
-- operator target can be located in frequency but not inspected for modulation.
-- iq_capture.py time-shares the single local V4 dongle with the running scanner
-- via the flock coordinator (spectrum/coordinator.py) and drops a .cs8 file the
-- operator pulls into SDR++/baudline/inspectrum.
--
-- forensic_trigger — the work queue. A row is INSERTed by the trigger inserter
--   (insert_trigger(), enforcing the 3/hr rate-limit + drop-if-queue-already-
--   pending + legal blocklist) or the operator CLI. The daemon polls
--   status='pending' oldest-first, claims by mutating status, and drives the
--   capture. Status transitions run as synchronous mutations (mutations_sync=1)
--   so the next poll cannot re-claim a just-captured trigger.
--
-- iq_captures — one row per successfully written .cs8 file (the results table;
--   the plan's "spectrum.iq_captures row"). Joins back to forensic_trigger on
--   trigger_id. This is the ClickHouse-side manifest; each .cs8 also carries a
--   .json sidecar so a file pulled off-box is self-describing without CH.
--
-- Deviations from spectrum/docs/forensic_capture.md's schema sketch (that doc
-- predates the coordinator and the dual-dongle work):
--   * dongle_id added (migration 017 added dongle_id across scanner tables).
--   * 'blocked'=5 status added for the legal blocklist (TETRA/cellular refusal).
--   * span_hz default 2048000 — a real rtl_tcp sample rate (== SCAN_SAMPLE_RATE),
--     not the doc's round 2000000.
--   * gain_db default 20.0 — the scanner default; the doc's 2.0 dB is near-deaf.
--   * new iq_captures results table (the doc had only the trigger table).
--
-- Precondition: none. Additive; scanner keeps running.
-- Rollback: DROP TABLE spectrum.iq_captures; DROP TABLE spectrum.forensic_trigger;

CREATE TABLE IF NOT EXISTS spectrum.forensic_trigger (
    trigger_id      String DEFAULT generateUUIDv4(),
    requested_at    DateTime64(3) DEFAULT now64(3),
    dongle_id       LowCardinality(String) DEFAULT 'v4-01',
    freq_hz         UInt32,
    span_hz         UInt32 DEFAULT 2048000,
    duration_s      Float32 DEFAULT 5.0,
    gain_db         Float32 DEFAULT 20.0,
    source          String,                          -- 'compression_event:<sweep_id>' / 'operator:<name>' / 'operator:cli'
    status          Enum8('pending'=0, 'captured'=1, 'failed'=2, 'rate_limited'=3, 'cancelled'=4, 'blocked'=5) DEFAULT 'pending',
    captured_at     Nullable(DateTime64(3)),
    capture_path    String DEFAULT '',
    capture_bytes   UInt64 DEFAULT 0,
    error           String DEFAULT ''
) ENGINE = MergeTree()
ORDER BY (requested_at, trigger_id)
TTL toDateTime(requested_at) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS spectrum.iq_captures (
    capture_id      String DEFAULT generateUUIDv4(),
    trigger_id      String,
    captured_at     DateTime64(3) DEFAULT now64(3),
    dongle_id       LowCardinality(String),
    freq_hz         UInt32,
    sample_rate_hz  UInt32,
    duration_s      Float32,
    gain_db         Float32,
    format          String DEFAULT 'cs8',
    path            String,
    size_bytes      UInt64,
    source          String
) ENGINE = MergeTree()
ORDER BY (captured_at, capture_id)
TTL toDateTime(captured_at) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;
