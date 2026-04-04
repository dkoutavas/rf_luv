#!/bin/sh
# Pipe spectrum scanner output to the ClickHouse ingest script.
# scanner.py connects to rtl_tcp, sweeps frequencies, outputs JSON lines.
# scan_ingest.py reads those lines and batch-inserts to ClickHouse.
exec python3 -u /app/scanner.py | python3 -u /app/scan_ingest.py
