-- 005: Reconcile known_frequencies.class_id legacy values (step 3 of 3)
--
-- Migration 003 renamed category → class_id but preserved the legacy short
-- codes (fm, airband, marine, etc.) which don't match signal_classes.class_id.
-- This migration maps them to the canonical values so the classifier's
-- known_frequencies prior matches the same vocabulary as signal_classes.
--
-- Unmapped (intentional, flagged for future signal_classes additions):
--   satcom  (NOAA APT weather-satellite) — no APT class in signal_classes yet
--   ism     (433.92 MHz mixed) — IoT payloads span too many modulations
--
-- ClickHouse's ALTER TABLE ... UPDATE is an async mutation — applies on merge
-- for MergeTree. Tiny reference table, so the mutations complete in seconds.

-- fm → broadcast_fm
ALTER TABLE spectrum.known_frequencies UPDATE class_id = 'broadcast_fm'
    WHERE class_id = 'fm';

-- airband ATIS (136.125 MHz) → am_airband_atis
ALTER TABLE spectrum.known_frequencies UPDATE class_id = 'am_airband_atis'
    WHERE class_id = 'airband' AND freq_hz = 136125000;

-- airband remainder (Tower, Approach, Guard) → am_airband_atc
ALTER TABLE spectrum.known_frequencies UPDATE class_id = 'am_airband_atc'
    WHERE class_id = 'airband';

-- marine AIS (161.975 / 162.025 MHz) → ais
ALTER TABLE spectrum.known_frequencies UPDATE class_id = 'ais'
    WHERE class_id = 'marine' AND freq_hz IN (161975000, 162025000);

-- marine remainder (Ch16, Ch1, Ch13, coast stations) → marine_vhf_channel
ALTER TABLE spectrum.known_frequencies UPDATE class_id = 'marine_vhf_channel'
    WHERE class_id = 'marine';

-- gov / business / ham / pmr → nfm_voice_repeater (all NFM voice in practice)
ALTER TABLE spectrum.known_frequencies UPDATE class_id = 'nfm_voice_repeater'
    WHERE class_id IN ('gov', 'business', 'ham', 'pmr');

-- broadcast-modulation OFDM (DVB-T multiplexes) → dvbt_mux
-- The non-OFDM "broadcast" entry (DAB 169 MHz) stays as-is; no DAB class exists.
ALTER TABLE spectrum.known_frequencies UPDATE class_id = 'dvbt_mux'
    WHERE class_id = 'broadcast' AND modulation = 'OFDM';

-- tetra already matches canonical (no-op but asserts intent)
ALTER TABLE spectrum.known_frequencies UPDATE class_id = 'tetra'
    WHERE class_id = 'tetra';
