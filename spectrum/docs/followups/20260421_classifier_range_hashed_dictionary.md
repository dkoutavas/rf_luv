# Followup — RANGE_HASHED ClickHouse Dictionary over signal_catalog

**Opened**: 2026-04-21

## Context
`spectrum.signal_catalog` (migration 011) is our reference taxonomy for 88–470 MHz, currently 66 entries. It's used today for:
- Human lookup via `signal_catalog_view`
- Phase 2 archaeology reporting
- Phase 5 forensic target selection (future)

At classifier-time (`spectrum/classifier.py`), the scoring logic does per-bin allocation lookups against `allocations` (12 rows). It doesn't use `signal_catalog`. But if it did, we'd want an O(1) range lookup that answers "what service covers this freq?" in microseconds, not a per-bin JOIN.

## Question
Is it worth promoting `signal_catalog` to a `CREATE DICTIONARY signal_catalog_dict LAYOUT(RANGE_HASHED)` resource, keyed on `freq_lo_hz/freq_hi_hz`, so `dictGet('signal_catalog_dict', 'service', (freq_hz, freq_hz))` becomes the idiomatic lookup?

## Why it matters
- Classifier.py runs on a 5-minute cadence. Even the "slow" way of joining against `signal_catalog` (66 rows) is fine for that cadence.
- BUT — if the scanner itself ever wants to label bins in real time (annotate peaks during sweep), a Dictionary is the only structure that's fast enough.
- Dictionaries also make the SQL readable. `WHERE signal_catalog_dict.service = 'marine_vhf'` reads like an API, not a join.

## Approach
1. Add migration `018_create_signal_catalog_dict.sql`:
   ```sql
   CREATE DICTIONARY IF NOT EXISTS spectrum.signal_catalog_dict (
       freq_lo_hz UInt32,
       freq_hi_hz UInt32,
       service String,
       confidence String,
       modulation_expected String,
       notes String
   )
   PRIMARY KEY freq_lo_hz
   SOURCE(CLICKHOUSE(TABLE 'signal_catalog' DATABASE 'spectrum'
                     USER 'spectrum' PASSWORD 'spectrum_local'))
   LAYOUT(RANGE_HASHED())
   RANGE(MIN freq_lo_hz MAX freq_hi_hz)
   LIFETIME(MIN 300 MAX 600);
   ```
2. Add a helper query in Grafana (e.g. "show last 10 compression events with service label") to verify it works.
3. Don't wire into classifier.py until there's a concrete scoring improvement to ship with it.

## Notes on sharp edges
- Multiple `signal_catalog` rows can cover the same freq (broader allocation + specific channel). Dictionary returns the FIRST match found. Decide the priority: specific channel first, then allocation — may need an `authority_order` field in the catalog.
- Dictionaries refresh on LIFETIME expiry (~5-10 min). After catalog changes, wait for refresh or `SYSTEM RELOAD DICTIONARY`.
- ClickHouse 24.3's RANGE_HASHED technically requires a numeric key (not just range). We'd use `freq_lo_hz` as key; range from `freq_lo_hz` to `freq_hi_hz`.

## Verdict for now
**Not worth the churn today.** Classifier is on a 5-min cadence; JOIN against 66 rows is instant. Revisit when one of these arises:
- Real-time bin labeling in scanner (unlikely near-term)
- Catalog grows past ~1000 entries and JOIN cost starts mattering
- Operator-facing queries get repetitive enough to want a dictGet helper

Keep this doc as a reminder that the right shape exists when we need it.
