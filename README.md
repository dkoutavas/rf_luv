# rf_luv — RTL-SDR Radio Lab

What does a "ghost-hunting" spirit box actually receive? FM radio, 150 ms at a
time. This repo has the receiver that proves it, the RDS labels that name every
station fragment, and the forensic tools that measured a hard 800 Hz bandwidth
shelf and near-identical acoustic fingerprints across 40 minutes of published
"investigation" video.

The rest of the repo is the radio lab that made it possible: a 24/7 spectrum
scanner, aircraft messaging, ship tracking, weather satellites and ISM sensors,
all on two 35-EUR USB radio dongles from a window in Athens.

## What you can do with it

- **Run a spectrum scanner** that sweeps 88–470 MHz every five minutes,
  detects peaks, tracks transients, and writes everything to ClickHouse with
  Grafana dashboards.
- **Replicate a spirit box** and label every audio fragment with its source FM
  station, then apply a delay effect and hear the "creepy voice" appear —
  because that is all it is.
- **Run forensic audio analysis** on public video: measure the bandwidth
  shelf, detect reused clips with chromaprint fingerprints, compare reverb
  tails, and correlate EMF-meter events with RF bursts.
- **Decode aircraft messages** (ACARS), **track ships** (AIS), **read weather
  satellites** (NOAA), and **monitor IoT sensors** (ISM 433 MHz) — each as
  its own pipeline with its own ClickHouse database and Grafana folder.

## Where it runs

One laptop in Athens (HP Omen, openSUSE Tumbleweed). Two RTL-SDR Blog USB
dongles: a V4 with an FM bandstop filter for the scanner, and a V3 without one
for the spirit-box work. Docker runs ClickHouse and Grafana on localhost. The
radios run as systemd user services with a watchdog. Daily backups land on a
second internal disk. Nothing is on the internet. GitHub hosts the code and
sample captures only.

## Hardware (~115 EUR)

| Item | What it does | Cost |
|------|--------------|------|
| RTL-SDR Blog V4 | Spectrum scanner (500 kHz – 1.7 GHz, 8-bit, 2 MS/s) | ~45 EUR |
| RTL-SDR Blog V3 | Spirit-box / FM / HF (same range + HF direct sampling) | ~35 EUR |
| FM bandstop filter | Inline SMA, rejects 88–108 MHz on the scanner dongle | ~15 EUR |
| Dipole antenna kit | Telescoping elements, magnetic base, SMA pigtail | ~20 EUR |
| K-II EMF meter | Reproduce the "paranormal" LED response (optional) | ~20 EUR |

Software: Python 3.10+, numpy, Docker, ClickHouse, Grafana. Everything else is
standard library. The one external binary is `fpcalc` (chromaprint) for the
acoustic fingerprinter.

## Get running

Write your dongle serials first (see [RESTORE.md](RESTORE.md) step 3 — one
dongle on the bus at a time, physical replug after each write). Then:

```bash
git clone https://github.com/dkoutavas/rf_luv.git && cd rf_luv
docker network create rf_luv_net && bash infra/up.sh
bash ops/install-host.sh --scanner v4-01 --gain 12 --backup-dir /data/rf-clickhouse-backups
# Two dongles:
# bash ops/install-host.sh --scanner v4-01 --ghost v3-01 --gain 12 --backup-dir /data/rf-clickhouse-backups
```

`install-host.sh` handles the DVB kernel blacklist, the udev rule, the rtl_tcp
units and watchdog, per-dongle config files, and daily backups in one command.
Run `--verify-only` to check everything passes. Open Grafana at
<http://localhost:3000> (admin/admin). The first spectrum sweep completes in
about four minutes.

Full rebuild manual: [RESTORE.md](RESTORE.md). The installer runs on openSUSE,
Debian/Ubuntu, Fedora and Arch (different package manager, same everything
else). A Windows host running `rtl_tcp.exe` is an alternative path documented
in [setup/install-windows.md](setup/install-windows.md).

## The ghost debunk

The `ghost/` pipeline rebuilds three pieces of "paranormal investigation"
equipment from first principles and measures what they actually do.

**Spirit box:** an FM radio that sweeps stations at 150 ms per step with no
squelch. Every "word" is a broadcast fragment. An RDS pre-pass labels each
fragment with its source station. Add a 90 ms slapback delay and the choppy
radio becomes a "creepy voice" — one knob on a delay plugin.

**EMF meter:** a K-II responds to phone GSM bursts, PMR446 handhelds, mains
wiring, and the camera. Its momentary button flickers the LEDs with no field at
all if your thumb pressure is uneven.

**Forensic findings on published video** (three episodes, 19 segments):

- Bandwidth shelf at 633–973 Hz across 40 minutes. Normal camera audio reaches
  16–20 kHz.
- Chromaprint acoustic fingerprint reuse above 0.5 in 90–100% of segment pairs.
  Live room audio does not share fingerprints across segments minutes apart.
- The simplest explanation: a pre-recorded or looped source with very low
  bandwidth, played in the room or mixed in post.

Full write-up (Greek and English): [ghost/REPORT.md](ghost/REPORT.md),
[ghost/REPORT_GR.md](ghost/REPORT_GR.md). Perception blind test:
[ghost/blindtest/](ghost/blindtest/).

## Pipelines

All pipelines share one ClickHouse and one Grafana on localhost. Each pipeline
has its own database, its own Grafana folder, and its own README.

| Pipeline | Status | What it does |
|----------|--------|-------------|
| [spectrum/](spectrum/) | running | Wideband 88–470 MHz scanner, peak and transient detection, signal classifier, hourly baselines |
| [ghost/](ghost/) | validated | Spirit-box replica, RDS labels, forensic audio tools (delay, bandwidth, fingerprint, spectrogram, reverb, EMF sync) |
| [acars/](acars/) | built | ACARS aircraft messages from Athens airport traffic |
| [rds/](rds/) | built | RDS station metadata decoder (PS, PI, RadioText) |
| [noaa/](noaa/) | partial | NOAA / Meteor weather-sat pass scheduler (recorder is a scaffold) |
| [adsb/](adsb/) | companion | ADS-B aircraft tracking with a live map |
| [ais/](ais/) | companion | AIS ship tracking (Piraeus / Saronic Gulf) |
| [ism/](ism/) | companion | ISM 433 MHz sensor and device decoding |

## How it fits together

```
 USB dongles          rtl_tcp (systemd)         Python (numpy + stdlib)       Docker
┌───────────┐        ┌────────────────┐        ┌─────────────────────┐      ┌────────────┐
│ V4 dongle │──USB──▶│ :1234 (scanner)│──TCP──▶│ scanner.py → ingest │─HTTP─▶│ ClickHouse │
│ (notch on)│        └────────────────┘        └─────────────────────┘      │ (8 dbs)    │
│           │                                                                │            │
│ V3 dongle │──USB──▶│ :1235 (ghost)  │──TCP──▶│ spiritbox.py → WAV  │─HTTP─▶│            │
│ (no notch)│        └────────────────┘        └─────────────────────┘      └─────┬──────┘
└───────────┘                                                                      │
                                                                              Grafana :3000
```

The scanner reconnects to rtl_tcp on every sweep to flush stale TCP buffers.
Each dongle is single-client, so only one consumer holds it at a time. On the
V4, rotating decoders (ACARS, ADS-B, AIS, ISM, RDS) time-share with the scanner
through a flock-based coordinator. The V3 is held by the ghost pipeline for the
length of a session.

A layered reliability stack keeps the scanner running unattended: a 30-second
watchdog, a root-level escalator past the circuit breaker (USB reset, xHCI
bounce), ClickHouse freshness and signal-quality probes, and ntfy.sh phone
alerts. Details: [CLAUDE.md](CLAUDE.md) → Reliability Stack.

## Antenna quick reference

Quarter-wave arm length: **arm (cm) = 7125 / frequency (MHz)**.

| Band | Frequency | Arm length | Notes |
|------|-----------|------------|-------|
| FM broadcast | ~100 MHz | 75 cm | Indoor, vertical |
| NOAA satellites | ~137 MHz | 53 cm | Patio, V-dipole at 120° |
| AIS ships | ~162 MHz | 45 cm | Window toward Piraeus |
| ADS-B aircraft | 1090 MHz | 6.5 cm | Window or patio, vertical |
| HF / shortwave | 3–30 MHz | long wire | 10–20 m, direct sampling mode |

Athens note: FM transmitters on Lycabettus and Hymettus are strong enough to
overload the dongle above gain 12. Use the FM bandstop filter on the scanner
dongle and start with low gain.

## Reference

| Document | What it covers |
|----------|---------------|
| [RESTORE.md](RESTORE.md) | Full rebuild from a fresh clone (packages, serials, installer, backups) |
| [QUICKREF.md](QUICKREF.md) | Live-operation cheat sheet (frequencies, commands, troubleshooting) |
| [CLAUDE.md](CLAUDE.md) | Hardware, RF environment, coding conventions, pipeline architecture, reliability stack |
| [ghost/REPORT.md](ghost/REPORT.md) | Mechanism write-up: how the equipment works and what the forensics found |
| [spectrum/README.md](spectrum/README.md) | Scanner setup, DSP pipeline, batch jobs, configuration |

## Legal

Listening to radio is legal in Greece and across the EU. This project does not
decode encrypted content (TETRA and digital voice are excluded). RDS is public
broadcast metadata. No transmission is made. Forensic analysis runs on publicly
available video only.
