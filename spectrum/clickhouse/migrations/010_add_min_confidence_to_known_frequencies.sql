-- 010: Add min_confidence to known_frequencies
--
-- classifier_health's known-good check previously hardcoded 8 bins with
-- per-bin confidence thresholds. Refactor pulls the reference list from
-- known_frequencies (the canonical registry), which needs a threshold
-- column. Per-class defaults below are conservative — they match what
-- the step-3 acceptance table used for these class_ids.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, and ALTER UPDATE rewrites are
-- safe on re-run (they emit the same values every time).

ALTER TABLE spectrum.known_frequencies
    ADD COLUMN IF NOT EXISTS min_confidence Float32 DEFAULT 0.6;

-- Signals we expect to classify strongly when present
ALTER TABLE spectrum.known_frequencies UPDATE min_confidence = 0.7
    WHERE class_id IN ('am_airband_atis', 'broadcast_fm', 'nfm_voice_repeater', 'dvbt_mux', 'ais');

-- Signals with intrinsically bursty / partial-window behavior — lower bar
ALTER TABLE spectrum.known_frequencies UPDATE min_confidence = 0.5
    WHERE class_id IN ('am_airband_atc', 'marine_vhf_channel');

-- Everything else (tetra + legacy satcom/ism/broadcast danglers) stays at 0.6.
