-- 022: ACARS classifier feedback
--
-- The acars/ pipeline (commit 4b57bf1) maintains acars.freq_activity, a
-- ReplacingMergeTree of (freq_mhz, dongle_id) keys recording recent ACARS
-- decode counts. CRC-validated decodes are ground truth: if we're seeing
-- ACARS messages at 131.525 MHz, that bin IS ACARS, no scoring needed.
--
-- The integration has two layers:
--   (a) Static: this migration adds `acars_downlink`/`acars_uplink` to
--       spectrum.signal_classes (with evidence_rules) and seeds the three
--       European ACARS channels into spectrum.known_frequencies. The
--       classifier's kf_match prior gives ACARS classes +3 score at those
--       freqs even before any live decode.
--   (b) Dynamic: ops/spectrum-acars-feedback/ runs hourly, reads
--       acars.freq_activity (cross-instance HTTP since acars and spectrum
--       run on different ClickHouse), and writes hard-confirmation rows
--       into spectrum.listening_log. The classifier treats listening_log
--       hits as operator-confirmation overrides at confidence 1.0.
--
-- This migration is the static layer. The feedback service is the dynamic
-- layer; see ops/spectrum-acars-feedback/README.md.

-- ─── New signal classes ────────────────────────────────────
-- Guarded by IF NOT EXISTS-style insert pattern (count() check).
-- Two classes because uplinks (no `flight` field) and downlinks behave
-- the same on the spectrum side — same freqs, same modulation, same
-- duty pattern — but are distinguished in acars.messages. Keeping them
-- separate in signal_classes lets future cross-DB views slice cleanly.

INSERT INTO spectrum.signal_classes
    (class_id, name, bw_min_hz, bw_max_hz, modulation, duty_pattern, burst_min_s, burst_max_s)
SELECT * FROM (
    SELECT 'acars_downlink' AS class_id,
           'ACARS aircraft downlink' AS name,
           3000  AS bw_min_hz,
           15000 AS bw_max_hz,
           'AM-MSK' AS modulation,
           'bursty_low' AS duty_pattern,
           0.5 AS burst_min_s,
           5   AS burst_max_s
    UNION ALL SELECT 'acars_uplink',
           'ACARS ground uplink',
           3000, 15000, 'AM-MSK', 'bursty_low', 0.5, 5
) AS seed
WHERE (SELECT count() FROM spectrum.signal_classes WHERE class_id IN ('acars_downlink', 'acars_uplink')) = 0;

-- ─── Evidence rules (matches the classifier's scoring schema) ──
-- bw_hz: ACARS occupies a narrow ~3-8 kHz channel within an AM lobe; the
--   peak detector measures the wider lobe so use a generous range.
-- duty_pattern: bursty_low — typical traffic at LGAV is a few messages
--   per minute per channel.
-- duty_24h_range: [0.001, 0.3] — rare bursts to moderate activity.
-- center_freq_hz_near: the three EU ACARS frequencies. Tight tolerance
--   (50 kHz, vs default 150 kHz) so only true ACARS bins score the +3.
-- requires_allocation_in: aviation_voice (118-137 MHz, seeded in mig 003).

-- NOTE: ClickHouse does NOT auto-concatenate adjacent string literals
-- (unlike Python). Keep the full JSON inline on a single string literal,
-- even when it gets long.
ALTER TABLE spectrum.signal_classes
UPDATE evidence_rules = '{"bw_hz":[3000,15000],"duty_pattern":["bursty_low","bursty_high"],"duty_24h_range":[0.001,0.3],"center_freq_hz_near":[131525000,131725000,131825000],"center_freq_tolerance_hz":50000,"requires_allocation_in":["aviation_voice"]}'
WHERE class_id IN ('acars_downlink', 'acars_uplink');

-- ─── Seed known_frequencies for the EU ACARS plan ──────────
-- The kf_match prior in classifier.py gives the matching class +3 score.
-- Guarded so we don't double-seed if the operator manually added them.
-- Confidence column is min_confidence (added by migration 010).

INSERT INTO spectrum.known_frequencies
    (freq_hz, bandwidth_hz, name, class_id, modulation, notes, min_confidence)
SELECT * FROM (
    SELECT 131525000 AS freq_hz, 8000 AS bandwidth_hz, 'ACARS Ch1 (131.525)' AS name,
           'acars_downlink' AS class_id, 'AM-MSK' AS modulation,
           'EU ACARS plan, primary downlink' AS notes, 0.6 AS min_confidence
    UNION ALL SELECT 131725000, 8000, 'ACARS Ch2 (131.725)', 'acars_downlink', 'AM-MSK',
                     'EU ACARS plan, secondary downlink', 0.6
    UNION ALL SELECT 131825000, 8000, 'ACARS Ch3 (131.825)', 'acars_downlink', 'AM-MSK',
                     'EU ACARS plan, tertiary downlink', 0.6
) AS seed
WHERE (SELECT count() FROM spectrum.known_frequencies
       WHERE freq_hz IN (131525000, 131725000, 131825000)) = 0;
