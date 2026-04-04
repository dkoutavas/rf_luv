-- ISM Band Device Monitoring — ClickHouse Schema
-- Ingested from rtl_433 JSON output via ism_ingest.py
--
-- rtl_433 decodes 200+ device protocols at 433.92 MHz:
-- weather stations, tire pressure sensors, doorbells,
-- smart home remotes, car keyfobs, temperature sensors, etc.
--
-- The schema maps common fields explicitly and stores the full
-- JSON in raw_json so nothing is lost from exotic protocols.
-- Think of raw_json like storing the full syslog line alongside
-- parsed structured fields.

CREATE DATABASE IF NOT EXISTS ism;

-- Main events table — every decoded device transmission
CREATE TABLE IF NOT EXISTS ism.events (
    timestamp       DateTime64(3) DEFAULT now64(3),
    model           String,                  -- device protocol/model name (e.g., "Acurite-Tower")
    device_id       String DEFAULT '',        -- device-specific ID
    channel         Nullable(String),        -- channel number (some devices use multiple)
    battery_ok      Nullable(Float32),       -- battery status (1.0 = OK, 0.0 = low)
    temperature_c   Nullable(Float32),       -- Celsius
    humidity        Nullable(Float32),       -- percent
    pressure_hpa    Nullable(Float32),       -- hectopascals
    wind_avg_km_h   Nullable(Float32),       -- average wind speed
    wind_max_km_h   Nullable(Float32),       -- gust speed
    wind_dir_deg    Nullable(Float32),       -- wind direction in degrees
    rain_mm         Nullable(Float32),       -- cumulative rainfall
    rssi            Nullable(Float32),       -- signal strength (dB)
    snr             Nullable(Float32),       -- signal-to-noise ratio (dB)
    raw_json        String DEFAULT ''        -- full rtl_433 JSON for fields not explicitly mapped
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, model, device_id)
TTL toDateTime(timestamp) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;

-- Materialized view: hourly device stats
CREATE MATERIALIZED VIEW IF NOT EXISTS ism.hourly_stats
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(hour)
ORDER BY hour
AS SELECT
    toStartOfHour(timestamp) AS hour,
    uniqState(model, device_id) AS unique_devices,
    countState() AS total_events,
    avgState(temperature_c) AS avg_temperature
FROM ism.events
GROUP BY hour;

-- Materialized view: latest reading per device
-- Useful for "what devices are active right now" dashboard panel
CREATE MATERIALIZED VIEW IF NOT EXISTS ism.device_latest
ENGINE = ReplacingMergeTree(last_seen)
ORDER BY (model, device_id)
AS SELECT
    model,
    device_id,
    argMax(channel, timestamp) AS channel,
    argMax(battery_ok, timestamp) AS battery_ok,
    argMax(temperature_c, timestamp) AS temperature_c,
    argMax(humidity, timestamp) AS humidity,
    argMax(pressure_hpa, timestamp) AS pressure_hpa,
    argMax(rssi, timestamp) AS rssi,
    max(timestamp) AS last_seen,
    count() AS event_count
FROM ism.events
GROUP BY model, device_id;
