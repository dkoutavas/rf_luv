-- 003: Add classifier reference tables (step 1 of 3)
--
-- allocations      — regulatory / observed frequency ranges (Greek/EU priors + local obs)
-- signal_classes   — canonical feature signatures per signal category (classifier targets)
-- known_frequencies.category  → renamed to class_id (same vocabulary as signal_classes)
-- listening_log.signal_type   → renamed to class_id; confirmed_freq_hz added; confirmed Bool dropped
--
-- Known dangling refs after this migration: existing known_frequencies rows have
-- class_id values like 'fm', 'airband', 'satcom', 'marine', 'ham', 'gov', 'business',
-- 'broadcast', 'ism', 'pmr' that are NOT in signal_classes. Intentional — the classifier
-- (step 3) is the right actor to consolidate. ClickHouse does not enforce FKs; dashboards
-- filtering by specific class_id values continue to work.

-- ─── Frequency allocations (regulatory ranges) ─────────────
CREATE TABLE IF NOT EXISTS spectrum.allocations (
    freq_start_hz   UInt32,
    freq_end_hz     UInt32,
    service         String,
    region          String DEFAULT 'GR',
    source          String DEFAULT '',
    notes           String DEFAULT ''
) ENGINE = MergeTree()
ORDER BY freq_start_hz;

-- ─── Canonical signal class signatures ─────────────────────
CREATE TABLE IF NOT EXISTS spectrum.signal_classes (
    class_id        String,
    name            String,
    bw_min_hz       UInt32,
    bw_max_hz       UInt32,
    modulation      String,
    duty_pattern    Enum8('continuous'=1, 'bursty_high'=2, 'bursty_low'=3, 'tdma'=4, 'unknown'=5),
    burst_min_s     Float32 DEFAULT 0,
    burst_max_s     Float32 DEFAULT 0,
    evidence_rules  String DEFAULT '{}'
) ENGINE = MergeTree()
ORDER BY class_id;

-- ─── Vocabulary alignment (idempotent; no-op on fresh init.sql) ──
ALTER TABLE spectrum.known_frequencies RENAME COLUMN IF EXISTS category TO class_id;

ALTER TABLE spectrum.listening_log RENAME COLUMN IF EXISTS signal_type TO class_id;
ALTER TABLE spectrum.listening_log ADD COLUMN IF NOT EXISTS confirmed_freq_hz UInt32 DEFAULT 0;
ALTER TABLE spectrum.listening_log DROP COLUMN IF EXISTS confirmed;

-- ─── Seed allocations (Greek/EU regulatory priors + observed) ─
-- Guarded by WHERE count()=0 so re-runs on hosts with data won't duplicate.
INSERT INTO spectrum.allocations (freq_start_hz, freq_end_hz, service, region, source, notes)
SELECT * FROM (
    SELECT  87500000 AS freq_start_hz, 108000000 AS freq_end_hz, 'broadcast_fm' AS service,         'GR' AS region, 'ECC' AS source,  'WFM, strong Lycabettus/Hymettus transmitters' AS notes
    UNION ALL SELECT 108000000, 117975000, 'aviation_navigation',  'EU', 'ECC',  'VOR/ILS'
    UNION ALL SELECT 118000000, 137000000, 'aviation_voice',       'EU', 'ECC',  'AM airband; Athens Tower/Approach/ATIS live here'
    UNION ALL SELECT 144000000, 146000000, 'amateur_2m',           'EU', 'IARU', 'SV ham 2m band'
    UNION ALL SELECT 146000000, 150000000, 'gov_military_vhf_gr',  'GR', 'obs',  'Observed repeaters 146.39/148.44/150.49'
    UNION ALL SELECT 150000000, 156000000, 'land_mobile_business', 'EU', 'ECC',  'PMR/business radio'
    UNION ALL SELECT 156000000, 162025000, 'marine_vhf',           'EU', 'ITU',  'Marine channels; AIS at 161.975 and 162.025'
    UNION ALL SELECT 162000000, 174000000, 'land_mobile_safety',   'EU', 'ECC',  'Public safety / utility'
    UNION ALL SELECT 174000000, 230000000, 'dvb_t_gr',             'GR', 'GR',   'Digital TV; 174-206 strong from Hymettus'
    UNION ALL SELECT 380000000, 400000000, 'tetra_gr',             'GR', 'GR',   'Emergency services — encrypted, do not decode'
    UNION ALL SELECT 430000000, 440000000, 'amateur_70cm',         'EU', 'IARU', 'SV ham 70cm band'
    UNION ALL SELECT 446000000, 446200000, 'pmr446',               'EU', 'CEPT', 'License-free handhelds'
) AS seed
WHERE (SELECT count() FROM spectrum.allocations) = 0;

-- ─── Seed signal_classes (canonical classifier targets) ────
INSERT INTO spectrum.signal_classes (class_id, name, bw_min_hz, bw_max_hz, modulation, duty_pattern, burst_min_s, burst_max_s)
SELECT * FROM (
    SELECT 'dvbt_mux' AS class_id, 'DVB-T multiplex' AS name, 7000000 AS bw_min_hz, 8000000 AS bw_max_hz, 'OFDM' AS modulation, 'continuous' AS duty_pattern, 0 AS burst_min_s, 0 AS burst_max_s
    UNION ALL SELECT 'am_airband_atis',    'Airband ATIS (continuous)',     8000,   15000, 'AM',       'continuous',  0,  0
    UNION ALL SELECT 'am_airband_atc',     'Airband ATC (bursty)',          8000,   15000, 'AM',       'bursty_low',  5, 60
    UNION ALL SELECT 'nfm_voice_repeater', 'NFM voice repeater',           12500,   25000, 'NFM',      'bursty_low',  2, 20
    UNION ALL SELECT 'marine_vhf_channel', 'Marine VHF channel',           20000,   30000, 'NFM',      'bursty_low',  2, 30
    UNION ALL SELECT 'ais',                'AIS ship positions',            9000,   12000, 'GMSK',     'tdma',        0,  0
    UNION ALL SELECT 'broadcast_fm',       'Broadcast FM',                150000,  200000, 'WFM',      'continuous',  0,  0
    UNION ALL SELECT 'tetra',              'TETRA (encrypted)',            20000,   30000, 'pi/4DQPSK','bursty_high', 0,  0
    UNION ALL SELECT 'unknown_continuous', 'Unknown continuous',               0,       0, 'unknown',  'continuous',  0,  0
    UNION ALL SELECT 'unknown_bursty',     'Unknown bursty',                   0,       0, 'unknown',  'bursty_low',  0,  0
) AS seed
WHERE (SELECT count() FROM spectrum.signal_classes) = 0;
