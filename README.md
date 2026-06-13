# rf_luv: RTL-SDR Radio Lab

Personal RTL-SDR Blog V3 exploration project, based in Athens, Greece. The repo started as a general-purpose SDR playground with pipelines for aircraft (ADS-B), ships (AIS), and ISM devices; it has since converged on the **spectrum scanner** as the primary, continuously-tested workload, with **ACARS** (aircraft messaging) as a second deployed decoder and **NOAA** weather-sat scheduling partially built. The ADS-B/AIS/ISM stacks are kept as companion experiments. Each pipeline is documented in its own directory.

> **Status (2026-06):** the production host `leap` is down with a failing disk; a new SSD in the same machine is the recovery plan. Code, schema, dashboards, and systemd units rebuild from this repo. ClickHouse *data* did not have a backup, which is now addressed by [`ops/clickhouse-backup/`](ops/clickhouse-backup/). The full bare-metal rebuild path is in [`RESTORE.md`](RESTORE.md), and the "Current Project State" section of [`CLAUDE.md`](CLAUDE.md) has the full picture.

## Quick start

The stack has two halves: `rtl_tcp` on the host that owns the USB dongle, and a Docker data layer (one shared ClickHouse + Grafana) plus the decoder you want, anywhere that can reach the dongle over TCP.

1. **Clone + bootstrap** (one-time): `bash bootstrap.sh`: strips WSL metadata, marks scripts executable, inits git.
2. **Host side: rtl_tcp**:
   - Linux: `bash ops/rtl-tcp/install.sh`: installs systemd user unit + 30 s watchdog.
   - Windows: follow [`setup/install-windows.md`](setup/install-windows.md) (Zadig, WinUSB, run `rtl_tcp.exe -a 0.0.0.0 -p 1234 -s 2048000`).
3. **Shared data layer** (once): `docker network create rf_luv_net` then `bash infra/up.sh`. This starts the single ClickHouse (8123/9000) and Grafana (3000), and the `ch-bootstrap` one-shot creates all six databases, users, and schema and loads the Athens known-frequencies seed automatically.
4. **Decoders**: the native systemd spectrum scanner on V3 writes straight into the shared ClickHouse (no compose needed). Rotating V4 decoders are managed with `bash pipeline.sh up|down|rotate <pipe>` where `<pipe>` is `adsb`, `ais`, `ism`, or `acars`.
5. **Dashboards**: <http://localhost:3000> (admin/admin); each pipeline has its own Grafana folder. First full spectrum sweep completes in ~4 minutes; airband sweeps every 60 s.
6. **Antenna**: stock dipole, arms sized for the band of interest (see table below), vertical, outdoors if possible.

If you're on WSL/openSUSE and want the local CLI toolchain (`rtl_433`, `multimon-ng`, `gpredict`, etc.): `bash setup/install-wsl.sh`. Not required for the Docker pipeline, only for ad-hoc CLI experiments.

## Architecture

```
┌─────────────┐   USB    ┌──────────────────┐   TCP   ┌──────────────────┐   pipe   ┌──────────────────┐   HTTP   ┌──────────────┐
│ RTL-SDR V3  │ ───────▶ │ rtl_tcp (host)   │ ──────▶ │ scanner.py       │ ──────▶ │ scan_ingest.py    │ ───────▶ │ ClickHouse   │
│ (dongle)    │          │ systemd+watchdog │  :1234  │ (FFT, detection) │   JSON  │ (batch inserter)  │          │              │
└─────────────┘          └──────────────────┘         └──────────────────┘         └──────────────────┘          └──────┬───────┘
                                                                                                                          │
                                                                                                                   Grafana :3000
```

The RTL-SDR is a USB device, so `rtl_tcp` runs on the host that physically owns the dongle and streams IQ samples over TCP. ClickHouse and Grafana are a single shared data layer (compose project `rf_luv_infra`); the scanner, ingest, and rotating decoders attach to the same `rf_luv_net` network and write into the one ClickHouse. Decoders reach `rtl_tcp` via `host.docker.internal` (V4 :1235 by default; the native spectrum scanner uses the V3 :1234). The dongle is single-client, so a given dongle is held by one consumer at a time.

Production host: `leap` (192.168.2.10, openSUSE Leap 15.6), where `rtl_tcp` is wrapped by a layered reliability stack: the user-level **watchdog** in `ops/rtl-tcp/` handles the per-process "RTL0 greeting but zero samples" failure mode (30 s probe, soft restart → USB unbind/rebind, circuit-breaker at fail #10). For unattended operation a root-level **escalator** in `ops/rtl-tcp/rtl-tcp-escalator.py` picks up after the circuit breaker: running the full per-device + xHCI bounce + restart sequence proven on V4, then `systemctl reboot` as a last resort. Two ClickHouse-level probes in `ops/spectrum-monitor/` cover failure modes the per-process watchdog can't see: a **freshness probe** watches `spectrum.scans` to catch downstream stalls (Docker, ingest, ClickHouse itself), and a **signal-quality probe** watches `spectrum.sweep_health.max_power` to catch RF-path failures where data flows but the scanner has gone deaf (antenna disconnect, loose connector, broken filter). Push alerts go via [ntfy.sh](https://ntfy.sh) using the helper in `ops/notify/`; a daily heartbeat confirms the alert pipe is alive. Install with `bash ops/install-trip-hardening.sh`.

## Pipelines

All pipelines share one ClickHouse (`127.0.0.1:8123` HTTP, `:9000` native) and one Grafana (`:3000`). Each pipeline gets its own ClickHouse **database** and its own Grafana **folder** rather than its own server. The old per-pipeline ports (8124-8128, 9001-9005, 3001-3005) are retired.

| Pipeline   | Status | Database | Grafana folder | Description |
|------------|--------|----------|----------------|-------------|
| `spectrum/`| **active**, primary | `spectrum` | Spectrum (default DS) | Wideband 88-470 MHz scanner, signal classifier, anomaly detection, baseline |
| `acars/`   | deployed, soak interrupted | `acars` | ACARS | ACARS aircraft messaging on V4. Soak started 2026-05-02, redeploys fresh (see [`acars/DEPLOY.md`](acars/DEPLOY.md)) |
| `noaa/`    | partial | `noaa` | NOAA | NOAA/Meteor pass scheduling + schema. Recorder is a scaffold; capture not implemented yet |
| `adsb/`    | companion | `adsb` | ADS-B | ADS-B aircraft tracking (readsb + tar1090 :8080). Historically ran on the Windows host, not leap |
| `ais/`     | companion | `ais` | AIS | AIS ship tracking (AIS-catcher). Built, not run on leap |
| `ism/`     | companion | `ism` | ISM | ISM 433 MHz device decoding (rtl_433). Built, not run on leap |

ClickHouse data for these databases is backed up off-host by [`ops/clickhouse-backup/`](ops/clickhouse-backup/) (daily logical snapshots). Deploy it and point `BACKUP_DIR` at off-host storage before collecting data you care about.

## Custom Python

The spectrum stack is intentionally dependency-light, numpy for DSP, stdlib for everything else, so each file is small and readable.

- **[`spectrum/scanner.py`](spectrum/scanner.py)**: custom `rtl_tcp` FFT client. `rtl_power`, the usual tool for this job, only speaks direct USB and was replaced. Implements: multi-preset scheduler (full 88–470 MHz every ~5 min, airband 118–137 MHz every 60 s, picked by "most overdue"); Hann-windowed FFT with linear-domain averaging (N=8) to avoid dB-averaging bias on bursty signals; peak detection (prominence vs. ±5 neighbor bins); transient detection (Δ≥15 dB vs. previous full sweep); adaptive gain with floor on ADC clipping; per-sweep health metadata (clipping, duration, worst-case bin).
- **[`spectrum/scan_ingest.py`](spectrum/scan_ingest.py)**: stdlib-only ClickHouse batch inserter. Reads JSON lines from the scanner, routes by message type (`bin` / `peak` / `event` / `health` / `run_start` / `run_update` / `run_end`) into the matching table, flushes on size or interval.
- **[`spectrum/migrate.py`](spectrum/migrate.py)**: numbered-SQL migration runner, applied at container start before the scanner pipe opens. Migrations live in `spectrum/clickhouse/migrations/`.
- **[`ops/rtl-tcp/rtl-tcp-watchdog.py`](ops/rtl-tcp/rtl-tcp-watchdog.py)**: 30 s systemd-timer watchdog. Connects to `rtl_tcp`, verifies the RTL0 greeting *and* ≥512 KB of actual IQ samples within 2 s. Escalation: restart the user unit → unbind/rebind the USB device. Stops at fail #10 (circuit breaker) so a stuck dongle can't trigger a USB-reset storm.
- **[`ops/rtl-tcp/rtl-tcp-escalator.py`](ops/rtl-tcp/rtl-tcp-escalator.py)**: root-level 5 min systemd timer that picks up where the watchdog stops. After CB-open ≥10 min on a serial it runs the proven manual recipe (per-device USB reset → xHCI controller bounce → restart user unit). After 3 unsuccessful unwedges in 24 h, or both serials CB-open ≥30 min, triggers `systemctl reboot` (rate-limited 1/6 h).
- **[`ops/spectrum-monitor/freshness-probe.py`](ops/spectrum-monitor/freshness-probe.py)**: 5 min ClickHouse-level liveness check. Catches scanner / Docker / ClickHouse / ingest failures the per-process watchdog can't see. WARN >10 min stale, CRITICAL >25 min stale, per `dongle_id`.
- **[`ops/spectrum-monitor/signal-quality-probe.py`](ops/spectrum-monitor/signal-quality-probe.py)**: 5 min RF-level liveness check. Catches "deaf scanner" failures: data still flows but every sweep reports near-noise-floor power (antenna disconnect, loose F-connector, broken filter). WARN max_power < -35 dBFS, CRITICAL < -40 dBFS over 30 min, per `dongle_id`.
- **[`ops/notify/notify.py`](ops/notify/notify.py)**: stdlib ntfy.sh push helper. Used by the escalator, freshness probe, signal-quality probe, and a daily heartbeat to reach a phone via [ntfy.sh](https://ntfy.sh). Topic configured in `/etc/rtl-scanner/notify.env`.

## Running the spectrum pipeline

1. Host-side rtl_tcp: install the reliability stack with `bash ops/rtl-tcp/install.sh` (Linux host) or, on Windows, run `rtl_tcp.exe -a 0.0.0.0 -p 1234 -s 2048000`.
2. Data layer (once): `docker network create rf_luv_net` then `bash infra/up.sh` (shared ClickHouse + Grafana, schema + seed auto-applied).
3. Scanner: the production host runs `scanner.py` natively under systemd against the shared ClickHouse on `127.0.0.1:8123`. To run it in a container instead, use the spectrum overlay (see [`spectrum/README.md`](spectrum/README.md)).
4. Dashboards: <http://localhost:3000> (admin/admin), Spectrum folder.

Full per-platform setup, environment variables, and troubleshooting live in [`spectrum/README.md`](spectrum/README.md). Windows driver swap (Zadig) and SDR++ first-boot are in [`setup/install-windows.md`](setup/install-windows.md).

## Dashboards & operator tools

Grafana at `:3000` (Spectrum folder) ships with auto-provisioned dashboards: current power spectrum, known-frequency traces, detected peaks, transient events, airband activity, anomaly detection vs. hourly baseline, and a **Listening Playbook** dashboard with an embedded HTML form (served by the shared `logging-form` nginx on `:8084`) that writes operator notes directly into `spectrum.listening_log`.

Two helper scripts query ClickHouse over HTTP:
- `spectrum/export-data.sh`: export scan data to CSV/markdown reports (see `spectrum/exports/`).
- `spectrum/investigate-freqs.sh`: generate an investigation checklist from recently detected peaks.

## Repo layout

```
infra/             # shared data layer: one ClickHouse + Grafana + logging-form + ch-bootstrap
  compose.yml        # compose project rf_luv_infra (8123/9000/3000/8084)
  up.sh              # bring up the data layer (creates rf_luv_net if absent)
  bootstrap.sh       # creates 6 dbs/users + grants, applies schema + Athens seed
  grafana/           # 6 datasources + 6 folders, one per pipeline
pipeline.sh        # bring rotating V4 decoders up/down/rotate: pipeline.sh <action> <pipe>
spectrum/          # primary pipeline: scanner, ingest, intelligence, migrations
  scanner.py         # rtl_tcp FFT client (custom, replaces rtl_power)
  scan_ingest.py     # JSON to ClickHouse batch inserter
  coordinator.py     # flock dongle lock (wired into scanner.py)
  classifier.py      # signal classifier (systemd timer, every 5 min)
  feature_extractor.py  # duty/burst/harmonic features feeding the classifier
  classifier_health.py  # classifier regression sentinel
  acars_feedback.py  # bridges acars.messages into spectrum.listening_log over HTTP
  migrate.py         # numbered-SQL migration runner
  clickhouse/        # init.sql + migrations/ + seeds/ (applied by infra ch-bootstrap)
  compose.overlay.yml   # optional containerized scanner (native systemd is the norm)
  logging/           # operator listening-log HTML form (served by infra logging-form :8084)
  docs/              # session handoffs, briefings, analysis reports
acars/             # ACARS aircraft messaging (deployed on V4; see DEPLOY.md)
noaa/              # NOAA/Meteor pass scheduling (recorder is a scaffold)
adsb/ ais/ ism/    # companion pipelines (each a compose.overlay.yml decoder)
ops/rtl-tcp/       # host-side rtl_tcp reliability: systemd unit, watchdog, escalator, USB reset
ops/rtl-coordinator/   # installs the flock dongle coordinator
ops/spectrum-monitor/  # ClickHouse freshness + signal-quality probes
ops/spectrum-classifier/ ops/spectrum-features/ ops/spectrum-classifier-health/  # intelligence timers
ops/spectrum-acars-feedback/  # hourly ACARS-to-classifier feedback timer
ops/noaa-pass-scheduler/  # NOAA scheduler + TLE refresh timers
ops/clickhouse-backup/    # daily off-host logical ClickHouse backups (deploy this)
ops/notify/        # ntfy.sh push helper + daily heartbeat
ops/remote-desktop/  # xrdp + tailscale remote-access setup
ops/install-trip-hardening.sh  # idempotent installer for the unattended-ops layer
scripts/           # one-shot CLI helpers (airband, ISM, AIS, satellite passes)
setup/             # WSL installer and Windows setup guide
notes/             # signal identification logs
recordings/        # IQ captures, scan CSVs, decoded images
CLAUDE.md          # full project context (hardware, RF environment, conventions, state)
RESTORE.md         # bare-metal rebuild runbook (clone, install order, data restore)
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
