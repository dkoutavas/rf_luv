# ADS-B aircraft tracking

Receive-only decoding of 1090 MHz ADS-B from aircraft around Athens.

```
RTL-SDR (USB) -> ultrafeeder (readsb + tar1090) -> SBS :30003 -> adsb-ingest -> ClickHouse -> Grafana
                                                -> aircraft.json -> adsb-ingest (meta poller)
```

## Status

Validated live on 2026-09-28 on the V4. Its water damage is in the VHF input,
and 1090 MHz takes the UHF path, so ADS-B still works on it. PR #36 added
aircraft metadata, airlines and coverage panels.

## How it runs

USB mode, not rtl_tcp. readsb has no rtl_tcp input, so it opens the dongle
itself. `pipeline.sh up adsb <serial>` runs `rf-mode listen <serial> --force`.
That stops `rtl-tcp@<serial>` and the scanner on it, and leaves a marker so the
freshness probe reports `LISTEN` instead of alerting. `down` runs
`rf-mode scan`, which starts both again.

```bash
bash infra/up.sh                       # shared ClickHouse + Grafana first
bash pipeline.sh up adsb v4-01         # readsb takes the V4 over USB
# map: http://localhost:8080 (tar1090, graphs1090 at /graphs1090/)
bash pipeline.sh down adsb v4-01       # rtl_tcp and the scanner come back
```

`pipeline.sh rotate` refuses to leave a USB pipe. Run `down` first.

Antenna: a vertical quarter-wave of 6.5 cm per arm (7125 / 1090 MHz), at a
window or on the patio.

Gain: readsb autogain by default. Set `ADSB_GAIN` to a number in dB to pin it,
for example while comparing antennas.

SELinux: Tumbleweed runs enforcing, and its policy blocks `container_t` from
`usb_device_t`. The ultrafeeder service sets `label=disable` for that reason.
The overlay passes the whole USB bus plus a cgroup rule for major 189. A fixed
`--device` path would break on every replug.

After a reboot, udev hands the dongle back to `rtl-tcp@` and the scanner. The
ultrafeeder restart policy is `on-failure`, so it does not fight them for the
dongle. Run `pipeline.sh up adsb` again to resume.

## Components

| Path | What it does |
|---|---|
| `compose.overlay.yml` | ultrafeeder (pinned by digest, readsb 3.16.17) and the `adsb-ingest` sidecar |
| `ingest.py` | Reads SBS BaseStation lines from `ultrafeeder:30003`, batches them, and inserts into `adsb.positions`. A daemon thread polls `aircraft.json` every 15 s and upserts `adsb.aircraft_meta`. |
| `Dockerfile.ingest` | `python:3.12-slim` with the ingest script |
| `clickhouse/migrations/` | Numbered schema, applied by `infra/migrate.py adsb` from the infra bootstrap |
| `tests/` | Parser and metadata tests with synthetic fixtures |

## Schema (`adsb` database)

- `positions`: one row per SBS `MSG` line, with whichever of position,
  altitude, speed, track, squawk and callsign that line carries. Partitioned by
  day, 90-day TTL.
- `aircraft_hourly`, `aircraft_latest`: materialized views for counts per hour
  and the newest state per ICAO address.
- `aircraft_meta` (migration 002): registration, ICAO type, description,
  emitter category and a military flag, keyed by ICAO address. It comes from
  readsb's enriched `aircraft.json`, so the aircraft database is never copied
  into the repo.
- `airlines` (migration 003): a hand-curated ICAO prefix to operator lookup
  for carriers seen at LGAV. The seed is count-guarded.

## Dashboard

ADS-B Overview - Athens, in the ADS-B folder. It shows aircraft now, message
rate, unique aircraft today, altitude distribution and traces, and recent
flights with registration and type. It also has a live geomap, airlines today
and coverage by bearing. Notable Today lists military aircraft and 7500, 7600
or 7700 squawks.

## Tests

```bash
python3 -m pytest adsb/tests          # or: bash scripts/run-tests.sh
```

They cover SBS identification, airborne position, velocity, squawk, malformed
lines, a quiet gap that must keep the connection, and metadata extraction from
a synthetic `aircraft.json`.
