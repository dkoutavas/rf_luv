-- 009: Add classifier_health table (monitoring layer)
--
-- Numbered 009 not 008 because 008 is already taken by
-- tune_evidence_rules. Schema matches the spec except
-- classifier_runtime_seconds is Nullable(Float32) — the classifier
-- writes all rows in a run with identical classified_at, so there
-- is no runtime proxy available from the data alone. Writer leaves
-- it NULL; a later classifier-side emission could populate it.
--
-- One row per classifier_health.py run, TTL 180d. No ReplacingMergeTree;
-- every run produces a new row (the history IS the point — drift over
-- time is what we watch).

CREATE TABLE IF NOT EXISTS spectrum.classifier_health (
    computed_at                       DateTime DEFAULT now(),
    total_classifications             UInt32,
    classifier_runtime_seconds        Nullable(Float32),

    confidence_distinct_values        UInt16,
    confidence_precision_tail_count   UInt32,

    harmonic_flags_total              UInt32,
    harmonic_flags_cross_allocation   UInt32,

    atis_confidence_current           Float32,
    continuous_signals_with_bursts    UInt32,

    class_distribution_json           String,
    unknowns_ratio                    Float32,
    confidence_mean                   Float32,

    known_good_passing                UInt8,
    known_good_total                  UInt8,
    known_good_failing_json           String,

    seconds_since_last_classification Float32,
    seconds_since_last_peak_features  Float32,
    seconds_since_last_sweep          Float32
) ENGINE = MergeTree()
ORDER BY computed_at
TTL computed_at + INTERVAL 180 DAY;
