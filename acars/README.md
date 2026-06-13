# ACARS Aircraft Communications Pipeline

Decodes ACARS (Aircraft Communications Addressing and Reporting System) messages from Athens airport traffic and stores them in ClickHouse for cross-correlation with the existing ADS-B pipeline.

```
RTL-SDR V4 (rtl_tcp on leap:1235)
  └→ acarsdec (Docker, ghcr.io/sdr-enthusiasts/docker-acarsdec, @sha256-pinned)
       └→ JSON datagrams (UDP :5550) → acars-ingest → shared ClickHouse
ClickHouse (acars database, shared server clickhouse:8123 / 127.0.0.1:8123)
  ├── messages (MergeTree, partitioned by day, 90-day TTL)
  ├── hourly_stats (AggregatingMergeTree: counts, uniques, avg level)
  ├── flight_latest (ReplacingMergeTree per flight)
  ├── tail_latest (ReplacingMergeTree per tail)
  └── freq_activity (ReplacingMergeTree per (freq, dongle): classifier-feedback hook)
        └→ Grafana (:3000, ACARS folder)
             └── ACARS Overview (auto-provisioned)
```

This pipeline no longer ships its own ClickHouse or Grafana. It is a rotating V4
decoder (`acars/compose.overlay.yml`, compose project `rf_luv_acars`) that
attaches to the shared `rf_luv_net` network and writes into the always-on data
layer (`infra/`). Bring it up with `bash pipeline.sh up acars`.

## Why this pipeline

ADS-B already gives you aircraft *positions*. ACARS gives the *content* layer: free-text crew messages, OOOI events (Out-of-gate / Off-runway / On-runway / In-gate), weather requests, ATC clearances, CPDLC application data via libacars. Joining ADS-B `hex_ident`/`callsign` to ACARS `tail`/`flight` produces a richer aircraft picture than either stream alone. Per the decoding-roadmap, this is the highest-ROI Tier 1 build.

## Architecture decisions

- **Decoder image**: the airframesio acarsdec fork via the sdr-enthusiasts image, which is actively maintained and has SoapySDR + Soapy-rtltcp built-in. Building from TLeconte upstream was rejected because it doesn't speak rtl_tcp natively. The overlay `@sha256`-pins the image per the project's "never use latest" rule; the SoapySDR build the soak ran on was `4.1.6Build1494` (built 2026-04-16). If the live digest could not be resolved at consolidation time it is left as a clearly-marked TODO placeholder to resolve on the next deploy.
- **rtl_tcp via SoapySDR**: `SOAPYSDR=driver=rtltcp,rtltcp=<host>:<port>` preserves the existing leap rtl_tcp watchdog/escalator stack. acarsdec is a TCP client to rtl_tcp, identical to how the spectrum scanner connects.
- **Decoder ↔ ingest via UDP**: mirrors the AIS pipeline (AIS-catcher → ais_ingest.py). Decoder image emits JSON via `OUTPUT_SERVER_MODE=udp` to `acars-ingest:5550`. No code in the decoder image; just configuration.
- **Numbered migrations**: schema lives under `clickhouse/migrations/NNN_*.sql`, applied by `migrate.py` at ingest container startup. ISM uses a single `init.sql`; the roadmap calls for numbered migrations in new pipelines from day 1.
- **Dongle assignment**: V4 hosts ACARS, V3 stays on scanning. See `acars/env.v4-01.example` for the per-dongle env shape mirroring `ops/rtl-scanner/env.v4-01.example`.
- **Classifier feedback**: `acars.freq_activity` is the hook. Once spectrum-classifier is taught to read it (via ClickHouse `remote()`), confirmed ACARS frequencies bump confidence in `spectrum.known_frequencies` automatically.

## Quick start (leap, production)

Assumes the shared data layer is already up (`docker network create rf_luv_net`
then `bash infra/up.sh`). The infra `ch-bootstrap` one-shot has already created
the `acars` database, user, and schema, so there is no per-pipeline migration
step here.

```bash
# 1. Stop the V4 scanner (V4 becomes ACARS-dedicated)
ssh dio_nysis@192.168.2.10 \
  'systemctl --user stop rtl-scanner@v4-01.service && \
   systemctl --user disable rtl-scanner@v4-01.service'
# rtl-tcp@v4-01.service stays running; acarsdec needs it.

# 2. Deploy the decoder against the always-on infra
ssh dio_nysis@192.168.2.10
cd ~/dev/rf_luv/acars
cp env.v4-01.example .env       # then edit if needed
cd ~/dev/rf_luv && bash pipeline.sh up acars

# 3. Watch
bash pipeline.sh logs acars     # or: docker logs -f the acarsdec / acars-ingest containers
# acarsdec should print "Decoded N messages" every minute or two
# acars-ingest should print "Flushed N rows" every BATCH_SIZE messages or FLUSH_INTERVAL_SECONDS

# 4. Open Grafana
# http://192.168.2.10:3000 (admin / admin), ACARS folder
```

## Local smoke test (no radio, no leap)

Bring up the shared data layer first, then start just the ingest worker. UDP
:5550 is internal to the `rf_luv_net` network (no host port), so the simplest
way to inject a synthetic message is from inside the ingest container itself.

```bash
docker network create rf_luv_net   # once
bash infra/up.sh                   # shared ClickHouse + Grafana, creates acars db + schema
bash pipeline.sh up acars          # then stop the acarsdec container so it doesn't claim a dongle:
docker stop acarsdec 2>/dev/null || true

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
# against the shared server (container name is just "clickhouse" now):
sleep 12
docker exec clickhouse clickhouse-client --user acars --password acars_local \
  --database acars --query "SELECT freq_mhz, flight, tail, text FROM messages"

# Open Grafana: http://localhost:3000 (admin/admin), ACARS folder
# Tear down the decoder: bash pipeline.sh down acars
```

## What to watch during the first-week soak

Per the decoding-roadmap verification checklist:

1. **ClickHouse insert rate**: `Grafana → ACARS Overview → Messages per minute`. At LGAV, expect bursts during arrival/departure waves; quiet periods overnight.
2. **Error rate**: `SELECT avg(err_count) FROM acars.messages WHERE timestamp > now() - INTERVAL 1 HOUR`. If consistently > 0.3, drop `ACARS_GAIN` by 5 dB.
3. **rtl_tcp recovery**: trigger the V4 escalator manually:
   ```
   ssh dio_nysis@192.168.2.10 sudo systemctl restart rtl-tcp@v4-01.service
   ```
   acarsdec should reconnect within ~30 s. If it doesn't, the SoapyRTLTCP plugin's reconnect logic is the suspect; check `bash pipeline.sh logs acars`.
4. **Cross-correlation with ADS-B**: random-sample 5 messages, check the `tail` against `adsb.aircraft_latest` for the same time window. If acars `tail` is consistently absent from ADS-B (broader-area receivers seeing planes leap doesn't), the V4 antenna or gain may be over-reaching.
5. **Frequency activity**: the `freq_activity` table should populate with rows for 131.525, 131.725, 131.825 within the first hour. Empty rows = decoder isn't tuning correctly; check `SOAPYSDR` env var.

## Schema migrations

The `acars` schema is applied by the infra `ch-bootstrap` one-shot when the
shared data layer starts, so a fresh `bash infra/up.sh` already has the tables.
Add new schema as `clickhouse/migrations/002_*.sql` (next number); the runner is
idempotent and skips already-applied versions via `acars.schema_migrations`.

```bash
# Show migration status (against the shared server)
docker exec clickhouse clickhouse-client --user acars --password acars_local \
  --database acars --query "SELECT version, name FROM schema_migrations ORDER BY version"
```

## Ports

ACARS has no ports of its own. It uses the shared data layer:

| Service | Host port | Purpose |
|---|---|---|
| ClickHouse HTTP | 8123 | Query / ingest (shared server, `acars` database) |
| ClickHouse native | 9000 | Grafana datasource |
| Grafana | 3000 | Dashboards (ACARS folder) |

acarsdec to acars-ingest UDP traffic stays on the `rf_luv_net` network (no host port).

## Files

- `clickhouse/migrations/001_init.sql` - initial schema (messages + 4 materialized views), applied by the infra ch-bootstrap
- `migrate.py` - stdlib-only migration runner (mirrors `spectrum/migrate.py`); also used by ch-bootstrap
- `acars_ingest.py` - UDP listener, batch ClickHouse insert into the shared server
- `entrypoint.sh` - runs the ingest worker (schema is applied by ch-bootstrap)
- `Dockerfile.ingest` - minimal python:3.12-slim
- `compose.overlay.yml` - acarsdec (@sha256-pinned) + acars-ingest on the shared rf_luv_net
- `env.v4-01.example` - leap V4 deployment template

Grafana provisioning for ACARS lives under `infra/grafana/provisioning/` (ACARS
datasource + folder), not in this directory.

## Build lessons (gotchas this pipeline encodes)

These bit during the smoke test. Future pipelines copying this template inherit the fixes; if you copy ism/ instead, you'll hit them again.

1. **ClickHouse healthcheck via `wget` fails on leap.** The busybox wget in `clickhouse/clickhouse-server:24.3-alpine` resolves `localhost` to an address ClickHouse isn't bound to (`Connection refused`), even though HTTP from other containers works fine. Fix: use `clickhouse-client --query "SELECT 1"` for the healthcheck. The shared `infra/compose.yml` uses exactly this. (Historically the per-pipeline ISM compose hit this on leap too; its healthcheck only worked because ISM never ran on leap.)
2. **ClickHouse 24.3 forbids DDL via HTTP GET.** Sending `CREATE TABLE` in a URL `?query=` parameter with no body yields `Code: 164. Cannot execute query in readonly mode. For queries over HTTP, method GET implies readonly.` Fix: `migrate.py` and `acars_ingest.py` always POST; SQL goes in the body for DDL/SELECT, in the URL with payload-in-body for INSERT (mirrors `spectrum/db.py`). The `urlopen(req, data=None)` path that `ism_ingest.py` uses for DDL would fail here; ISM gets away with it because its DDL runs at clickhouse boot via `/docker-entrypoint-initdb.d/` (root-perms, different code path).
3. **Healthcheck error visibility.** `urllib` raises `HTTPError` without exposing the response body by default, so the actual ClickHouse error stays hidden. Both Python files capture and log `e.read()` on `HTTPError`, so future schema bugs surface in logs instead of leaving you guessing.
