-- 028: Anomalies and the signal of the day (M4)
--
-- spectrum/anomalies.py runs hourly over spectrum.emitters_current and writes
-- what was new or strange in the last 24 h. Kinds:
--   new_emitter, gone_quiet, louder_than_usual, broken_pattern,
--   new_tetra_carrier
-- Artifact suspects never raise one. Silence is counted in swept hours only.
--
-- anomalies: ReplacingMergeTree(detected_at) keyed by (kind, dongle_id,
-- emitter_id, event_day). The hourly runs see the same event many times.
-- The key makes them one row per event per local day, and the newest
-- detail wins. event_at is when it happened (first peak of a new emitter,
-- the end of the Nth silent swept hour, the first peak at an odd hour...),
-- not when the job noticed. confidence is 'low' for level anomalies from a
-- dongle whose levels are not trusted (dongle_roles, the V4).
--
-- signal_of_the_day: one pick per local day, re-picked every hour, the
-- latest pick wins. reason is the written why, for the dashboard and the
-- daily digest.
--
-- Precondition: 026. Additive.
-- Rollback: DROP both tables.

CREATE TABLE IF NOT EXISTS spectrum.anomalies (
    kind            LowCardinality(String),
    dongle_id       LowCardinality(String),
    emitter_id      String,
    event_day       Date,
    event_at        DateTime64(3),
    detected_at     DateTime,
    center_hz       UInt32,
    name            String,
    confidence      LowCardinality(String),
    detail          String
) ENGINE = ReplacingMergeTree(detected_at)
ORDER BY (kind, dongle_id, emitter_id, event_day)
TTL toDateTime(event_at) + INTERVAL 365 DAY;

CREATE TABLE IF NOT EXISTS spectrum.signal_of_the_day (
    day             Date,
    picked_at       DateTime,
    dongle_id       LowCardinality(String),
    emitter_id      String,
    center_hz       UInt32,
    name            String,
    name_source     LowCardinality(String),
    score           Int32,
    reason          String
) ENGINE = ReplacingMergeTree(picked_at)
ORDER BY day
TTL day + INTERVAL 365 DAY;
