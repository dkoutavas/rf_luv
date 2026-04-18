-- Spectrum Scanner -- ClickHouse Schema
-- Power spectral density measurements from rtl_tcp FFT sweeps.
-- Normalized: one row per frequency bin per sweep.
-- Think of it like storing a spectrogram with one row per time-frequency pixel.

CREATE DATABASE IF NOT EXISTS spectrum;

-- Main scans table -- every frequency bin measurement
-- ORDER BY (freq_hz, timestamp) for frequency-first queries:
-- "what's happening at 156.8 MHz over the last week?"
CREATE TABLE IF NOT EXISTS spectrum.scans (
    timestamp       DateTime64(3) DEFAULT now64(3),
    freq_hz         UInt32,              -- center frequency of this bin (Hz)
    power_dbfs      Float32,             -- measured power (dBFS, relative to ADC full scale)
    sweep_id        String DEFAULT '',   -- groups all bins from one sweep
    run_id          String DEFAULT ''    -- links to scan_runs for config tracking
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (freq_hz, timestamp)
TTL toDateTime(timestamp) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;

-- Known frequencies -- reference table for signal identification
-- Pre-populated with Athens-area frequencies. class_id loosely matches
-- spectrum.signal_classes but is not FK-enforced; see migration 003.
CREATE TABLE IF NOT EXISTS spectrum.known_frequencies (
    freq_hz         UInt32,
    bandwidth_hz    UInt32 DEFAULT 0,
    name            String,
    class_id        String,
    modulation      String DEFAULT '',
    notes           String DEFAULT ''
) ENGINE = MergeTree()
ORDER BY freq_hz;

-- Hourly baseline -- rolling average power per frequency bin
-- Used for anomaly detection: "is this signal stronger than usual?"
-- Like computing a noise floor profile per frequency.
CREATE MATERIALIZED VIEW IF NOT EXISTS spectrum.hourly_baseline
ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(hour)
ORDER BY (freq_hz, hour)
AS SELECT
    toStartOfHour(timestamp) AS hour,
    freq_hz,
    avgState(power_dbfs) AS avg_power,
    stddevPopState(power_dbfs) AS std_power,
    countState() AS sample_count
FROM spectrum.scans
GROUP BY hour, freq_hz;

-- Latest reading per frequency bin
-- Query-time view — always correct, trivial cost at this data volume
-- (~23K rows in a 30-minute window = nothing for ClickHouse)
CREATE VIEW IF NOT EXISTS spectrum.freq_latest AS
SELECT
    freq_hz,
    argMax(power_dbfs, timestamp) AS power_dbfs,
    argMax(sweep_id, timestamp) AS sweep_id,
    max(timestamp) AS last_seen
FROM spectrum.scans
WHERE timestamp > now() - INTERVAL 30 MINUTE
GROUP BY freq_hz;

-- Detected spectral peaks -- bins significantly above their neighbors
-- Peak detection runs in scanner.py, results stored here for dashboarding.
-- Like peak-picking in audio spectral analysis.
CREATE TABLE IF NOT EXISTS spectrum.peaks (
    timestamp       DateTime64(3) DEFAULT now64(3),
    freq_hz         UInt32,
    power_dbfs      Float32,
    prominence_db   Float32,        -- how far above neighboring bins
    sweep_id        String DEFAULT '',
    run_id          String DEFAULT ''
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (freq_hz, timestamp)
TTL toDateTime(timestamp) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;

-- Sweep health -- per-sweep metadata for clipping detection and monitoring
-- One row per sweep. clipped=1 means ADC saturation was detected in raw IQ samples.
-- max_clip_fraction is the worst-case ratio of clipped samples (0 or 255) across
-- all captures in the sweep. >5% indicates gain is too high for that frequency.
CREATE TABLE IF NOT EXISTS spectrum.sweep_health (
    timestamp       DateTime64(3) DEFAULT now64(3),
    sweep_id        String,
    preset          String,
    bin_count       UInt32,
    max_power       Float32,
    max_power_dvbt  Float32 DEFAULT -100.0,
    sweep_duration_ms UInt32,
    gain_db         Float32 DEFAULT 0.0,
    clipped         Bool DEFAULT false,
    max_clip_fraction Float32 DEFAULT 0.0,
    worst_clip_freq_hz UInt32 DEFAULT 0,
    clipped_captures UInt32 DEFAULT 0,
    total_captures  UInt32 DEFAULT 0,
    run_id          String DEFAULT ''
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY timestamp
TTL toDateTime(timestamp) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;

-- Transient signal events -- signals that appear or disappear between sweeps
-- Like an edge detector on the spectrum: fires when something changes.
CREATE TABLE IF NOT EXISTS spectrum.events (
    timestamp       DateTime64(3) DEFAULT now64(3),
    freq_hz         UInt32,
    event_type      String,         -- 'appeared' or 'disappeared'
    power_dbfs      Float32,        -- current power
    prev_power      Float32,        -- previous sweep power
    delta_db        Float32,        -- absolute change
    sweep_id        String DEFAULT '',
    run_id          String DEFAULT ''
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, freq_hz)
TTL toDateTime(timestamp) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;

-- Scan runs -- tracks scanner configuration sessions for A/B comparisons
-- Each scanner startup creates a new run with antenna config, gain, etc.
-- Measured fields (noise_floor, peak) are filled after the first full sweep.
CREATE TABLE IF NOT EXISTS spectrum.scan_runs (
    run_id              String,
    started_at          DateTime64(3),
    ended_at            Nullable(DateTime64(3)),
    gain_db             Float32,
    antenna_position    String DEFAULT '',
    antenna_arms_cm     Float32 DEFAULT 0,
    antenna_orientation_deg UInt16 DEFAULT 0,
    antenna_height_m    Float32 DEFAULT 0,
    notes               String DEFAULT '',
    noise_floor_dbfs    Nullable(Float32),
    peak_signal_dbfs    Nullable(Float32),
    peak_signal_freq_hz Nullable(UInt32)
) ENGINE = MergeTree()
ORDER BY started_at;

-- Seed known frequencies (only if table is empty)
INSERT INTO spectrum.known_frequencies (freq_hz, bandwidth_hz, name, class_id, modulation, notes)
SELECT * FROM (
    SELECT 99600000 AS freq_hz, 200000 AS bandwidth_hz, 'Kosmos FM 99.6' AS name, 'fm' AS class_id, 'WFM' AS modulation, 'Strong local FM' AS notes
    UNION ALL SELECT 105800000, 200000, 'Skai 105.8', 'fm', 'WFM', 'Strong local FM'
    UNION ALL SELECT 118100000, 8333, 'Athens Tower', 'airband', 'AM', 'Airport tower'
    UNION ALL SELECT 118575000, 8333, 'Athens Approach', 'airband', 'AM', 'ATC approach control'
    UNION ALL SELECT 121500000, 8333, 'Guard / Emergency', 'airband', 'AM', 'International distress'
    UNION ALL SELECT 136125000, 8333, 'Athens ATIS', 'airband', 'AM', 'Automated weather'
    UNION ALL SELECT 137100000, 50000, 'NOAA 19 / Meteor M2-3', 'satcom', 'APT', 'Weather satellite'
    UNION ALL SELECT 137620000, 50000, 'NOAA 15', 'satcom', 'APT', 'Weather satellite'
    UNION ALL SELECT 137912500, 50000, 'NOAA 18', 'satcom', 'APT', 'Weather satellite'
    UNION ALL SELECT 156800000, 25000, 'Marine Ch16', 'marine', 'NFM', 'Distress/calling'
    UNION ALL SELECT 161975000, 25000, 'AIS Ch87', 'marine', 'digital', 'Ship positions'
    UNION ALL SELECT 162025000, 25000, 'AIS Ch88', 'marine', 'digital', 'Ship positions'
    UNION ALL SELECT 384000000, 25000, 'Greek TETRA', 'tetra', 'digital', 'Emergency services'
    UNION ALL SELECT 144775000, 12500, 'Greek 2m Ham', 'ham', 'NFM', 'Observed voice conversation'
    UNION ALL SELECT 148000000, 200000, 'Military/Gov VHF', 'gov', 'NFM', 'Strong persistent signal'
    UNION ALL SELECT 150500000, 200000, 'Military/Gov VHF', 'gov', 'NFM', 'Strong persistent signal'
    UNION ALL SELECT 152500000, 200000, 'Business Radio', 'business', 'NFM', 'Commercial repeater'
    UNION ALL SELECT 156050000, 25000, 'Marine Ch1', 'marine', 'NFM', 'Port operations - Piraeus'
    UNION ALL SELECT 156650000, 25000, 'Marine Ch13', 'marine', 'NFM', 'Bridge-to-bridge'
    UNION ALL SELECT 158080000, 25000, 'Marine Coast Stn', 'marine', 'NFM', 'Piraeus Radio coast station'
    UNION ALL SELECT 160130000, 25000, 'Marine Coast TX', 'marine', 'NFM', 'Coast station duplex TX'
    UNION ALL SELECT 160730000, 25000, 'Marine Coast Rpt', 'marine', 'NFM', 'Piraeus coast radio repeater'
    UNION ALL SELECT 146390000, 12500, 'Military VHF 146.39', 'gov', 'NFM', 'Strong persistent repeater'
    UNION ALL SELECT 150490000, 12500, 'Military VHF 150.49', 'gov', 'NFM', 'Strong persistent repeater'
    UNION ALL SELECT 169000000, 1000000, 'DAB/Business VHF', 'broadcast', 'digital', 'Digital radio infrastructure'
    UNION ALL SELECT 182110000, 7000000, 'DVB-T Mux Hymettus', 'broadcast', 'OFDM', 'Digital TV Ch6'
    UNION ALL SELECT 186210000, 7000000, 'DVB-T Mux Hymettus', 'broadcast', 'OFDM', 'Digital TV Ch7'
    UNION ALL SELECT 191250000, 7000000, 'DVB-T Mux Hymettus', 'broadcast', 'OFDM', 'Digital TV Ch8'
    UNION ALL SELECT 195350000, 7000000, 'DVB-T Mux Hymettus', 'broadcast', 'OFDM', 'Digital TV Ch9'
    UNION ALL SELECT 200140000, 7000000, 'DVB-T Mux Hymettus', 'broadcast', 'OFDM', 'Digital TV Ch10'
    UNION ALL SELECT 433920000, 0, 'ISM 433.92', 'ism', 'mixed', 'Sensors, remotes'
    UNION ALL SELECT 446006250, 12500, 'PMR446 Ch1', 'pmr', 'NFM', 'License-free radios'
    UNION ALL SELECT 446018750, 12500, 'PMR446 Ch2', 'pmr', 'NFM', 'License-free radios'
    UNION ALL SELECT 446031250, 12500, 'PMR446 Ch3', 'pmr', 'NFM', 'License-free radios'
    UNION ALL SELECT 446210000, 12500, 'PMR446 (observed)', 'pmr', 'NFM', 'Ringing preamble + transmission'
) AS seed
WHERE (SELECT count() FROM spectrum.known_frequencies) = 0;

-- Listening log -- operator notes from active monitoring sessions
-- Insert via browser form on the Listening Playbook dashboard,
-- or via CLI: curl 'http://localhost:8126/?user=spectrum&password=spectrum_local' \
--   --data-binary "INSERT INTO spectrum.listening_log (...) VALUES (...)"
CREATE TABLE IF NOT EXISTS spectrum.listening_log (
    id                  String DEFAULT generateUUIDv4(),
    timestamp           DateTime64(3) DEFAULT now64(3),
    freq_mhz            Float32,                  -- tuned frequency (user input)
    mode                String DEFAULT 'NFM',
    heard               String DEFAULT '',
    class_id            String DEFAULT '',        -- matches spectrum.signal_classes, loose
    language            String DEFAULT '',
    notes               String DEFAULT '',
    confirmed_freq_hz   UInt32 DEFAULT 0          -- measured carrier (0 = not yet confirmed)
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY timestamp
TTL toDateTime(timestamp) + INTERVAL 365 DAY;

-- Classifier health -- one row per classifier_health.py run; see migration 009
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

-- Classifier decisions -- computed by spectrum/classifier.py
-- See migration 007; one row per freq_hz per run, collapsed by classified_at
CREATE TABLE IF NOT EXISTS spectrum.signal_classifications (
    freq_hz            UInt32,
    class_id           String,
    confidence         Float32,
    reasoning          String,
    features_snapshot  String,
    classified_at      DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(classified_at)
ORDER BY freq_hz;

-- Per-peak features -- computed by spectrum/feature_extractor.py
-- See migration 004 for full doc; one row per freq_hz per run, collapsed by computed_at
CREATE TABLE IF NOT EXISTS spectrum.peak_features (
    freq_hz             UInt32,
    bandwidth_hz        UInt32,
    duty_cycle_1h       Float32,
    duty_cycle_24h      Float32,
    duty_cycle_7d       Float32,
    burst_p50_s         Nullable(Float32),
    burst_p95_s         Nullable(Float32),
    diurnal_pattern     Array(Float32),
    weekday_pattern     Array(Float32),
    harmonic_of_hz      Nullable(UInt32),
    power_mean_dbfs     Float32,
    power_p95_dbfs      Float32,
    power_std_db        Float32,
    sweeps_observed_24h UInt32,
    computed_at         DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(computed_at)
ORDER BY freq_hz;
