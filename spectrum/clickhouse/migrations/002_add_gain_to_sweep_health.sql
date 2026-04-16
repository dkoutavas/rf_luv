-- 002: Add gain_db to sweep_health for adaptive gain tracking
--
-- The scanner now auto-reduces gain when ADC clipping is detected.
-- Recording the effective gain per sweep lets us correlate gain changes
-- with data quality improvements in dashboards.

ALTER TABLE spectrum.sweep_health ADD COLUMN IF NOT EXISTS gain_db Float32 DEFAULT 0.0 AFTER sweep_duration_ms;
