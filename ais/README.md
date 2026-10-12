# AIS ship tracking

Receive-only decoding of marine AIS from ships in Piraeus and the Saronic Gulf.

```
rtl_tcp -> AIS-catcher (dual channel) -> NMEA over UDP :10110 -> ais-ingest (ais_decoder.py) -> ClickHouse -> Grafana
```

## Status

Validated live on 2026-09-29 on the V3: 141 ships and 602 positions in about a
minute. The antenna was a 44 cm dipole pointed SW toward Piraeus. PR #35 added message
types 4, 21 and 27.

## How it runs

rtl_tcp mode. AIS-catcher is an rtl_tcp client. rtl_tcp serves one client at a
time, so `pipeline.sh up ais <serial>` pauses `rtl-scanner@<serial>` for the
session and exports that dongle's `RTL_TCP_PORT`. `down` resumes the scanner.

```bash
bash infra/up.sh                       # shared ClickHouse + Grafana first
bash pipeline.sh up ais v3-01          # pauses the V3 scanner
bash pipeline.sh down ais v3-01        # the scanner comes back
```

Dongle: marine AIS is VHF (161.975 and 162.025 MHz). The water-damaged V4 is
16.5 dB down at VHF, so AIS runs on the V3. ADS-B on the V4 keeps running.

Antenna: a vertical dipole with 44 cm arms (7125 / 162 MHz), pointed SW toward
Piraeus.

Gain: tuner AGC by default (`AIS_GAIN=auto`). The bare V3 sits next to strong
Athens FM, and a fixed 40 dB overloads its front end. Pin a number while
comparing antennas: `AIS_GAIN=30 bash pipeline.sh up ais v3-01`.

## Components

| Path | What it does |
|---|---|
| `compose.overlay.yml` | AIS-catcher v0.62 and the `ais-ingest` sidecar |
| `ais_decoder.py` | Stdlib AIVDM decoder: types 1, 2, 3 (position), 4 (base station), 5 (static and voyage), 18 (Class B position), 21 (aid to navigation), 24 (Class B static) and 27 (long-range position). Reassembles multi-sentence messages. |
| `ais_ingest.py` | Listens for NMEA datagrams on UDP 10110, decodes them, batches rows and inserts into `ais.positions` |
| `Dockerfile.ingest` | `python:3.12-slim` with the decoder and ingest |
| `clickhouse/migrations/` | Numbered schema, applied by `infra/migrate.py ais` from the infra bootstrap |
| `tests/` | Decoder tests on synthetic NMEA |

Types 9 (SAR aircraft) and 19 (extended Class B) are not decoded. A test asserts
that type 9 returns `None`, so update it if you add type 9.

## Schema (`ais` database)

- `positions`: one row per decoded message, keyed by MMSI. Dynamic fields
  (speed, course, heading, position, nav status) and static fields (name, type,
  callsign, destination, IMO, dimensions) share the table. Partitioned by day,
  90-day TTL.
- `ship_types_source`: AIS ship type codes to names.
- `zones`: four bounding boxes (Piraeus Port, Salamina Strait, Elefsina Bay,
  Saronic Open). The seed is count-guarded.
- `hourly_stats`, `ship_latest`: materialized views for counts per hour and the
  newest state per MMSI.

## Dashboard

AIS Overview - Piraeus / Saronic Gulf, in the AIS folder. Ships now, message
rate, ship types, recent ships, speed distribution and traces, a live geomap,
ships by zone, and an Integrity Anomalies table.

AIS has no authentication, so the integrity table shows receive-only spoofing
tells. It flags impossible speed, position jumps that imply more than 100 kn,
and one MMSI with conflicting names. Empty is the healthy case.

## Tests

```bash
python3 -m pytest ais/tests           # or: bash scripts/run-tests.sh
```

The expected values were cross-checked once against pyais 3.2.3 on 2026-09-28.
pyais is not a dependency, at runtime or in the tests.
