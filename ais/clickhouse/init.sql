-- AIS Ship Tracking -ClickHouse Schema
-- Ingested from AIS-catcher NMEA output via ais_ingest.py
--
-- AIS message types stored:
--   1-3: Position reports (Class A) -speed, lat, lon, course, heading
--   5:   Static/voyage data -ship name, type, callsign, destination, dimensions
--   18:  Position reports (Class B) -smaller/leisure vessels
--   24:  Static data (Class B) -name, type, callsign

CREATE DATABASE IF NOT EXISTS ais;

-- Main positions table -all decoded AIS messages land here
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

-- Ship type lookup -ITU-R M.1371-5 Table 53
-- Source table must be populated before the dictionary is created
CREATE TABLE IF NOT EXISTS ais.ship_types_source (
    code UInt8,
    name String
) ENGINE = MergeTree() ORDER BY code;

INSERT INTO ais.ship_types_source VALUES
    (20, 'Wing in ground'),
    (30, 'Fishing'),
    (31, 'Towing'),
    (32, 'Towing (large)'),
    (33, 'Dredging/underwater ops'),
    (34, 'Diving ops'),
    (35, 'Military ops'),
    (36, 'Sailing'),
    (37, 'Pleasure craft'),
    (40, 'High-speed craft'),
    (50, 'Pilot vessel'),
    (51, 'Search and rescue'),
    (52, 'Tug'),
    (53, 'Port tender'),
    (54, 'Anti-pollution'),
    (55, 'Law enforcement'),
    (58, 'Medical transport'),
    (59, 'Noncombatant (RR)'),
    (60, 'Passenger'),
    (69, 'Passenger -No info'),
    (70, 'Cargo'),
    (79, 'Cargo -No info'),
    (80, 'Tanker'),
    (89, 'Tanker -No info'),
    (90, 'Other'),
    (99, 'Other -No info');

CREATE DICTIONARY IF NOT EXISTS ais.ship_types (
    code UInt8,
    name String
)
PRIMARY KEY code
SOURCE(CLICKHOUSE(TABLE 'ship_types_source' DB 'ais' USER 'ais' PASSWORD 'ais_local'))
LAYOUT(FLAT())
LIFETIME(0);

-- Geofencing zones -rectangular bounding boxes for Saronic Gulf areas
-- Used in dashboard queries via inline multiIf (priority order: most specific first)
CREATE TABLE IF NOT EXISTS ais.zones (
    zone_id   UInt8,
    zone_name String,
    lat_min   Float64,
    lat_max   Float64,
    lon_min   Float64,
    lon_max   Float64
) ENGINE = MergeTree() ORDER BY zone_id;

INSERT INTO ais.zones VALUES
    (1, 'Piraeus Port',    37.93, 37.96, 23.60, 23.66),
    (2, 'Salamina Strait', 37.90, 37.96, 23.48, 23.58),
    (3, 'Elefsina Bay',    38.01, 38.06, 23.48, 23.56),
    (4, 'Saronic Open',    37.50, 38.10, 23.20, 24.00);

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
-- Uses argMaxIf so that position fields (from types 1-3, 18) don't clobber
-- identity fields (from types 5, 24) with NULL, and vice versa.
-- Think of it like a UPSERT that only overwrites non-NULL columns.
CREATE MATERIALIZED VIEW IF NOT EXISTS ais.ship_latest
ENGINE = ReplacingMergeTree(last_seen)
ORDER BY mmsi
AS SELECT
    mmsi,
    argMaxIf(ship_name,   timestamp, ship_name   IS NOT NULL) AS ship_name,
    argMaxIf(callsign,    timestamp, callsign    IS NOT NULL) AS callsign,
    argMaxIf(ship_type,   timestamp, ship_type   IS NOT NULL) AS ship_type,
    argMaxIf(destination,  timestamp, destination  IS NOT NULL) AS destination,
    argMaxIf(imo,          timestamp, imo          IS NOT NULL) AS imo,
    argMaxIf(lat,          timestamp, lat          IS NOT NULL) AS lat,
    argMaxIf(lon,          timestamp, lon          IS NOT NULL) AS lon,
    argMaxIf(speed,        timestamp, speed        IS NOT NULL) AS speed,
    argMaxIf(course,       timestamp, course       IS NOT NULL) AS course,
    argMaxIf(heading,      timestamp, heading      IS NOT NULL) AS heading,
    argMaxIf(nav_status,   timestamp, nav_status   IS NOT NULL) AS nav_status,
    max(timestamp) AS last_seen,
    count() AS message_count
FROM ais.positions
GROUP BY mmsi;
