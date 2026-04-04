-- AIS Ship Tracking — ClickHouse Schema
-- Ingested from AIS-catcher NMEA output via ais_ingest.py
--
-- AIS message types stored:
--   1-3: Position reports (Class A) — speed, lat, lon, course, heading
--   5:   Static/voyage data — ship name, type, callsign, destination, dimensions
--   18:  Position reports (Class B) — smaller/leisure vessels
--   24:  Static data (Class B) — name, type, callsign

CREATE DATABASE IF NOT EXISTS ais;

-- Main positions table — all decoded AIS messages land here
-- Position data (types 1-3, 18) has lat/lon/speed/course
-- Static data (types 5, 24) has ship_name/callsign/destination
CREATE TABLE IF NOT EXISTS ais.positions (
    timestamp       DateTime64(3) DEFAULT now64(3),
    mmsi            UInt32,              -- Maritime Mobile Service Identity (unique per vessel)
    msg_type        UInt8,               -- AIS message type (1-3, 5, 18, 24)
    nav_status      Nullable(UInt8),     -- navigation status (0=engine, 1=anchor, 5=moored...)
    speed           Nullable(Float32),   -- knots (SOG)
    lat             Nullable(Float64),   -- degrees
    lon             Nullable(Float64),   -- degrees
    course          Nullable(Float32),   -- degrees (COG)
    heading         Nullable(UInt16),    -- degrees (true heading)
    ship_name       Nullable(String),    -- vessel name (from type 5/24)
    ship_type       Nullable(UInt8),     -- vessel type code
    callsign        Nullable(String),    -- radio callsign
    destination     Nullable(String),    -- reported destination (from type 5)
    imo             Nullable(UInt32),    -- IMO number (from type 5)
    dim_bow         Nullable(UInt16),    -- meters, bow to AIS antenna
    dim_stern       Nullable(UInt16),    -- meters, stern to AIS antenna
    dim_port        Nullable(UInt8),     -- meters, port to AIS antenna
    dim_starboard   Nullable(UInt8)      -- meters, starboard to AIS antenna
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, mmsi)
TTL toDateTime(timestamp) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;

-- Materialized view: unique ships per hour + message stats
CREATE MATERIALIZED VIEW IF NOT EXISTS ais.hourly_stats
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(hour)
ORDER BY hour
AS SELECT
    toStartOfHour(timestamp) AS hour,
    uniqState(mmsi) AS unique_ships,
    countState() AS total_messages,
    avgState(speed) AS avg_speed
FROM ais.positions
GROUP BY hour;

-- Materialized view: latest known state per vessel
-- Merges position data with static data via argMax — whichever message
-- arrived most recently for each field wins. This is how we get
-- "MMSI 237012345 is BLUE STAR PATMOS at 37.94N 23.65E doing 18 knots"
CREATE MATERIALIZED VIEW IF NOT EXISTS ais.ship_latest
ENGINE = ReplacingMergeTree(last_seen)
ORDER BY mmsi
AS SELECT
    mmsi,
    argMax(ship_name, timestamp) AS ship_name,
    argMax(callsign, timestamp) AS callsign,
    argMax(ship_type, timestamp) AS ship_type,
    argMax(destination, timestamp) AS destination,
    argMax(imo, timestamp) AS imo,
    argMax(lat, timestamp) AS lat,
    argMax(lon, timestamp) AS lon,
    argMax(speed, timestamp) AS speed,
    argMax(course, timestamp) AS course,
    argMax(heading, timestamp) AS heading,
    argMax(nav_status, timestamp) AS nav_status,
    max(timestamp) AS last_seen,
    count() AS message_count
FROM ais.positions
GROUP BY mmsi;
