# rf_luv — RTL-SDR Radio Lab

Personal RTL-SDR Blog V3 exploration project, based in Athens, Greece. The repo started as a general-purpose SDR playground with pipelines for aircraft (ADS-B), ships (AIS), and ISM devices; it has since converged on the **spectrum scanner** as the primary, continuously-tested workload. The other pipelines are kept as companion experiments and are documented in their own directories.

## Architecture

```
┌─────────────┐   USB    ┌──────────────────┐   TCP   ┌──────────────────┐   pipe   ┌──────────────────┐   HTTP   ┌──────────────┐
│ RTL-SDR V3  │ ───────▶ │ rtl_tcp (host)   │ ──────▶ │ scanner.py       │ ──────▶ │ scan_ingest.py    │ ───────▶ │ ClickHouse   │
│ (dongle)    │          │ systemd+watchdog │  :1234  │ (FFT, detection) │   JSON  │ (batch inserter)  │          │              │
└─────────────┘          └──────────────────┘         └──────────────────┘         └──────────────────┘          └──────┬───────┘
                                                                                                                          │
                                                                                                                   Grafana :3003
```

The RTL-SDR is a USB device, so `rtl_tcp` runs on the host that physically owns the dongle and streams IQ samples over TCP. All downstream containers — scanner, ingest, ClickHouse, Grafana — run in Docker and reach `rtl_tcp` via `host.docker.internal`. The same bridge is reused by every pipeline, but **only one pipeline can hold the dongle at a time** (single-client).

Production host: `leap` (192.168.2.10, openSUSE Leap 15.6), where `rtl_tcp` is wrapped by the reliability stack in `ops/rtl-tcp/` — a systemd user unit plus a 30-second watchdog timer that restarts the service and, as escalation, soft-replugs the USB device when the dongle enters its "RTL0 greeting but zero samples" failure mode.

## Pipelines

| Pipeline   | Status    | Grafana | ClickHouse | Description                                              |
|------------|-----------|---------|------------|----------------------------------------------------------|
| `spectrum/`| **active**| :3003   | :8126/:9003| Wideband 88–470 MHz scanner, anomaly detection, baseline |
| `adsb/`    | companion | :3000   | :8123/:9000| ADS-B aircraft tracking (readsb + tar1090 :8080)         |
| `ais/`     | companion | :3001   | :8124/:9001| AIS ship tracking (AIS-catcher)                          |
| `ism/`     | companion | :3002   | :8125/:9002| ISM 433 MHz device decoding (rtl_433)                    |

## Custom Python

The spectrum stack is intentionally dependency-light — numpy for DSP, stdlib for everything else — so each file is small and readable.

- **[`spectrum/scanner.py`](spectrum/scanner.py)** — custom `rtl_tcp` FFT client. `rtl_power`, the usual tool for this job, only speaks direct USB and was replaced. Implements: multi-preset scheduler (full 88–470 MHz every ~5 min, airband 118–137 MHz every 60 s, picked by "most overdue"); Hann-windowed FFT with linear-domain averaging (N=8) to avoid dB-averaging bias on bursty signals; peak detection (prominence vs. ±5 neighbor bins); transient detection (Δ≥15 dB vs. previous full sweep); adaptive gain with floor on ADC clipping; per-sweep health metadata (clipping, duration, worst-case bin).
- **[`spectrum/scan_ingest.py`](spectrum/scan_ingest.py)** — stdlib-only ClickHouse batch inserter. Reads JSON lines from the scanner, routes by message type (`bin` / `peak` / `event` / `health` / `run_start` / `run_update` / `run_end`) into the matching table, flushes on size or interval.
- **[`spectrum/migrate.py`](spectrum/migrate.py)** — numbered-SQL migration runner, applied at container start before the scanner pipe opens. Migrations live in `spectrum/clickhouse/migrations/`.
- **[`ops/rtl-tcp/rtl-tcp-watchdog.py`](ops/rtl-tcp/rtl-tcp-watchdog.py)** — 30 s systemd-timer watchdog. Connects to `rtl_tcp`, verifies the RTL0 greeting *and* ≥512 KB of actual IQ samples within 2 s. Escalation: restart the user unit → unbind/rebind the USB device.

## Running the spectrum pipeline

1. Host-side rtl_tcp: install the reliability stack with `bash ops/rtl-tcp/install.sh` (Linux host) or, on Windows, run `rtl_tcp.exe -a 0.0.0.0 -p 1234 -s 2048000`.
2. Pipeline: `cd spectrum && docker compose up -d`.
3. Dashboards: <http://localhost:3003> (admin/admin).

Full per-platform setup, environment variables, and troubleshooting live in [`spectrum/README.md`](spectrum/README.md). Windows driver swap (Zadig) and SDR++ first-boot are in [`setup/install-windows.md`](setup/install-windows.md).

## Dashboards & operator tools

Grafana at `:3003` ships with auto-provisioned dashboards: current power spectrum, known-frequency traces, detected peaks, transient events, airband activity, anomaly detection vs. hourly baseline, and a **Listening Playbook** dashboard with an embedded HTML form (served from `spectrum/logging/` via nginx on `:8084`) that writes operator notes directly into `spectrum.listening_log`.

Two helper scripts query ClickHouse over HTTP:
- `spectrum/export-data.sh` — export scan data to CSV/markdown reports (see `spectrum/exports/`).
- `spectrum/investigate-freqs.sh` — generate an investigation checklist from recently detected peaks.

## Repo layout

```
spectrum/          # primary pipeline — scanner, ingest, migrations, ClickHouse schema, Grafana
  scanner.py         # rtl_tcp FFT client (custom, replaces rtl_power)
  scan_ingest.py     # JSON → ClickHouse batch inserter
  migrate.py         # numbered-SQL migration runner
  clickhouse/        # init.sql + migrations/
  grafana/           # provisioned datasources and dashboards
  logging/           # operator listening-log HTML form (served on :8084)
ops/rtl-tcp/       # systemd unit, watchdog, USB reset — host-side rtl_tcp reliability
adsb/ ais/ ism/    # companion pipelines (see each directory's docker-compose.yml)
scripts/           # one-shot CLI helpers (airband, ISM, AIS, satellite passes)
setup/             # WSL installer and Windows setup guide
notes/             # signal identification logs
recordings/        # IQ captures, scan CSVs, decoded images
CLAUDE.md          # full project context (hardware, RF environment, conventions)
QUICKREF.md        # live-operation cheat sheet
```

## Antenna quick reference

Arm length formula: **arm_cm = 7125 / freq_MHz** (quarter wavelength per dipole arm).

| Target          | Frequency  | Dipole arm | Notes                         |
|-----------------|------------|------------|-------------------------------|
| FM Radio        | ~100 MHz   | 75 cm      | Indoor, vertical              |
| NOAA Satellites | ~137 MHz   | 53 cm      | Patio, V-dipole 120°          |
| AIS Ships       | ~162 MHz   | 45 cm      | Window toward Piraeus         |
| ADS-B Planes    | 1090 MHz   | 6.5 cm     | Window/patio, vertical        |
| HF / Shortwave  | 3–30 MHz   | long wire  | 10–20 m wire, direct sampling |

Current spectrum-scanner deployment: stock dipole on rooftop tripod, 57 cm arms, gain floor 2 dB with adaptive reduction on clipping.
