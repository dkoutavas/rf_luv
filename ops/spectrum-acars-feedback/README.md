# spectrum-acars-feedback

Hourly cross-pipeline feedback service: takes ACARS decode confirmations from `acars.freq_activity` and promotes them into `spectrum.listening_log` so the spectrum classifier treats those freqs as operator-confirmed at confidence 1.0.

## Why

The spectrum classifier (`spectrum/classifier.py`) already understands two confidence boosters:

1. **`spectrum.known_frequencies`** — soft prior, +3 score in the rule engine. Migration 022 seeds the three EU ACARS freqs (131.525 / 131.725 / 131.825 MHz) here so the classifier knows they exist *in principle*.
2. **`spectrum.listening_log`** — hard override, sets confidence to 1.0 for matches within 150 kHz tolerance. Originally an operator-only path ("I tuned, I confirmed").

CRC-validated ACARS decodes are ground truth. They belong in the second category — there's no ambiguity to score. This service is the bridge: every hour it reads `acars.freq_activity` (the ReplacingMergeTree of freq → message_count maintained by the acars pipeline) and writes a confirmation row for each freq with ≥ N recent messages. The classifier picks it up on its next 5-min run.

## Architecture

```
acars (ClickHouse :8127)         spectrum (ClickHouse :8126)
└── freq_activity FINAL  ─[1h]─→ listening_log INSERT  ─[5min]─→ classifier picks up
       (ground truth)             (operator-confirm path)         confidence=1.0
```

The script does NOT use ClickHouse `remote()` — keeps the cross-DB write explicit, stdlib-only Python, no extra ClickHouse user permissions.

## Files

- `../../spectrum/acars_feedback.py` — the script (lives with the spectrum code so `db.py`-style helpers stay coherent)
- `spectrum-acars-feedback.service` — systemd oneshot unit
- `spectrum-acars-feedback.timer` — hourly at `:15`
- `install.sh` — idempotent installer

## Tunables

Set via Environment= in the .service file or `--user edit`:

| Env var | Default | Notes |
|---|---|---|
| `ACARS_FEEDBACK_LOOKBACK_HOURS` | 24 | Window over which `last_seen` must fall |
| `ACARS_FEEDBACK_MIN_MESSAGES` | 10 | Cumulative `message_count` floor (raise if false-positives) |
| `ACARS_FEEDBACK_DRY_RUN` | 0 | Set to 1 to log what would happen without inserting |

## Idempotency

Each insert tags the `notes` field with `acars-feedback YYYY-MM-DD dongle=… msgs=…`. The script skips a freq if today's tag is already present in `listening_log`. So re-running is safe; missing one hour is fine; running every minute would still write at most one row per freq per day.

## Failure modes

- **`acars` ClickHouse unreachable** (pipeline not yet deployed): script logs a warning and exits 0. Designed to be deployed before ACARS itself.
- **No qualifying freqs**: script logs "no-op" and exits 0.
- **Spectrum ClickHouse unreachable**: HTTPError raised, systemd records the failure. Hourly retry handles transient issues.

## Verifying it works

```bash
# Run once now, with dry-run, to see what it would do
ACARS_FEEDBACK_DRY_RUN=1 \
  systemctl --user start --wait spectrum-acars-feedback.service
journalctl --user -u spectrum-acars-feedback -n 30

# Real run
systemctl --user start spectrum-acars-feedback.service
# Confirm rows landed
clickhouse-client --user spectrum --password spectrum_local --database spectrum \
  --query "SELECT timestamp, freq_mhz, class_id, notes FROM listening_log WHERE class_id LIKE 'acars_%' ORDER BY timestamp DESC LIMIT 5 FORMAT Vertical"

# Then check the next classifier run picks them up
journalctl --user -u spectrum-classifier -n 30
clickhouse-client --user spectrum --password spectrum_local --database spectrum \
  --query "SELECT freq_hz, class_id, confidence FROM signal_classifications FINAL WHERE class_id LIKE 'acars_%' FORMAT Vertical"
```

## Disable

```bash
systemctl --user disable --now spectrum-acars-feedback.timer
# (the .service unit is harmless — it won't run without the timer)
```
