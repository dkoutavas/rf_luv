#!/usr/bin/env bash
set -euo pipefail
#
# clickhouse-backup: logical per-table snapshots of the rf_luv ClickHouse
# databases to off-host storage.
#
# WHY THIS EXISTS: the 2026-06 leap disk failure exposed that every pipeline's
# data lived only in a Docker named volume, with zero backups. Months of
# spectrum.scans (and the one-shot FM-bandstop A/B baseline) had no recovery
# path. Total CH footprint is under 5 GB, so a daily full logical dump is cheap
# insurance. See ops/clickhouse-backup/README.md.
#
# HOW IT WORKS (dependency-free: no extra binaries, no server reconfig):
#   for each configured database -> for each MergeTree-family table:
#     docker exec <CONTAINER> clickhouse-client --user <db> --password <pass> \
#       --query "SELECT * FROM <db>.<table> FORMAT Native" | gzip > table.native.gz
#   One shared ClickHouse container holds every database (post-consolidation),
#   so the per-db user/pass select the database, not a per-db container.
#   ClickHouse Native format preserves AggregateFunction states, so rollup
#   (AggregatingMergeTree / ReplacingMergeTree) tables restore exactly.
#   SHOW CREATE output is also captured per snapshot for self-containment,
#   though the repo migrations remain the canonical schema source.
#
# Restore with restore.sh. Old snapshots prune past RETENTION_DAYS.
#
# Deploy: bash ops/clickhouse-backup/install.sh   (daily user timer)
# Manual: bash ops/clickhouse-backup/backup.sh
#
# Config: /etc/rtl-scanner/clickhouse-backup.env (see clickhouse-backup.env.example)

# ---- config (env-overridable) -----------------------------------------------
ENV_FILE="${CLICKHOUSE_BACKUP_ENV:-/etc/rtl-scanner/clickhouse-backup.env}"
# shellcheck disable=SC1090
[ -r "$ENV_FILE" ] && . "$ENV_FILE"

# Where snapshots land. Point this at OFF-HOST storage (external drive, NAS
# mount, rclone-mounted bucket). A backup on the same failing disk is no backup.
BACKUP_DIR="${BACKUP_DIR:-/var/backups/rf-clickhouse}"

# Which databases to dump. Post-consolidation all six live on one server;
# spectrum/acars hold the irreplaceable data, adsb/ais/ism/noaa are companion
# pipelines (often empty) but cheap to include.
DATABASES="${DATABASES:-spectrum acars adsb ais ism noaa}"

# The single shared ClickHouse container (post-2026-06 consolidation). All
# databases live in it; the per-db container names (clickhouse-<db>) are gone.
CONTAINER="${CH_CONTAINER:-clickhouse}"

RETENTION_DAYS="${RETENTION_DAYS:-14}"

# ---- helpers ----------------------------------------------------------------
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[ok]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }

notify_fail() {
    # Best-effort phone alert via the existing ntfy helper. Never let a failed
    # notification mask the real backup failure.
    if command -v rf-notify >/dev/null 2>&1; then
        rf-notify CRITICAL "clickhouse-backup failed" -m "$1" -t floppy_disk || true
    fi
}

# Per-db credentials. Convention: user == db name, password == <db>_local,
# overridable per db via <DBUPPER>_PASSWORD in the env file (e.g. SPECTRUM_PASSWORD).
db_user() { echo "$1"; }
db_pass() {
    local db="$1" var
    var="$(echo "$db" | tr '[:lower:]' '[:upper:]')_PASSWORD"
    echo "${!var:-${db}_local}"
}

# ts is UTC, sortable; safe as a directory name.
TS="$(date -u +%Y%m%dT%H%M%SZ)"

fail() { err "$*"; notify_fail "$*"; exit 1; }
trap 'fail "unexpected error at line $LINENO"' ERR

# ---- main -------------------------------------------------------------------
info "clickhouse-backup starting (ts=$TS, dir=$BACKUP_DIR, container=$CONTAINER, dbs=[$DATABASES])"
mkdir -p "$BACKUP_DIR"

# Test the ONE shared container once, up front. Doing this per-db inside the
# loop (as a previous version did, keying on a non-existent clickhouse-<db>
# name) would skip EVERY database and silently produce a zero-table backup,
# the exact zero-backup trap this whole tool exists to prevent.
if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" >/dev/null 2>&1; then
    fail "container '$CONTAINER' not running; nothing to back up. Bring the infra stack up first."
fi

total_tables=0
for db in $DATABASES; do
    user="$(db_user "$db")"
    pass="$(db_pass "$db")"

    ch() { docker exec "$CONTAINER" clickhouse-client --user "$user" --password "$pass" "$@"; }

    # MergeTree-family base + explicit MV-target tables. Skip Views,
    # MaterializedView definitions, Dictionaries, and implicit .inner tables
    # (those repopulate from base inserts via the MV chain on restore).
    mapfile -t tables < <(ch --query "
        SELECT name FROM system.tables
        WHERE database='${db}'
          AND engine LIKE '%MergeTree%'
          AND name NOT LIKE '.inner%'
          AND name NOT LIKE '.tmp%'
        ORDER BY name FORMAT TabSeparated")

    if [ "${#tables[@]}" -eq 0 ]; then
        warn "$db: no MergeTree tables found; skipping"
        continue
    fi

    dest="$BACKUP_DIR/$db/$TS"
    mkdir -p "$dest"
    : > "$dest/MANIFEST.tsv"
    : > "$dest/schema.sql"

    for t in "${tables[@]}"; do
        [ -z "$t" ] && continue
        # Schema (best-effort, for self-containment).
        {
            echo "-- ${db}.${t}"
            ch --query "SHOW CREATE TABLE ${db}.${t}" --format TabSeparatedRaw || true
            echo ";"
            echo
        } >> "$dest/schema.sql"

        # Data: Native preserves aggregate states; gzip on the way out.
        ch --query "SELECT * FROM ${db}.${t} FORMAT Native" | gzip -c > "$dest/${t}.native.gz"

        rows="$(ch --query "SELECT count() FROM ${db}.${t}")"
        bytes="$(stat -c %s "$dest/${t}.native.gz" 2>/dev/null || echo 0)"
        printf '%s\t%s\t%s\n' "$t" "$rows" "$bytes" >> "$dest/MANIFEST.tsv"
        total_tables=$((total_tables + 1))
    done

    # Mark newest snapshot for restore --latest.
    ln -sfn "$TS" "$BACKUP_DIR/$db/latest"

    snap_bytes="$(du -sh "$dest" | cut -f1)"
    info "$db: ${#tables[@]} tables -> $dest ($snap_bytes)"

    # Retention: drop snapshots older than RETENTION_DAYS (keeps 'latest' link
    # valid because find only removes timestamp dirs, not the symlink).
    find "$BACKUP_DIR/$db" -mindepth 1 -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" \
        -exec rm -rf {} + 2>/dev/null || true
done

trap - ERR
info "clickhouse-backup done ($total_tables tables across [$DATABASES])"
