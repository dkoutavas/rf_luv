# Followup — verify must-fixes #1/#2/#3 once leap recovers

**Opened**: 2026-04-21

## Context
During the commit session for today's reviewer must-fixes, leap-box ClickHouse became unresponsive (hung query, HTTP accepting but not responding; SSH banner timing out). The timing is suspicious — it started shortly after migration 015 (`ALTER MODIFY COLUMN estimated_emitter_* Nullable(...)`) was submitted to the 84-row `compression_events` table.

Commits 9dc3f1b (fix #1/#3) and 0e6b6eb (fix #2) shipped the code and migrations but:
- Migration 015 is in unknown state.
- Migration 016 was never submitted.
- No archaeology re-run was performed.
- No verification of the feature_extractor filter.

## Checklist on recovery

### 1. Diagnose the outage
```bash
ssh dio_nysis@192.168.2.10
uptime
journalctl --since '2026-04-21 20:40' --until '2026-04-21 21:15' -p warning
docker ps --format '{{.Names}} {{.Status}}'
docker logs --tail 200 clickhouse-spectrum | tail -100
docker exec clickhouse-spectrum clickhouse-client --user spectrum --password spectrum_local \
    --query "SELECT mutation_id, is_done, latest_fail_reason FROM system.mutations"
```

If the 015 mutation is stuck: `KILL MUTATION WHERE mutation_id = '...'` then re-apply.

### 2. Check compression_events schema
```sql
DESCRIBE spectrum.compression_events
```
Should show:
- `estimated_emitter_freq_hz Nullable(UInt32)`
- `estimated_emitter_power_dbfs Nullable(Float32)`
- `sig_clip_fm UInt8` (if 016 was applied)

If Nullable didn't stick, drop + recreate the table from 012 + 015 + 016 in sequence.

### 3. Apply migration 016 if missing
```bash
curl "http://192.168.2.10:8126/?user=spectrum&password=spectrum_local&database=spectrum" \
     --data-binary @spectrum/clickhouse/migrations/016_add_sig_clip_fm.sql
```
Record it in `schema_migrations` alongside 015.

### 4. Truncate and re-run archaeology
```bash
curl ... --data-binary "TRUNCATE TABLE spectrum.compression_events"
cd ~/dev/rf_luv/spectrum
python3 analysis/detect_compression.py --backfill --min-tier low > /tmp/backfill_v2.log 2>&1 &
# Wait ~10 min
```

### 5. Record actual counts
Compare:
| Tier | v1 counts (pre-fix) | v2 counts |
|---|---|---|
| none | 2,357 | ? |
| low | 73 | ? |
| medium | 10 | ? |
| high | 1 | ? |

Update `spectrum/analysis/compression_events.md` with the v2 table and note what changed:
- How many events lost emitter attribution (went NULL)?
- Did the Apr 21 11:55 event keep its 304.19 MHz attribution?
- Did the Apr 19 14:03 event keep 166.87 MHz, or go NULL (spur block was tiles 45-54, emitter tile 38 — outside ±2)?
- Did any events shift tier (medium → low, etc.)?
- Did any events get LOST entirely (failing the new tighter sig_clip threshold)?

### 6. Verify fix #2 against the Apr 21 event
```sql
-- Before fix #2, FM carriers dragged toward -25 dBFS if Apr 21 11:55 was
-- in the 24h window. After fix, they should stay near their baseline -14 dBFS.
SELECT freq_hz/1e6 AS mhz, power_mean_dbfs, sweeps_observed_24h
FROM spectrum.peak_features FINAL
WHERE freq_hz IN (99600000, 105800000, 191250000)  -- Kosmos FM, Skai, DVB-T Ch8
  AND computed_at > now() - INTERVAL 2 HOUR
ORDER BY freq_hz;
```
The power_mean_dbfs values should be within 1-2 dB of what the same bins showed on a "quiet" day (e.g. 2026-04-14). If they're still 8-10 dB low, the filter isn't working.

### 7. Flag any narrative contradictions
If v2 counts look dramatically different from the briefing's 11 medium+ events:
- Update `docs/briefing_20260421_next_steps.md` with an addendum noting the revised findings.
- Don't silently edit the briefing — it was a point-in-time artifact.

## Expected outcome
A short session report appended to `compression_events.md` with the v2 counts and what changed. If everything behaves as expected, the only narrative update is "the 9 −174 kHz events now correctly have NULL emitter attribution." If something surprises us (fewer events, tier shifts), flag it explicitly.

## Hard-mode contingency
If leap is permanently broken (bad ClickHouse data directory, SSD failure, OOM-killed by the ALTER), the recovery path is:
1. `docker compose down` on leap.
2. Back up whatever's left of `/var/lib/docker/volumes/spectrum_clickhouse-data/`.
3. `docker compose up -d` — migrations 001-016 will re-apply from scratch on empty DB.
4. Lose 16 days of history. Accept and move on.

Don't take option 4 lightly — that's the archaeology data. Pause and confirm with human before doing this.
