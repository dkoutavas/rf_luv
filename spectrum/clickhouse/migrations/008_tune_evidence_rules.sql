-- 008: Tune evidence_rules against first-run classifier output (step 3 tuning)
--
-- First classifier run on leap (2026-04-18) hit three calibration issues,
-- all rooted in a step-2 feature-extractor artifact: the duty_24h baseline
-- is the 24h p10 of the bin's own samples. For strong continuous signals
-- (broadcast FM, ATIS, in-mux DVB-T), the signal dominates the p10 →
-- baseline+6 dB threshold is met rarely → measured duty_24h is small →
-- classifier derives "bursty_low" pattern and fails "continuous" rules.
--
-- Flag for step 2.5: replace per-bin p10 baseline with either a neighbor-
-- bin minimum or an absolute noise-floor proxy so continuous-signal
-- detection via duty is actually reliable. Until then, these rules
-- compensate by accepting bursty_low as a valid derived pattern for
-- classes we KNOW are continuous from the allocation + kf_match prior.
--
-- Tuning summary (diff vs migration 006):
-- * dvbt_mux       bw_hz lower bound 3 MHz → 200 kHz (in-mux bins measure
--                   200-600 kHz locally because 3 dB drops within ±5 bins)
-- * am_airband_atis duty_pattern + bursty_low; drop duty_24h_min 0.5 (both
--                   unreliable for continuous signals in current features)
-- * broadcast_fm   duty_pattern + bursty_low; drop duty_24h_min 0.7 (same)
-- * other classes unchanged — their rules are already calibrated.

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules =
    '{"bw_hz":[200000,999000000],"duty_pattern":["continuous"],"duty_24h_min":0.5,"requires_allocation_in":["dvb_t_gr"]}'
    WHERE class_id = 'dvbt_mux';

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules =
    '{"bw_hz":[200000,800000],"duty_pattern":["continuous","bursty_low"],"requires_allocation_in":["broadcast_fm"]}'
    WHERE class_id = 'broadcast_fm';

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules =
    '{"bw_hz":[200000,500000],"duty_pattern":["continuous","bursty_low"],"requires_allocation_in":["aviation_voice"]}'
    WHERE class_id = 'am_airband_atis';
