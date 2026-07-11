-- RDS (Radio Data System) pipeline — initial schema.
--
-- RDS is the 1187.5 bps data subcarrier at 57 kHz on an FM broadcast MPX
-- signal. It carries public station metadata: PS (station name), PI code,
-- RadioText, PTY (programme type), TP/TA flags, and clock-time. This is
-- public broadcast metadata only — nothing private is decoded.
--
-- Numbered migrations + migrate.py from day one (mirrors acars/spectrum).
-- The database + user are created earlier by the infra ch-bootstrap; this
-- migration only creates tables and views (same contract as acars 001).

CREATE TABLE IF NOT EXISTS rds.messages (
    timestamp        DateTime64(3) DEFAULT now64(3),
    freq_hz          UInt32,                              -- tuned FM carrier (99600000)
    pi_code          UInt16,                              -- Programme Identification
    group_type       LowCardinality(String),              -- '0A','0B','2A','2B','4A',...
    tp               UInt8 DEFAULT 0,                     -- Traffic Programme flag
    ta               UInt8 DEFAULT 0,                     -- Traffic Announcement flag
    pty              UInt8 DEFAULT 0,                     -- Programme Type (0-31)
    ms               UInt8 DEFAULT 0,                     -- Music/Speech flag
    ps               LowCardinality(String) DEFAULT '',   -- 8-char station name, set only when fully assembled
    radiotext        String DEFAULT '',                   -- up to 64 chars, set only when assembled/flushed
    clock_utc        Nullable(DateTime),                  -- from 4A CT group
    clock_offset_min Int16 DEFAULT 0,                     -- local offset in minutes
    block_errors     UInt8 DEFAULT 0,                     -- CRC-failed blocks since previous emitted group
    dongle_id        LowCardinality(String) DEFAULT '',   -- 'v4-01', mirrors acars/spectrum convention
    raw_group        String DEFAULT ''                    -- '1234 0408 E0E0 4B4F' hex, debugging
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, pi_code)
TTL toDateTime(timestamp) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;

-- Hourly rollup: group volume, distinct stations, mean block-error rate
-- (the pipeline-health signal). AggregatingMergeTree, acars.hourly_stats shape.
CREATE MATERIALIZED VIEW IF NOT EXISTS rds.hourly_stats
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(hour)
ORDER BY (hour, dongle_id)
AS SELECT
    toStartOfHour(timestamp) AS hour,
    dongle_id,
    countState()             AS total_groups,
    uniqState(pi_code)       AS unique_stations,
    avgState(block_errors)   AS avg_block_errors
FROM rds.messages
GROUP BY hour, dongle_id;

-- Canonical latest state per station (PI). This is what the dashboard's
-- "you are tuned to X" stat reads. acars.flight_latest pattern verbatim:
-- argMaxIf keeps the last non-empty PS/RadioText without NULL-clobbering.
CREATE MATERIALIZED VIEW IF NOT EXISTS rds.station_latest
ENGINE = ReplacingMergeTree(last_seen)
ORDER BY pi_code
AS SELECT
    pi_code,
    argMax(freq_hz, timestamp)                        AS freq_hz,
    argMaxIf(ps, timestamp, ps != '')                 AS ps,
    argMaxIf(radiotext, timestamp, radiotext != '')   AS radiotext,
    argMax(pty, timestamp)                            AS pty,
    argMax(tp, timestamp)                             AS tp,
    argMax(ta, timestamp)                             AS ta,
    max(timestamp)                                    AS last_seen,
    count()                                           AS group_count
FROM rds.messages
GROUP BY pi_code;
