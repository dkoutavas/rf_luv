-- 007: Add signal_classifications table (step 3 of 3)
--
-- One row per (freq_hz, classified_at) with the classifier's decision.
-- ReplacingMergeTree(classified_at) collapses on merge. Query with FINAL or
-- argMax(_, classified_at) for authoritative reads.
--
-- Populated by spectrum/classifier.py on a systemd timer (~5 min cadence,
-- offset +30s from feature_extractor so fresh features are available).

CREATE TABLE IF NOT EXISTS spectrum.signal_classifications (
    freq_hz            UInt32,
    class_id           String,
    confidence         Float32,
    reasoning          String,              -- JSON blob with score trace + evidence
    features_snapshot  String,              -- JSON of peak_features row used
    classified_at      DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(classified_at)
ORDER BY freq_hz;
