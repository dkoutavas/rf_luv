# Spectrum Scanner

Wideband RF spectrum scanner: sweeps 88-470 MHz, detects peaks and transients, builds hourly baselines.

```
RTL-SDR (via rtl_tcp) -> scanner.py (FFT) -> scan_ingest.py (JSON) -> ClickHouse -> Grafana
```

## What the scanner actually does

`rtl_power` — the usual tool for wideband sweeping — only speaks direct USB, so `scanner.py` is a custom Python `rtl_tcp` client that replaces it. Both components are intentionally thin: numpy for DSP, stdlib HTTP for ClickHouse.

**DSP pipeline** — for each tuning step: read IQ bytes → convert unsigned-8-bit to complex baseband → Hann window → FFT → take `|X|²` to get linear power → average N=8 captures in the **linear** domain (averaging dB underestimates bursty signals, same as RMS vs. average in audio) → convert to dBFS → downsample FFT bins into 100 kHz output bins.

**Scheduler** — two presets share the dongle: a full 88–470 MHz sweep every ~280 s and an airband 118–137 MHz sweep every 60 s. The loop picks whichever preset is most overdue, reconnects to `rtl_tcp` for every sweep (to flush stale TCP buffers), and discards an initial 128 KB to let the tuner PLL settle after large frequency jumps.

**Signal intelligence** — two detectors run on every sweep:
- *Peaks*: bins ≥10 dB above the average of their ±5 neighbors (spectral prominence, like peak-picking in an audio analyzer).
- *Transients*: ≥15 dB delta vs. the same bin in the previous full sweep (edge detection in frequency space, marked `appeared` / `disappeared`).

**Data-quality guards** — raw IQ is checked for ADC clipping (samples pinned at 0 or 255); >5 % clipping triggers a gain reduction with a configurable floor (`SCAN_GAIN_MIN`). DVB-T range (174–230 MHz) is excluded from the sweep-health max-power calc since strong local transmitters there are expected.

**Output contract** — one JSON line per bin, plus separate lines for peaks, transient events, sweep health, and run-start / run-update / run-end markers. `scan_ingest.py` reads the stream, routes each message type to its table, and batch-inserts on size-or-interval.

## Setup

### Option A: Native Linux

Tested on openSUSE Leap 15.6. Should work on any distro with Docker Engine 20.10+.

#### Prerequisites

- Docker Engine 20.10+ and Docker Compose v2
- rtl-sdr tools: `rtl_tcp`, `rtl_test`
  - openSUSE: `sudo zypper install rtl-sdr`
  - Debian/Ubuntu: `sudo apt install rtl-sdr`

#### USB setup (one-time)

1. Blacklist the DVB kernel modules that claim the dongle:

```bash
sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf << 'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null  # or reboot
```

2. Add udev rule for non-root USB access:

```bash
sudo tee /etc/udev/rules.d/20-rtlsdr.rules << 'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE="0666"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

3. Verify:

```bash
rtl_test -t
# Should show: Found 1 device(s), Realtek RTL2838, R820T/R860 tuner
```

#### Start the pipeline

```bash
# 1. Start rtl_tcp on the host
rtl_tcp -a 0.0.0.0 -p 1234 -s 2048000 &

# 2. Start the containers
docker compose up -d

# 3. Open Grafana
# http://localhost:3003 (admin/admin)
```

#### How containers reach rtl_tcp

The `docker-compose.yml` uses `extra_hosts` to map `host.docker.internal` to the host's Docker bridge IP (typically 172.17.0.1). This works on Docker Engine 20.10+ — no code changes needed vs the WSL setup.

### Option B: WSL / Windows

1. Swap the RTL-SDR driver with Zadig (see `setup/install-windows.md`)
2. Start rtl_tcp on Windows:
   ```
   rtl_tcp.exe -a 0.0.0.0 -p 1234 -s 2048000
   ```
3. In WSL: `docker compose up -d`

`host.docker.internal` resolves to the Windows host automatically in WSL Docker.

## Environment Variables

All set in `docker-compose.yml` under the `spectrum-scanner` service:

| Variable | Default | Description |
|----------|---------|-------------|
| `RTL_TCP_HOST` | `host.docker.internal` | rtl_tcp server address |
| `RTL_TCP_PORT` | `1234` | rtl_tcp server port |
| `SCAN_FREQ_START` | `88000000` | Sweep start frequency (Hz) |
| `SCAN_FREQ_END` | `470000000` | Sweep end frequency (Hz) |
| `SCAN_BIN_WIDTH` | `100000` | Frequency bin width (Hz) |
| `SCAN_GAIN` | `12` | Tuner gain (dB) |
| `SCAN_FFT_SIZE` | `1024` | FFT window size |
| `SCAN_SAMPLE_RATE` | `2048000` | Sample rate (S/s) |
| `SCAN_INTERVAL_SECONDS` | `280` | Full sweep interval |
| `SCAN_AIRBAND_INTERVAL` | `60` | Airband-only sweep interval |
| `SCAN_PEAK_THRESHOLD` | `10` | Peak detection threshold (dB above neighbors) |
| `SCAN_TRANSIENT_THRESHOLD` | `15` | Transient detection threshold (dB change) |
| `SCAN_ANTENNA_POSITION` | — | Free-text position label |
| `SCAN_ANTENNA_ARMS_CM` | — | Dipole arm length for metadata |
| `SCAN_ANTENNA_ORIENTATION` | — | Antenna bearing (degrees) |
| `SCAN_ANTENNA_HEIGHT_M` | — | Antenna height for metadata |

## Ports

| Service | Port | Description |
|---------|------|-------------|
| Grafana | 3003 | Dashboards (admin/admin) |
| ClickHouse HTTP | 8126 | Query API (used by export scripts) |
| ClickHouse Native | 9003 | Used by Grafana datasource |

## Classifier reference tables

Two read-only lookup tables seeded by migration `003_add_classifier_tables.sql`:

- `allocations` — regulatory / observed frequency ranges (`freq_start_hz`, `freq_end_hz`, `service`, `region`, `source`, `notes`). Covers 87.5 MHz–446.2 MHz with Greek/EU priors plus local observations. Use with a range lookup (`WHERE freq_start_hz <= X AND freq_end_hz > X`).
- `signal_classes` — canonical feature signatures for a forthcoming rule-based classifier (`class_id`, `bw_min_hz`, `bw_max_hz`, `modulation`, `duty_pattern`, burst durations, `evidence_rules` JSON). Loosely matched by `known_frequencies.class_id` and `listening_log.class_id`; no FK enforcement.

## Helper Scripts

- `export-data.sh` — export scan data from ClickHouse to CSV/markdown reports
- `investigate-freqs.sh` — generate frequency investigation checklist from detected peaks

Both scripts connect to ClickHouse at `localhost:8126` by default.

## Troubleshooting

**`usb_claim_interface error -6`** — DVB kernel module is claiming the dongle. Blacklist it (see USB setup above), then unplug and replug the dongle.

**`usb_open error -3`** — udev rule missing or permissions wrong. Check `/etc/udev/rules.d/20-rtlsdr.rules` and reload.

**No data in Grafana** — check scanner logs: `docker compose logs -f spectrum-scanner`. Common causes: rtl_tcp not running, wrong host/port, firewall blocking 1234.

**Connection refused to rtl_tcp** — either rtl_tcp isn't running, or it's bound to `127.0.0.1` instead of `0.0.0.0`. Containers need to reach it via the Docker bridge, so bind to `0.0.0.0`.

**Only one pipeline at a time** — the RTL-SDR dongle is single-client. Stop other pipelines (adsb, ais, ism) before starting the spectrum scanner.
