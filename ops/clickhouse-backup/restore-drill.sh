#!/usr/bin/env bash
set -euo pipefail
#
# restore-drill.sh: prove the latest backups restore, on a throwaway server.
#
# A backup nobody has restored is a guess. This starts a disposable ClickHouse
# container (same image as the live one, no published ports, no volume),
# builds all databases in it with the normal ch-bootstrap one-shot, restores
# every database from its latest snapshot with restore.sh, and compares each
# restored table and view rollup against the row count recorded in the
# snapshot's MANIFEST.tsv. The live server is never touched.
#
# Run it monthly, and before any ClickHouse upgrade (it is the rollback test).
# To test a candidate version before an upgrade, set DRILL_IMAGE:
#   DRILL_IMAGE=clickhouse/clickhouse-server:<tag> bash ops/clickhouse-backup/restore-drill.sh
#
# Usage:
#   bash ops/clickhouse-backup/restore-drill.sh            # all databases
#   bash ops/clickhouse-backup/restore-drill.sh spectrum   # just some
#
# Exit status: 0 when every table matches, 1 otherwise.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${CLICKHOUSE_BACKUP_ENV:-/etc/rtl-scanner/clickhouse-backup.env}"
# shellcheck disable=SC1090
[ -r "$ENV_FILE" ] && . "$ENV_FILE"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/rf-clickhouse}"
DATABASES="${*:-${DATABASES:-spectrum acars adsb ais ism noaa rds ghost}}"

# CH_ADMIN_PASSWORD for the drill server's default user, from the infra env.
# shellcheck disable=SC1091
. "$REPO/infra/.env"
: "${CH_ADMIN_PASSWORD:?infra/.env must set CH_ADMIN_PASSWORD}"

DRILL=clickhouse-drill
NET=rf_luv_net
IMAGE="${DRILL_IMAGE:-$(docker inspect -f '{{.Config.Image}}' clickhouse)}"
BOOTSTRAP_IMAGE=rf_luv_infra-ch-bootstrap

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[ok]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
die()  { echo -e "${RED}[x]${NC} $*" >&2; exit 1; }

docker image inspect "$BOOTSTRAP_IMAGE" >/dev/null 2>&1 \
    || die "$BOOTSTRAP_IMAGE image missing; run infra/up.sh once to build it"
docker inspect "$DRILL" >/dev/null 2>&1 && die "$DRILL already exists; remove it first"

cleanup() { docker rm -f "$DRILL" >/dev/null 2>&1 || true; }
trap cleanup EXIT

info "starting $DRILL ($IMAGE, no ports, no volume)"
docker run -d --rm --name "$DRILL" --network "$NET" \
    -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
    -e CLICKHOUSE_PASSWORD="$CH_ADMIN_PASSWORD" \
    "$IMAGE" >/dev/null
for _ in $(seq 60); do
    docker exec "$DRILL" clickhouse-client --password "$CH_ADMIN_PASSWORD" -q "SELECT 1" \
        >/dev/null 2>&1 && break
    sleep 2
done

info "building schema with ch-bootstrap against $DRILL"
docker run --rm --network "$NET" -v "$REPO:/repo:ro,z" \
    -e CH_ADMIN_PASSWORD="$CH_ADMIN_PASSWORD" -e CH_HOST="$DRILL" \
    -e PYTHONDONTWRITEBYTECODE=1 \
    "$BOOTSTRAP_IMAGE" /repo/infra/bootstrap.sh >/dev/null

failures=0
printf '\n%-9s %-28s %12s %12s  %s\n' "database" "table" "snapshot" "restored" "result"
for db in $DATABASES; do
    snap="$(readlink -f "$BACKUP_DIR/$db/latest" 2>/dev/null || true)"
    if [ -z "$snap" ] || [ ! -f "$snap/MANIFEST.tsv" ]; then
        warn "$db: no snapshot with a MANIFEST.tsv in $BACKUP_DIR/$db; skipped"
        continue
    fi
    CH_CONTAINER="$DRILL" bash "$REPO/ops/clickhouse-backup/restore.sh" \
        --db "$db" --from "$snap" >/dev/null
    pvar="$(echo "$db" | tr '[:lower:]' '[:upper:]')_PASSWORD"
    pass="${!pvar:-${db}_local}"
    while IFS=$'\t' read -r name expected _bytes; do
        [ -z "$name" ] && continue
        got="$(docker exec "$DRILL" clickhouse-client --user "$db" --password "$pass" \
            -q "SELECT count() FROM ${db}.${name}" 2>/dev/null || echo "error")"
        if [ "$got" = "$expected" ]; then result="PASS"; else result="FAIL"; failures=$((failures + 1)); fi
        printf '%-9s %-28s %12s %12s  %s\n' "$db" "$name" "$expected" "$got" "$result"
    done < "$snap/MANIFEST.tsv"
done

echo
if [ "$failures" -eq 0 ]; then
    info "restore drill passed: every table and view matches its snapshot"
else
    die "restore drill: $failures mismatch(es)"
fi
