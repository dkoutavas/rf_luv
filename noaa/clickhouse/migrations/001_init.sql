-- NOAA / Meteor weather satellite passes -- ClickHouse Schema (initial)
--
-- Each row = one satellite pass over the receiver location, with the
-- recording artifacts (WAV / decoded image) and quality metrics.
-- Lower volume than the live-streaming pipelines (~10 passes/day max),
-- but every row points at sizable on-disk artifacts so we keep image
-- *paths* in the table, not the images themselves.
--
-- Lifecycle:
--   pending      → scheduler inserted the row (pass predicted, not yet recorded)
--   recording    → recorder.py started rtl_fm
--   recorded     → WAV exists; awaiting decode
--   decoded      → image_path is set + SNR computed
--   failed       → see notes for why
--
-- Status transitions are recorded by re-INSERTing rows with the same
-- (pass_start, satellite) — ReplacingMergeTree on top via the latest_pass
-- view. Final state is whatever the latest INSERT for that pass said.

CREATE TABLE IF NOT EXISTS noaa.passes (
    inserted_at     DateTime64(3) DEFAULT now64(3),  -- row write time

    -- Pass identity (composite PK)
    pass_start      DateTime64(3),
    satellite       LowCardinality(String),          -- 'NOAA-15', 'NOAA-18', 'NOAA-19', 'METEOR-M2-3'
    pass_end        DateTime64(3),

    -- Pass geometry (from TLE prediction at time of insert)
    freq_mhz        Float32,
    max_elevation   Float32,                          -- peak elevation angle, degrees
    aos_azimuth     Float32 DEFAULT 0,                -- azimuth at acquisition-of-signal
    los_azimuth     Float32 DEFAULT 0,                -- azimuth at loss-of-signal
    duration_s      UInt16 DEFAULT 0,

    -- Recording / decode artifacts
    iq_path         String DEFAULT '',
    wav_path        String DEFAULT '',
    image_path      String DEFAULT '',
    decoder         LowCardinality(String) DEFAULT '',  -- 'noaa-apt', 'satdump', ''
    snr_db          Float32 DEFAULT 0,
    samples_lost    UInt32 DEFAULT 0,

    -- Lifecycle
    status          LowCardinality(String) DEFAULT 'pending',
                    -- pending | recording | recorded | decoded | failed
    notes           String DEFAULT '',

    -- Provenance
    dongle_id       LowCardinality(String) DEFAULT '',
    coordinator_locked Bool DEFAULT false             -- did we take the rtl-coordinator lock?
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(pass_start)
ORDER BY (pass_start, satellite, inserted_at)
TTL toDateTime(pass_start) + INTERVAL 365 DAY
SETTINGS index_granularity = 8192;

-- Latest row per (pass_start, satellite) — picks up the most recent INSERT
-- as the canonical state of a pass.
--
-- ClickHouse 24.3 ILLEGAL_AGGREGATION quirk: the version column's OUTPUT
-- alias must NOT match the column name used inside the argMax(...) calls
-- of the SAME SELECT. Aliasing `max(inserted_at) AS inserted_at` makes the
-- parser see argMax(field, inserted_at) as nesting argMax(inserted_at, ...).
-- Solution: alias the version column to a different name (`latest_seen`)
-- and reference that in the engine clause. Discovered 2026-05-02.
CREATE MATERIALIZED VIEW IF NOT EXISTS noaa.pass_latest
ENGINE = ReplacingMergeTree(latest_seen)
ORDER BY (pass_start, satellite)
AS SELECT
    pass_start,
    satellite,
    argMax(pass_end,        inserted_at) AS pass_end,
    argMax(freq_mhz,        inserted_at) AS freq_mhz,
    argMax(max_elevation,   inserted_at) AS max_elevation,
    argMax(duration_s,      inserted_at) AS duration_s,
    argMax(wav_path,        inserted_at) AS wav_path,
    argMax(image_path,      inserted_at) AS image_path,
    argMax(decoder,         inserted_at) AS decoder,
    argMax(snr_db,          inserted_at) AS snr_db,
    argMax(status,          inserted_at) AS status,
    argMax(notes,           inserted_at) AS notes,
    argMax(dongle_id,       inserted_at) AS dongle_id,
    max(inserted_at)                     AS latest_seen
FROM noaa.passes
GROUP BY pass_start, satellite;

-- Monthly summary -- counts per satellite, success rate, avg SNR.
CREATE MATERIALIZED VIEW IF NOT EXISTS noaa.monthly_summary
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMM(month)
ORDER BY (month, satellite)
AS SELECT
    toStartOfMonth(pass_start) AS month,
    satellite,
    countState() AS total_passes,
    sumStateIf(1, status = 'decoded') AS decoded_passes,
    avgStateIf(snr_db, status = 'decoded') AS avg_snr_db,
    avgState(max_elevation) AS avg_max_elevation
FROM noaa.passes
GROUP BY month, satellite;
