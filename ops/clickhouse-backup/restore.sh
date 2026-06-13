#!/usr/bin/env bash
set -euo pipefail
#
# restore.sh: restore a rf_luv ClickHouse database from a backup.sh snapshot.
#
# Procedure (correct-by-construction, no double-counting):
#   1. The fresh stack must already be up and migrated (tables + MVs exist):
#        bash infra/up.sh   # ch-bootstrap migrates schema for all 6 databases
#   2. DETACH every materialized view so re-inserting base data does NOT fan
#      out into rollup tables (we restore those rollups from their own dumps).
#   3. For each dumped table: TRUNCATE then INSERT ... FORMAT Native.
#   4. ATTACH the materialized views back so they are live for future inserts.
#
# This reproduces the snapshot exactly: base tables AND explicit MV-target
# rollups come back to their saved state. Implicit .inner MV storage was not
# dumped (its data is derived); it repopulates from subsequent live inserts.
#
# Usage:
#   bash ops/clickhouse-backup/restore.sh --db spectrum --latest
#   bash ops/clickhouse-backup/restore.sh --db acars --from 20260613T041700Z
#   bash ops/clickhouse-backup/restore.sh --db spectrum --from /path/to/snapshot --dry-run

ENV_FILE="${CLICKHOUSE_BACKUP_ENV:-/etc/rtl-scanner/clickhouse-backup.env}"
# shellcheck disable=SC1090
[ -r "$ENV_FILE" ] && . "$ENV_FILE"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/rf-clickhouse}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[ok]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }
die()  { err "$*"; exit 1; }

DB=""; FROM=""; USE_LATEST=0; DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --db)      DB="$2"; shift 2 ;;
        --from)    FROM="$2"; shift 2 ;;
        --latest)  USE_LATEST=1; shift ;;
        --dry-run) DRY=1; shift ;;
        *) die "unknown arg: $1" ;;
    esac
done
[ -n "$DB" ] || die "--db is required"

# Resolve snapshot directory.
if [ "$USE_LATEST" -eq 1 ]; then
    SNAP="$BACKUP_DIR/$DB/latest"
elif [ -n "$FROM" ] && [ -d "$FROM" ]; then
    SNAP="$FROM"
elif [ -n "$FROM" ]; then
    SNAP="$BACKUP_DIR/$DB/$FROM"
else
    die "provide --latest or --from <timestamp|path>"
fi
[ -d "$SNAP" ] || die "snapshot not found: $SNAP"
info "restoring $DB from $(readlink -f "$SNAP")"

# Post-consolidation: one shared ClickHouse container holds every database.
# Per-DB user/pass select the database; CH_CONTAINER overrides the name.
container="${CH_CONTAINER:-clickhouse}"
user="$DB"
pvar="$(echo "$DB" | tr '[:lower:]' '[:upper:]')_PASSWORD"
pass="${!pvar:-${DB}_local}"
docker inspect -f '{{.State.Running}}' "$container" >/dev/null 2>&1 \
    || die "$container is not running. Bring the infra stack up + migrate first."
ch() { docker exec "$container" clickhouse-client --user "$user" --password "$pass" "$@"; }
ch_in() { docker exec -i "$container" clickhouse-client --user "$user" --password "$pass" "$@"; }

# Enumerate materialized views to quiesce during the restore.
mapfile -t mvs < <(ch --query "
    SELECT name FROM system.tables
    WHERE database='${DB}' AND engine LIKE '%MaterializedView%'
    ORDER BY name FORMAT TabSeparated")

if [ "$DRY" -eq 1 ]; then
    warn "DRY RUN: would detach ${#mvs[@]} MVs, then restore:"
    for f in "$SNAP"/*.native.gz; do
        [ -e "$f" ] || continue
        t="$(basename "$f" .native.gz)"
        echo "    TRUNCATE + INSERT $DB.$t  ($(du -h "$f" | cut -f1))"
    done
    exit 0
fi

info "detaching ${#mvs[@]} materialized views"
for mv in "${mvs[@]}"; do
    [ -z "$mv" ] && continue
    ch --query "DETACH TABLE ${DB}.${mv}" || warn "could not detach $mv"
done

restored=0
for f in "$SNAP"/*.native.gz; do
    [ -e "$f" ] || continue
    t="$(basename "$f" .native.gz)"
    # Skip dumped tables that are themselves MVs we just detached.
    if printf '%s\n' "${mvs[@]}" | grep -qx "$t"; then
        continue
    fi
    if ! ch --query "EXISTS TABLE ${DB}.${t}" | grep -qx 1; then
        warn "table ${DB}.${t} does not exist (run migrations first); skipping"
        continue
    fi
    ch --query "TRUNCATE TABLE ${DB}.${t}"
    gunzip -c "$f" | ch_in --query "INSERT INTO ${DB}.${t} FORMAT Native"
    info "restored ${DB}.${t}"
    restored=$((restored + 1))
done

info "reattaching materialized views"
for mv in "${mvs[@]}"; do
    [ -z "$mv" ] && continue
    ch --query "ATTACH TABLE ${DB}.${mv}" || warn "could not reattach $mv (ATTACH MATERIALIZED VIEW may be needed)"
done

info "restore complete: $restored tables into $DB"
warn "verify: docker exec $container clickhouse-client --user $user --password '***' --query \"SELECT count() FROM ${DB}.scans\" (or the relevant base table)"
