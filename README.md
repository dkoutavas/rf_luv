# RTL-SDR Lab

Personal RTL-SDR radio exploration project. RTL-SDR Blog V3 (R860/RTL2832U) based in Athens, Greece.

## Quick Start

### 1. Install WSL dependencies

```bash
bash setup/install-wsl.sh
```

### 2. Windows setup

Follow [setup/install-windows.md](setup/install-windows.md) — install Zadig, SDR++, and optionally usbipd.

### 3. When the dongle arrives

1. Plug it in → run Zadig → replace driver with WinUSB
2. Open SDR++ → select RTL-SDR source → tune to ~100 MHz
3. You should hear FM radio. Adjust gain until signal is clear without noise floor rising too much.

### 4. ADS-B pipeline

```bash
# Option A: rtl_tcp bridge (run on Windows first)
# rtl_tcp -a 0.0.0.0 -p 1234 -s 2048000

# Then in WSL:
cd adsb
docker compose up -d

# Open:
#   http://localhost:8080  — live aircraft map (tar1090)
#   http://localhost:3000  — Grafana dashboards (admin/admin)
```

### 5. AIS ship tracking

```bash
# Start rtl_tcp on Windows first, then:
cd ais
docker compose up -d

# Open:
#   http://localhost:3001  — Grafana dashboards (admin/admin)
# Antenna: dipole arms ~44 cm, vertical, pointed SW toward Piraeus
```

### 6. ISM band monitoring

```bash
# Start rtl_tcp on Windows first, then:
cd ism
docker compose up -d

# Open:
#   http://localhost:3002  — Grafana dashboards (admin/admin)
# Antenna: dipole arms ~16.4 cm, vertical, at window
```

### 7. Spectrum survey

```bash
# Requires direct USB access (usbipd)
bash scripts/spectrum-scan.sh          # VHF/UHF overview
bash scripts/spectrum-scan.sh fm       # FM band only
bash scripts/spectrum-scan.sh full     # 24 MHz — 1.7 GHz
```

**Note:** only one pipeline can use the dongle at a time. Stop one before starting another.

## Project Structure

```
setup/              → installation scripts and guides
adsb/               → ADS-B tracking pipeline (Docker Compose)
ais/                → AIS ship tracking pipeline (Docker Compose)
ism/                → ISM band IoT monitoring pipeline (Docker Compose)
scripts/            → utility scripts for scanning, recording
config/             → SDR++ and tool configurations
recordings/         → IQ recordings, scan CSVs, decoded images
notes/              → signal identification notes
CLAUDE.md           → Claude Code project context
```

## Antenna Quick Reference

| Target          | Frequency   | Dipole Arm | Notes                        |
|-----------------|-------------|------------|------------------------------|
| FM Radio        | ~100 MHz    | 75 cm      | Indoor, vertical             |
| NOAA Satellites | ~137 MHz    | 53 cm      | Patio, V-dipole 120°         |
| AIS Ships       | ~162 MHz    | 45 cm      | Window toward Piraeus        |
| ADS-B Planes    | 1090 MHz    | 6.5 cm     | Window/patio, vertical       |
| HF/Shortwave    | 3-30 MHz    | Long wire  | 10-20m wire, direct sampling |

Arm length formula: **7125 / frequency_in_MHz** = arm length in cm
