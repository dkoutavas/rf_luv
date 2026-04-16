#!/bin/bash
set -eo pipefail
# Run schema migrations, then pipe spectrum scanner output to ClickHouse ingest.
# migrate.py waits for ClickHouse and applies any pending migrations.
# scanner.py connects to rtl_tcp, sweeps frequencies, outputs JSON lines.
# scan_ingest.py reads those lines and batch-inserts to ClickHouse.
# pipefail ensures we exit if scanner.py crashes (not just if ingest fails).
python3 -u /app/migrate.py
exec python3 -u /app/scanner.py | python3 -u /app/scan_ingest.py
