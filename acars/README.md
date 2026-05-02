# ACARS Aircraft Communications Pipeline

Decodes ACARS (Aircraft Communications Addressing and Reporting System) messages from Athens airport traffic and stores them in ClickHouse for cross-correlation with the existing ADS-B pipeline.

```
RTL-SDR V4 (rtl_tcp on leap:1235)
  └→ acarsdec (Docker, ghcr.io/sdr-enthusiasts/docker-acarsdec)
       └→ JSON datagrams (UDP :5550) → acars-ingest → ClickHouse
ClickHouse (acars database)
  ├── messages (MergeTree, partitioned by day, 90-day TTL)
  ├── hourly_stats (AggregatingMergeTree — counts, uniques, avg level)
  ├── flight_latest (ReplacingMergeTree per flight)
  ├── tail_latest (ReplacingMergeTree per tail)
  └── freq_activity (ReplacingMergeTree per (freq, dongle) — classifier-feedback hook)
        └→ Grafana (:3004)
             └── ACARS Overview (auto-provisioned)
```

## Why this pipeline

ADS-B already gives you aircraft *positions*. ACARS gives the *content* layer: free-text crew messages, OOOI events (Out-of-gate / Off-runway / On-runway / In-gate), weather requests, ATC clearances, CPDLC application data via libacars. Joining ADS-B `hex_ident`/`callsign` to ACARS `tail`/`flight` produces a richer aircraft picture than either stream alone. Per the decoding-roadmap, this is the highest-ROI Tier 1 build.

## Architecture decisions

- **Decoder image**: `ghcr.io/sdr-enthusiasts/docker-acarsdec:4.1.6Build1494` (built 2026-04-16). Uses the airframesio acarsdec fork, which is actively maintained and has SoapySDR + Soapy-rtltcp built-in. Building from TLeconte upstream was rejected because it doesn't speak rtl_tcp natively.
- **rtl_tcp via SoapySDR**: `SOAPYSDR=driver=rtltcp,rtltcp=<host>:<port>` — preserves the existing leap rtl_tcp watchdog/escalator stack. acarsdec is a TCP client to rtl_tcp, identical to how the spectrum scanner connects.
- **Decoder ↔ ingest via UDP**: mirrors the AIS pipeline (AIS-catcher → ais_ingest.py). Decoder image emits JSON via `OUTPUT_SERVER_MODE=udp` to `acars-ingest:5550`. No code in the decoder image; just configuration.
- **Numbered migrations**: schema lives under `clickhouse/migrations/NNN_*.sql`, applied by `migrate.py` at ingest container startup. ISM uses a single `init.sql`; the roadmap calls for numbered migrations in new pipelines from day 1.
- **Dongle assignment**: V4 hosts ACARS, V3 stays on scanning. See `acars/env.v4-01.example` for the per-dongle env shape mirroring `ops/rtl-scanner/env.v4-01.example`.
- **Classifier feedback**: `acars.freq_activity` is the hook. Once spectrum-classifier is taught to read it (via ClickHouse `remote()`), confirmed ACARS frequencies bump confidence in `spectrum.known_frequencies` automatically.

## Quick start (leap, production)

```bash
# 1. Stop the V4 scanner (V4 becomes ACARS-dedicated)
ssh dio_nysis@192.168.2.10 \
  'systemctl --user stop rtl-scanner@v4-01.service && \
   systemctl --user disable rtl-scanner@v4-01.service'
# rtl-tcp@v4-01.service stays running — acarsdec needs it.

# 2. Deploy
ssh dio_nysis@192.168.2.10
cd ~/dev/rf_luv/acars
cp env.v4-01.example .env       # then edit if needed
docker compose up -d --build

# 3. Watch
docker compose logs -f acarsdec acars-ingest
# acarsdec should print "Decoded N messages" every minute or two
# acars-ingest should print "Flushed N rows" every BATCH_SIZE messages or FLUSH_INTERVAL_SECONDS

# 4. Open Grafana
# http://192.168.2.10:3004 (admin / admin)
```

## Local smoke test (no radio, no leap)

UDP :5550 is internal to the compose network (no host port), so the simplest
way to inject a synthetic message is from inside the ingest container itself.

```bash
cd acars/
docker compose up -d clickhouse acars-ingest grafana
# (skip acarsdec — it would try to claim a dongle)

# Synthesize a downlink ACARS message and send it to the ingest UDP listener
docker exec acars-ingest python3 -c '
import socket, json, time
msg = {"timestamp": time.time(), "freq": "131.525", "level": "-25.5",
       "label": "H1", "tail": "SX-DGT", "flight": "AEE123",
       "text": "SMOKE TEST", "depa": "LGAV", "dsta": "LFPG", "end": 1}
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(json.dumps(msg).encode(), ("127.0.0.1", 5550))
'

# Wait for batch flush (FLUSH_INTERVAL_SECONDS, default 10s), then verify
sleep 12
docker exec clickhouse-acars clickhouse-client --user acars --password acars_local \
  --database acars --query "SELECT freq_mhz, flight, tail, text FROM messages"

# Open Grafana — http://localhost:3004 (admin/admin)
# Tear down: docker compose down -v
```

## What to watch during the first-week soak

Per the decoding-roadmap verification checklist:

1. **ClickHouse insert rate**: `Grafana → ACARS Overview → Messages per minute`. At LGAV, expect bursts during arrival/departure waves; quiet periods overnight.
2. **Error rate**: `SELECT avg(err_count) FROM acars.messages WHERE timestamp > now() - INTERVAL 1 HOUR`. If consistently > 0.3, drop `ACARS_GAIN` by 5 dB.
3. **rtl_tcp recovery**: trigger the V4 escalator manually:
   ```
   ssh dio_nysis@192.168.2.10 sudo systemctl restart rtl-tcp@v4-01.service
   ```
   acarsdec should reconnect within ~30 s. If it doesn't, the SoapyRTLTCP plugin's reconnect logic is the suspect — check `docker compose logs acarsdec`.
4. **Cross-correlation with ADS-B**: random-sample 5 messages, check the `tail` against `adsb.aircraft_latest` for the same time window. If acars `tail` is consistently absent from ADS-B (broader-area receivers seeing planes leap doesn't), the V4 antenna or gain may be over-reaching.
5. **Frequency activity**: the `freq_activity` table should populate with rows for 131.525, 131.725, 131.825 within the first hour. Empty rows = decoder isn't tuning correctly; check `SOAPYSDR` env var.

## Schema migrations

Add new schema as `clickhouse/migrations/002_*.sql` (next number). The runner is idempotent and skip-already-applied via `acars.schema_migrations`.

```bash
# Show migration status
docker compose exec acars-ingest python3 /app/migrate.py --status

# Dry-run pending migrations
docker compose exec acars-ingest python3 /app/migrate.py --dry-run
```

## Ports

| Service | Host port | Container port | Purpose |
|---|---|---|---|
| ClickHouse HTTP | 8127 | 8123 | Query / ingest |
| ClickHouse native | 9004 | 9000 | Grafana datasource |
| Grafana | 3004 | 3000 | Dashboards |

acarsdec → acars-ingest UDP traffic stays on the compose network (no host port).

## Files

- `clickhouse/migrations/001_init.sql` — initial schema (messages + 4 materialized views)
- `migrate.py` — stdlib-only migration runner (mirrors `spectrum/migrate.py`)
- `acars_ingest.py` — UDP listener, batch ClickHouse insert
- `entrypoint.sh` — runs migrations then ingest worker
- `Dockerfile.ingest` — minimal python:3.12-slim
- `docker-compose.yml` — 4 services on ports 8127/9004/3004
- `grafana/provisioning/` — datasource + dashboards auto-provisioning
- `env.v4-01.example` — leap V4 deployment template

## Build lessons (gotchas this pipeline encodes)

These bit during the smoke test. Future pipelines copying this template inherit the fixes; if you copy ism/ instead, you'll hit them again.

1. **ClickHouse healthcheck via `wget` fails on leap.** The busybox wget in `clickhouse/clickhouse-server:24.3-alpine` resolves `localhost` to an address ClickHouse isn't bound to (`Connection refused`), even though HTTP from other containers works fine. Fix: use `clickhouse-client --query "SELECT 1"` for the healthcheck, same as `spectrum/docker-compose.yml`. ISM hits this on leap too — its current healthcheck only works because ISM doesn't run on leap.
2. **ClickHouse 24.3 forbids DDL via HTTP GET.** Sending `CREATE TABLE` in a URL `?query=` parameter with no body yields `Code: 164. Cannot execute query in readonly mode. For queries over HTTP, method GET implies readonly.` Fix: `migrate.py` and `acars_ingest.py` always POST — SQL goes in the body for DDL/SELECT, in the URL with payload-in-body for INSERT (mirrors `spectrum/db.py`). The `urlopen(req, data=None)` path that `ism_ingest.py` uses for DDL would fail here; ISM gets away with it because its DDL runs at clickhouse boot via `/docker-entrypoint-initdb.d/` (root-perms, different code path).
3. **Healthcheck error visibility.** `urllib` raises `HTTPError` without exposing the response body by default — the actual ClickHouse error stays hidden. Both Python files capture and log `e.read()` on `HTTPError`, so future schema bugs surface in logs instead of leaving you guessing.
