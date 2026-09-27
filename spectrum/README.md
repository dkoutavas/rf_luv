# Spectrum Scanner

Wideband RF spectrum scanner: sweeps 88-470 MHz, detects peaks and transients, builds hourly baselines.

```
RTL-SDR (via rtl_tcp) -> scanner.py (FFT) -> scan_ingest.py (JSON) -> ClickHouse -> Grafana
```

## What the scanner actually does

`rtl_power` - the usual tool for wideband sweeping - only speaks direct USB, so `scanner.py` is a custom Python `rtl_tcp` client that replaces it. Both components are intentionally thin: numpy for DSP, stdlib HTTP for ClickHouse.

**DSP pipeline** - for each tuning step: read IQ bytes → convert unsigned-8-bit to complex baseband → Hann window → FFT → take `|X|²` to get linear power → average N=8 captures in the **linear** domain (averaging dB underestimates bursty signals, same as RMS vs. average in audio) → convert to dBFS → downsample FFT bins into 100 kHz output bins.

**Scheduler** - two presets share the dongle: a full 88–470 MHz sweep every ~280 s and an airband 118–137 MHz sweep every 60 s. The loop picks whichever preset is most overdue, reconnects to `rtl_tcp` for every sweep (to flush stale TCP buffers), and discards an initial 128 KB to let the tuner PLL settle after large frequency jumps.

**Signal intelligence** - two detectors run on every sweep:
- *Peaks*: bins ≥10 dB above the average of their ±5 neighbors (spectral prominence, like peak-picking in an audio analyzer).
- *Transients*: ≥15 dB delta vs. the same bin in the previous full sweep (edge detection in frequency space, marked `appeared` / `disappeared`).

**Data-quality guards** - raw IQ is checked for ADC clipping (samples pinned at 0 or 255); >5 % clipping triggers a gain reduction with a configurable floor (`SCAN_GAIN_MIN`). DVB-T range (174–230 MHz) is excluded from the sweep-health max-power calc since strong local transmitters there are expected.

**Output contract** - one JSON line per bin, plus separate lines for peaks, transient events, sweep health, and run-start / run-update / run-end markers. `scan_ingest.py` reads the stream, routes each message type to its table, and batch-inserts on size-or-interval.

## Signal autopsy (blind analyzer)

The scanner only ever stores FFT *power* - it can locate a signal in frequency but not tell you *what it is*. `analysis/blind_analyze.py` (feature D3) closes that gap: it eats a `.cs8` raw-IQ capture (produced by D2's `iq_capture.py`, results table `spectrum.iq_captures`) and prints an "identity card" - occupied bandwidth, modulation family (CW / AM / WFM / NFM / OOK / 2FSK / 4FSK / BPSK / QPSK-PSK / OFDM / noise / digital-unknown), symbol/baud rate, carrier offset, and an OFDM flag - with **zero prior knowledge** of the emitter. It is rule-based blind signal analysis: a Welch PSD for occupied bandwidth, the FFT-based analytic signal for instantaneous amplitude/phase/frequency discriminants, 2nd/4th-order cumulants to separate PSK from AM/FM/noise, and the squaring-trick spectral line (a cyclostationary estimate, cross-checked against envelope autocorrelation) for baud. Run it with `--file <path.cs8>` (fully offline; reads the `.json` sidecar for sample rate / center freq) or `--capture-id <uuid>` (looks the path up in `iq_captures`). Each run prints a numbered reasoning trace and, unless `--dry-run`, writes one `spectrum.blind_signal_features` row (migration 024). Structure mirrors `analysis/detect_compression.py`: a numpy-only feature library plus a main that writes its own table.

**Honest limits.** Correctness is gated only on *clean* rect-pulse synthetics; the classifier is rule-based and makes no claims about low-SNR, fading, or pulse-shaped robustness - real captures may legitimately land in `digital-unknown` / low-confidence. The 2.048 MS/s dongle ceiling caps observable baud well under ~1 Msym/s: fine for NFM / FSK / paging / marine-data, but wideband OFDM / DVB-T only *partially* fits one capture span, so those verdicts are emitted low-confidence and flagged as such in the reasoning. Baud, OFDM `Tu`, and subcarrier spacing are `Nullable` and come back NULL when the estimate does not clear its confidence bar (the honest-NULL posture of `compression_events`).

**Legal stance.** Characterizing a waveform's modulation and baud is analysis of *physics* and extracts no message content - it works on encrypted carriers without ever touching payload. Digital signals that fire no known family are labeled `digital-unknown` with the fixed note *"digital, likely encrypted -> out of scope; payload will not be decoded"*; decode is never attempted.

**Roadmap - D3.1 (not built here).** A follow-up will wire blind verdicts into the classifier via a new `modulation_in` evidence axis in `signal_classes.evidence_rules`: a small `latest_modulation_for(freq_hz)` helper plus a few lines in `classifier.score_class` rewarding a class whose expected modulation matches the observed family, weighted by confidence. That needs its own migration + classifier re-baseline, so it deliberately does **not** ride along with D3.

## Setup

The scanner runs natively under systemd, one `rtl-tcp@<serial>` plus one
`rtl-scanner@<serial>` user unit per dongle, writing into the shared ClickHouse
on `127.0.0.1:8123`. There is no containerized scanner.

1. Bring up the shared data layer (once). The infra `ch-bootstrap` one-shot
   creates the `spectrum` database, applies the schema, and loads the Athens
   known-frequencies seed.
   ```bash
   docker network create rf_luv_net
   bash ../infra/up.sh
   ```
2. Install the host side: DVB blacklist, udev rule, units, env files, backups.
   ```bash
   bash ../ops/install-host.sh --scanner v4-01 --gain 12 --backup-dir /data/rf-clickhouse-backups
   ```
   Replace `v4-01` with the serial of the dongle that should scan. `--verify-only`
   prints a PASS/FAIL check.
3. Install the reliability layer: escalator, freshness and signal-quality
   probes, desktop or ntfy alerts.
   ```bash
   bash ../ops/install-trip-hardening.sh
   ```
4. Open Grafana at <http://localhost:3000> (admin/admin), Spectrum folder. The
   first full sweep lands within a minute.

`../RESTORE.md` has the full ordered runbook, and the `Reliability Stack`
section of the top-level `CLAUDE.md` explains the layers.

## Configuration

Each dongle's scanner reads `/etc/rtl-scanner/<serial>.env`, created by
`install-host.sh` from [`../ops/rtl-scanner/env.v4-01.example`](../ops/rtl-scanner/env.v4-01.example).
Edit it, then `systemctl --user restart rtl-scanner@<serial>`; each restart opens
a new run in `scan_runs` stamped with the antenna fields. Full guide:
[`docs/CUSTOMIZE.md`](docs/CUSTOMIZE.md).

The most commonly changed variables:

| Variable | Example | When to change |
|---|---|---|
| `SCAN_DONGLE_ID` | `v3-01` | Match the dongle's EEPROM serial |
| `SCAN_GAIN` | `12` | Lower if sweeps clip; the scanner also steps it down itself on clipping. 12 needs an FM bandstop on an outdoor antenna; without one, 7.7 or less |
| `SCAN_FREQ_START`/`SCAN_FREQ_END` | `88000000`/`470000000` | Different band of interest |
| `SCAN_SETTLE_BYTES` | `786432` | Bytes dropped after each retune (192 ms); raise it if a slower dongle smears |
| `SCAN_ANTENNA_*` | patio position, 67 cm arms | Whenever the antenna moves, so runs can be told apart |
| `SCAN_DVBT_EXCLUDE_*` | `174..230 MHz` | DVB-T not in Band III at your location |

Internal pipeline knobs (FFT size, batch size) are env-overridable too; see
`spectrum/config.py` for the shared defaults the batch jobs use.

## Ports

Spectrum has no ports of its own. It uses the shared data layer:

| Service | Port | Description |
|---------|------|-------------|
| Grafana | 3000 | Dashboards (admin/admin), Spectrum folder (default datasource) |
| ClickHouse HTTP | 8123 | Query API (used by export scripts), `spectrum` database |
| ClickHouse Native | 9000 | Used by the Grafana datasource |

The old per-pipeline spectrum ports (Grafana 3003, ClickHouse 8126/9003) are
retired.

## Classifier reference tables

Two read-only lookup tables seeded by migration `003_add_classifier_tables.sql`:

- `allocations` - regulatory / observed frequency ranges (`freq_start_hz`, `freq_end_hz`, `service`, `region`, `source`, `notes`). Covers 87.5 MHz–446.2 MHz with Greek/EU priors plus local observations. Use with a range lookup (`WHERE freq_start_hz <= X AND freq_end_hz > X`).
- `signal_classes` - canonical feature signatures for a forthcoming rule-based classifier (`class_id`, `bw_min_hz`, `bw_max_hz`, `modulation`, `duty_pattern`, burst durations, `evidence_rules` JSON). Loosely matched by `known_frequencies.class_id` and `listening_log.class_id`; no FK enforcement.

## Batch jobs (systemd-deployed on the local host)

Three Python scripts run on a 5-minute cadence as systemd user timers - they read from the ingest tables, compute features / classifications / health diagnostics, and write back. They're not part of the live ingest path; the scanner+ingest pipeline runs without them.

| Script | Reads | Writes | Cadence |
|---|---|---|---|
| `feature_extractor.py` | `peaks`, `scans`, `sweep_health`, `allocations` | `peak_features` | 5 min |
| `classifier.py` | `peak_features`, `known_frequencies`, `allocations`, `signal_classes`, `listening_log` | `signal_classifications` | 5 min |
| `classifier_health.py` | `signal_classifications`, baselines | `classifier_health` | 5 min |

They install as `spectrum-features.{service,timer}`, `spectrum-classifier.{service,timer}`, `spectrum-classifier-health.{service,timer}` under `~/.config/systemd/user/`, with `ExecStart=/usr/bin/python3 %h/dev/rf_luv/spectrum/<file>.py` (Tumbleweed's `python3` is 3.13). Status: `systemctl --user list-timers | grep spectrum-`.

A fourth, `analysis/detect_compression.py`, is a one-shot/backfill tool - runnable manually for archaeology, no production timer.

## Helper Scripts

- `export-data.sh` - export scan data from ClickHouse to CSV/markdown reports
- `investigate-freqs.sh` - generate frequency investigation checklist from detected peaks

Both scripts connect to the shared ClickHouse at `localhost:8123` by default.

## Troubleshooting

**`usb_claim_interface error -6`** - DVB kernel module is claiming the dongle. Blacklist it (see USB setup above), then unplug and replug the dongle.

**`usb_open error -3`** - udev rule missing or permissions wrong. Check `/etc/udev/rules.d/20-rtlsdr.rules` and reload.

**No data in Grafana** - check scanner logs: `journalctl --user -u rtl-scanner@<serial>`. Common causes: rtl_tcp not running, wrong host/port, firewall blocking 1234.

**Connection refused to rtl_tcp** - either rtl_tcp isn't running, or it's bound to `127.0.0.1` instead of `0.0.0.0`. Containers need to reach it via the Docker bridge, so bind to `0.0.0.0`.

**One consumer per dongle** - `rtl_tcp` serves one client, and the scanner holds its connection for the whole run. To lend a dongle to SDR++, `ops/rf-mode pause <serial>`, then `ops/rf-mode scan <serial>` afterwards. `pipeline.sh up <pipe> <serial>` and `down` do the same for Docker decoders.
