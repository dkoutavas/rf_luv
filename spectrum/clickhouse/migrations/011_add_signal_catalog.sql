-- 011: Add signal_catalog reference table
--
-- Expected-signals catalog for 88-470 MHz in the Polygono/Athens area.
-- Distinct from known_frequencies:
--   known_frequencies → classifier prior (~35 rows, each row biases scoring)
--   signal_catalog    → reference taxonomy (~150 rows, citation-backed, read-only taxonomy)
--
-- Phase 2/3 queries look up "what service covers this freq?" via range scan.
-- Catalog is small enough (<200 rows) that a linear scan is instant — no dictionary.
--
-- Sources are cited in allocation_source. Confidence tiers:
--   'high'    — ITU/CEPT binding regulation or observed on this receiver
--   'medium'  — regional/national convention, widely published but not verified here
--   'low'     — inferred from public sources, needs cross-check with AIP/EETT
--   'unknown' — mystery zones flagged for investigation
--
-- Training-data derived frequencies are NOT trusted — past Claude-supplied LGAV ATC
-- frequencies have been wrong. Only Tower/Approach/ATIS/Guard (already confirmed in
-- known_frequencies) are in the 'high' tier for aviation voice.

CREATE TABLE IF NOT EXISTS spectrum.signal_catalog (
    id                  UInt32,
    freq_lo_hz          UInt32,          -- allocation/channel lower bound
    freq_hi_hz          UInt32,          -- allocation/channel upper bound
    freq_center_hz      UInt32,          -- convenience = (lo+hi)/2
    freq_span_hz        UInt32,          -- convenience = hi - lo
    service             String,          -- e.g. 'broadcast_fm', 'maritime_vhf_ch16', 'mil_uhf_aero'
    allocation_source   String,          -- e.g. 'ITU RR 5.208', 'CEPT ERC/DEC/(98)25', 'AIP Greece GEN 3.4', 'observed'
    modulation_expected String DEFAULT 'unknown',
    typical_duty        String DEFAULT 'unknown',  -- 'continuous', 'bursty_low', 'bursty_high', 'tdma', 'pulsed'
    confidence          String DEFAULT 'medium',   -- 'high', 'medium', 'low', 'unknown'
    notes               String DEFAULT ''
) ENGINE = MergeTree()
ORDER BY freq_lo_hz;

-- Human-readable view with MHz and "confirmed_here" status (joined at query time).
-- ClickHouse 24.3 disallows range expressions in JOIN ON, so we pre-aggregate the
-- match counts via a cross-join subquery. Both tables are tiny, so this is cheap.
CREATE VIEW IF NOT EXISTS spectrum.signal_catalog_view AS
SELECT
    c.id                                    AS id,
    c.freq_center_hz / 1000000.0            AS freq_center_mhz,
    c.freq_span_hz / 1000000.0              AS freq_span_mhz,
    c.freq_lo_hz / 1000000.0                AS freq_lo_mhz,
    c.freq_hi_hz / 1000000.0                AS freq_hi_mhz,
    c.service                               AS service,
    c.allocation_source                     AS allocation_source,
    c.modulation_expected                   AS modulation_expected,
    c.typical_duty                          AS typical_duty,
    c.confidence                            AS confidence,
    c.notes                                 AS notes,
    ifNull(conf.hit_count, 0) > 0           AS confirmed_here
FROM spectrum.signal_catalog c
LEFT JOIN (
    SELECT c.id AS id, count() AS hit_count
    FROM spectrum.signal_catalog c, spectrum.known_frequencies kf
    WHERE kf.freq_hz BETWEEN c.freq_lo_hz AND c.freq_hi_hz
    GROUP BY c.id
) AS conf USING id;

-- ─── Seed data ─────────────────────────────────────────────
-- Guarded by count()=0 so re-runs on hosts with data won't duplicate.
INSERT INTO spectrum.signal_catalog
    (id, freq_lo_hz, freq_hi_hz, freq_center_hz, freq_span_hz,
     service, allocation_source, modulation_expected, typical_duty, confidence, notes)
SELECT
    id, freq_lo_hz, freq_hi_hz,
    toUInt32((freq_lo_hz + freq_hi_hz) / 2) AS freq_center_hz,
    toUInt32(freq_hi_hz - freq_lo_hz) AS freq_span_hz,
    service, allocation_source, modulation_expected, typical_duty, confidence, notes
FROM (

    -- ═══════════════════════════════════════════════════════════
    -- BROADCAST FM (87.5-108 MHz) — ITU RR 5.190, ECC
    -- ═══════════════════════════════════════════════════════════
    SELECT   1 AS id,  87500000 AS freq_lo_hz, 108000000 AS freq_hi_hz,
             'broadcast_fm' AS service,
             'ITU RR 5.190 / ECC' AS allocation_source,
             'WFM' AS modulation_expected,
             'continuous' AS typical_duty,
             'high' AS confidence,
             'Full FM broadcast band; strong Athens transmitters on Lycabettus/Hymettus' AS notes
    UNION ALL SELECT   2,  99500000,  99700000, 'broadcast_fm',             'observed',                    'WFM',     'continuous', 'high',   'Kosmos FM 99.6 — confirmed in known_frequencies'
    UNION ALL SELECT   3, 105700000, 105900000, 'broadcast_fm',             'observed',                    'WFM',     'continuous', 'high',   'Skai 105.8 — confirmed in known_frequencies'

    -- ═══════════════════════════════════════════════════════════
    -- AIR NAVIGATION (108-117.975 MHz) — VOR / ILS localizer
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT   4, 108000000, 111975000, 'aviation_nav_ils_loc',     'ICAO Annex 10 / ECC',         'AM-DSB',  'continuous', 'high',   'ILS localizer band (odd 10 kHz spacing within 108-112 reserved for ILS LOC)'
    UNION ALL SELECT   5, 112000000, 117975000, 'aviation_nav_vor',         'ICAO Annex 10 / ECC',         'AM-DSB',  'continuous', 'high',   'VOR nav aids — continuous morse ident + 30 Hz sub-carriers'

    -- ═══════════════════════════════════════════════════════════
    -- AIRBAND / AERONAUTICAL MOBILE R (118-137 MHz) — ICAO
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT  10, 118000000, 137000000, 'aviation_voice',           'ITU RR 5.200 / ICAO Annex 10', 'AM',      'bursty_low', 'high',   'AM airband (8.33 kHz spacing in EU). Tower/Approach/ATIS live here'
    UNION ALL SELECT  11, 118090000, 118110000, 'aviation_voice_atc',       'AIP Greece GEN 3.4 / observed','AM',      'bursty_low', 'high',   'Athens Tower 118.100 MHz — confirmed'
    UNION ALL SELECT  12, 118565000, 118585000, 'aviation_voice_atc',       'AIP Greece GEN 3.4 / observed','AM',      'bursty_low', 'high',   'Athens Approach 118.575 MHz — confirmed'
    UNION ALL SELECT  13, 121490000, 121510000, 'aviation_voice_emergency', 'ITU RR 5.200 / ICAO',          'AM',      'bursty_low', 'high',   'International aeronautical emergency (Guard) 121.5 MHz'
    UNION ALL SELECT  14, 136115000, 136135000, 'aviation_voice_atis',      'AIP Greece GEN 3.4 / observed','AM',      'continuous', 'high',   'Athens ATIS 136.125 MHz — confirmed'
    UNION ALL SELECT  15, 131000000, 137000000, 'aviation_data_acars',      'ARINC 618 / ECC',              'MSK',     'bursty_low', 'medium', 'ACARS data — typically 131.525/131.725/131.825 in EU'
    UNION ALL SELECT  16, 118000000, 137000000, 'aviation_voice_atc_unverified', 'AIP Greece (sector freqs need AIP lookup)', 'AM', 'bursty_low', 'low', 'Athens ACC + other LGAV sectors — DO NOT trust freqs from training data; cross-check AIP'

    -- ═══════════════════════════════════════════════════════════
    -- AMATEUR 2m (144-146 MHz) — IARU Region 1
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT  20, 144000000, 146000000, 'amateur_2m',               'IARU R1 Bandplan 2022',        'various', 'bursty_low', 'high',   'SV Greek ham 2m band'
    UNION ALL SELECT  21, 144795000, 144805000, 'amateur_2m_ssb_call',      'IARU R1 Bandplan 2022',        'SSB',     'bursty_low', 'medium', 'SSB calling 144.300, but 144.775 observed with voice — check band plan'
    UNION ALL SELECT  22, 144770000, 144780000, 'amateur_2m_voice',         'observed',                     'NFM',     'bursty_low', 'high',   'Observed 144.775 MHz voice — see signal-log.txt 2026-04-02'
    UNION ALL SELECT  23, 145000000, 145800000, 'amateur_2m_repeater_out',  'IARU R1 Bandplan 2022',        'NFM',     'bursty_low', 'high',   'Repeater outputs (inputs at -600 kHz shift)'

    -- ═══════════════════════════════════════════════════════════
    -- GOV / MILITARY VHF (146-156 MHz) — Greek national, observed
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT  30, 146000000, 150000000, 'gov_military_vhf_gr',      'EETT national plan (general)', 'NFM',     'bursty_low', 'medium', 'Greek government/military VHF; observed repeaters at 146.39 / 148.44 / 150.49'
    UNION ALL SELECT  31, 146385000, 146395000, 'gov_military_vhf_gr',      'observed',                     'NFM',     'bursty_low', 'high',   'Military VHF 146.39 MHz — strong persistent repeater'
    UNION ALL SELECT  32, 148435000, 148445000, 'gov_military_vhf_gr',      'observed',                     'NFM',     'bursty_low', 'high',   'Military/Gov VHF 148.44 MHz — see notes/analysis-report-20260410.md'
    UNION ALL SELECT  33, 150485000, 150495000, 'gov_military_vhf_gr',      'observed',                     'NFM',     'bursty_low', 'high',   'Military VHF 150.49 MHz — strong persistent repeater'
    UNION ALL SELECT  34, 150000000, 156000000, 'land_mobile_business',     'ECC/DEC + EETT',               'NFM',     'bursty_low', 'medium', 'PMR / business radio VHF (150-156)'
    UNION ALL SELECT  35, 152490000, 152510000, 'land_mobile_business',     'observed',                     'NFM',     'bursty_low', 'high',   'Business radio 152.5 MHz — confirmed'
    UNION ALL SELECT  36, 164700000, 164800000, 'land_mobile_unknown_gr',   'observed',                     'NFM',     'bursty_low', 'high',   '164.73 MHz cluster — hypothesis: Piraeus port dispatch (see session_164_168_mhz_briefing.md)'
    UNION ALL SELECT  37, 166700000, 166800000, 'land_mobile_unknown_gr',   'observed',                     'NFM',     'bursty_low', 'high',   '166.77 MHz cluster — hypothesis: port/maritime logistics'
    UNION ALL SELECT  38, 168800000, 168850000, 'land_mobile_unknown_gr',   'observed',                     'NFM',     'bursty_low', 'high',   '168.82 MHz cluster — hypothesis: port/maritime logistics'

    -- ═══════════════════════════════════════════════════════════
    -- MARITIME VHF (156-162.025 MHz) — ITU-R M.1084 appendix 18
    -- ═══════════════════════════════════════════════════════════
    -- Simplex channels (ship-to-ship or distress/calling)
    UNION ALL SELECT  50, 156025000, 162025000, 'maritime_vhf',             'ITU-R M.1084 Appendix 18',     'NFM',     'bursty_low', 'high',   'Marine VHF band; 25 kHz spacing. Piraeus line-of-sight'
    UNION ALL SELECT  51, 156037500, 156062500, 'maritime_vhf_ch01',        'ITU-R M.1084 App 18',          'NFM',     'bursty_low', 'high',   'Ch01 156.050 MHz — port ops (confirmed in known_frequencies as Piraeus)'
    UNION ALL SELECT  52, 156287500, 156312500, 'maritime_vhf_ch06',        'ITU-R M.1084 App 18',          'NFM',     'bursty_low', 'high',   'Ch06 156.300 MHz — intership safety'
    UNION ALL SELECT  53, 156637500, 156662500, 'maritime_vhf_ch13',        'ITU-R M.1084 App 18',          'NFM',     'bursty_low', 'high',   'Ch13 156.650 MHz — bridge-to-bridge (observed 2026-04-02, English voice)'
    UNION ALL SELECT  54, 156787500, 156812500, 'maritime_vhf_ch16',        'ITU-R M.1084 App 18',          'NFM',     'bursty_low', 'high',   'Ch16 156.800 MHz — distress/calling'
    UNION ALL SELECT  55, 156512500, 156537500, 'maritime_vhf_ch70',        'ITU-R M.1084 App 18',          'GMSK',    'tdma',       'high',   'Ch70 156.525 MHz — DSC (Digital Selective Calling)'
    UNION ALL SELECT  56, 158067500, 158092500, 'maritime_coast_duplex',    'ITU-R M.1084 App 18',          'NFM',     'bursty_low', 'high',   'Coast station 158.08 MHz — Piraeus Radio'
    UNION ALL SELECT  57, 160117500, 160142500, 'maritime_coast_duplex',    'ITU-R M.1084 App 18',          'NFM',     'bursty_low', 'high',   'Coast TX 160.13 MHz (paired with ship 155.x)'
    UNION ALL SELECT  58, 160717500, 160742500, 'maritime_coast_duplex',    'ITU-R M.1084 App 18',          'NFM',     'bursty_low', 'high',   'Coast repeater 160.73 MHz'
    UNION ALL SELECT  59, 161962500, 161987500, 'maritime_ais_ch87b',       'ITU-R M.1371 / M.1084',        'GMSK',    'tdma',       'high',   'AIS-1 161.975 MHz — ship positions'
    UNION ALL SELECT  60, 162012500, 162037500, 'maritime_ais_ch88b',       'ITU-R M.1371 / M.1084',        'GMSK',    'tdma',       'high',   'AIS-2 162.025 MHz — ship positions'

    -- ═══════════════════════════════════════════════════════════
    -- LAND MOBILE / PUBLIC SAFETY (162.05-174 MHz)
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT  70, 162050000, 174000000, 'land_mobile_safety',       'ECC/DEC',                      'NFM',     'bursty_low', 'medium', 'Public safety / utility VHF'
    UNION ALL SELECT  71, 169000000, 170000000, 'paging_or_business',       'ECC / EETT',                   'digital', 'continuous', 'medium', 'DAB-like / paging (169 MHz area)'

    -- ═══════════════════════════════════════════════════════════
    -- DVB-T BAND III (174-230 MHz) — national (Greek national plan)
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT  80, 174000000, 230000000, 'dvb_t_gr',                 'EETT national DTT plan',       'OFDM',    'continuous', 'high',   'DVB-T Band III; 7 MHz channels from Hymettus'
    UNION ALL SELECT  81, 178610000, 185610000, 'dvb_t_gr_ch6',             'EETT / Hymettus MUX',          'OFDM',    'continuous', 'high',   'Ch6 182.11 — Hymettus MUX (confirmed)'
    UNION ALL SELECT  82, 182710000, 189710000, 'dvb_t_gr_ch7',             'EETT / Hymettus MUX',          'OFDM',    'continuous', 'high',   'Ch7 186.21 — Hymettus MUX (confirmed)'
    UNION ALL SELECT  83, 187750000, 194750000, 'dvb_t_gr_ch8',             'EETT / Hymettus MUX',          'OFDM',    'continuous', 'high',   'Ch8 191.25 — Hymettus MUX (confirmed)'
    UNION ALL SELECT  84, 191850000, 198850000, 'dvb_t_gr_ch9',             'EETT / Hymettus MUX',          'OFDM',    'continuous', 'high',   'Ch9 195.35 — Hymettus MUX (confirmed)'
    UNION ALL SELECT  85, 196640000, 203640000, 'dvb_t_gr_ch10',            'EETT / Hymettus MUX',          'OFDM',    'continuous', 'high',   'Ch10 200.14 — Hymettus MUX (confirmed)'

    -- ═══════════════════════════════════════════════════════════
    -- GAP 230-380 MHz — mostly NATO / military in most of Region 1
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT 100, 225000000, 400000000, 'mil_uhf_aero',             'NATO AFIC / ICAO Annex 10 (mil)','AM',    'bursty_low', 'medium', 'NATO UHF aeronautical (225-400 MHz) — strong candidate for Athens approach traffic'
    UNION ALL SELECT 101, 243000000, 243010000, 'mil_uhf_aero_emergency',   'NATO / ICAO',                  'AM',      'bursty_low', 'high',   'International military distress 243.0 MHz (mirror of civilian 121.5)'
    UNION ALL SELECT 102, 303000000, 305000000, 'mil_uhf_aero_mystery',     'observed',                     'unknown', 'pulsed',     'unknown','MYSTERY ZONE: strong narrow event at 303.89 MHz (2026-04-12) and 304.19 MHz (2026-04-21), +2.9 and +3.5 dBFS respectively. Compressed LNA for ~0.9s. Hypothesis: close-passing military UHF aero'

    -- ═══════════════════════════════════════════════════════════
    -- TETRA (380-400 + 410-430 MHz) — CEPT
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT 110, 380000000, 400000000, 'tetra_gr',                 'CEPT ERC/DEC/(96)01 / EETT',   'pi/4DQPSK','bursty_high','high',  'Greek TETRA emergency services (encrypted)'
    UNION ALL SELECT 111, 410000000, 430000000, 'tetra_civil',              'CEPT / ECC',                   'pi/4DQPSK','bursty_high','low',   'Civil TETRA band (use varies by country)'

    -- ═══════════════════════════════════════════════════════════
    -- AMATEUR 70cm (430-440 MHz) — IARU R1
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT 120, 430000000, 440000000, 'amateur_70cm',             'IARU R1 Bandplan 2022',        'various', 'bursty_low', 'high',   'SV Greek ham 70cm band'

    -- ═══════════════════════════════════════════════════════════
    -- ISM 433 MHz (CEPT ERC/REC 70-03 annex 1)
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT 130, 433050000, 434790000, 'ism_433_europe',           'CEPT ERC/REC 70-03 annex 1',   'mixed',   'bursty_low', 'high',   'ISM band — weather stations, car keys, home automation'
    UNION ALL SELECT 131, 433910000, 433930000, 'ism_433_center',           'CEPT / observed',              'OOK/FSK', 'bursty_low', 'high',   '433.92 MHz center — dense OOK/FSK IoT traffic'

    -- ═══════════════════════════════════════════════════════════
    -- PMR446 (446.0-446.2 MHz) — CEPT ERC/DEC/(98)25 + (15)05
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT 140, 446000000, 446200000, 'pmr446',                   'CEPT ERC/DEC/(98)25',          'NFM',     'bursty_low', 'high',   'License-free handhelds; 16 channels 6.25 kHz steps'
    UNION ALL SELECT 141, 446000000, 446012500, 'pmr446_ch01',              'CEPT',                         'NFM',     'bursty_low', 'high',   'Ch1 446.00625'
    UNION ALL SELECT 142, 446012500, 446025000, 'pmr446_ch02',              'CEPT',                         'NFM',     'bursty_low', 'high',   'Ch2 446.01875'
    UNION ALL SELECT 143, 446025000, 446037500, 'pmr446_ch03',              'CEPT',                         'NFM',     'bursty_low', 'high',   'Ch3 446.03125'
    UNION ALL SELECT 144, 446037500, 446050000, 'pmr446_ch04',              'CEPT',                         'NFM',     'bursty_low', 'high',   'Ch4 446.04375'
    UNION ALL SELECT 145, 446050000, 446062500, 'pmr446_ch05',              'CEPT',                         'NFM',     'bursty_low', 'high',   'Ch5 446.05625'
    UNION ALL SELECT 146, 446062500, 446075000, 'pmr446_ch06',              'CEPT',                         'NFM',     'bursty_low', 'high',   'Ch6 446.06875'
    UNION ALL SELECT 147, 446075000, 446087500, 'pmr446_ch07',              'CEPT',                         'NFM',     'bursty_low', 'high',   'Ch7 446.08125'
    UNION ALL SELECT 148, 446087500, 446100000, 'pmr446_ch08',              'CEPT',                         'NFM',     'bursty_low', 'high',   'Ch8 446.09375'
    UNION ALL SELECT 149, 446200000, 446212500, 'pmr446_ch_observed',       'observed',                     'NFM',     'bursty_low', 'high',   'Observed 446.210 MHz — ringing preamble (see signal-log.txt 2026-04-02)'

    -- ═══════════════════════════════════════════════════════════
    -- NOAA APT WEATHER SATELLITES — ITU RR 5.208
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT 150, 137075000, 137125000, 'satcom_noaa_apt',          'ITU RR 5.208 / NOAA',          'APT',     'pulsed',     'high',   'NOAA 19 / Meteor M2-3 137.100 MHz — pass-dependent'
    UNION ALL SELECT 151, 137595000, 137645000, 'satcom_noaa_apt',          'ITU RR 5.208 / NOAA',          'APT',     'pulsed',     'high',   'NOAA 15 137.620 MHz — pass-dependent'
    UNION ALL SELECT 152, 137885000, 137940000, 'satcom_noaa_apt',          'ITU RR 5.208 / NOAA',          'APT',     'pulsed',     'high',   'NOAA 18 137.9125 MHz — pass-dependent'

    -- ═══════════════════════════════════════════════════════════
    -- TV/TRUNK CONTROL — 450-470 MHz land mobile
    -- ═══════════════════════════════════════════════════════════
    UNION ALL SELECT 160, 450000000, 470000000, 'land_mobile_uhf',          'ECC/DEC + EETT',               'NFM',     'bursty_low', 'medium', 'UHF land mobile / trunked radio'

) AS seed
WHERE (SELECT count() FROM spectrum.signal_catalog) = 0;
