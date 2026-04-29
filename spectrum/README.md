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

Two deployment paths — pick one based on what you need:

| Path | Best for | Quick start |
|---|---|---|
| **Docker, all-in-one** | Trying it out, single host, no production reliability | `docker compose --profile scanner up -d` |
| **systemd watchdog stack** | Unattended production, multi-dongle, USB recovery, ntfy alerts | `bash ../ops/rtl-tcp/install.sh` |

The Docker path runs `scanner.py` + `scan_ingest.py` in a container alongside ClickHouse / Grafana / nginx-logging. The systemd path runs the same Python on bare metal under `rtl-tcp@<serial>` + `rtl-scanner@<serial>` user units, with a 30s watchdog (`../ops/rtl-tcp/`), root-level escalator past circuit breaker (`../ops/rtl-tcp/rtl-tcp-escalator.py`), ClickHouse-freshness probe (`../ops/spectrum-monitor/`), and ntfy.sh alerts (`../ops/notify/`). ClickHouse and Grafana stay containerized in both paths.

### Option A: Docker quick start (Linux / WSL / macOS)

Prerequisites:
- Docker Engine 20.10+ and Docker Compose v2
- rtl-sdr tools: `rtl_tcp`, `rtl_test`
  - openSUSE: `sudo zypper install rtl-sdr`
  - Debian/Ubuntu: `sudo apt install rtl-sdr`
- Linux only — DVB blacklist + udev rule (one-time):
  ```bash
  sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf << 'EOF'
  blacklist dvb_usb_rtl28xxu
  blacklist rtl2832
  blacklist rtl2830
  EOF
  sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null  # or reboot
  sudo tee /etc/udev/rules.d/20-rtlsdr.rules << 'EOF'
  SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", MODE="0666"
  EOF
  sudo udevadm control --reload-rules && sudo udevadm trigger
  ```
  Verify: `rtl_test -t` reports `Realtek RTL2838, R820T/R860 tuner`.

- Windows / WSL: swap the dongle driver with Zadig per [`../setup/install-windows.md`](../setup/install-windows.md).

Bring it up:

```bash
# 1. Start rtl_tcp on the host (Linux)
rtl_tcp -a 0.0.0.0 -p 1234 -s 2048000 &
#    or on Windows
# rtl_tcp.exe -a 0.0.0.0 -p 1234 -s 2048000

# 2. Optional but recommended: copy the env template and tune for your location
cp .env.example .env  # then edit .env (see docs/CUSTOMIZE.md)

# 3. Start the full stack — note the --profile scanner flag
docker compose --profile scanner up -d

# 4. Open Grafana at http://localhost:3003 (admin/admin)
```

The `--profile scanner` flag is required because the scanner service is gated — without it, `docker compose up -d` brings up only ClickHouse + Grafana + nginx-logging (useful for production hosts where the scanner runs natively via systemd).

`docker-compose.yml` uses `extra_hosts: host.docker.internal:host-gateway` so containers reach a host-side `rtl_tcp`. This works on Linux Docker 20.10+, WSL2 Docker, and Docker Desktop without further config.

### Option B: systemd watchdog stack (production)

Used on the project's `leap` host. Adds USB recovery, freshness monitoring, alerting:

```bash
bash ../ops/rtl-tcp/install.sh           # systemd user units + watchdog
bash ../ops/install-trip-hardening.sh    # root escalator + freshness + ntfy
```

ClickHouse and Grafana still run via `docker compose up -d` (without `--profile scanner`). See `../ops/rtl-tcp/install.sh` and the `Reliability Stack on Leap` section in the top-level `../CLAUDE.md` for details.

## Configuration

All knobs are env vars; copy `.env.example` to `.env` and edit. Compose auto-reads `.env` for `${VAR}` substitution. Full guide: [`docs/CUSTOMIZE.md`](docs/CUSTOMIZE.md) walks through dongle serial, antenna metadata, location-specific known_frequencies, sweep band, and remote rtl_tcp.

The most commonly-overridden vars:

| Variable | Default | When to change |
|---|---|---|
| `SCAN_DONGLE_ID` | `v3-01` | Match your EEPROM serial (`rtl_eeprom -d 0`) |
| `SCAN_GAIN` | `12` | Adapt to your RF environment (auto-reduces on clipping) |
| `SCAN_FREQ_START`/`SCAN_FREQ_END` | `88000000`/`470000000` | Different band of interest |
| `RTL_TCP_HOST` | `host.docker.internal` | rtl_tcp on a different machine |
| `SCAN_DVBT_EXCLUDE_*` | `174..230 MHz` | DVB-T not in Band III at your location |
| `CLICKHOUSE_PASSWORD` | `spectrum_local` | Before exposing port 8126 publicly |
| `GF_SECURITY_ADMIN_PASSWORD` | `admin` | Before exposing Grafana publicly |

The full list of overrideable vars is in `.env.example`. Internal pipeline knobs (FFT size, batch size, etc.) are all env-overridable too but live alongside the production-tuned defaults — see `spectrum/config.py` for shared defaults consumed by the batch jobs.

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

## Batch jobs (systemd-deployed on the production host)

Three Python scripts run on a 5-minute cadence as systemd user timers — they read from the ingest tables, compute features / classifications / health diagnostics, and write back. They're not part of the live ingest path; the scanner+ingest pipeline runs without them.

| Script | Reads | Writes | Cadence |
|---|---|---|---|
| `feature_extractor.py` | `peaks`, `scans`, `sweep_health`, `allocations` | `peak_features` | 5 min |
| `classifier.py` | `peak_features`, `known_frequencies`, `allocations`, `signal_classes`, `listening_log` | `signal_classifications` | 5 min |
| `classifier_health.py` | `signal_classifications`, baselines | `classifier_health` | 5 min |

On leap they're deployed as `spectrum-features.{service,timer}`, `spectrum-classifier.{service,timer}`, `spectrum-classifier-health.{service,timer}` under `~/.config/systemd/user/`, with `ExecStart=/usr/bin/python3.11 %h/dev/rf_luv/spectrum/<file>.py`. Status: `systemctl --user list-timers | grep spectrum-`.

A fourth, `analysis/detect_compression.py`, is a one-shot/backfill tool — runnable manually for archaeology, no production timer.

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
