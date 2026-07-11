# rf_luv: RTL-SDR Radio Lab

## What This Is

Personal radio exploration project using an RTL-SDR Blog V4 dongle. Based in Athens, Greece (Polygono neighborhood, elevated central Athens). Projects range from spectrum analysis to aircraft tracking to number station hunting.

This file is the Claude Code project prompt. It contains everything needed to assist with any task in this project.

---

## Current Project State (2026-07)

> Status snapshot so this prompt reflects reality, not aspiration. Update it when state changes.

**Platform:** the target is one **local openSUSE Tumbleweed PC (WSL2)**, user
`dio_nysi` (`/home/dio_nysi`, `python3` = 3.13), with **one RTL-SDR Blog V4
(`v4-01`)**. The V4's FM notch is an inline SMA filter that unscrews for FM-band
work (RDS, WFM); leave it on for wideband scanning. A V3 is optional. On this
host the V4 is the scanner dongle. **Stage 0 (single-host reset)** is landing on
branch `refactor/stage0-single-host-reset`: repoint `python3.11`→`python3`,
reconcile user `dio_nysis`→`dio_nysi`, collapse the two-dongle defaults to
`v4-01`, and drop the leap-only remote/USB-recovery machinery. Until Stage 0
boots clean on this host, the reliability units silently fail to start.

**Leap is retired.** `leap` (192.168.2.10, openSUSE Leap 15.6, user `dio_nysis`,
the 2014 Dell Inspiron that ran the pipelines) went down in June 2026 with a
failed disk and is **not coming back in the near term** (financial). Do not plan
around its return. The code, schema, dashboards, and systemd units rebuild from
this repo onto the local host; the **data did not survive** — there was no
ClickHouse backup, so months of `spectrum.scans` and the one-shot FM-bandstop
A/B baseline are gone. Every post-processing feature needs a fresh local
collection runway first, and a backup running before data accumulates.

**Pipeline status (working vs not):**

| Pipeline | State | Reality |
|----------|-------|---------|
| **spectrum** | working, primary | The steady tenant. Scanner + ingest + classifier + feature-extractor + health monitor, proven on leap (now retired) against the V3; on the local host they run against the V4. Backs the reliability stack. |
| **acars** | built, soak interrupted | Soak started 2026-05-02 on leap's V4; leap went down before the 1-week soak finished and its result was never recorded. Redeploys fresh on the local V4 (see `acars/DEPLOY.md`). |
| **noaa** | partial | Scheduler + TLE refresh + ClickHouse schema are real and run hourly under `NOAA_DRY_RUN=1`. The **recorder (`noaa/recorder.py`) is a scaffold** that marks every pass `failed`; no rtl-tcp orchestration, no WAV/PNG capture yet. |
| **adsb** | companion, dormant | Code complete; historically ran against a **separate ClickHouse on the Windows host**, not leap. Never a steady tenant. |
| **ais** | companion, built-not-deployed | Complete, never deployed. |
| **ism** | companion, built-not-deployed | Complete, never deployed. |

**Cross-pipeline wiring:**
- **Dongle coordinator: landed and wired.** `spectrum/coordinator.py` (flock
  lock) is imported and used by `spectrum/scanner.py`; `ops/rtl-coordinator/`
  installs it. Not yet exercised against a real second consumer (the only
  intended one, the NOAA recorder, is still a scaffold). With one V4, the
  coordinator matters once a second consumer time-shares that dongle.
- **ACARS to classifier feedback: shipped, not "TBD".** `spectrum/acars_feedback.py`
  (a separate hourly timer, `ops/spectrum-acars-feedback/`) bridges the two
  ClickHouse databases over **plain HTTP, not `remote()`**, and reads
  `acars.messages` directly (the `acars.freq_activity` MV undercounts and is
  effectively dead). It writes confirmed ACARS frequencies into
  `spectrum.listening_log`. Whether it ever wrote a row was never confirmed
  before leap went down.

**Backups:** `ops/clickhouse-backup/` provides daily off-host logical snapshots
of the ClickHouse databases (added 2026-06 in response to the disk failure).
Consolidation put all six databases in one Docker volume, so the single-disk
SPOF is now worse than it was on leap. **Deploy this and point `BACKUP_DIR` off
the local disk before collecting new data** so the next disk failure is not
another total loss.

**Restore:** the ordered local-host restore runbook (clone, dongle serial,
install order, data restore, secret recreation) is in `RESTORE.md`.

**Repo hygiene:** the companion pipelines (adsb/ais/ism) still use a single
`init.sql` rather than numbered migrations; spectrum/acars/noaa use numbered
migrations + `migrate.py`.

---

## Owner Profile

- Platform/DevOps engineer, daily driver is Kubernetes, ArgoCD, Docker, observability pipelines
- Background in audio engineering / DSP, solid on FFT, filtering, sampling, frequency domain
- New to RF, bridge concepts from audio where possible (IQ samples ↔ mid/side stereo, waterfall ↔ spectrogram, etc.)
- Environment: **WSL openSUSE Tumbleweed** (fish shell) on Windows 11 (HP Omen laptop)
- Professional stack includes ClickHouse, Grafana, VictoriaMetrics, apply same pipeline patterns here
- Tools: git, Docker, Claude Code, VSCodium

## Hardware

- **RTL-SDR Blog V4** (`v4-01`, the local host's dongle): R828D tuner, RTL2832U
  ADC, 1PPM TCXO, SMA connector. On this host the V4 is the scanner dongle.
  - Same 500 kHz – 1766 MHz normal / 0 – 28.8 MHz direct-sampling range as the V3
  - Max stable sample rate: 2.048 MS/s; 8-bit ADC (~50 dB dynamic range)
  - **FM notch is a removable inline SMA filter**: screw it in for wideband
    scanning (rejects the strong Lycabettus/Hymettus FM transmitters), unscrew it
    for FM-band work like RDS or WFM.
- **RTL-SDR Blog V3** (`v3-01`, optional): R860 tuner, RTL2832U ADC, 1PPM TCXO.
  - Normal mode: 500 kHz – 1766 MHz
  - Direct sampling (Q-branch): 0 – 28.8 MHz (HF/shortwave)
  - Max stable sample rate: 2.048 MS/s; 8-bit ADC (~50 dB dynamic range)
- **Dipole antenna kit**: telescoping elements, magnetic base, SMA pigtail
- USB access: dongle is a USB device, WSL doesn't see USB natively (see USB strategy below)

## Physical Location & RF Environment

- Ground floor apartment, Polygono (one of the highest residential points in central Athens)
- Old stone walls: 6-7m thick in places, **blocks UHF signals aggressively**
- Desk faces window directly to street level (pavement)
- Patio available with more sky view
- Elevated relative to most of central Athens, good line of sight for VHF/UHF
- Near Athens airport (LGAV) approach paths, excellent for ADS-B
- Line of sight toward Piraeus/Saronic Gulf, good for AIS maritime
- Strong FM transmitters on Lycabettus and Hymettus, potential overload source

### Indoor vs Outdoor Reality

| Band | Indoor (desk/window) | Patio | Notes |
|---|---|---|---|
| FM (88-108 MHz) | Works easily | Overkill | Strong signals penetrate walls |
| HF shortwave | Wire out window | Better | Need long wire antenna regardless |
| VHF (118-174 MHz) | Window OK | Better | Airband, marine, NOAA sats |
| UHF (380-470 MHz) | Marginal | Window minimum | TETRA, PMR, ISM, walls block |
| 1090 MHz (ADS-B) | Window only | Best | Needs line of sight to sky |
| 137 MHz (satellites) | No | Patio required | Need sky view for overhead passes |

---

## Project Directory Structure

```
rf_luv/
├── CLAUDE.md                   # this file (Claude Code context)
├── QUICKREF.md                 # cheat sheet for live radio use
├── README.md                   # project overview & quick-start
├── bootstrap.sh                # one-time setup: organize files, clean junk, git init
├── .gitignore
│
├── setup/
│   ├── install-wsl.sh          # openSUSE Tumbleweed package installer
│   └── install-windows.md      # Windows-side setup guide (Zadig, SDR++, usbipd)
│
├── infra/                      # shared always-on data layer (compose project rf_luv_infra)
│   ├── compose.yml             # one ClickHouse (8123/9000) + Grafana (3000) + logging-form (8084) + ch-bootstrap
│   ├── up.sh                   # bring up the data layer (creates rf_luv_net if absent)
│   ├── bootstrap.sh            # creates the 6 dbs/users + grants, applies schema + Athens seed
│   ├── Dockerfile.bootstrap    # glibc base with clickhouse-client + python3 for the one-shot
│   ├── clickhouse/cors.xml     # server-wide CORS (moved out of spectrum)
│   └── grafana/provisioning/   # 6 datasources + 6 folders, one per pipeline
│
├── pipeline.sh                 # bring rotating V4 decoders up/down/rotate: pipeline.sh up|down|rotate <pipe>
│
├── adsb/                       # ADS-B aircraft tracking pipeline (rotating V4 decoder)
│   ├── compose.overlay.yml     # readsb + tar1090 + ingest (no own ClickHouse/Grafana)
│   ├── Dockerfile.ingest       # container for ingest.py
│   ├── ingest.py               # SBS BaseStation → ClickHouse batch inserter
│   └── clickhouse/
│       └── init.sql            # positions table, materialized views, TTL (applied by ch-bootstrap)
│
├── ais/                        # AIS ship tracking pipeline (rotating V4 decoder)
│   ├── compose.overlay.yml     # AIS-catcher + ingest (no own ClickHouse/Grafana)
│   ├── Dockerfile.ingest       # container for ais_ingest.py + ais_decoder.py
│   ├── ais_ingest.py           # NMEA UDP → ClickHouse batch inserter
│   ├── ais_decoder.py          # stdlib-only AIVDM decoder (msg types 1-3, 5, 18, 24)
│   └── clickhouse/
│       └── init.sql            # positions table, ship_latest, hourly_stats views
│
├── ism/                        # ISM band IoT device monitoring pipeline (rotating V4 decoder)
│   ├── compose.overlay.yml     # rtl_433 + ingest (no own ClickHouse/Grafana)
│   ├── Dockerfile.ingest       # rtl_433 + Python in single container
│   ├── entrypoint.sh           # pipes rtl_433 JSON stdout → ism_ingest.py
│   ├── ism_ingest.py           # JSON line reader → ClickHouse batch inserter
│   └── clickhouse/
│       └── init.sql            # events table, device_latest, hourly_stats views
│
├── spectrum/                   # Wideband spectrum scanner ("RF weather station")
│   ├── compose.overlay.yml     # optional containerized scanner (native systemd is the norm)
│   ├── Dockerfile.scanner      # python:3.12-slim + numpy
│   ├── entrypoint.sh           # pipes scanner.py → scan_ingest.py
│   ├── scanner.py              # custom rtl_tcp FFT scanner (replaces rtl_power)
│   ├── scan_ingest.py          # JSON line reader → ClickHouse batch inserter
│   └── clickhouse/             # init.sql + migrations/ + seeds/ (applied by ch-bootstrap)
│
├── acars/                      # ACARS aircraft messaging pipeline (rotating V4 decoder)
│   ├── compose.overlay.yml     # acarsdec (sdr-enthusiasts image, @sha256-pinned) + ingest
│   ├── Dockerfile.ingest       # python:3.12-slim, only the ingest worker; decoder is prebuilt
│   ├── entrypoint.sh           # runs UDP listener (schema applied by ch-bootstrap)
│   ├── acars_ingest.py         # UDP datagram reader → ClickHouse batch inserter
│   ├── migrate.py              # numbered SQL migration runner (mirrors spectrum/migrate.py)
│   ├── clickhouse/migrations/  # 001_init.sql etc.
│   └── env.v4-01.example       # V4 deployment template
│
├── scripts/
│   ├── spectrum-scan.sh        # rtl_power wideband scanning with band presets
│   ├── satellite-pass.sh       # NOAA/Meteor pass recording with rtl_fm
│   ├── ism-monitor.sh          # rtl_433 ISM device decoder (433 MHz)
│   ├── ais-monitor.sh          # AIS ship tracking (161/162 MHz)
│   └── airband-listen.sh       # Athens ATC listener (AM airband)
│
├── notes/                      # signal identification logs, listening playbook
└── docs/archive/               # dated historical runbooks (e.g. old morning-checks)
```

(`infra/`, `ops/`, `setup/` also live at the repo root; see README.md for the
full tree. `config/` (SDR++ settings) and `recordings/` (IQ/CSV/PNG captures)
are gitignored working dirs created on demand, not tracked, so they may not be
present in a fresh clone.)

---

## USB Access Strategy

The RTL-SDR is a USB device. WSL cannot see USB hardware without extra steps.

### Approach A: Hybrid (recommended, start here)
- **SDR GUI apps** (SDR++, SDR#) run on **Windows** with native USB
- **rtl_tcp** runs on **Windows**, streams IQ samples over TCP to localhost
- **Processing/pipelines** run in **WSL/Docker**, connecting to rtl_tcp
```
# Windows (CMD or PowerShell):
rtl_tcp.exe -a 0.0.0.0 -p 1234 -s 2048000

# WSL tools connect to 127.0.0.1:1234 automatically
# The native spectrum scanner and the rotating decoder overlays both point at
# the single V4 on host.docker.internal:1234 (they time-share it via the coordinator)
```

### Approach B: usbipd (full Linux USB passthrough)
- Attaches USB device to WSL over IP, all tools run natively in WSL
- Higher latency, can drop samples at high rates
```powershell
# Windows PowerShell (admin):
winget install usbipd
usbipd list                              # find RTL2832U
usbipd bind --busid <BUSID>
usbipd attach --wsl --busid <BUSID>

# WSL: verify with lsusb | grep RTL
```

### When writing scripts / tools
- Always note which USB approach a script assumes
- If script calls `rtl_fm`, `rtl_sdr`, `rtl_power` directly → needs usbipd OR rtl_tcp bridge
- Docker services using `READSB_DEVICE_TYPE: rtltcp` → uses rtl_tcp (Approach A)
- Docker services using `READSB_DEVICE_TYPE: rtlsdr` + `devices:` → needs usbipd (Approach B)

---

## Setup Steps (do in order)

### Phase 1: Before Dongle Arrives

- [ ] **Run bootstrap**: `bash bootstrap.sh`: organizes flat files into directory structure, kills Zone.Identifier files, sets permissions, git inits
- [ ] **Install WSL packages**: `bash setup/install-wsl.sh`: installs rtl-sdr, GNU Radio, multimon-ng, rtl_433, sox, satellite tools. Note any failures for manual follow-up.
- [ ] **Windows setup**: follow `setup/install-windows.md`:
  - Download Zadig (zadig.akeo.ie)
  - Download SDR++ (github.com/AlexandreRouma/SDRPlusPlus/releases), Windows x64 zip
  - Download rtl-sdr Windows binaries (for rtl_tcp.exe): ftp.osmocom.org/binaries/windows/rtl-sdr/
  - Optionally: `winget install usbipd` for USB passthrough

### Phase 2: Dongle Arrives, First Contact

- [ ] **Driver swap**: plug in dongle → open Zadig → Options → List All Devices → select "Bulk-In, Interface (Interface 0)" → target WinUSB → Replace Driver
- [ ] **First signal**: open SDR++ → Source: RTL-SDR → sample rate 2.048 MHz → gain 30 dB → tune to ~100 MHz → hear FM radio
- [ ] **Gain calibration**: start at 0 dB, increase by 5 until signal-to-noise peaks. Sweet spot usually 28-42 dB. If ghost signals appear, gain is too high.
- [ ] **Explore the spectrum**: slowly scroll from 80 MHz upward in SDR++. Use sigidwiki.com to identify unknown signals.

### Phase 3: ADS-B Pipeline

- [ ] **Start rtl_tcp on Windows**: `rtl_tcp.exe -a 0.0.0.0 -p 1234 -s 2048000` (the single V4 on `host.docker.internal:1234`)
- [ ] **Set antenna**: dipole arms ~6.5 cm each, vertical, at window or patio
- [ ] **Bring up the data layer (once)**: `docker network create rf_luv_net` then `bash infra/up.sh` (starts the shared ClickHouse on 8123/9000 and Grafana on 3000)
- [ ] **Launch the decoder**: `bash pipeline.sh up adsb` (starts the ADS-B readsb + ingest against the always-on infra)
- [ ] **Verify**: open http://localhost:8080 (tar1090 map), aircraft should appear within minutes
- [ ] **Check Grafana**: open http://localhost:3000 (admin/admin), the ClickHouse adsb datasource is auto-provisioned, dashboards live under the ADS-B folder
- [ ] **Monitor ingest**: `bash pipeline.sh logs adsb` (or `docker logs -f` the ingest container): should see batch flush messages
- [ ] **Feeder setup** (optional): register at FlightAware/ADSBx for stats and comparison

### Phase 4: Ongoing Projects

Each script in `scripts/` is self-contained with usage instructions in its header.

**Spectrum survey:**
```bash
bash scripts/spectrum-scan.sh           # VHF/UHF overview (80-500 MHz)
bash scripts/spectrum-scan.sh fm        # FM band only
bash scripts/spectrum-scan.sh full      # everything 24 MHz - 1.7 GHz
# Visualize: python3 ~/.local/bin/heatmap.py recordings/scan_*.csv output.png
```

**ISM band devices (most immediately rewarding after FM):**
```bash
bash scripts/ism-monitor.sh             # live decoded devices
bash scripts/ism-monitor.sh analyze     # 5-min scan + summary
```

**AIS ship tracking:**
```bash
bash scripts/ais-monitor.sh             # live ship messages
# Point antenna SW toward Piraeus, vertical, arms ~45 cm
```

**Airband (ATC communications):**
```bash
bash scripts/airband-listen.sh approach  # Athens Approach
bash scripts/airband-listen.sh tower     # Athens Tower
```

**HF / Number stations (evening project):**
1. In SDR++: Source → RTL-SDR → Direct Sampling → Q-branch
2. Tune to 4.625 MHz (UVB-76 "The Buzzer")
3. Set demod to USB (upper sideband), wider AM also works
4. Best after sunset when ionospheric propagation improves
5. Need long wire antenna: 10-20m stranded copper wire from patio, connected to SMA center pin
6. 9:1 balun between wire and dongle improves matching but not required initially

**Weather satellites:**
```bash
bash scripts/satellite-pass.sh noaa15   # record NOAA 15 pass
bash scripts/satellite-pass.sh noaa19   # record NOAA 19 pass
# Must be on patio! V-dipole: 53cm arms, 120° angle, tilted N-S
# Check pass times: n2yo.com or gpredict
# Decode: noaa-apt <wav_file> -o output.png
```

---

## Key Athens Frequencies

```
FM Broadcast        88–108 MHz         Strong, indoor test signal
Athens Approach     118.575 MHz        ATC (AM demod), may hear from desk
Athens Tower        118.1 MHz          ATC (AM demod)
ATIS                136.125 MHz        Automated airport weather
NOAA 15             137.620 MHz        Weather satellite (patio only)
NOAA 18             137.9125 MHz       Weather satellite
NOAA 19             137.100 MHz        Weather satellite
Meteor M2-3         137.100 MHz        Russian weather sat (LRPT digital)
Marine Ch16         156.800 MHz        Distress/calling
AIS Ch87            161.975 MHz        Ship positions
AIS Ch88            162.025 MHz        Ship positions
Greek TETRA         380–400 MHz        Emergency services (digital)
ISM Band            433.920 MHz        Sensors, weather stations, remotes
PMR446              446.0–446.2 MHz    License-free walkie-talkies
ADS-B               1090 MHz           Aircraft transponders
UVB-76 (HF)         4.625 MHz          Number station (direct sampling mode)
Voice of Greece     9.420 / 9.935 MHz  Shortwave broadcast (HF)
BBC WS              9.410 MHz          Shortwave (HF)
WWV Time Signal     10.000 MHz         NIST time broadcast (HF, from USA)
```

## Antenna Quick Reference

Formula: **arm length (cm) = 7125 / frequency (MHz)**: this gives quarter wavelength per dipole arm.

```
FM Radio     100 MHz  →  75.0 cm/arm   vertical         indoor OK
NOAA Sats    137 MHz  →  52.0 cm/arm   V-dipole 120°    patio only
AIS Ships    162 MHz  →  44.0 cm/arm   vertical         window toward Piraeus
TETRA        390 MHz  →  18.3 cm/arm   vertical         window minimum
ISM/433      434 MHz  →  16.4 cm/arm   vertical         window helps
ADS-B       1090 MHz  →   6.5 cm/arm   vertical         patio/window
HF (sw)     3-30 MHz  →  long wire     horizontal-ish   10-20m wire from patio
```

---

## ADS-B Pipeline Architecture

```
RTL-SDR (1090 MHz, via rtl_tcp on Windows)
  └→ readsb (Docker, decoder)
       ├→ Beast output (:30005) → tar1090 (web map, :8080)
       ├→ SBS/BaseStation (:30003) → ingest.py → ClickHouse
       └→ Raw output (:30002)

ClickHouse (adsb database)
  ├── positions table (MergeTree, partitioned by day, 90-day TTL)
  ├── aircraft_hourly (materialized view, uniq aircraft, avg altitude)
  └── aircraft_latest (materialized view, last known state per hex_ident)
        └→ Grafana (:3000, ADS-B folder)
             └── adsb-overview dashboard (auto-provisioned)
                 • Aircraft count (live + over time)
                 • Message rate
                 • Altitude histogram
                 • Recent flights table
                 • Altitude traces for top aircraft
```

**Key design decisions:**
- ClickHouse over Postgres/VictoriaMetrics because ADS-B is high-cardinality time series with analytical queries (GROUP BY hex_ident, altitude histograms, position aggregations), exactly ClickHouse's sweet spot
- ingest.py is stdlib-only Python (no dependencies) to keep the Docker image tiny and the code obvious
- SBS BaseStation format chosen over Beast binary because it's human-readable CSV, easy to debug and parse
- Materialized views handle rollups at write time so dashboards query pre-aggregated data

## AIS Pipeline Architecture

```
RTL-SDR (161.975 + 162.025 MHz, via rtl_tcp on Windows)
  └→ AIS-catcher (Docker, dual-channel decoder)
       └→ NMEA sentences (UDP :10110) → ais_ingest.py → ClickHouse

ClickHouse (ais database, shared server 8123/9000)
  ├── positions table (MergeTree, partitioned by day, 90-day TTL)
  ├── hourly_stats (materialized view, uniq ships, avg speed)
  └── ship_latest (materialized view, last known state per MMSI)
        └→ Grafana (:3000, AIS folder)
             └── ais-overview dashboard (auto-provisioned)
                 • Ship count (live + over time)
                 • Message rate
                 • Ship types bar chart
                 • Recent ships table
                 • Speed distribution + traces
```

**Key design decisions:**
- AIS-catcher over rtl_ais because it supports rtl_tcp input (rtl_ais requires direct USB)
- Custom stdlib-only AIVDM decoder (ais_decoder.py), 6-bit dearmoring, msg types 1-3/5/18/24, multi-sentence reassembly
- UDP transport from decoder to ingest, one NMEA sentence per datagram, no framing needed
- ship_latest view uses argMaxIf to merge position data (types 1-3, 18) with identity data (types 5, 24) without NULL clobbering

## ISM Pipeline Architecture

```
RTL-SDR (433.92 MHz, via rtl_tcp on Windows)
  └→ rtl_433 (in Docker, protocol decoder)
       └→ JSON stdout (pipe) → ism_ingest.py → ClickHouse

ClickHouse (ism database, shared server 8123/9000)
  ├── events table (MergeTree, partitioned by day, 180-day TTL)
  ├── hourly_stats (materialized view, uniq devices, avg temperature)
  └── device_latest (materialized view, last reading per device)
        └→ Grafana (:3000, ISM folder)
             └── ism-overview dashboard (auto-provisioned)
                 • Active devices + event rate
                 • Device types bar chart
                 • Temperature + humidity traces
                 • Recent events table
```

**Key design decisions:**
- rtl_433 and Python ingest in a single container, stdout pipe is the simplest IPC
- 180-day TTL (vs 90 for ADS-B/AIS), ISM data is interesting for seasonal device patterns
- raw_json column stores full rtl_433 output, covers all 200+ protocols without explicit field mapping

## ACARS Pipeline Architecture

```
RTL-SDR V4 (rtl_tcp :1234, time-shared with the scanner via the coordinator)
  └→ acarsdec (Docker, ghcr.io/sdr-enthusiasts/docker-acarsdec; see image-tag note below)
       └→ JSON datagrams (UDP :5550) → acars_ingest.py → ClickHouse

ClickHouse (acars database, shared server 8123/9000)
  ├── messages table (MergeTree, partitioned by day, 90-day TTL)
  ├── hourly_stats (AggregatingMergeTree, counts, uniques, avg level/err)
  ├── flight_latest (ReplacingMergeTree per flight callsign)
  ├── tail_latest (ReplacingMergeTree per tail registration, uplinks too)
  └── freq_activity (ReplacingMergeTree per (freq, dongle), classifier-feedback hook)
        └→ Grafana (:3000, ACARS folder)
             └── ACARS Overview dashboard (auto-provisioned)
                 • Message rate, unique flights/tails (stat row)
                 • Messages per minute (24h timeseries)
                 • Top flights, tails, ACARS labels (bar charts)
                 • Frequency channel activity, signal level over time
                 • Top OOOI airport pairs (depa→dsta routes)
                 • Recent messages table (last 100, full text)
                 • freq_activity (validates classifier-feedback loop)
```

**Key design decisions:**
- Decoder is the **airframesio acarsdec fork** via the maintained sdr-enthusiasts
  Docker image, has SoapySDR + Soapy-rtltcp built-in. Building from TLeconte
  upstream was rejected because that branch doesn't speak rtl_tcp.
- **UDP transport** between decoder and ingest (mirrors AIS), not piped stdout
  (ISM): the decoder image is sealed and doesn't pipe JSON to stdout in the
  configurations available; UDP-to-acars-ingest:5550 is the supported path.
- **Numbered SQL migrations** from day 1 (`clickhouse/migrations/NNN_*.sql`)
  + a Python migrator running at ingest container startup, mirroring spectrum.
  ISM's single-init.sql pattern doesn't support schema evolution.
- ACARS gives the **content layer** to ADS-B's positions: joinable on
  (tail, flight) for crew messages, OOOI events, weather requests, CPDLC
  app data. The natural pair to the existing aviation pipeline.
- `freq_activity` was meant to be the **classifier-feedback hook**, but the
  shipped implementation diverged from this plan. The actual feedback path is
  `spectrum/acars_feedback.py` (hourly timer in `ops/spectrum-acars-feedback/`):
  it reads `acars.messages` directly (not `freq_activity`, whose MV undercounts)
  over **plain HTTP, not `remote()`**, and writes confirmed ACARS frequencies
  into `spectrum.listening_log` (not `known_frequencies`). The classifier then
  treats those as a soft prior. So this is shipped, not "TBD", and lives in a
  separate process from the classifier.
- **Image tag:** the acarsdec decoder is `@sha256`-pinned in `acars/compose.overlay.yml` (never `latest`); resolve and pin the live digest on the next deploy (the soak ran on SoapySDR build `4.1.6Build1494`).

## Spectrum Scanner Pipeline Architecture

```
RTL-SDR (88-470 MHz sweep, via rtl_tcp on Windows)
  └→ scanner.py (Docker, custom rtl_tcp FFT engine, numpy)
       └→ JSON lines (stdout pipe) → scan_ingest.py → ClickHouse

ClickHouse (spectrum database, shared server 8123/9000)
  ├── scans table (MergeTree, one row per freq bin per sweep, 180-day TTL)
  ├── peaks table (spectral peaks, bins above their neighbors)
  ├── events table (transient signals, appeared/disappeared between sweeps)
  ├── known_frequencies (27 Athens signals: FM, ATC, marine, TETRA, ISM, DVB-T, military)
  ├── hourly_baseline (materialized view, avg/stddev per freq per hour)
  └── freq_latest (materialized view, latest reading per bin)
        └→ Grafana (:3000, Spectrum folder; default datasource)
             └── spectrum-overview dashboard (auto-provisioned)
                 • Current power spectrum (bar chart, full 88-470 MHz)
                 • Signal power over time at known frequencies
                 • Active known frequencies table
                 • Detected peaks (auto-found spectral peaks)
                 • Signal events (transient appear/disappear)
                 • Airband activity (60s resolution ATC traces)
                 • Anomaly detection (signals above hourly baseline)
```

**Key design decisions:**
- Custom Python rtl_tcp client replaces rtl_power (which can't use rtl_tcp network input)
- Normalized data model: one row per frequency bin per sweep, ORDER BY (freq_hz, timestamp) for frequency-first queries
- 8x FFT averaging + 5ms PLL settle + 32KB buffer drain for stable measurements (~1 dB sweep-to-sweep)
- Multi-preset sweep scheduling: full band every 5 min, airband every 60s
- Peak detection: bins >10 dB above their 5-neighbor average
- Transient detection: >15 dB change between consecutive sweeps
- Reconnect to rtl_tcp for each sweep to prevent TCP buffer stale data accumulation

## NOAA Pipeline Architecture

```
celestrak TLEs (tle_refresh.sh, weekly)  +  RX location (lat/lon/alt)
  └→ scheduler.py (hourly :05, systemd user timer)
       ├→ orbit-predictor: next 12h of NOAA 15/18/19 + Meteor M2 passes
       └→ noaa.passes (pending rows)  [under NOAA_DRY_RUN=1, the default]
            └→ recorder.py (SCAFFOLD, see below)

ClickHouse (noaa database, shared server 8123/9000)
  ├── passes table (MergeTree, partitioned by month, 365-day TTL)
  ├── pass_latest (ReplacingMergeTree: canonical state per pass)
  └── monthly_summary (AggregatingMergeTree: decode rate, avg SNR per sat per month)
        └→ Grafana (:3000, NOAA folder), noaa-overview dashboard
```

**State and key decisions:**
- **The recorder is a scaffold, not a working capture path.** `noaa/recorder.py`
  logs the rtl-tcp orchestration it *would* run (stop the scanner, record the
  pass with rtl_fm, restart, decode with noaa-apt), then unconditionally marks
  the pass `failed` with note `scaffold: rtl-tcp orchestration not implemented
  yet`. No WAV or PNG is produced. Implementing real capture is what would first
  exercise the dongle coordinator (the recorder is the intended second consumer
  time-sharing the V4 alongside the scanner).
- The scheduler runs with `NOAA_DRY_RUN=1` by default, which **returns before
  inserting** pending rows. Flipping to `0` (in `/etc/rtl-scanner/noaa-scheduler.env`)
  is only meaningful once the recorder is real.
- Numbered migrations + a stdlib `migrate.py` (same pattern as ACARS), run at
  the migrator container's startup.
- NOAA runs on the scanner dongle (the V4 locally; the V3 on leap): weather-sat
  passes at 137 MHz sit in the VHF range the scanner already covers, so the
  recorder time-shares that dongle through the coordinator rather than adding a
  second one.

## Reliability Stack

The local host carries a layered reliability stack, each layer catches a failure
mode the layer above can't see. Code lives under `ops/`. It was built and proven
on leap (now retired); Stage 0 prunes the leap-only remote/USB-recovery pieces
that are inert or harmful on a local WSL2 desk: the escalator's `systemctl
reboot` rung is disabled by default (`REBOOT_ENABLED=0` — on WSL2 a reboot
bounces the VM, not the USB-owning Windows host), the ssh-based unwedge scripts
are gone, and `notify.py` falls back to a local desktop popup (`notify-send`)
when no ntfy topic is set. The data/RF probes, the coordinator, and the
serial-identity layer stay.

```
            ┌──────────────────────────────────────────────────────────────┐
            │  rtl-tcp-watchdog  (user-level, 30s)                         │  fastest
            │   probe → soft restart → unbind/rebind → CB-open at fail #10 │  per-process
            └──────────────────────────────────────────────────────────────┘
                              ↓ stops here
            ┌──────────────────────────────────────────────────────────────┐
            │  rtl-tcp-escalator  (root, 5min)                             │  USB layer
            │   on CB-open ≥10min: rtl-usb-reset + xHCI bounce + restart   │
            │   on 3 failed unwedges/24h or both CB ≥30min: systemctl reboot│  hardware
            └──────────────────────────────────────────────────────────────┘
                              ↓ stops here
            ┌──────────────────────────────────────────────────────────────┐
            │  freshness-probe  (root, 5min)                               │  data layer
            │   queries spectrum.scans per dongle_id                       │
            │   WARN >10min stale, CRITICAL >25min stale                   │  catches
            │   catches: Docker death, ClickHouse OOM, ingest broken       │  downstream
            └──────────────────────────────────────────────────────────────┘
                              ↓ stops here (data flowing but useless?)
            ┌──────────────────────────────────────────────────────────────┐
            │  signal-quality-probe  (root, 5min)                          │  RF layer
            │   queries spectrum.sweep_health per dongle_id (full sweeps)  │
            │   WARN max_power < -35 dBFS over 30min                        │  catches
            │   CRITICAL max_power < -40 dBFS over 30min                    │  deaf
            │   catches: antenna disconnect, loose F-connector, broken filter │ scanner
            └──────────────────────────────────────────────────────────────┘
                              ↓
            ┌──────────────────────────────────────────────────────────────┐
            │  notify.py → ntfy.sh → operator's phone                      │  alerting
            │   topic in /etc/rtl-scanner/notify.env (gitignored)          │
            │   daily heartbeat 09:00 UTC confirms pipe is alive           │
            └──────────────────────────────────────────────────────────────┘
```

**Action log**: every layer appends JSON lines to `/var/log/rtl-recovery.log` (logrotate weekly, 4-week retention). On return from a trip, `cat /var/log/rtl-recovery.log | jq` is the single source of truth for what happened.

**State files** (root-owned, world-readable):
- `/var/lib/rtl-tcp-escalator/state.json`: per-serial unwedge attempts in last 24h, cb_first_seen timestamps, last_reboot_ts
- `/var/lib/spectrum-monitor/freshness.json`: current freshness level + stale_sec per dongle_id
- `/var/lib/spectrum-monitor/signal_quality.json`: current signal level + max_pwr per dongle_id over the last 30 min
- `/run/user/1000/rtl-tcp-watchdog-<serial>.state`: per-serial consecutive_failures + last_hard_reset_ts (user-level, the watchdog's own state)

**Install**: `bash ops/install-trip-hardening.sh`: idempotent, one sudo prompt. Picks up the existing `ops/rtl-tcp/install.sh` watchdog stack as a prerequisite (run that first if `rtl-tcp@v4-01.service` doesn't exist yet).

**Failure modes this stack does NOT cover**:
- Chip-lockup (the 2026-04-28 V3 incident pattern: hot-but-enumerated, no software response). Hardware mitigation only, per-port-power hub (e.g. YEPKIT YKUSH3) or smart plug for whole-machine cycle.
- Kernel panic / hard hang. `systemctl reboot` can't help; smart plug only.
- Outbound network down for >24h. ntfy alerts won't reach the phone.
- Shared-bus hardware fault flapping the dongle. On the single-V4 local host the reboot rung is disabled, so this needs the smart-plug / powered-hub mitigation below. (On leap's two-dongle host the escalator handled each serial independently and could reboot per the both-CB-open threshold.)
- Antenna or filter physical failure: signal-quality-probe **alerts** (within 30 min) but cannot self-recover. Operator inspection / re-seat connectors required. The 2026-04-29 V3 RF-chain failure was the canonical case, coax/F-connector at the FM bandstop loosened, sweep `max_power` dropped 30 dB, fixed by replug.
- **Disk failure / data durability.** This whole stack keeps RF data *flowing*;
  it does nothing to keep it *safe*. The 2026-06 leap disk failure was the
  canonical case: all ClickHouse data lived in Docker volumes on one disk with no
  backup, and that data is gone. Mitigation is a separate layer,
  `ops/clickhouse-backup/` (daily off-host logical snapshots), which must be
  running before the local host collects data worth keeping. Consolidation put
  all six databases in one Docker volume, so the single-disk SPOF is worse than
  it was on leap.

## Port Allocation

The pipelines no longer each run their own ClickHouse and Grafana. There is
**one shared data layer** (compose project `rf_luv_infra`, file
`infra/compose.yml`) that every pipeline writes into:

| Service     | Host port | Notes |
|-------------|-----------|-------|
| ClickHouse HTTP   | 8123 | one server, six databases (adsb/ais/ism/spectrum/acars/noaa) |
| ClickHouse native | 9000 | Grafana datasources connect here |
| Grafana           | 3000 | one instance, six datasources + six folders (one per pipeline) |
| logging-form      | 8084 | nginx serving the listening-log HTML form |
| tar1090 (ADS-B)   | 8080 | only when the adsb decoder is up |

**Retired ports.** The old per-pipeline ClickHouse ports 8124-8128 and
9001-9005, and the old per-pipeline Grafana ports 3001-3005, no longer exist.
Anything that used to hit `:8126` (spectrum), `:8127` (acars), etc. now hits the
single server on `127.0.0.1:8123` (HTTP) / `127.0.0.1:9000` (native) from the
host, or the `clickhouse` container alias from inside the `rf_luv_net` network.
The six Grafana dashboards now live as six folders inside the single Grafana on
`:3000` (spectrum is the default datasource).

ClickHouse backups land off-host via `ops/clickhouse-backup/` (no port; daily
user timer, see "Backups" in Current Project State).

**Bring-up.** Create the shared network once (`docker network create rf_luv_net`)
then start the always-on data layer with `bash infra/up.sh` (which also creates
the network if absent). Rotating V4 decoders are managed with
`bash pipeline.sh up|down|rotate <pipe>` (`<pipe>` is one of adsb/ais/ism/acars);
each runs as its own compose project (`rf_luv_<pipe>`, file
`<pipe>/compose.overlay.yml`) attached to the same network and pointed at the
shared ClickHouse. Schema and the Athens known-frequencies seed are applied
automatically by the infra `ch-bootstrap` one-shot, so the old manual
`docker exec -i clickhouse-<db> ... < seed` steps are no longer required.

The local host runs **one dongle**, the V4 on rtl_tcp :1234, with its templated
systemd stack (`rtl-tcp@v4-01`, `rtl-tcp-watchdog@v4-01`, `rtl-scanner@v4-01`).
The V4 belongs to the native systemd spectrum scanner, which writes to the
shared ClickHouse on `127.0.0.1:8123`. A second consumer time-shares the same
dongle via flock through the coordinator: `spectrum/coordinator.py` (installed by
`ops/rtl-coordinator/`) is wired into `scanner.py`. The mechanism is in place but
not yet exercised against a real second consumer, so in practice one consumer
holds the dongle today.

History: leap ran **two** dongles — V3 on :1234 for the scanner, V4 on :1235 for
the rotating `pipeline.sh` decoders — each with its own templated stack. That
split retired with leap; on the local host the scanner and any rotating decoder
share the single V4 through the coordinator.

**Dongle assignment policy:**
- **V4 (`v4-01`, port 1234)**: the local host's only dongle. Runs the wideband
  scanner by default with the FM notch screwed in. Narrow-band decoders (ACARS,
  POCSAG/marine voice/PMR446) time-share it through the coordinator. Unscrew the
  inline notch for FM-band work (RDS, WFM); screw it back for wideband sweeps.
- **V3 (optional)**: a second dongle, if added, can take FM-bandstopped wideband
  scanning so the V4 is free for narrow-band or FM-band work.

---

## Common Pitfalls & Debugging

**FM broadcast overload**: Athens has powerful FM transmitters on Lycabettus and Hymettus. Symptoms: phantom signals across the spectrum, signals that move when you retune. Fix: reduce gain to 20-25 dB, or get an FM notch filter (band-stop at 88-108 MHz).

**Gain too high**: if you see weak "ghost" copies of strong signals at unexpected frequencies, that's intermodulation from excessive gain. The RTL-SDR's 8-bit ADC saturates easily. Always start low (20 dB) and increase.

**Stone wall attenuation**: anything above ~300 MHz is significantly weakened by the thick walls. For ADS-B, AIS, ISM: get the antenna to the window or outside. A 3-5m USB extension cable with ferrite chokes lets you keep the laptop at your desk.

**Direct sampling sensitivity**: HF mode (for number stations, shortwave) has much lower sensitivity than normal mode. Use the longest wire antenna possible, and try at night when ionospheric propagation is stronger.

**Sample drops**: if rtl_power or rtl_fm report "samples lost" or audio glitches, reduce sample rate to 1.024 MS/s. USB 2.0 bandwidth and usbipd overhead can cause this.

**Docker networking**: `host.docker.internal` resolves to the Windows host from within Docker containers on WSL. If rtl_tcp is running on Windows, containers can reach it at `host.docker.internal:1234`. If this doesn't work, find the Windows IP with `ip route show default` in WSL and use that.

**rtl_tcp connection refused**: check Windows Firewall, it may block rtl_tcp. Allow it through, or use `127.0.0.1` instead of `0.0.0.0` if only connecting from the same machine.

---

## Coding Conventions

- Shell scripts: **bash** (not fish) with `set -euo pipefail`: fish is the interactive shell but scripts need portability
- Python: 3.11+, prefer stdlib, minimal external deps. Type hints welcome but not required.
- Docker: always pin image tags to specific versions, never use `latest`
- Data formats: JSON Lines (`.jsonl`) for streaming data, CSV for scan results
- Comments: explain RF/SDP concepts inline, this is a learning project, not a production codebase
- File naming: lowercase, hyphens (not underscores) for scripts
- Configs: YAML for Docker/Grafana, SQL for ClickHouse

## When Helping With This Project

- Connect RF concepts to audio/DSP analogies where possible
- If a script needs direct USB access, say so explicitly and mention the rtl_tcp alternative
- For antenna questions: always include the arm length calculation and orientation
- For new signals: reference sigidwiki.com for identification
- For ADS-B pipeline changes: maintain the readsb → ClickHouse → Grafana architecture
- When suggesting new tools: prefer packages in Tumbleweed repos, fall back to source builds
- Legal: listening is legal in Greece (as in most EU countries). Decoding encrypted comms is not. TETRA and some digital services are encrypted, note this when relevant.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
