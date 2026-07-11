#!/bin/bash
set -eo pipefail
# Apply schema migrations, then run the reader piped into the ingest worker.
# pipefail ensures we exit if the reader crashes (not just if ingest fails),
# mirroring ism's rtl_433 pipe: reader exit tears down the container.
#
# migrate.py is idempotent (skips applied versions) and does NOT create the
# database -- the infra ch-bootstrap PHASE 1 already did (same contract as
# acars/noaa). Running it here too keeps the containerized pipeline
# self-sufficient on hosts where ch-bootstrap has not been run.
#
# rds_reader.py connects to rtl_tcp on the V4 (:1234), tunes RDS_FREQ_HZ at
# 228000 S/s, runs the numpy DSP chain, and prints one JSON line per decoded
# RDS group to stdout. rds_ingest.py batches those into ClickHouse.

python3 -u /app/migrate.py

exec python3 -u /app/rds_reader.py | \
    python3 -u /app/rds_ingest.py
