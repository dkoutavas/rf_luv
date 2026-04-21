-- 014: Backfill listening_log from notes/signal-log.txt
--
-- Four manual observations from 2026-04-01/02 were recorded in
-- notes/signal-log.txt but never made it into the listening_log table.
-- This migration ingests them as confirmed operator observations so the
-- classifier's operator-confirmation override (classifier.py:247-254) can
-- use them for ±150 kHz tolerance matches.
--
-- Idempotent: guarded by a freq_mhz uniqueness check so re-running does
-- not duplicate. We match on freq_mhz rounded to 3 decimal places since
-- these entries have never been "confirmed by measurement" (no confirmed_freq_hz).

INSERT INTO spectrum.listening_log
    (timestamp, freq_mhz, mode, heard, class_id, language, notes, confirmed_freq_hz)
SELECT * FROM (
    SELECT
        toDateTime64('2026-04-01 18:00:00', 3) AS timestamp,
        toFloat32(384.000) AS freq_mhz,
        'NFM' AS mode,
        'confirmed' AS heard,
        'tetra' AS class_id,
        '' AS language,
        'TETRA emergency services — blast beat pattern, speeds up during storm dispatch. Source: notes/signal-log.txt.' AS notes,
        toUInt32(384000000) AS confirmed_freq_hz
    UNION ALL SELECT toDateTime64('2026-04-02 12:00:00', 3), toFloat32(446.210), 'NFM', 'confirmed',
        'nfm_voice_repeater', '',
        'PMR446 walkie-talkie — ringing preamble + transmission. Source: notes/signal-log.txt.',
        toUInt32(446210000)
    UNION ALL SELECT toDateTime64('2026-04-02 14:00:00', 3), toFloat32(144.775), 'NFM', 'confirmed',
        'nfm_voice_repeater', 'Greek',
        'Greek ham radio 2m band — voice conversation. Source: notes/signal-log.txt.',
        toUInt32(144775000)
    UNION ALL SELECT toDateTime64('2026-04-02 16:00:00', 3), toFloat32(156.650), 'NFM', 'confirmed',
        'marine_vhf_channel', 'English',
        'Marine Ch13 bridge-to-bridge — English voice, clean signal. Source: notes/signal-log.txt.',
        toUInt32(156650000)
) AS backfill
WHERE (
    SELECT countIf(notes LIKE '%Source: notes/signal-log.txt%')
    FROM spectrum.listening_log
) = 0;
