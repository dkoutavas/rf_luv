-- Airline lookup: ICAO 3-letter callsign prefix -> operator name.
-- A callsign like "AEE261" starts with the operator's ICAO code, so the
-- dashboard maps substring(callsign, 1, 3) to a readable name. This is a small
-- hand-curated set of the operators seen at Athens (LGAV), not a full registry.

CREATE TABLE IF NOT EXISTS adsb.airlines (
    icao3  String,
    name   String
) ENGINE = ReplacingMergeTree()
ORDER BY icao3;

-- Count-guarded seed so re-running the migration does not duplicate rows.
INSERT INTO adsb.airlines (icao3, name)
SELECT icao3, name FROM (
    SELECT 'AEE' AS icao3, 'Aegean Airlines' AS name
    UNION ALL SELECT 'OAL', 'Olympic Air'
    UNION ALL SELECT 'SEH', 'Sky Express'
    UNION ALL SELECT 'RYR', 'Ryanair'
    UNION ALL SELECT 'RUK', 'Ryanair UK'
    UNION ALL SELECT 'WZZ', 'Wizz Air'
    UNION ALL SELECT 'EJU', 'easyJet Europe'
    UNION ALL SELECT 'EZY', 'easyJet'
    UNION ALL SELECT 'VLG', 'Vueling'
    UNION ALL SELECT 'VOE', 'Volotea'
    UNION ALL SELECT 'TVF', 'Transavia France'
    UNION ALL SELECT 'TRA', 'Transavia'
    UNION ALL SELECT 'EWG', 'Eurowings'
    UNION ALL SELECT 'PGT', 'Pegasus Airlines'
    UNION ALL SELECT 'DLH', 'Lufthansa'
    UNION ALL SELECT 'BAW', 'British Airways'
    UNION ALL SELECT 'AFR', 'Air France'
    UNION ALL SELECT 'KLM', 'KLM'
    UNION ALL SELECT 'SWR', 'Swiss'
    UNION ALL SELECT 'AUA', 'Austrian Airlines'
    UNION ALL SELECT 'THY', 'Turkish Airlines'
    UNION ALL SELECT 'UAE', 'Emirates'
    UNION ALL SELECT 'QTR', 'Qatar Airways'
    UNION ALL SELECT 'ETD', 'Etihad Airways'
    UNION ALL SELECT 'ELY', 'El Al'
    UNION ALL SELECT 'MSR', 'Egyptair'
    UNION ALL SELECT 'SVA', 'Saudia'
    UNION ALL SELECT 'ETH', 'Ethiopian Airlines'
    UNION ALL SELECT 'ITY', 'ITA Airways'
    UNION ALL SELECT 'IBE', 'Iberia'
    UNION ALL SELECT 'TAP', 'TAP Air Portugal'
    UNION ALL SELECT 'FIN', 'Finnair'
    UNION ALL SELECT 'SAS', 'SAS'
) AS seed
WHERE (SELECT count() FROM adsb.airlines) = 0;
