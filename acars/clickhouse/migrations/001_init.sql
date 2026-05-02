-- ACARS Aircraft Communications -- ClickHouse Schema (initial)
--
-- ACARS = Aircraft Communications Addressing and Reporting System.
-- 2400-baud MSK over AM in the airband (131.525, 131.725, 131.825 MHz
-- in Europe). Carries free-text crew messages, ATC clearances, OOOI
-- events (Out-of-gate / Off-runway / On-runway / In-gate), weather
-- requests, and CPDLC/ADS-C application data via libacars.
--
-- Field set sourced from acarsdec output.c buildjson() — confirmed
-- against acarsdec-3.7. acarsdec emits freq/level as STRINGS (with
-- "%.3f"/"%.1f" formatting), so the ingest worker parses them to
-- floats before inserting.
--
-- Joinable to adsb.positions on (tail, flight) for end-to-end aircraft
-- correlation: ADS-B gives position/altitude, ACARS gives intent/text.

CREATE TABLE IF NOT EXISTS acars.messages (
    timestamp       DateTime64(3) DEFAULT now64(3),

    -- RF metadata
    freq_mhz        Float32,                                     -- parsed from acarsdec "freq" string
    channel         UInt8 DEFAULT 0,                             -- acarsdec channel index (0..N-1)
    level_db        Float32 DEFAULT 0,                           -- parsed from acarsdec "level" string
    err_count       UInt8 DEFAULT 0,                             -- "error" field — CRC errors

    -- ACARS protocol fields
    mode            LowCardinality(String) DEFAULT '',           -- single-char, typically "2"
    label           LowCardinality(String) DEFAULT '',           -- 2-char ACARS label (e.g. "H1", "_d", "B6")
    block_id        String DEFAULT '',
    ack             String DEFAULT '',                           -- bool or single char in source; stringified
    tail            LowCardinality(String) DEFAULT '',           -- aircraft registration (e.g. "N12345")
    flight          LowCardinality(String) DEFAULT '',           -- callsign (e.g. "AAL123") -- downlink only
    msgno           String DEFAULT '',
    text            String DEFAULT '',
    msg_end         UInt8 DEFAULT 0,                             -- "end" — ETB marker

    -- OOOI / flight progress fields (sparse, present mostly on H1/B1 labels)
    depa            LowCardinality(String) DEFAULT '',
    dsta            LowCardinality(String) DEFAULT '',
    eta             String DEFAULT '',
    gtout           String DEFAULT '',
    gtin            String DEFAULT '',
    wloff           String DEFAULT '',
    wlin            String DEFAULT '',

    -- libacars decoded application data (CPDLC, ADS-C, OOOI parser)
    sublabel        String DEFAULT '',
    mfi             String DEFAULT '',
    libacars_app    LowCardinality(String) DEFAULT '',           -- name of decoded app if libacars matched
    libacars_json   String DEFAULT '',                           -- nested libacars output, raw JSON

    -- Decoder ground truth (where the message came from)
    dongle_id       LowCardinality(String) DEFAULT '',           -- e.g. "v4-01" -- mirrors spectrum.scans
    station_id      LowCardinality(String) DEFAULT '',           -- acarsdec --station-id flag

    -- Catch-all so future acarsdec fields aren't lost
    raw_json        String DEFAULT ''
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, flight, tail)
TTL toDateTime(timestamp) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;

-- Hourly aggregation -- per-hour counts, uniques, signal level distribution
-- Used for "is the pipe alive?" dashboards and for capacity planning.
CREATE MATERIALIZED VIEW IF NOT EXISTS acars.hourly_stats
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(hour)
ORDER BY (hour, dongle_id)
AS SELECT
    toStartOfHour(timestamp) AS hour,
    dongle_id,
    countState() AS total_messages,
    uniqState(flight) AS unique_flights,
    uniqState(tail) AS unique_tails,
    avgState(level_db) AS avg_level_db,
    avgState(err_count) AS avg_err_count
FROM acars.messages
GROUP BY hour, dongle_id;

-- Latest seen per flight callsign -- for "who's flying now" dashboard panel.
-- Note: uplinks lack the flight field, so this naturally excludes them.
CREATE MATERIALIZED VIEW IF NOT EXISTS acars.flight_latest
ENGINE = ReplacingMergeTree(last_seen)
ORDER BY flight
AS SELECT
    flight,
    argMax(tail, timestamp)         AS tail,
    argMax(depa, timestamp)         AS depa,
    argMax(dsta, timestamp)         AS dsta,
    argMax(text, timestamp)         AS last_text,
    argMax(label, timestamp)        AS last_label,
    argMax(freq_mhz, timestamp)     AS last_freq_mhz,
    argMax(level_db, timestamp)     AS last_level_db,
    max(timestamp)                  AS last_seen,
    count()                         AS message_count
FROM acars.messages
WHERE flight != ''
GROUP BY flight;

-- Latest seen per tail registration -- includes uplinks (which have no flight).
CREATE MATERIALIZED VIEW IF NOT EXISTS acars.tail_latest
ENGINE = ReplacingMergeTree(last_seen)
ORDER BY tail
AS SELECT
    tail,
    argMax(flight, timestamp)       AS last_flight,
    argMax(text, timestamp)         AS last_text,
    argMax(label, timestamp)        AS last_label,
    argMax(freq_mhz, timestamp)     AS last_freq_mhz,
    argMax(level_db, timestamp)     AS last_level_db,
    max(timestamp)                  AS last_seen,
    count()                         AS message_count
FROM acars.messages
WHERE tail != ''
GROUP BY tail;

-- Frequency activity table -- counts per frequency per dongle.
-- Exists primarily to feed back into spectrum.classifier: a frequency
-- with steady ACARS traffic is high-confidence proof of an ACARS station.
-- The spectrum-classifier service reads this via remote() once configured;
-- no cross-DB write happens from this pipeline (different ClickHouse host).
CREATE MATERIALIZED VIEW IF NOT EXISTS acars.freq_activity
ENGINE = ReplacingMergeTree(last_seen)
ORDER BY (freq_mhz, dongle_id)
AS SELECT
    freq_mhz,
    dongle_id,
    max(timestamp) AS last_seen,
    count() AS message_count,
    avg(level_db) AS avg_level_db
FROM acars.messages
GROUP BY freq_mhz, dongle_id;
