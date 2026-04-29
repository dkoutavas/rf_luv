# spectrum/clickhouse/seeds/

Optional location-specific seed data for `spectrum.known_frequencies`. Loaded
*after* migrations, so the tables exist; `init.sql` and the migration files
themselves only contain schema (no INSERTs into known_frequencies after
this split).

## Why a separate directory

`init.sql` used to bake ~35 Athens-area frequencies into the schema bootstrap.
That made `git clone && docker compose up -d` produce a working pipeline —
but it also meant a contributor in any other city started with a misleading
known_frequencies table that biased the classifier toward signals that
couldn't be there.

Splitting the seed out keeps the schema portable and lets each operator
load (or write) the seed that matches their location.

## Loading a seed

After `docker compose up -d` completes (ClickHouse healthy, migrations
applied):

```bash
cat spectrum/clickhouse/seeds/known_frequencies_athens.sql \
  | docker exec -i clickhouse-spectrum clickhouse-client \
      --user spectrum --password "${CLICKHOUSE_PASSWORD:-spectrum_local}"
```

Each seed is idempotent (`WHERE (SELECT count() FROM ...) = 0`) — safe to
re-run, won't duplicate rows.

## Writing your own seed

Copy `known_frequencies_template.sql.example` to
`known_frequencies_<your_location>.sql`, replace the rows, and load as
above.

## Existing seeds

- `known_frequencies_athens.sql` — Athens / Polygono / Piraeus area
  (FM broadcast, Athens airfield ATC, Greek TETRA, Hymettus DVB-T,
  PMR446, ISM 433 MHz, marine VHF). 35 rows.

## Caveats

- `spectrum.signal_catalog` (migration 011) is *not* split — it's a 150-row
  taxonomy that mixes universal regulatory ranges (ITU/ICAO bounds) with
  ~30 Athens-specific entries. A contributor in another country can either
  delete the rows that don't apply, or just leave them — the catalog is
  read-only reference data and doesn't bias the classifier.
- The systemd-deployed batch jobs (classifier, feature_extractor) read from
  these tables. Their first useful run after seeding will pick up the new rows.
