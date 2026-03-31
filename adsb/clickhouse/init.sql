-- ADS-B ClickHouse Schema
-- Ingested from readsb SBS BaseStation format (port 30003)
--
-- SBS format fields:
-- MSG,transmission_type,session_id,aircraft_id,hex_ident,flight_id,
-- date_generated,time_generated,date_logged,time_logged,
-- callsign,altitude,ground_speed,track,lat,lon,vertical_rate,
-- squawk,alert,emergency,spi,is_on_ground

CREATE DATABASE IF NOT EXISTS adsb;

-- Main positions table — this is where the bulk of data goes
-- Using MergeTree with time-based ordering for efficient range queries
CREATE TABLE IF NOT EXISTS adsb.positions (
    timestamp       DateTime64(3) DEFAULT now64(3),
    hex_ident       String,              -- ICAO 24-bit address (e.g., '4B1613')
    callsign        Nullable(String),    -- flight number (e.g., 'OA261')
    altitude        Nullable(Int32),     -- feet
    ground_speed    Nullable(Float32),   -- knots
    track           Nullable(Float32),   -- degrees (heading)
    lat             Nullable(Float64),
    lon             Nullable(Float64),
    vertical_rate   Nullable(Int32),     -- feet/min
    squawk          Nullable(String),    -- transponder code
    is_on_ground    Nullable(UInt8),     -- 0/1
    msg_type        UInt8 DEFAULT 0      -- SBS message subtype (1-8)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, hex_ident)
TTL timestamp + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;

-- Materialized view: unique aircraft per hour (for dashboard)
CREATE MATERIALIZED VIEW IF NOT EXISTS adsb.aircraft_hourly
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(hour)
ORDER BY hour
AS SELECT
    toStartOfHour(timestamp) AS hour,
    uniqState(hex_ident) AS unique_aircraft,
    countState() AS total_messages,
    avgState(altitude) AS avg_altitude
FROM adsb.positions
GROUP BY hour;

-- Materialized view: per-aircraft summary (latest known state)
CREATE MATERIALIZED VIEW IF NOT EXISTS adsb.aircraft_latest
ENGINE = ReplacingMergeTree(last_seen)
ORDER BY hex_ident
AS SELECT
    hex_ident,
    argMax(callsign, timestamp) AS callsign,
    argMax(lat, timestamp) AS lat,
    argMax(lon, timestamp) AS lon,
    argMax(altitude, timestamp) AS altitude,
    argMax(ground_speed, timestamp) AS ground_speed,
    argMax(track, timestamp) AS track,
    max(timestamp) AS last_seen,
    count() AS message_count
FROM adsb.positions
GROUP BY hex_ident;
