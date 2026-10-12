# ISM 433 MHz device monitoring

Receive-only decoding of 433.92 MHz ISM devices: weather stations, tire
pressure sensors, doorbells and remotes.

```
rtl_tcp -> rtl_433 -> JSON lines on stdout -> ism_ingest.py -> ClickHouse -> Grafana
```

rtl_433 and the ingest run in one container, joined by a pipe. `pipefail` in
`entrypoint.sh` makes the container exit when either side dies.

## Status

Companion pipeline. The code, schema, dashboard and parser tests exist. No live
run on the Omen is documented in the repo.

## How it runs

rtl_tcp mode. rtl_433 is an rtl_tcp client. rtl_tcp serves one client at a
time, so `pipeline.sh up ism <serial>` pauses `rtl-scanner@<serial>` for the
session and exports that dongle's `RTL_TCP_PORT`. `down` resumes the scanner.
The decoder does not take the rtl-coordinator lock.

```bash
bash infra/up.sh                       # shared ClickHouse + Grafana first
bash pipeline.sh up ism <serial>       # pauses that dongle's scanner
bash pipeline.sh down ism <serial>     # the scanner comes back
```

Dongle: 433.92 MHz is UHF, so the water-damaged V4 works as well as the V3.
readsb holds the V4 over USB while ADS-B is up, so take ADS-B down first if you
pick the V4. Picking the V3 stops the spectrum scanner for the session.

Antenna: a vertical dipole with 16.4 cm arms (7125 / 433.92 MHz), at a window.

Gain: fixed at 40 dB in `compose.overlay.yml` (`ISM_GAIN`). There is no
pipeline.sh override yet.

## Components

| Path | What it does |
|---|---|
| `compose.overlay.yml` | The single `rtl433-ingest` service |
| `Dockerfile.ingest` | `hertzg/rtl_433:24.10-alpine` plus python3 |
| `entrypoint.sh` | Runs `rtl_433 -d rtl_tcp:... -f 433.92M -M time:utc:usec -M level -F json` and pipes it into the ingest |
| `ism_ingest.py` | Maps known rtl_433 fields to typed columns, keeps the full JSON in `raw_json`, batches and inserts into `ism.events` |
| `clickhouse/migrations/` | Numbered schema, applied by `infra/migrate.py ism` from the infra bootstrap |
| `tests/` | Parser tests on synthetic rtl_433 JSON |

## Schema (`ism` database)

- `events`: one row per decoded transmission. Typed columns for model, device
  id, channel, battery, temperature, humidity, pressure, wind, rain, RSSI and
  SNR. Everything else from the 200+ rtl_433 protocols stays in `raw_json`.
  Partitioned by day, 180-day TTL.
- `hourly_stats`, `device_latest`: materialized views for counts per hour and
  the newest state per model and device id.

## Dashboard

ISM Band Overview - 433 MHz, in the ISM folder. Active devices, event rate,
unique devices today, device types, temperature and humidity readings, and
recent events.

## Tests

```bash
python3 -m pytest ism/tests           # or: bash scripts/run-tests.sh
```

They cover a weather sensor line, a line with no model (dropped), and a
non-numeric value (skipped).
