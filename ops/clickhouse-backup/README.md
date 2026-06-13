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

`backup.sh` walks each configured database and, for every MergeTree-family
table, runs:

```
docker exec clickhouse-<db> clickhouse-client \
  --query "SELECT * FROM <db>.<table> FORMAT Native" | gzip > <table>.native.gz
```

- **No extra binaries, no server reconfiguration.** Pure `docker exec` +
  `clickhouse-client` + `gzip`, in keeping with the project's stdlib ethos.
- **Native format preserves AggregateFunction states**, so AggregatingMergeTree
  and ReplacingMergeTree rollup tables restore exactly.
- Implicit `.inner` materialized-view storage and `Dictionary` tables are
  skipped: they are derived and repopulate from base inserts / reload from
  their source table on restore.
- Each run writes a timestamped snapshot `BACKUP_DIR/<db>/<UTC-timestamp>/`
  containing `<table>.native.gz` files, a `MANIFEST.tsv` (table, rows, bytes),
  and a `schema.sql` (SHOW CREATE, for self-containment). A `latest` symlink
  points at the newest snapshot. Snapshots older than `RETENTION_DAYS` prune.

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
| `DATABASES` | `spectrum acars` | space-separated db list |
| `RETENTION_DAYS` | `14` | prune snapshots older than this |
| `<DB>_PASSWORD` | `<db>_local` | per-db password override |

## Restore

Bring the target stack up first so migrations recreate the schema (tables +
materialized views), then restore the data:

```bash
cd spectrum && docker compose up -d        # migrate.py creates schema + MVs
bash ops/clickhouse-backup/restore.sh --db spectrum --latest
```

`restore.sh` detaches the materialized views, `TRUNCATE`s and re-inserts each
dumped table from its Native dump, then reattaches the MVs. Detaching the MVs
during the insert prevents base-table rows from fanning out and double-counting
into the rollup tables (which are restored from their own dumps). Use
`--from <timestamp>` for a specific snapshot, or `--dry-run` to preview.

## Verify a backup

```bash
cat $BACKUP_DIR/spectrum/latest/MANIFEST.tsv     # table  rows  bytes
journalctl --user -u clickhouse-backup -n 50     # last run log
systemctl --user list-timers clickhouse-backup.timer
```

A failed run fires a CRITICAL ntfy alert via `rf-notify` (if installed).

## Limitations

- Logical (per-table) backup, not physical. Fine for this scale (<5 GB);
  restore time is an insert, not a file copy.
- The adsb ClickHouse instance has historically run on the Windows host, not
  leap. Add `adsb` to `DATABASES` only on the host that actually runs it.
- This is disaster recovery, not point-in-time: granularity is one snapshot per
  day (per `OnCalendar`).
