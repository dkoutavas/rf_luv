-- acarsdec 0b7ba27 (the image pinned on 2026-09-27) reports the channel noise
-- floor next to each message's level, e.g. "level": -55.0, "noise": -65.5.
-- level_db - noise_db is the per-message SNR. Rows from before this column
-- read 0 (the value is still in raw_json).
ALTER TABLE acars.messages ADD COLUMN IF NOT EXISTS noise_db Float32 DEFAULT 0 AFTER level_db;
