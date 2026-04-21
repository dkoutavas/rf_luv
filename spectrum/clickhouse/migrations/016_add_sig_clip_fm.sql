-- 016: Split sig_clip into UHF-compression vs FM-overload
--
-- v1's sig_clip was "worst_clip_freq_hz outside 170-230 MHz AND
-- clipped_captures > 0". That catches both genuine UHF compression and
-- mundane heavy FM overload (88-108 MHz). They mean different things and
-- should be stored independently.
--
-- After this migration:
--   sig_clip     — worst_clip_freq outside FM (88-108) AND outside DVB-T (174-230)
--                   AND clipped_captures ≥ 5. This is the compression marker.
--   sig_clip_fm  — worst_clip_freq in 88-108 AND clipped_captures ≥ 5. Normal
--                   baseline condition in Athens; stored for observability but
--                   NOT aggregated into match_tier.
--
-- match_tier aggregation rule unchanged: sig_spur + sig_baseline + sig_clip.

ALTER TABLE spectrum.compression_events
    ADD COLUMN IF NOT EXISTS sig_clip_fm UInt8 DEFAULT 0 AFTER sig_clip;
