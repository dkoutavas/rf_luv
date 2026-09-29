-- POCSAG paging schema.
-- Applied by infra/migrate.py as the `pocsag` user (see infra/bootstrap.sh).
-- The database + user are created first by infra/clickhouse/bootstrap.sql.

CREATE DATABASE IF NOT EXISTS pocsag;

-- One row per decoded page. message holds the decoded text (stored locally
-- only; see pocsag/README.md for the privacy posture).
CREATE TABLE IF NOT EXISTS pocsag.messages (
    timestamp   DateTime64(3) DEFAULT now64(3),
    freq_hz     UInt32,                     -- tuned channel, stamped by ingest
    protocol    LowCardinality(String),     -- POCSAG512 / POCSAG1200 / POCSAG2400
    capcode     UInt32,                     -- pager address
    function    UInt8,                      -- 0-3 (sub-address / message class)
    msg_type    LowCardinality(String),     -- alpha / numeric / tone
    message     String,                     -- decoded text
    raw_line    String,                     -- verbatim multimon-ng line
    dongle_id   LowCardinality(String)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, capcode)
TTL toDateTime(timestamp) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;

-- Pages per hour per baud rate (for dashboard rate panels).
CREATE MATERIALIZED VIEW IF NOT EXISTS pocsag.hourly_stats
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(hour)
ORDER BY (hour, protocol)
AS SELECT
    toStartOfHour(timestamp) AS hour,
    protocol,
    uniqState(capcode) AS unique_capcodes,
    countState() AS total_pages
FROM pocsag.messages
GROUP BY hour, protocol;

-- Latest page per pager address. ReplacingMergeTree keeps the row with the
-- newest last_seen. message_count is per-insert-block (the same MV caveat as
-- ais.ship_latest); use count() over pocsag.messages for an exact total.
CREATE MATERIALIZED VIEW IF NOT EXISTS pocsag.capcode_latest
ENGINE = ReplacingMergeTree(last_seen)
ORDER BY capcode
AS SELECT
    capcode,
    argMax(message, timestamp)  AS last_message,
    argMax(msg_type, timestamp) AS last_msg_type,
    max(timestamp)              AS last_seen,
    count()                     AS message_count
FROM pocsag.messages
GROUP BY capcode;
