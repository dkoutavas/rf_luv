-- 021: Seed HF entries into known_frequencies
--
-- The original known_frequencies seed in init.sql:144-181 covers VHF/UHF
-- only because the spectrum scanner sweeps 88-470 MHz and HF was out of
-- band for the pipeline. Owner is now actively listening on HF via direct
-- sampling (V3 Q-branch) / native HF (V4) and these confirmed targets
-- belong in the canonical catalog so the Grafana dashboard's "Active
-- known frequencies" panel reflects them too.
--
-- Idempotent via `WHERE freq_hz NOT IN (...)` — re-running this migration
-- on a populated table is safe.
--
-- class_id introduces three new HF labels following the existing
-- lowercase-hyphenated pattern: hf-broadcast, hf-numbers, hf-time.
-- known_frequencies.class_id is a loose label (not FK-enforced against
-- spectrum.signal_classes — see migration 003 comment), so no parallel
-- INSERT into signal_classes is required.
--
-- Unidentified emitters (e.g. 9.7097 MHz CW high-pitch tone) are NOT
-- seeded here — they live in notes/listening-playbook.md under "Active
-- investigations" until confirmed.

INSERT INTO spectrum.known_frequencies (freq_hz, bandwidth_hz, name, class_id, modulation, notes)
SELECT freq_hz, bandwidth_hz, name, class_id, modulation, notes FROM (
    SELECT  4625000 AS freq_hz, 12000 AS bandwidth_hz, 'UVB-76 "The Buzzer"' AS name, 'hf-numbers'    AS class_id, 'AM' AS modulation, 'Russian number station, 4625 kHz'                       AS notes
    UNION ALL SELECT  9410000,  5000, 'BBC World Service',         'hf-broadcast', 'AM', 'UK shortwave, 31m band'
    UNION ALL SELECT  9420000,  5000, 'Voice of Greece',           'hf-broadcast', 'AM', 'Greek shortwave, 31m band'
    UNION ALL SELECT  9935000,  5000, 'Voice of Greece (alt)',     'hf-broadcast', 'AM', 'Greek shortwave, alt frequency'
    UNION ALL SELECT 10000000,  5000, 'WWV Time Signal',           'hf-time',      'AM', 'NIST time broadcast, 10 MHz, from USA'
    UNION ALL SELECT  4996000,   100, 'RWM Time Signal 4996',      'hf-time',      'CW', 'Russian time/frequency standard, CW pulses'
    UNION ALL SELECT  9996000,   100, 'RWM Time Signal 9996',      'hf-time',      'CW', 'Russian time/frequency standard, CW pulses'
) AS seed
WHERE seed.freq_hz NOT IN (SELECT freq_hz FROM spectrum.known_frequencies);
