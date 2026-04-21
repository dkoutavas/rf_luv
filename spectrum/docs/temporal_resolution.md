# Sub-sweep temporal resolution — design

**Status: design only. No code or schema changes proposed for immediate merge; migration sketch included for future implementation.**

## Problem

`scanner.py:448` assigns **one `timestamp` to every bin in a sweep** (stamped once before the 186-tile sweep begins). All 3917 rows of a full sweep share the same `timestamp` value. This caps our temporal resolution at ~2.3 s (the sweep duration) and makes it impossible to localize sub-sweep phenomena like the 2026-04-21 11:55:04.960 compression event — which we reconstructed to ~0.9 s within the sweep only by computing tile indices from `freq_hz` and inferring tile order from the known low-to-high sweep pattern.

Current sweep shape (from `scanner.py:248-297`):

- Full sweep: 186 tiles × ~12.15 ms/tile ≈ 2261 ms
- Each tile: 5 ms PLL settle + 32 KB discard (~8 ms) + 8 captures × 1024-sample FFT
- All bins from the sweep get `sweep_ts = datetime.now(...)` captured once at line 448

## Recommendation — new `scan_tiles` table

Don't alter `scans`. Don't add per-capture timestamps. **Add a sibling table `scan_tiles` with one row per tile per sweep.**

### Why not ALTER scans

`scans` has ~21 bins per tile (100 kHz bins across a 2.048 MHz tile). Adding `tile_idx UInt16` + `tile_started_at DateTime64(3)` to scans means replicating tile metadata 21× per tile. ClickHouse's column compression dedups it well, but the columns still decompress into memory per query, and the asymmetry only gets worse once we add per-tile columns that don't belong on a per-bin row (gain applied to this tile, tile clip fraction, IQ blob reference for Phase 5, etc.).

### Why not per-capture timestamps

8 captures per tile × 186 tiles × 200 full sweeps/day + airband = **~3-4 M rows/day** just for capture metadata. Net benefit is ~1.5 ms temporal resolution vs 12 ms — negligible for our use cases. Phase 5 gives us on-demand full IQ for the few events that actually matter.

### Schema

```sql
-- New migration 015_add_scan_tiles.sql
CREATE TABLE IF NOT EXISTS spectrum.scan_tiles (
    sweep_id          String,
    tile_idx          UInt16,
    tile_center_hz    UInt32,
    tile_started_at   DateTime64(3),
    tile_duration_ms  UInt16,
    clip_fraction     Float32   DEFAULT 0,
    worst_sample      UInt8     DEFAULT 0,   -- max of |raw - 127| across captures in this tile
    gain_applied_db   Float32   DEFAULT 0,
    iq_capture_path   String    DEFAULT ''   -- set by Phase 5 if raw IQ saved
) ENGINE = MergeTree()
ORDER BY (sweep_id, tile_idx)
TTL toDateTime(tile_started_at) + INTERVAL 180 DAY
SETTINGS index_granularity = 512;

-- Also add a derived tile_idx to scans for query convenience (nullable, cheap)
ALTER TABLE spectrum.scans
    ADD COLUMN IF NOT EXISTS tile_idx UInt16 DEFAULT 0;
```

### Storage cost

- Full sweeps: ~200/day × 186 tiles = 37.2k rows/day
- Airband sweeps: ~1440/day × 10 tiles = 14.4k rows/day
- Total: ~52k rows/day, ~17 M rows/year
- Estimated compressed size: ~1 MB/day = **~360 MB/year for per-tile metadata**.
- scans `tile_idx` column: 2 bytes × 11.7 M rows = 23 MB uncompressed, <1 MB after compression (low cardinality).

Both trivial against the existing ~44 MiB for 16 days of scans data.

### Scanner change

`spectrum/scanner.py` emits a new JSON marker per tile inside the `sweep()` loop:

```python
# After set_frequency + settle + discard, before the capture loop:
tile_started_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
tile_idx = (center - FREQ_START - SAMPLE_RATE // 2) // SAMPLE_RATE
print(json.dumps({
    "tile_start": True,
    "sweep_id": sweep_id,
    "tile_idx": int(tile_idx),
    "tile_center_hz": int(center),
    "tile_started_at": tile_started_at,
    "gain_db": effective_gain,
}), flush=True)
t0_tile = time.monotonic()

# ...existing 8-capture loop...

# After the loop:
print(json.dumps({
    "tile_end": True,
    "sweep_id": sweep_id,
    "tile_idx": int(tile_idx),
    "tile_duration_ms": int((time.monotonic() - t0_tile) * 1000),
    "clip_fraction": tile_worst_clip,
    "worst_sample": tile_worst_sample,
}), flush=True)
```

Each bin emitted in the tile then carries `"tile_idx": tile_idx` so scan_ingest.py can populate the new `tile_idx` column on `scans`.

### Ingest change

`spectrum/scan_ingest.py` gains a new handler for `tile_start`/`tile_end` messages:

- `tile_start` → buffer a partial row (sweep_id + tile_idx as key; tile_started_at, tile_center_hz, gain_applied_db)
- `tile_end` → complete the row (tile_duration_ms, clip_fraction, worst_sample) and stage for insert
- Flush to `scan_tiles` alongside the existing scans batch on the `flush` marker

No change to how `scans` rows are batched; just an additional `tile_idx` column populated from the incoming message.

### Query-pattern impact on existing Grafana panels

**None.** Existing panels ignore `tile_idx` (it defaults to 0 for old data). Queries that need sub-sweep resolution opt-in to joining `scan_tiles` on `sweep_id`:

```sql
-- "show me the exact moment compression started on Apr 21 11:55"
SELECT t.tile_started_at, t.tile_idx, t.tile_center_hz,
       s.freq_hz/1e6 AS mhz, s.power_dbfs
FROM scan_tiles t
JOIN scans s ON t.sweep_id = s.sweep_id AND t.tile_idx = s.tile_idx
WHERE t.sweep_id = 'full:2026-04-21 11:55:04.960'
ORDER BY t.tile_idx;
```

## Event-triggered cheap IQ save (in-scope for Phase 4)

Separate from Phase 5 forensic mode. Design goal: whenever a single tile sees `power_dbfs > 0 dBFS`, scanner writes the raw IQ bytes from (tile-1, tile, tile+1) to `/var/lib/spectrum/iq_cheap/<sweep_id>_<tile_idx>.cs8`.

### Size math

- 3 tiles × 8 captures × 1024 samples × 2 bytes = **49152 bytes ≈ 48 KB per event**
- Events observed: 1 clipping-like event per day (Phase 2 backfill will calibrate)
- 48 KB/event × 365 events/year ≈ **18 MB/year**

Absolutely trivial. Keep indefinitely.

### Implementation

In `scanner.py`'s `sweep()` function, retain the last 3 tiles' raw IQ bytes in a deque:

```python
from collections import deque
iq_ring = deque(maxlen=3)  # each entry: (tile_idx, iq_bytes_concatenated)

# Inside tile loop, after reading all 8 captures:
iq_ring.append((tile_idx, tile_iq_concatenated))

# After checking clip_fraction / peak power of THIS tile:
if any_bin_in_this_tile_above_0dbfs:
    # flush the ring to disk
    out_path = f"/var/lib/spectrum/iq_cheap/{sweep_id.replace(':','_').replace(' ','_')}_tile_{tile_idx:04d}.cs8"
    with open(out_path, 'wb') as f:
        for ti, iq in iq_ring:
            f.write(iq)
    # Store pointer in scan_tiles
    print(json.dumps({"tile_iq_saved": True, "sweep_id": sweep_id, "tile_idx": tile_idx,
                      "path": out_path}), flush=True)
```

`scan_ingest.py` updates the `scan_tiles.iq_capture_path` column on receipt.

### Rotation

Same mechanism as Phase 5: `iq_cheap/` directory capped at e.g. 500 MB (far more than we need), oldest-file-deleted rotation.

## What doesn't change

- `spectrum.scans` table schema (except one new `tile_idx` column).
- Existing Grafana panels, classifier, feature_extractor, classifier_health.
- Migration order: this is migration 015 (after 014 backfill_listening_log).
- Scanner sweep timing / bin count / FFT size.

## Migration discipline

- `scan_tiles` is a new table, not a rename. No data loss risk.
- `scans.tile_idx` is nullable (DEFAULT 0). Backfilling for the 11.7 M existing rows is optional — `tile_idx` is derivable from `freq_hz` via the documented tile formula `toUInt16((freq_hz - 88000000) / 2048000)`. A follow-up migration can `ALTER TABLE ... UPDATE` if ever needed.
- Scanner change ships BEHIND a feature flag: `EMIT_TILE_EVENTS=true` env var. Ingest tolerates both formats so rollback is an env-var flip, not a code rollback.

## Verification

1. Apply migration 015 on leap; confirm `DESCRIBE scan_tiles` matches spec.
2. Rebuild scanner image with new code; start with `EMIT_TILE_EVENTS=true`.
3. After one full sweep, `SELECT count() FROM scan_tiles WHERE sweep_id = <new-sweep-id>` should return 186.
4. `SELECT tile_started_at FROM scan_tiles WHERE sweep_id = <new-sweep-id> ORDER BY tile_idx LIMIT 10` should show strictly-increasing timestamps spaced ~12 ms apart.
5. Temporarily send a strong signal to verify `tile_iq_saved` marker fires and file appears in `/var/lib/spectrum/iq_cheap/`.

## Nice-to-haves (not needed for v1)

- Backfill `scans.tile_idx` for the 11.7 M existing rows via `ALTER TABLE ... UPDATE tile_idx = toUInt16((freq_hz - 88000000) / 2048000)`. One-time mutation.
- Time-synced rtl_tcp: currently each sweep reopens the rtl_tcp connection, giving sub-ms jitter. Enough for the design here, but if we ever want sample-precise cross-sweep alignment we'd need a persistent rtl_tcp session — a bigger change.
