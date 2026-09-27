# clickhouse-backup

Daily off-host logical backups of the rf_luv ClickHouse databases.

## Why this exists

The 2026-06 leap disk failure exposed that every pipeline's data lived only in
a Docker named volume on a single failing disk, with no backup of any kind.
The schema is safe (it lives in the repo as migrations), but the data was not:
months of `spectrum.scans`, the one-shot FM-bandstop V3-pre / V4-post A/B
baseline, the ACARS soak decodes, and all `listening_log` operator notes had no
recovery path. Total ClickHouse footprint is under 5 GB, so a daily full dump
is cheap insurance. This closes that gap.

## What it does

`backup.sh` walks each configured database in the shared `clickhouse`
container and, for every MergeTree-family table, runs:

```
docker exec clickhouse clickhouse-client \
  --query "SELECT * FROM <db>.<table> SETTINGS final = 1 FORMAT Native" | gzip > <table>.native.gz
```

- **No extra binaries, no server reconfiguration.** Pure `docker exec` +
  `clickhouse-client` + `gzip`, in keeping with the project's stdlib ethos.
- **Native format preserves AggregateFunction states**, so AggregatingMergeTree
  and ReplacingMergeTree rollup tables restore exactly. `final = 1` collapses
  rows that background merges have not collapsed yet, so the MANIFEST row count
  is the logical count and matches a restored table.
- **Materialized views with hidden storage are dumped too.** A view created
  without `TO <table>` keeps its rollups in a hidden `.inner_id.<uuid>` table.
  `SELECT * FROM <view>` reads that storage, so each such view gets its own
  `<view>.native.gz`. The rollups cannot be rebuilt from the base table after
  a restore, because the view only sees rows inserted after it exists. Views
  with an explicit `TO` target (spectrum `hourly_baseline_mv`) need no dump:
  the target table is dumped like any other.
- `Dictionary` tables are skipped: they reload from their source table.
- Each run writes a timestamped snapshot `BACKUP_DIR/<db>/<UTC-timestamp>/`
  containing `<table>.native.gz` and `<view>.native.gz` files, a `MANIFEST.tsv`
  (name, rows, bytes), and a `schema.sql` (SHOW CREATE of every table and view,
  for self-containment). A `latest` symlink points at the newest snapshot.
  Snapshots older than `RETENTION_DAYS` prune.

## Deploy

```bash
bash ops/clickhouse-backup/install.sh
```

Installs a user-level systemd timer (`clickhouse-backup.timer`, daily 04:17
UTC), creates the config file from the example, creates the backup dir, and
runs one backup as a smoke test.

### Configure off-host storage (do this)

Edit `/etc/rtl-scanner/clickhouse-backup.env` and set `BACKUP_DIR` to storage
that is **not on the ClickHouse disk**: an external USB drive, an NFS/SMB mount
to another machine, or an rclone-mounted cloud bucket. A backup on the same
disk that fails is not a backup. The installer warns if `BACKUP_DIR` looks
local.

Defaults (all overridable in the env file):

| Var | Default | Meaning |
|-----|---------|---------|
| `BACKUP_DIR` | `/var/backups/rf-clickhouse` | snapshot target (set off-host) |
| `DATABASES` | all eight | space-separated db list |
| `RETENTION_DAYS` | `14` | prune snapshots older than this |
| `<DB>_PASSWORD` | `<db>_local` | per-db password override |

## Restore

Bring the data layer up first so `ch-bootstrap` recreates the schema (tables
and materialized views), then restore the data:

```bash
bash infra/up.sh                            # ch-bootstrap creates schema + MVs
bash ops/clickhouse-backup/restore.sh --db spectrum --latest
```

`restore.sh` detaches the materialized views, `TRUNCATE`s and re-inserts each
dumped table from its Native dump, then reattaches the views and restores the
view rollups from their own dumps (`INSERT INTO <view>` writes to the hidden
storage). Detaching the views during the base insert stops base-table rows
from fanning out and double-counting into the rollups. Use `--from <timestamp>`
for a specific snapshot, or `--dry-run` to preview.

## Restore drill

```bash
bash ops/clickhouse-backup/restore-drill.sh            # all databases
bash ops/clickhouse-backup/restore-drill.sh spectrum   # just some
```

The drill proves the latest snapshots restore, without touching the live
server. It starts a throwaway `clickhouse-drill` container (same image, no
ports, no volume), builds the schema with the normal `ch-bootstrap` image,
restores every database with `restore.sh`, and compares each table and view
against its `MANIFEST.tsv` row count. It prints PASS or FAIL per row, exits 1
on any mismatch, and removes the container on exit. Run it monthly and before
any ClickHouse upgrade.

## Verify a backup

```bash
cat $BACKUP_DIR/spectrum/latest/MANIFEST.tsv     # table  rows  bytes
journalctl --user -u clickhouse-backup -n 50     # last run log
systemctl --user list-timers clickhouse-backup.timer
```

A failed run fires a CRITICAL alert via `rf-notify` (if installed): ntfy if a
topic is set, else a desktop popup.

## Limitations

- Logical (per-table) backup, not physical. Fine for this scale (<5 GB);
  restore time is an insert, not a file copy.
- All eight databases live in the one shared ClickHouse (`infra/`), so the
  default `DATABASES` list covers every pipeline, including ones with no data
  yet.
- This is disaster recovery, not point-in-time: granularity is one snapshot per
  day (per `OnCalendar`).
