# Followup — dongle_id downstream consumers

**Opened**: 2026-04-22, in the session that prepared leap for V4 (migrations 017–020, dongle-aware scanner/ingest, systemd templates).
**Status (updated 2026-04-23)**: all four code consumers and the Grafana dashboard are done in commit `9d66c0d`. Operational + schema followups remain — see status table below.

## Status summary

| Item | Status | Where |
|---|---|---|
| `detect_compression.py` per-dongle baseline + `--dongle-id` flag | ✅ done | commit `9d66c0d` |
| `feature_extractor.py` per-dongle loop | ✅ done | commit `9d66c0d` |
| `classifier.py` per-dongle loop | ✅ done | commit `9d66c0d` |
| `classifier_health.py` | ⏸ deferred (intentionally optional) | — |
| Grafana `spectrum-overview.json` `$dongle` variable + per-panel filters | ✅ done (single-select, not multi-select) | commit `9d66c0d` |
| `spectrum/docker-compose.yml` `spectrum-scanner` block removal | ⏳ pending — after ~1 week of stable native scanner runs | earliest 2026-04-29 |
| Migration 021 — `scan_runs.filter` column | ⏳ pending — only needed if Phase 8 self-A/B queries get awkward | optional |

End-to-end verification is now via the V3 self-A/B (post-2026-04-23 14:35 UTC, see `spectrum/docs/ab_comparison.md`), not the dual-dongle `dongle_comparison_view` we originally planned.

## Context

Migrations 017–020 gave every scanner-owned table a `dongle_id` column, rebuilt `hourly_baseline` with per-dongle GROUP BY, and added `spectrum.dongle_comparison_view`. Scanner + ingest tag every write with `dongle_id='v3-01'` (by default — the env file on leap makes it explicit).

**While only V3 is ingesting this is all fine.** Everything downstream that reads the tables currently gets per-frequency aggregates that implicitly belong to v3-01 because there's no other data to mix in. The moment V4 starts ingesting, any query that lacks an `AND dongle_id = 'v3-01'` filter silently conflates the two dongles, producing averages that are the mean of two different regimes.

This file lists everything that must be fixed before V4 is enabled. Each item has a file + line range (verified at time of writing — line numbers may have drifted if other work landed first).

## Code consumers

### `spectrum/analysis/detect_compression.py`

Reads `hourly_baseline` to compute `baseline_depression_db` (median bin depression vs hourly baseline). After migration 019, the MV is keyed on `(freq_hz, dongle_id, hour)`. Without a dongle filter the query returns a union across dongles — `avgMerge` over two different noise regimes will return a nonsensical middle value.

- **File**: `spectrum/analysis/detect_compression.py`
- **Queries to update**: search for `hourly_baseline` in the file. All of them need `AND dongle_id = :dongle_id` added to their WHERE.
- **New parameter**: `--dongle-id` CLI flag, defaulting to `v3-01`. Everything else in the script is per-sweep (already keyed on `sweep_id`, which we keep per-dongle via the 018 ORDER BY change) so the dongle filter only needs to propagate to the baseline lookups.
- **Priority**: **must** be fixed before V4 ingests. Otherwise the first detector run post-V4 will produce misleading compression_events.

### `spectrum/feature_extractor.py`

Reads `peaks`, `scans`, `sweep_health`, `allocations`; writes `peak_features`.

- **File**: `spectrum/feature_extractor.py`
- **Current behavior**: computes features globally across all rows in each source table.
- **Target behavior**: loop over dongles (derived from `SELECT DISTINCT dongle_id FROM spectrum.scans WHERE timestamp > now() - INTERVAL 1 HOUR`), compute features per-dongle, write rows with the appropriate `dongle_id`.
- **Constraint**: `peak_features` is now `ORDER BY (freq_hz, dongle_id)` (migration 018), so per-dongle writes don't collapse.
- **Priority**: should be fixed before V4 ingests. A v3-only default would mean V4 never gets features computed — classifier gap for V4.

### `spectrum/classifier.py`

Reads `peak_features` (FINAL), `known_frequencies`, `allocations`, `signal_classes`, `listening_log`; writes `signal_classifications`.

- **File**: `spectrum/classifier.py`
- **Current behavior**: reads all features, writes one row per `freq_hz`.
- **Target behavior**: loop over dongles. `signal_classifications` now has `ORDER BY (freq_hz, dongle_id)` so per-dongle writes coexist.
- **Priority**: should be fixed before V4 ingests, same reasoning as feature_extractor.

### `spectrum/classifier_health.py`

One-shot per timer fire, writes `classifier_health`. Single row per run across all classifications.

- **File**: `spectrum/classifier_health.py`
- **Decision**: keep this global across dongles for now. Per-dongle classifier health is interesting but not critical. If needed, add a `dongle_id` column to `classifier_health` in a future migration and loop here too.
- **Priority**: optional; do only if per-dongle observability becomes useful.

## Grafana dashboards

Grafana panels on spectrum-overview will conflate V3+V4 data once V4 ingests. Each panel that aggregates over `scans`, `peaks`, or `events` needs either a `$dongle` dashboard variable (preferred — lets the operator switch views) or a hardcoded `dongle_id='v3-01'` filter.

Known panels to update (line numbers from exploration agent, 2026-04-22; verify on the current file before editing):

- `spectrum/grafana/provisioning/dashboards/json/spectrum-overview.json`
  - Spectrogram (line ~204) — `GROUP BY freq_hz`, no dongle filter. Add `dongle_id = '$dongle'`.
  - Known-signals table (line ~340) — similar.
  - Noise-floor panel (line ~302) — reads hourly_baseline, no dongle filter. Must filter after 019.
  - Presence panel (line ~632) — `WHERE sweep_id LIKE 'full:%'` pattern. sweep_id format is unchanged (still `{preset}:{ts}`), so the LIKE still works; add dongle_id filter.
  - All other panels in the same file — sweep with grep for `spectrum.scans|spectrum.peaks|spectrum.events|hourly_baseline`.

**Add a dashboard variable**:
```json
"templating": {
  "list": [
    {
      "name": "dongle",
      "type": "custom",
      "query": "v3-01,v4-01",
      "current": {"value": "v3-01"},
      "includeAll": true
    }
  ]
}
```

Then every panel query gets `AND dongle_id IN ($dongle)` (works with multi-select or single).

ADS-B, AIS, and ISM dashboards don't read from the spectrum pipeline — no updates needed there.

## Operational cleanup

### `spectrum/docker-compose.yml`

Once the native systemd scanner (`rtl-scanner@v3-01.service`) has been running stably for ~1 week and we're confident in the native path, remove the `spectrum-scanner` service definition from docker-compose.yml. Keep ClickHouse and Grafana — those stay containerized.

- **File**: `spectrum/docker-compose.yml`
- **Scope**: delete the `spectrum-scanner` service block (lines 25–62 at last read). Leave `clickhouse`, `grafana`, and any networks/volumes that depend on them.
- **Priority**: cosmetic; the container stays stopped but the definition in the file invites mistakes like `docker compose up` recreating it.

## Schema

### `scan_runs.filter` column (for FM filter installation later)

When the FM bandstop filter gets installed on V3 (end of A/B week), we want each new `scan_runs` entry to record which filter is in line. Not in scope for this session, but:

- New migration (021?): `ALTER TABLE spectrum.scan_runs ADD COLUMN IF NOT EXISTS filter String DEFAULT '';`
- Update `scanner.py` to accept `SCAN_FILTER` env var and include it in `run_start` JSON.
- Update `scan_ingest.py` to pass it through to `scan_runs`.

Stored but not used by queries yet — retrospective tag for when filter decisions get revisited.

## Verification after each followup

**Note (2026-04-23)**: the original verification plan below assumed V4 had the FM filter and shared an antenna with V3 (so `dongle_comparison_view` would show a clean FM-band delta). Hardware reality changed: V3 got the filter directly, V4 has its own antenna at the patio window. The view's deltas now reflect antenna+filter+position together and aren't a clean filter-only signal.

**Current verification mechanism** is the V3 self-A/B (timestamp cutoff at `2026-04-22 14:35:12 UTC`), documented in `spectrum/docs/ab_comparison.md`. Per-dongle code correctness is verified by:

1. `SELECT dongle_id, count() FROM scans WHERE timestamp > now() - INTERVAL 5 MINUTE GROUP BY dongle_id` — both dongles should show comparable row counts.
2. `SELECT dongle_id, count() FROM peak_features FINAL WHERE computed_at > now() - INTERVAL 1 HOUR GROUP BY dongle_id` — both dongles should appear.
3. Same query against `signal_classifications`.
4. Latest `scan_runs.run_id` should start with `run_v3-01_` or `run_v4-01_` (not `run_2026...`).

If any of those return only `v3-01`, the per-dongle loop in the corresponding consumer didn't pick up V4. Re-check `9d66c0d` is deployed on leap.

### Original (now-stale) verification plan

```bash
clickhouse-client --port 9003 -q "
  SELECT freq_mhz_tile, delta_noise_floor_db
  FROM spectrum.dongle_comparison_view
  WHERE hour > now() - INTERVAL 24 HOUR
    AND freq_mhz_tile BETWEEN 88 AND 108  -- FM band; largest expected delta
  ORDER BY delta_noise_floor_db DESC
  LIMIT 20
"
```

This still runs and returns numbers, but interpret them as antenna-diversity deltas, not filter deltas (see `ab_comparison.md` "Antenna diversity" section).
