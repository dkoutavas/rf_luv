-- 015: Make compression_events.estimated_emitter_* nullable
--
-- v1 detector filled (0, -100) as a sentinel when no clear emitter peak
-- existed. That was semantically ambiguous (0 Hz is a valid UInt32) and
-- frequently misleading — the v1 emitter estimator fabricated attribution
-- by picking strong peaks OUTSIDE the compression zone (e.g. the 164-168
-- MHz Piraeus repeater cluster during any compression event that had a
-- spur block in FM tiles). After the v2 estimator, these become NULL
-- attributions by design.
--
-- Converting UInt32 → Nullable(UInt32) / Float32 → Nullable(Float32) is a
-- ClickHouse metadata-only change when the existing column has no actual
-- nulls (which is our case — all existing rows have concrete values).

ALTER TABLE spectrum.compression_events
    MODIFY COLUMN estimated_emitter_freq_hz Nullable(UInt32);

ALTER TABLE spectrum.compression_events
    MODIFY COLUMN estimated_emitter_power_dbfs Nullable(Float32);
