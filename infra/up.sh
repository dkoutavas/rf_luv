#!/usr/bin/env bash
# Bring up the always-on data layer (compose project rf_luv_infra) and block
# until the one-shot ch-bootstrap has finished creating identities + schema.
#
# Run from anywhere; paths are resolved relative to this script. Pipelines are
# brought up separately with the top-level pipeline.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Run from the infra dir so Compose auto-loads infra/.env (CH_ADMIN_PASSWORD)
# from the working directory and resolves the relative bind-mount paths.
cd "$SCRIPT_DIR"
COMPOSE_FILE="compose.yml"
PROJECT=rf_luv_infra
NET=rf_luv_net

# The external network is shared by infra + every pipeline project. Create it
# if absent; ignore the "already exists" case.
if ! docker network inspect "$NET" >/dev/null 2>&1; then
    echo "[up] creating docker network $NET"
    docker network create "$NET"
else
    echo "[up] docker network $NET already exists"
fi

echo "[up] starting $PROJECT"
docker compose -p "$PROJECT" -f "$COMPOSE_FILE" up -d

# Block on the bootstrap one-shot. `docker wait` returns the container's exit
# code; non-zero means schema creation failed and the data layer is not ready.
echo "[up] waiting for ch-bootstrap to finish"
BOOTSTRAP_RC="$(docker wait ch-bootstrap)"
if [ "$BOOTSTRAP_RC" != "0" ]; then
    echo "[up] ERROR: ch-bootstrap exited $BOOTSTRAP_RC; dumping its logs:" >&2
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" logs ch-bootstrap >&2 || true
    exit 1
fi

echo "[up] ch-bootstrap completed cleanly"
echo
echo "rf_luv data layer is up:"
echo "  ClickHouse HTTP    : http://127.0.0.1:8123  (per-db users <db>/<db>_local)"
echo "  ClickHouse native  : 127.0.0.1:9000"
echo "  Grafana            : http://127.0.0.1:3000"
echo "  Logging form       : http://127.0.0.1:8084"
echo
echo "Bring a rotating pipeline up with: ./pipeline.sh up <acars|adsb|ais|ism|spectrum>"
