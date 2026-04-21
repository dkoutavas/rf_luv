-- 012: Add compression_events table for Phase 2 archaeology + Phase 3 live detection
--
-- One row per detected LNA/ADC compression event. Detection is performed by
-- spectrum/analysis/detect_compression.py (one-shot historical backfill +
-- optional scheduled re-run after each new full sweep).
--
-- The 3-part signature is scored as three independent binary sub-flags:
--   sig_spur      — ≥N consecutive tiles with argMax at near-constant offset
--   sig_baseline  — median bin depression > 5 dB vs hourly_baseline across non-FM bins
--   sig_clip      — worst_clip_freq_hz outside 170-230 MHz AND clipped_captures > 0
--
-- match_tier aggregates: all 3 → 'high', any 2 → 'medium', 1 → 'low', 0 → 'none'.
-- No unified "probability" — we have one unambiguous training example
-- (2026-04-21 11:55:04.960 UTC) and no ground truth for pre-Phase-4 events.

CREATE TABLE IF NOT EXISTS spectrum.compression_events (
    sweep_id                    String,
    timestamp                   DateTime64(3),
    estimated_emitter_freq_hz   UInt32,
    estimated_emitter_power_dbfs Float32,
    spur_offset_hz              Int32,         -- signed: positive = peak above tile center
    spur_block_tile_lo          UInt16,
    spur_block_tile_hi          UInt16,
    spur_block_tile_count       UInt16,
    spur_offset_stddev_khz      Float32,       -- lower = more convincing spur comb
    baseline_depression_db      Float32,       -- median bin depression vs hourly baseline (non-FM)
    baseline_bins_sampled       UInt32,
    worst_clip_freq_hz          UInt32,
    clipped_captures            UInt32,
    sig_spur                    UInt8,
    sig_baseline                UInt8,
    sig_clip                    UInt8,
    match_tier                  Enum8('none'=0, 'low'=1, 'medium'=2, 'high'=3),
    detected_at                 DateTime64(3) DEFAULT now64(3),
    detector_version            String DEFAULT 'v1'
) ENGINE = ReplacingMergeTree(detected_at)
ORDER BY (timestamp, sweep_id)
TTL toDateTime(timestamp) + INTERVAL 365 DAY
SETTINGS index_granularity = 8192;
