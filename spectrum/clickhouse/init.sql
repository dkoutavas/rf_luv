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
    sweep_id        String DEFAULT ''    -- groups all bins from one sweep
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (freq_hz, timestamp)
TTL toDateTime(timestamp) + INTERVAL 180 DAY
SETTINGS index_granularity = 8192;

-- Known frequencies -- reference table for signal identification
-- Pre-populated with Athens-area frequencies
CREATE TABLE IF NOT EXISTS spectrum.known_frequencies (
    freq_hz         UInt32,
    bandwidth_hz    UInt32 DEFAULT 0,
    name            String,
    category        String,
    modulation      String DEFAULT '',
    notes           String DEFAULT ''
) ENGINE = MergeTree()
ORDER BY freq_hz;

INSERT INTO spectrum.known_frequencies (freq_hz, bandwidth_hz, name, category, modulation, notes) VALUES
    (99600000,  200000, 'Kosmos FM 99.6',        'fm',      'WFM', 'Strong local FM'),
    (105800000, 200000, 'Skai 105.8',            'fm',      'WFM', 'Strong local FM'),
    (118100000, 8333,   'Athens Tower',           'airband', 'AM',  'Airport tower'),
    (118575000, 8333,   'Athens Approach',        'airband', 'AM',  'ATC approach control'),
    (121500000, 8333,   'Guard / Emergency',      'airband', 'AM',  'International distress'),
    (136125000, 8333,   'Athens ATIS',            'airband', 'AM',  'Automated weather'),
    (137100000, 50000,  'NOAA 19 / Meteor M2-3', 'satcom',  'APT', 'Weather satellite'),
    (137620000, 50000,  'NOAA 15',               'satcom',  'APT', 'Weather satellite'),
    (137912500, 50000,  'NOAA 18',               'satcom',  'APT', 'Weather satellite'),
    (156800000, 25000,  'Marine Ch16',            'marine',  'NFM', 'Distress/calling'),
    (161975000, 25000,  'AIS Ch87',              'marine',  'digital', 'Ship positions'),
    (162025000, 25000,  'AIS Ch88',              'marine',  'digital', 'Ship positions'),
    (384000000, 25000,  'Greek TETRA',           'tetra',   'digital', 'Emergency services'),
    (433920000, 0,      'ISM 433.92',            'ism',     'mixed',   'Sensors, remotes'),
    (446006250, 12500,  'PMR446 Ch1',            'pmr',     'NFM', 'License-free radios'),
    (446018750, 12500,  'PMR446 Ch2',            'pmr',     'NFM', 'License-free radios'),
    (446031250, 12500,  'PMR446 Ch3',            'pmr',     'NFM', 'License-free radios');

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
-- Fast "current spectrum" display without scanning the full table
CREATE MATERIALIZED VIEW IF NOT EXISTS spectrum.freq_latest
ENGINE = ReplacingMergeTree(last_seen)
ORDER BY freq_hz
AS SELECT
    freq_hz,
    argMax(power_dbfs, timestamp) AS power_dbfs,
    argMax(sweep_id, timestamp) AS sweep_id,
    max(timestamp) AS last_seen
FROM spectrum.scans
GROUP BY freq_hz;
