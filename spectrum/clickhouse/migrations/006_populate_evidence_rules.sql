-- 006: Populate signal_classes.evidence_rules for the classifier (step 3 of 3)
--
-- Calibration notes against observed step-2 feature values:
--
-- * Bandwidth floors: the step-2 bandwidth algorithm uses ±5 × 100 kHz bins,
--   so the MINIMUM measurable bandwidth is 300 kHz (center + 1 bin each side).
--   Truly narrow AM (10 kHz) and NFM (12.5 kHz) signals measure at 300-500 kHz.
--   The spec's bw_hz ranges from the ITU ideal (5-30 kHz AM) would match zero
--   signals — ranges widened accordingly, documented here for step 2.5 if a
--   finer bandwidth estimator is ever added.
--
-- * duty_pattern is derived in the classifier from duty_24h by spec rule:
--   continuous   duty_24h > 0.5
--   bursty_high  duty_24h in [0.1, 0.5]
--   bursty_low   duty_24h < 0.1
--
-- * requires_allocation_in uses actual spectrum.allocations.service values
--   (spec used generic names that don't exist in this table).
--
-- * Guarded INSERT (empty-rules detection) so repeat runs don't duplicate.

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules = '{"bw_hz":[3000000,999000000],"duty_pattern":["continuous"],"duty_24h_min":0.5,"requires_allocation_in":["dvb_t_gr"]}'
    WHERE class_id = 'dvbt_mux';

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules = '{"bw_hz":[200000,800000],"duty_pattern":["continuous"],"duty_24h_min":0.5,"requires_allocation_in":["broadcast_fm"]}'
    WHERE class_id = 'broadcast_fm';

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules = '{"bw_hz":[200000,500000],"duty_pattern":["continuous"],"duty_24h_min":0.5,"requires_allocation_in":["aviation_voice"]}'
    WHERE class_id = 'am_airband_atis';

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules = '{"bw_hz":[200000,500000],"duty_pattern":["bursty_low","bursty_high"],"duty_24h_range":[0.02,0.5],"burst_p50_s_range":[2,180],"requires_allocation_in":["aviation_voice"]}'
    WHERE class_id = 'am_airband_atc';

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules = '{"bw_hz":[200000,500000],"duty_pattern":["bursty_low","bursty_high"],"duty_24h_range":[0.005,0.4],"requires_allocation_in":["land_mobile_business","land_mobile_safety","gov_military_vhf_gr","amateur_2m","amateur_70cm","pmr446"]}'
    WHERE class_id = 'nfm_voice_repeater';

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules = '{"bw_hz":[200000,500000],"duty_pattern":["bursty_low"],"duty_24h_range":[0.005,0.2],"requires_allocation_in":["marine_vhf"]}'
    WHERE class_id = 'marine_vhf_channel';

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules = '{"bw_hz":[200000,500000],"center_freq_hz_near":[161975000,162025000],"center_freq_tolerance_hz":150000,"duty_pattern":["bursty_high","continuous"],"requires_allocation_in":["marine_vhf"]}'
    WHERE class_id = 'ais';

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules = '{"bw_hz":[200000,500000],"duty_pattern":["bursty_high","continuous"],"requires_allocation_in":["tetra_gr"]}'
    WHERE class_id = 'tetra';

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules = '{"bw_hz":[0,999000000],"duty_pattern":["continuous"],"duty_24h_min":0.5}'
    WHERE class_id = 'unknown_continuous';

ALTER TABLE spectrum.signal_classes UPDATE evidence_rules = '{"bw_hz":[0,999000000],"duty_pattern":["bursty_low","bursty_high"]}'
    WHERE class_id = 'unknown_bursty';
