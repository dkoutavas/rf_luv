-- ghost pipeline — initial schema.
--
-- The ghost/ pipeline replicates and debunks "paranormal" investigation gear
-- (spirit box, EMF meter, Bluetooth speaker) from first principles on the SDR
-- station, and runs offline forensics on public video. This schema holds three
-- things: the spirit-box sweep steps (ghost.spins), the RDS station table built
-- per session to label those steps (ghost.stations), and per-segment forensic
-- findings on published video (ghost.segments).
--
-- All reception / self-generated / public-video analysis. No message payloads
-- are decoded; RDS PS/PI/RadioText is public broadcast metadata only.
--
-- Numbered migrations + migrate.py from day one (mirrors acars/spectrum/rds).
-- The database + user are created earlier by the infra ch-bootstrap; this
-- migration only creates tables (same contract as rds 001).

-- One row per spirit-box step. A "spin" is one dwell on one frequency: the
-- receiver hops the FM band with no squelch and no lock, exactly like an SB7,
-- and dumps whatever audio was on that channel for dwell_ms. rds_* is filled
-- from the per-session RDS pre-pass so every fragment carries its source station.
CREATE TABLE IF NOT EXISTS ghost.spins (
    timestamp     DateTime64(3) DEFAULT now64(3),
    session_id    String,                              -- one spirit-box run
    sweep_mode    LowCardinality(String),              -- 'forward' | 'reverse' | 'random'
    step_idx      UInt32,                              -- 0-based position within the session
    freq_hz       UInt32,                              -- tuned FM carrier for this step
    dwell_ms      UInt16,                              -- dwell time (SB7-style, ~100-350 ms)
    rssi_dbfs     Float32,                             -- per-step received power
    clip_fraction Float32 DEFAULT 0,                   -- ADC clipping fraction (0..1)
    rds_pi        UInt16 DEFAULT 0,                    -- Programme Identification of the source station
    rds_ps        LowCardinality(String) DEFAULT '',   -- 8-char station name, if known
    rds_rt        String DEFAULT '',                   -- RadioText of the source station, if known
    station_name  String DEFAULT '',                   -- human label (PS or a hand-entry)
    wav_path      String DEFAULT '',                   -- WAV this step's audio landed in
    dongle_id     LowCardinality(String) DEFAULT 'v3-01'
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (session_id, step_idx)
TTL toDateTime(timestamp) + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;

-- Per-session FM station table from the RDS pre-pass. Before the sweep, the
-- spirit box dwells ~2 s on each strong FM carrier and decodes PS/PI/RadioText,
-- building the freq -> station map used to label spins. ReplacingMergeTree keeps
-- the latest reading per frequency. Mirrors rds.station_latest.
CREATE TABLE IF NOT EXISTS ghost.stations (
    freq_hz    UInt32,
    pi_code    UInt16 DEFAULT 0,
    ps         String DEFAULT '',
    radiotext  String DEFAULT '',
    pty        UInt8 DEFAULT 0,
    rssi_dbfs  Float32 DEFAULT 0,
    session_id String DEFAULT '',
    last_seen  DateTime64(3) DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(last_seen)
ORDER BY (freq_hz);

-- Per-segment forensic findings on published video (module 2). One row per
-- analysed segment (a flagged "voice" clip or a stretch of room tone). Numeric
-- findings are Nullable and come back NULL when a tool cannot measure them
-- confidently (the honest-NULL posture of spectrum.blind_signal_features).
CREATE TABLE IF NOT EXISTS ghost.segments (
    video_id    String,                                -- opaque id (e.g. yt-dlp id); no channel/person names
    t_start     Float64,                               -- segment start (s) within the source audio
    t_end       Float64,                               -- segment end (s)
    kind        LowCardinality(String),                -- 'voice' | 'room' | 'clap' | 'emf_event' ...
    delay_ms    Nullable(Float32),                     -- measured slapback tap (delay_estimate.py)
    feedback    Nullable(Float32),                     -- slapback feedback fraction
    upper_bw_hz Nullable(UInt32),                      -- effective upper audio bandwidth (bandlimit.py)
    rt60_s      Nullable(Float32),                     -- room RT60 (reverb_match.py)
    fingerprint String DEFAULT '',                     -- fpcalc chromaprint (fingerprint.py)
    notes       String DEFAULT '',
    analyzed_at DateTime DEFAULT now()
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(analyzed_at)
ORDER BY (video_id, t_start)
SETTINGS index_granularity = 8192;
