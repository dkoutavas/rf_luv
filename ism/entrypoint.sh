#!/bin/sh
# Pipe rtl_433 JSON output directly into the Python ingest script.
# rtl_433 connects to rtl_tcp on Windows, decodes ISM band devices,
# and outputs one JSON line per transmission to stdout.
#
# -d rtl_tcp:...   connect to rtl_tcp server (Windows)
# -f 433.92M       ISM band center frequency
# -g 40            tuner gain (dB)
# -M time:utc:usec timestamps in UTC with microseconds
# -M level         include signal strength (rssi/snr)
# -F json          output format: JSON lines to stdout

exec rtl_433 \
    -d "rtl_tcp:${RTL_TCP_HOST:-host.docker.internal}:${RTL_TCP_PORT:-1234}" \
    -f "${ISM_FREQ:-433.92M}" \
    -g "${ISM_GAIN:-40}" \
    -M time:utc:usec \
    -M level \
    -F json \
    2>/dev/null | \
    python3 -u /app/ism_ingest.py
