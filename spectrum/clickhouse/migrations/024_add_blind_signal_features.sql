-- 024: Blind signal analyzer results — modulation identity card (feature D3)
--
-- One table backing spectrum/analysis/blind_analyze.py, the offline blind
-- signal analyzer. It eats a .cs8 IQ capture (produced by D2's
-- spectrum/iq_capture.py, results table iq_captures @ migration 023) and prints
-- + persists an "identity card": occupied bandwidth, modulation family
-- (CW/AM/WFM/NFM/OOK/2FSK/4FSK/BPSK/QPSK-PSK/OFDM/noise/digital-unknown),
-- symbol/baud rate, carrier offset, and an OFDM flag — with zero prior
-- knowledge of the emitter. Structured like spectrum/analysis/detect_compression.py:
-- a feature-extractor library plus a main that writes its own table and an
-- ordered rule-trace.
--
-- Legal posture baked into the data model: characterizing a waveform's
-- modulation/baud is analysis of physics and extracts NO message content. It
-- works on encrypted carriers WITHOUT touching payload; those land as
-- modulation='digital-unknown' with the out-of-scope note in `reasoning`, and
-- no decode is ever attempted.
--
-- Column notes:
--   * modulation is LowCardinality(String), NOT Enum8 — a newly-recognized
--     family must not force a migration.
--   * baud_hz / ofdm_tu_us / ofdm_subcarrier_hz are Nullable — the analyzer
--     emits NULL when the estimate does not clear its confidence bar, mirroring
--     compression_events' honest NULL-attribution pattern (migration 015).
--   * capture_id / trigger_id / dongle_id are join keys back to iq_captures /
--     forensic_trigger (023); '' for a bare `--file` run with no DB provenance.
--   * features is a JSON blob of every raw discriminant (gamma_max, sigma_*,
--     C20/C40/C42 abs+complex, flatness, psd_kurtosis, env/freq modes+positions,
--     baud line SNR, cp peak) so thresholds can be re-tuned offline without
--     recapturing.
--   * reasoning is the newline-joined ordered rule trace.
--
-- Precondition: none. Additive; scanner and all other pipelines keep running.
-- Rollback: DROP TABLE spectrum.blind_signal_features;

CREATE TABLE IF NOT EXISTS spectrum.blind_signal_features (
    analysis_id         String DEFAULT generateUUIDv4(),
    analyzed_at         DateTime64(3) DEFAULT now64(3),
    capture_id          String DEFAULT '',
    trigger_id          String DEFAULT '',
    dongle_id           LowCardinality(String) DEFAULT '',
    file_path           String,
    freq_hz             UInt32 DEFAULT 0,
    sample_rate_hz      UInt32,
    snr_db              Float32,
    occupied_bw_hz      Float32,
    carrier_offset_hz   Float32,
    modulation          LowCardinality(String),          -- 'CW','AM','WFM','NFM','OOK','2FSK','4FSK','BPSK','QPSK/PSK','OFDM','noise','digital-unknown'
    confidence          Float32,
    baud_hz             Nullable(Float32),
    baud_confidence     LowCardinality(String) DEFAULT 'none',   -- 'high' | 'medium' | 'none'
    ofdm_flag           UInt8 DEFAULT 0,
    ofdm_tu_us          Nullable(Float32),
    ofdm_subcarrier_hz  Nullable(Float32),
    features            String DEFAULT '',               -- JSON blob: raw discriminants, full re-tune surface
    reasoning           String DEFAULT '',               -- newline-joined ordered rule trace
    source              String DEFAULT '',
    analyzer_version    LowCardinality(String) DEFAULT 'v1'
) ENGINE = MergeTree()
ORDER BY (analyzed_at, analysis_id)
TTL toDateTime(analyzed_at) + INTERVAL 365 DAY
SETTINGS index_granularity = 8192;
