#!/bin/bash
set -eo pipefail
# ACARS ingest entrypoint:
#   1. wait for ClickHouse + apply numbered migrations (idempotent)
#   2. exec the UDP listener
#
# acarsdec runs in a separate container (ghcr.io/sdr-enthusiasts/docker-acarsdec)
# and ships JSON datagrams here over UDP. This container only owns ingest.
# Decoder/ingest split mirrors the AIS pipeline (AIS-catcher → ais_ingest.py).

python3 -u /app/migrate.py
exec python3 -u /app/acars_ingest.py
