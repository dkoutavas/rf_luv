-- Aircraft metadata from readsb's tar1090 database (registration, type, etc.).
-- The SBS/BaseStation stream on :30003 carries only positions and callsigns;
-- readsb also serves an enriched aircraft.json (TAR1090_ENABLE_AC_DB=true) with
-- registration and type. The ingest polls that over HTTP and upserts here, so
-- the aircraft database itself is never copied into this repo.

CREATE TABLE IF NOT EXISTS adsb.aircraft_meta (
    hex_ident     String,                 -- ICAO 24-bit address, joins positions
    registration  String,                 -- tail (readsb "r"), e.g. SX-AQM
    type_code     String,                 -- ICAO type (readsb "t"), e.g. E120
    description   String,                 -- long type (readsb "desc")
    category      String,                 -- ADS-B emitter category, e.g. A3
    mil           UInt8,                   -- military, from readsb dbFlags bit 0
    last_seen     DateTime DEFAULT now()   -- last poll that carried this row
) ENGINE = ReplacingMergeTree(last_seen)
ORDER BY hex_ident
-- first-seen per aircraft is min(timestamp) in adsb.positions, so it is not
-- duplicated here (ReplacingMergeTree would overwrite it on every poll).
SETTINGS index_granularity = 8192;
