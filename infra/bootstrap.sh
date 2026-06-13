#!/usr/bin/env bash
# Shared-ClickHouse bootstrap: creates per-db identities, then applies every
# pipeline's schema as its own least-privilege user. Run once by the ch-bootstrap
# service after ClickHouse is healthy. Idempotent end to end (every CREATE is
# IF NOT EXISTS, every seed is count-guarded, every migrate.py skips applied
# versions), so a restart of ch-bootstrap re-runs cleanly.
#
# The repo is bind-mounted read-only at /repo. clickhouse-client talks the native
# protocol on clickhouse:9000; the python migrators talk HTTP on clickhouse:8123.
set -euo pipefail

REPO=/repo
CH_HOST=clickhouse
CH_NATIVE_PORT=9000
CH_HTTP_PORT=8123

: "${CH_ADMIN_PASSWORD:?CH_ADMIN_PASSWORD must be set (the built-in 'default' admin password)}"

log() { echo "[bootstrap] $*"; }

# Apply a .sql file via clickhouse-client (native protocol) as a given user.
# Fail-loud: clickhouse-client returns non-zero on any statement error and
# set -e aborts the whole bootstrap.
apply_sql() {
    local user="$1" password="$2" file="$3"
    # Current database == the user's own db (user==db convention), so any
    # unqualified DDL resolves to the right place; fully-qualified statements
    # are unaffected. 'default' is used for PHASE 1 (identities) and exists
    # before any per-db database does.
    local database="${4:-$user}"
    log "apply ${file} as user '${user}' (database '${database}')"
    clickhouse-client \
        --host "$CH_HOST" --port "$CH_NATIVE_PORT" \
        --user "$user" --password "$password" \
        --database "$database" \
        --multiquery < "$file"
}

# ─── PHASE 1: identities (as the built-in 'default' admin) ──────────────────
# Must run first: the acars/noaa migrators do NOT create their own database,
# and every per-db user below must already exist before its schema is applied.
log "PHASE 1: per-db databases, users, grants"
apply_sql default "$CH_ADMIN_PASSWORD" "$REPO/infra/clickhouse/bootstrap.sql"

# ─── PHASE 2: per-pipeline schema, each as its per-db user ──────────────────
# The six pipelines are independent of one another, but each has the internal
# ordering enforced below.

# adsb: single idempotent init.sql (tables + MVs, no seeds).
log "PHASE 2a: adsb"
apply_sql adsb adsb_local "$REPO/adsb/clickhouse/init.sql"

# ism: single idempotent init.sql (tables + MVs, no seeds).
log "PHASE 2b: ism"
apply_sql ism ism_local "$REPO/ism/clickhouse/init.sql"

# ais: consolidated idempotent schema (init + ship_latest migration folded in,
# seeds count-guarded). The old init.sql / migrate_ship_latest.sql are retired.
log "PHASE 2c: ais"
apply_sql ais ais_local "$REPO/ais/clickhouse/bootstrap.sql"

# spectrum: init.sql -> Athens seed -> migrate.py, IN THAT ORDER.
#   The Athens 27-row known_frequencies catalog MUST load before migrate.py,
#   because spectrum migration 021 inserts HF rows into known_frequencies and
#   the seed file's `WHERE (SELECT count() FROM known_frequencies) = 0` guard
#   would then see a non-empty table and skip the entire catalog. Seed first,
#   migrate second.
log "PHASE 2d: spectrum (init -> athens seed -> migrate.py)"
apply_sql spectrum spectrum_local "$REPO/spectrum/clickhouse/init.sql"
apply_sql spectrum spectrum_local "$REPO/spectrum/clickhouse/seeds/known_frequencies_athens.sql"
log "spectrum migrate.py"
# migrate.py imports db.py which does `from config import config`; config.py
# reads CLICKHOUSE_HOST/PORT/DB/USER/PASSWORD and db.py talks HTTP, so point it
# at the HTTP port and run with cwd=spectrum so the local imports resolve.
( cd "$REPO/spectrum" && \
  CLICKHOUSE_HOST="$CH_HOST" \
  CLICKHOUSE_PORT="$CH_HTTP_PORT" \
  CLICKHOUSE_DB=spectrum \
  CLICKHOUSE_USER=spectrum \
  CLICKHOUSE_PASSWORD=spectrum_local \
  python3 migrate.py )

# acars: migrate.py only (it does NOT create the database; PHASE 1 did).
# migrate.py reads CLICKHOUSE_HOST/PORT/DB/USER/PASSWORD and talks HTTP.
log "PHASE 2e: acars migrate.py"
( cd "$REPO/acars" && \
  CLICKHOUSE_HOST="$CH_HOST" \
  CLICKHOUSE_PORT="$CH_HTTP_PORT" \
  CLICKHOUSE_DB=acars \
  CLICKHOUSE_USER=acars \
  CLICKHOUSE_PASSWORD=acars_local \
  python3 migrate.py )

# noaa: migrate.py only (same pattern as acars; does NOT create the database).
log "PHASE 2f: noaa migrate.py"
( cd "$REPO/noaa" && \
  CLICKHOUSE_HOST="$CH_HOST" \
  CLICKHOUSE_PORT="$CH_HTTP_PORT" \
  CLICKHOUSE_DB=noaa \
  CLICKHOUSE_USER=noaa \
  CLICKHOUSE_PASSWORD=noaa_local \
  python3 migrate.py )

log "bootstrap complete"
