-- AIS Ship Tracking - consolidated idempotent ClickHouse schema.
--
-- This file supersedes ais/clickhouse/init.sql + ais/clickhouse/migrate_ship_latest.sql.
-- It is applied once, as the per-db user 'ais', by infra/bootstrap.sh against the
-- shared ClickHouse server. Two changes versus the originals make it safe to
-- re-run on an already-populated database:
--   1. Every seed INSERT (ship_types_source, zones) is guarded by a
--      `WHERE (SELECT count() FROM <tbl>) = 0` so a re-run never double-inserts.
--   2. The corrected (argMaxIf, no-NULL-clobber) ship_latest view from
--      migrate_ship_latest.sql is folded in here exactly once; the backfill
--      INSERT from that migration is dropped because on a fresh database the
--      materialized view captures inserts going forward and there is nothing
--      to backfill, and on a re-run a blind backfill would double-count.
--
-- The ship_types dictionary keeps the WITH-credentials SOURCE form
-- (USER 'ais' PASSWORD 'ais_local') so it loads under the per-db user on the
-- shared server. migrate_ship_latest.sql omitted the creds, which only worked
-- when the dictionary was created by an admin-equivalent user.
--
-- AIS message types stored:
--   1-3: Position reports (Class A) - speed, lat, lon, course, heading
--   5:   Static/voyage data - ship name, type, callsign, destination, dimensions
--   18:  Position reports (Class B) - smaller/leisure vessels
--   24:  Static data (Class B) - name, type, callsign

CREATE DATABASE IF NOT EXISTS ais;

-- Main positions table - all decoded AIS messages land here.
-- Position data (types 1-3, 18) has lat/lon/speed/course.
-- Static data (types 5, 24) has ship_name/callsign/destination.
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

-- Ship type lookup - ITU-R M.1371-5 Table 53.
-- Source table must be populated before the dictionary is created.
CREATE TABLE IF NOT EXISTS ais.ship_types_source (
    code UInt8,
    name String
) ENGINE = MergeTree() ORDER BY code;

-- Seed guarded so a re-run on a populated table is a no-op.
INSERT INTO ais.ship_types_source (code, name)
SELECT code, name FROM (
    SELECT 20 AS code, 'Wing in ground' AS name
    UNION ALL SELECT 30, 'Fishing'
    UNION ALL SELECT 31, 'Towing'
    UNION ALL SELECT 32, 'Towing (large)'
    UNION ALL SELECT 33, 'Dredging/underwater ops'
    UNION ALL SELECT 34, 'Diving ops'
    UNION ALL SELECT 35, 'Military ops'
    UNION ALL SELECT 36, 'Sailing'
    UNION ALL SELECT 37, 'Pleasure craft'
    UNION ALL SELECT 40, 'High-speed craft'
    UNION ALL SELECT 50, 'Pilot vessel'
    UNION ALL SELECT 51, 'Search and rescue'
    UNION ALL SELECT 52, 'Tug'
    UNION ALL SELECT 53, 'Port tender'
    UNION ALL SELECT 54, 'Anti-pollution'
    UNION ALL SELECT 55, 'Law enforcement'
    UNION ALL SELECT 58, 'Medical transport'
    UNION ALL SELECT 59, 'Noncombatant (RR)'
    UNION ALL SELECT 60, 'Passenger'
    UNION ALL SELECT 69, 'Passenger - No info'
    UNION ALL SELECT 70, 'Cargo'
    UNION ALL SELECT 79, 'Cargo - No info'
    UNION ALL SELECT 80, 'Tanker'
    UNION ALL SELECT 89, 'Tanker - No info'
    UNION ALL SELECT 90, 'Other'
    UNION ALL SELECT 99, 'Other - No info'
) AS seed
WHERE (SELECT count() FROM ais.ship_types_source) = 0;

CREATE DICTIONARY IF NOT EXISTS ais.ship_types (
    code UInt8,
    name String
)
PRIMARY KEY code
SOURCE(CLICKHOUSE(TABLE 'ship_types_source' DB 'ais' USER 'ais' PASSWORD 'ais_local'))
LAYOUT(FLAT())
LIFETIME(0);

-- Geofencing zones - rectangular bounding boxes for Saronic Gulf areas.
-- Used in dashboard queries via inline multiIf (priority order: most specific first).
CREATE TABLE IF NOT EXISTS ais.zones (
    zone_id   UInt8,
    zone_name String,
    lat_min   Float64,
    lat_max   Float64,
    lon_min   Float64,
    lon_max   Float64
) ENGINE = MergeTree() ORDER BY zone_id;

-- Seed guarded so a re-run on a populated table is a no-op.
INSERT INTO ais.zones (zone_id, zone_name, lat_min, lat_max, lon_min, lon_max)
SELECT zone_id, zone_name, lat_min, lat_max, lon_min, lon_max FROM (
    SELECT 1 AS zone_id, 'Piraeus Port'    AS zone_name, 37.93 AS lat_min, 37.96 AS lat_max, 23.60 AS lon_min, 23.66 AS lon_max
    UNION ALL SELECT 2, 'Salamina Strait', 37.90, 37.96, 23.48, 23.58
    UNION ALL SELECT 3, 'Elefsina Bay',    38.01, 38.06, 23.48, 23.56
    UNION ALL SELECT 4, 'Saronic Open',    37.50, 38.10, 23.20, 24.00
) AS seed
WHERE (SELECT count() FROM ais.zones) = 0;

-- Materialized view: unique ships per hour + message stats.
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

-- Materialized view: latest known state per vessel.
-- Uses argMaxIf so that position fields (from types 1-3, 18) don't clobber
-- identity fields (from types 5, 24) with NULL, and vice versa.
-- Think of it like a UPSERT that only overwrites non-NULL columns.
-- (Folded in from migrate_ship_latest.sql, the corrected no-NULL-clobber form.)
CREATE MATERIALIZED VIEW IF NOT EXISTS ais.ship_latest
ENGINE = ReplacingMergeTree(last_seen)
ORDER BY mmsi
AS SELECT
    mmsi,
    argMaxIf(ship_name,   timestamp, ship_name   IS NOT NULL) AS ship_name,
    argMaxIf(callsign,    timestamp, callsign    IS NOT NULL) AS callsign,
    argMaxIf(ship_type,   timestamp, ship_type   IS NOT NULL) AS ship_type,
    argMaxIf(destination, timestamp, destination IS NOT NULL) AS destination,
    argMaxIf(imo,         timestamp, imo         IS NOT NULL) AS imo,
    argMaxIf(lat,         timestamp, lat         IS NOT NULL) AS lat,
    argMaxIf(lon,         timestamp, lon         IS NOT NULL) AS lon,
    argMaxIf(speed,       timestamp, speed       IS NOT NULL) AS speed,
    argMaxIf(course,      timestamp, course      IS NOT NULL) AS course,
    argMaxIf(heading,     timestamp, heading     IS NOT NULL) AS heading,
    argMaxIf(nav_status,  timestamp, nav_status  IS NOT NULL) AS nav_status,
    max(timestamp) AS last_seen,
    count() AS message_count
FROM ais.positions
GROUP BY mmsi;
