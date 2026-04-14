-- 001: Add ADC clipping detection columns to sweep_health
--
-- The spectrum scanner now checks raw IQ bytes for ADC saturation
-- before computing FFT. These columns track clipping per sweep so
-- we can correlate gain settings with data quality.

ALTER TABLE spectrum.sweep_health ADD COLUMN IF NOT EXISTS clipped Bool DEFAULT false;
ALTER TABLE spectrum.sweep_health ADD COLUMN IF NOT EXISTS max_clip_fraction Float32 DEFAULT 0.0;
ALTER TABLE spectrum.sweep_health ADD COLUMN IF NOT EXISTS worst_clip_freq_hz UInt32 DEFAULT 0;
ALTER TABLE spectrum.sweep_health ADD COLUMN IF NOT EXISTS clipped_captures UInt32 DEFAULT 0;
ALTER TABLE spectrum.sweep_health ADD COLUMN IF NOT EXISTS total_captures UInt32 DEFAULT 0;
