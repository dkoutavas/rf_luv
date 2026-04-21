# Followup — distinguish "compression with clear emitter" vs "compression of unknown origin"

**Opened**: 2026-04-21

## Context
After fix #1 (emitter estimator constrain + NULL attribution), some detected compression events will have `estimated_emitter_freq_hz = NULL`. That's the correct answer when no peak inside the compression zone qualifies — we're honestly saying "yes we see compression, but we can't localise its source from power-spectrum data alone."

A downstream Grafana/user reading these events sees a boolean-ish distinction:
- Emitter known → "compression at 304.19 MHz on Apr 21"
- Emitter NULL → "compression on 2026-04-17 12:05 from unknown"

That distinction is informative enough that it should probably be a first-class field rather than derived from NULL-ness of another column.

## Question
Should we add a `match_subtype` or `classification` enum column to `compression_events` that captures this dimension explicitly? Candidates:
- `emitter_localised` — NULL-emitter-freq is false, we have a MHz value
- `emitter_ambiguous` — NULL-emitter-freq is true, compression confirmed but source unknown
- `spur_only` — low-tier detection, no depression or clip confirmation

## Why it matters
- Grafana panels / dashboards need to group events in ways humans understand. `COUNT(*) WHERE estimated_emitter_freq_hz IS NOT NULL` is OK but brittle.
- Downstream listening-playbook entries only make sense for `emitter_localised` events.
- Phase 5 forensic capture should only trigger on `emitter_localised` events (we need to know what frequency to tune to).

## Approach
1. Add `match_subtype Enum8(...)` column to `compression_events` (migration 017).
2. Populate it in `detect_compression.py`:
   - `'emitter_localised'` if tier ≥ medium AND estimated_emitter_freq_hz IS NOT NULL
   - `'emitter_ambiguous'` if tier ≥ medium AND estimated_emitter_freq_hz IS NULL
   - `'spur_only'` if tier = low
3. Add a Grafana panel grouped by subtype.

## Expected outcome
One new column, minor detector update, one Grafana panel. Should take less than an hour. Probably worth doing in the next session after Apr 21's must-fixes land.

## Open design question
Is this really a separate enum, or does "match_tier + emitter_null" already carry the same information? Defensible either way. Lean toward the explicit enum because humans skim columns, not compute joint conditions.
