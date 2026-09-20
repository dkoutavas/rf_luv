# rf_luv — RTL-SDR Radio Lab

A home radio lab on two USB software-defined radio dongles, based in Athens,
Greece. The station runs a 24/7 spectrum scanner across 88–470 MHz, decodes
aircraft messages and ship positions, schedules weather satellite passes, and
monitors IoT sensors — all writing to a shared ClickHouse database with Grafana
dashboards on localhost.

One of the pipelines (`ghost/`) rebuilds the equipment used in "paranormal
investigation" content and measures what it actually does. That work is
documented in its own [report](ghost/REPORT.md), but the project is the radio
lab, not the debunk.

## What you can do with it

- **Scan the spectrum.** Sweep 88–470 MHz every five minutes, detect peaks,
  track transients, build hourly baselines.
- **Decode aircraft messages.** ACARS from Athens airport traffic, with flight
  and tail tracking.
- **Track ships.** AIS positions from the Saronic Gulf and Piraeus.
- **Schedule weather satellites.** NOAA and Meteor M2 pass predictions with TLE
  refresh (recorder still a scaffold).
- **Monitor IoT sensors.** ISM 433 MHz devices: weather stations, tire sensors,
  doorbells.
- **Replicate a spirit box.** Sweep the FM band, label every fragment with its
  source station via RDS, and run forensic audio analysis on published video.

## Where it runs

One laptop in Athens (HP Omen, openSUSE Tumbleweed). Two RTL-SDR Blog USB
dongles: a V4 with an FM bandstop filter for the scanner, and a V3 without one
for FM-band work. Docker runs ClickHouse and Grafana on localhost. The radios
run as systemd user services with a watchdog. Daily backups land on a second
internal disk. Nothing is on the internet. GitHub hosts the code and sample
captures only.

## Hardware (~115 EUR)

| Item | What it does | Cost |
|------|--------------|------|
| RTL-SDR Blog V4 | Spectrum scanner (500 kHz – 1.7 GHz, 8-bit, 2 MS/s) | ~45 EUR |
| RTL-SDR Blog V3 | FM / ghost / HF (same range + HF direct sampling) | ~35 EUR |
| FM bandstop filter | Inline SMA, rejects 88–108 MHz on the scanner dongle | ~15 EUR |
| Dipole antenna kit | Telescoping elements, magnetic base, SMA pigtail | ~20 EUR |

Software: Python 3.10+, numpy, Docker, ClickHouse, Grafana. Everything else is
standard library.

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
Debian/Ubuntu, Fedora and Arch. A Windows host running `rtl_tcp.exe` is an
alternative documented in [setup/install-windows.md](setup/install-windows.md).

## Pipelines

All pipelines share one ClickHouse and one Grafana on localhost. Each pipeline
has its own database, its own Grafana folder, and its own README.

| Pipeline | Status | What it does |
|----------|--------|-------------|
| [spectrum/](spectrum/) | running | Wideband 88–470 MHz scanner, peak and transient detection, signal classifier, hourly baselines |
| [acars/](acars/) | built | ACARS aircraft messages from Athens airport traffic |
| [rds/](rds/) | built | RDS station metadata decoder (PS, PI, RadioText) |
| [noaa/](noaa/) | partial | NOAA / Meteor weather-sat pass scheduler (recorder is a scaffold) |
| [adsb/](adsb/) | companion | ADS-B aircraft tracking with a live map |
| [ais/](ais/) | companion | AIS ship tracking (Piraeus / Saronic Gulf) |
| [ism/](ism/) | companion | ISM 433 MHz sensor and device decoding |
| [ghost/](ghost/) | validated | Spirit-box replica, RDS labels, forensic audio tools |

## ghost/ — the paranormal-equipment pipeline

This started as a weekend curiosity after encountering a Greek YouTube channel
that uses "paranormal investigation" equipment with guests who do not appear to
be in a position to evaluate the claims being made around them. The engineering
question was simple enough to be worth answering properly: can every output these
devices produce be reproduced from first principles on a home SDR station? Yes.

The pipeline replicates and analyses three types of equipment: spirit boxes, EMF
detectors, and portable speakers. These are commercially sold devices whose
outputs are routinely presented as evidence of anomalous phenomena, but whose
operating principles are straightforward and reproducible.

A spirit box is a modified FM/AM receiver that sweeps broadcast frequencies
without locking, producing fragments of station audio that listeners interpret as
meaningful speech via pareidolia. The pipeline builds a software replica on the
SDR station, tagging every audio fragment with its source station via RDS decode.
Add a 90 ms slapback delay and the choppy radio becomes a "creepy voice" — one
knob on a delay plugin.

Forensic analysis of three published episodes (19 segments) found a bandwidth
shelf at 633–973 Hz (normal camera audio reaches 16–20 kHz) and chromaprint
fingerprint reuse above 0.5 in 90–100% of segment pairs. The simplest
explanation: a pre-recorded or looped source with very low bandwidth.

Full write-up: [ghost/REPORT.md](ghost/REPORT.md) (English),
[ghost/REPORT_GR.md](ghost/REPORT_GR.md) (Greek). Perception blind test:
[ghost/blindtest/](ghost/blindtest/). Pipeline details:
[ghost/README.md](ghost/README.md).

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
