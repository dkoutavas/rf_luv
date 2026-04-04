# rf_luv — RTL-SDR Radio Lab

## What This Is

Personal radio exploration project using an RTL-SDR Blog V3 dongle. Based in Athens, Greece (Polygono neighborhood — elevated central Athens). Projects range from spectrum analysis to aircraft tracking to number station hunting.

This file is the Claude Code project prompt. It contains everything needed to assist with any task in this project.

---

## Owner Profile

- Platform/DevOps engineer, daily driver is Kubernetes, ArgoCD, Docker, observability pipelines
- Background in audio engineering / DSP — solid on FFT, filtering, sampling, frequency domain
- New to RF — bridge concepts from audio where possible (IQ samples ↔ mid/side stereo, waterfall ↔ spectrogram, etc.)
- Environment: **WSL openSUSE Tumbleweed** (fish shell) on Windows 11 (HP Omen laptop)
- Professional stack includes ClickHouse, Grafana, VictoriaMetrics — apply same pipeline patterns here
- Tools: git, Docker, Claude Code, VSCodium

## Hardware

- **RTL-SDR Blog V3**: R860 tuner, RTL2832U ADC, 1PPM TCXO, SMA connector
  - Normal mode: 500 kHz – 1766 MHz
  - Direct sampling (Q-branch): 0 – 28.8 MHz (HF/shortwave)
  - Max stable sample rate: 2.048 MS/s
  - 8-bit ADC (~50 dB dynamic range)
- **Dipole antenna kit**: telescoping elements, magnetic base, SMA pigtail
- USB access: dongle is a USB device, WSL doesn't see USB natively (see USB strategy below)

## Physical Location & RF Environment

- Ground floor apartment, Polygono (one of the highest residential points in central Athens)
- Old stone walls: 6-7m thick in places — **blocks UHF signals aggressively**
- Desk faces window directly to street level (pavement)
- Patio available with more sky view
- Elevated relative to most of central Athens — good line of sight for VHF/UHF
- Near Athens airport (LGAV) approach paths — excellent for ADS-B
- Line of sight toward Piraeus/Saronic Gulf — good for AIS maritime
- Strong FM transmitters on Lycabettus and Hymettus — potential overload source

### Indoor vs Outdoor Reality

| Band | Indoor (desk/window) | Patio | Notes |
|---|---|---|---|
| FM (88-108 MHz) | Works easily | Overkill | Strong signals penetrate walls |
| HF shortwave | Wire out window | Better | Need long wire antenna regardless |
| VHF (118-174 MHz) | Window OK | Better | Airband, marine, NOAA sats |
| UHF (380-470 MHz) | Marginal | Window minimum | TETRA, PMR, ISM — walls block |
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
├── adsb/                       # ADS-B aircraft tracking pipeline
│   ├── docker-compose.yml      # readsb + tar1090 + ClickHouse + Grafana + ingest
│   ├── Dockerfile.ingest       # container for ingest.py
│   ├── ingest.py               # SBS BaseStation → ClickHouse batch inserter
│   ├── clickhouse/
│   │   └── init.sql            # positions table, materialized views, TTL
│   └── grafana/
│       └── provisioning/
│           ├── datasources/
│           │   └── clickhouse.yml
│           └── dashboards/
│               ├── dashboards.yml
│               └── json/
│                   └── adsb-overview.json  # pre-built dashboard
│
├── ais/                        # AIS ship tracking pipeline
│   ├── docker-compose.yml      # AIS-catcher + ClickHouse + Grafana + ingest
│   ├── Dockerfile.ingest       # container for ais_ingest.py + ais_decoder.py
│   ├── ais_ingest.py           # NMEA UDP → ClickHouse batch inserter
│   ├── ais_decoder.py          # stdlib-only AIVDM decoder (msg types 1-3, 5, 18, 24)
│   ├── clickhouse/
│   │   └── init.sql            # positions table, ship_latest, hourly_stats views
│   └── grafana/
│       └── provisioning/       # datasource + dashboard auto-provisioning
│
├── ism/                        # ISM band IoT device monitoring pipeline
│   ├── docker-compose.yml      # rtl_433 + ClickHouse + Grafana
│   ├── Dockerfile.ingest       # rtl_433 + Python in single container
│   ├── entrypoint.sh           # pipes rtl_433 JSON stdout → ism_ingest.py
│   ├── ism_ingest.py           # JSON line reader → ClickHouse batch inserter
│   ├── clickhouse/
│   │   └── init.sql            # events table, device_latest, hourly_stats views
│   └── grafana/
│       └── provisioning/       # datasource + dashboard auto-provisioning
│
├── scripts/
│   ├── spectrum-scan.sh        # rtl_power wideband scanning with band presets
│   ├── satellite-pass.sh       # NOAA/Meteor pass recording with rtl_fm
│   ├── ism-monitor.sh          # rtl_433 ISM device decoder (433 MHz)
│   ├── ais-monitor.sh          # AIS ship tracking (161/162 MHz)
│   └── airband-listen.sh       # Athens ATC listener (AM airband)
│
├── config/                     # tool configs (SDR++ settings, etc.)
├── recordings/                 # IQ recordings, scan CSVs, decoded images
└── notes/                      # signal identification logs
```

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
# The ADS-B docker-compose.yml is pre-configured for this (host.docker.internal:1234)
```

### Approach B: usbipd (full Linux USB passthrough)
- Attaches USB device to WSL over IP — all tools run natively in WSL
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

- [ ] **Run bootstrap**: `bash bootstrap.sh` — organizes flat files into directory structure, kills Zone.Identifier files, sets permissions, git inits
- [ ] **Install WSL packages**: `bash setup/install-wsl.sh` — installs rtl-sdr, GNU Radio, multimon-ng, rtl_433, sox, satellite tools. Note any failures for manual follow-up.
- [ ] **Windows setup**: follow `setup/install-windows.md`:
  - Download Zadig (zadig.akeo.ie)
  - Download SDR++ (github.com/AlexandreRouma/SDRPlusPlus/releases) — Windows x64 zip
  - Download rtl-sdr Windows binaries (for rtl_tcp.exe): ftp.osmocom.org/binaries/windows/rtl-sdr/
  - Optionally: `winget install usbipd` for USB passthrough

### Phase 2: Dongle Arrives — First Contact

- [ ] **Driver swap**: plug in dongle → open Zadig → Options → List All Devices → select "Bulk-In, Interface (Interface 0)" → target WinUSB → Replace Driver
- [ ] **First signal**: open SDR++ → Source: RTL-SDR → sample rate 2.048 MHz → gain 30 dB → tune to ~100 MHz → hear FM radio
- [ ] **Gain calibration**: start at 0 dB, increase by 5 until signal-to-noise peaks. Sweet spot usually 28-42 dB. If ghost signals appear, gain is too high.
- [ ] **Explore the spectrum**: slowly scroll from 80 MHz upward in SDR++. Use sigidwiki.com to identify unknown signals.

### Phase 3: ADS-B Pipeline

- [ ] **Start rtl_tcp on Windows**: `rtl_tcp.exe -a 0.0.0.0 -p 1234 -s 2048000`
- [ ] **Set antenna**: dipole arms ~6.5 cm each, vertical, at window or patio
- [ ] **Launch stack**: `cd adsb && docker compose up -d`
- [ ] **Verify**: open http://localhost:8080 (tar1090 map) — aircraft should appear within minutes
- [ ] **Check Grafana**: open http://localhost:3000 (admin/admin) — ClickHouse datasource should be auto-provisioned, dashboard available under "ADS-B Dashboards"
- [ ] **Monitor ingest**: `docker compose logs -f adsb-ingest` — should see batch flush messages
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
3. Set demod to USB (upper sideband) — wider AM also works
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
Athens Approach     118.575 MHz        ATC (AM demod) — may hear from desk
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

Formula: **arm length (cm) = 7125 / frequency (MHz)** — this gives quarter wavelength per dipole arm.

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
  ├── aircraft_hourly (materialized view — uniq aircraft, avg altitude)
  └── aircraft_latest (materialized view — last known state per hex_ident)
        └→ Grafana (:3000)
             └── adsb-overview dashboard (auto-provisioned)
                 • Aircraft count (live + over time)
                 • Message rate
                 • Altitude histogram
                 • Recent flights table
                 • Altitude traces for top aircraft
```

**Key design decisions:**
- ClickHouse over Postgres/VictoriaMetrics because ADS-B is high-cardinality time series with analytical queries (GROUP BY hex_ident, altitude histograms, position aggregations) — exactly ClickHouse's sweet spot
- ingest.py is stdlib-only Python (no dependencies) to keep the Docker image tiny and the code obvious
- SBS BaseStation format chosen over Beast binary because it's human-readable CSV, easy to debug and parse
- Materialized views handle rollups at write time so dashboards query pre-aggregated data

## AIS Pipeline Architecture

```
RTL-SDR (161.975 + 162.025 MHz, via rtl_tcp on Windows)
  └→ AIS-catcher (Docker, dual-channel decoder)
       └→ NMEA sentences (UDP :10110) → ais_ingest.py → ClickHouse

ClickHouse (ais database, :8124/:9001)
  ├── positions table (MergeTree, partitioned by day, 90-day TTL)
  ├── hourly_stats (materialized view — uniq ships, avg speed)
  └── ship_latest (materialized view — last known state per MMSI)
        └→ Grafana (:3001)
             └── ais-overview dashboard (auto-provisioned)
                 • Ship count (live + over time)
                 • Message rate
                 • Ship types bar chart
                 • Recent ships table
                 • Speed distribution + traces
```

**Key design decisions:**
- AIS-catcher over rtl_ais because it supports rtl_tcp input (rtl_ais requires direct USB)
- Custom stdlib-only AIVDM decoder (ais_decoder.py) — 6-bit dearmoring, msg types 1-3/5/18/24, multi-sentence reassembly
- UDP transport from decoder to ingest — one NMEA sentence per datagram, no framing needed
- ship_latest view merges position data (types 1-3, 18) with identity data (types 5, 24) via argMax

## ISM Pipeline Architecture

```
RTL-SDR (433.92 MHz, via rtl_tcp on Windows)
  └→ rtl_433 (in Docker, protocol decoder)
       └→ JSON stdout (pipe) → ism_ingest.py → ClickHouse

ClickHouse (ism database, :8125/:9002)
  ├── events table (MergeTree, partitioned by day, 180-day TTL)
  ├── hourly_stats (materialized view — uniq devices, avg temperature)
  └── device_latest (materialized view — last reading per device)
        └→ Grafana (:3002)
             └── ism-overview dashboard (auto-provisioned)
                 • Active devices + event rate
                 • Device types bar chart
                 • Temperature + humidity traces
                 • Recent events table
```

**Key design decisions:**
- rtl_433 and Python ingest in a single container — stdout pipe is the simplest IPC
- 180-day TTL (vs 90 for ADS-B/AIS) — ISM data is interesting for seasonal device patterns
- raw_json column stores full rtl_433 output — covers all 200+ protocols without explicit field mapping

## Port Allocation

| Pipeline | ClickHouse HTTP | ClickHouse Native | Grafana | Extra |
|----------|----------------|-------------------|---------|-------|
| ADS-B    | 8123           | 9000              | 3000    | tar1090: 8080 |
| AIS      | 8124           | 9001              | 3001    | — |
| ISM      | 8125           | 9002              | 3002    | — |

Only one pipeline can use rtl_tcp at a time (single dongle).

---

## Common Pitfalls & Debugging

**FM broadcast overload**: Athens has powerful FM transmitters on Lycabettus and Hymettus. Symptoms: phantom signals across the spectrum, signals that move when you retune. Fix: reduce gain to 20-25 dB, or get an FM notch filter (band-stop at 88-108 MHz).

**Gain too high**: if you see weak "ghost" copies of strong signals at unexpected frequencies, that's intermodulation from excessive gain. The RTL-SDR's 8-bit ADC saturates easily. Always start low (20 dB) and increase.

**Stone wall attenuation**: anything above ~300 MHz is significantly weakened by the thick walls. For ADS-B, AIS, ISM: get the antenna to the window or outside. A 3-5m USB extension cable with ferrite chokes lets you keep the laptop at your desk.

**Direct sampling sensitivity**: HF mode (for number stations, shortwave) has much lower sensitivity than normal mode. Use the longest wire antenna possible, and try at night when ionospheric propagation is stronger.

**Sample drops**: if rtl_power or rtl_fm report "samples lost" or audio glitches, reduce sample rate to 1.024 MS/s. USB 2.0 bandwidth and usbipd overhead can cause this.

**Docker networking**: `host.docker.internal` resolves to the Windows host from within Docker containers on WSL. If rtl_tcp is running on Windows, containers can reach it at `host.docker.internal:1234`. If this doesn't work, find the Windows IP with `ip route show default` in WSL and use that.

**rtl_tcp connection refused**: check Windows Firewall — it may block rtl_tcp. Allow it through, or use `127.0.0.1` instead of `0.0.0.0` if only connecting from the same machine.

---

## Coding Conventions

- Shell scripts: **bash** (not fish) with `set -euo pipefail` — fish is the interactive shell but scripts need portability
- Python: 3.11+, prefer stdlib, minimal external deps. Type hints welcome but not required.
- Docker: always pin image tags to specific versions, never use `latest`
- Data formats: JSON Lines (`.jsonl`) for streaming data, CSV for scan results
- Comments: explain RF/SDP concepts inline — this is a learning project, not a production codebase
- File naming: lowercase, hyphens (not underscores) for scripts
- Configs: YAML for Docker/Grafana, SQL for ClickHouse

## When Helping With This Project

- Connect RF concepts to audio/DSP analogies where possible
- If a script needs direct USB access, say so explicitly and mention the rtl_tcp alternative
- For antenna questions: always include the arm length calculation and orientation
- For new signals: reference sigidwiki.com for identification
- For ADS-B pipeline changes: maintain the readsb → ClickHouse → Grafana architecture
- When suggesting new tools: prefer packages in Tumbleweed repos, fall back to source builds
- Legal: listening is legal in Greece (as in most EU countries). Decoding encrypted comms is not. TETRA and some digital services are encrypted — note this when relevant.
