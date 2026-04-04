-- Migration: fix ship_latest NULL clobbering + add ship_types dictionary + geofencing zones
--
-- Run this on an existing deployment where ship_latest was created with bare argMax.
-- Procedure:
--   1. docker compose stop ais-ingest
--   2. docker exec -i clickhouse-ais clickhouse-client --user ais --password ais_local --multiquery < clickhouse/migrate_ship_latest.sql
--   3. docker compose start ais-ingest

-- ─── Step 1: Fix ship_latest view ────────────────────────

DROP VIEW IF EXISTS ais.ship_latest;

CREATE MATERIALIZED VIEW ais.ship_latest
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

-- Backfill from existing position data
INSERT INTO ais.ship_latest
SELECT
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

-- ─── Step 2: Ship type lookup dictionary ─────────────────

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
    (69, 'Passenger — No info'),
    (70, 'Cargo'),
    (79, 'Cargo — No info'),
    (80, 'Tanker'),
    (89, 'Tanker — No info'),
    (90, 'Other'),
    (99, 'Other — No info');

CREATE DICTIONARY IF NOT EXISTS ais.ship_types (
    code UInt8,
    name String
)
PRIMARY KEY code
SOURCE(CLICKHOUSE(TABLE 'ship_types_source' DB 'ais'))
LAYOUT(FLAT())
LIFETIME(0);

-- ─── Step 3: Geofencing zones ────────────────────────────

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
