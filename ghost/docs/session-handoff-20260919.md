# ghost/ — live bring-up session handoff (2026-09-19)

First live hardware run of the ghost pipeline on the two-dongle Omen. Everything
below actually ran; the numbers are measured, not planned.

## What ran

- **Shared data layer up** (`infra/up.sh`): ClickHouse + Grafana + logging-form,
  all 8 databases created. The `ghost` DB tables (`spins`, `stations`, `segments`)
  are live; an insert/read/truncate round-trip through `spectrum/db.py` as the
  `ghost` user passed.
- **V3 spirit box** (`ghost/spiritbox.py`, rtl_tcp :1235): two full 87.5–108 MHz
  forward sweeps, 206 steps each, 30.9 s labelled WAV per run, written to
  `ghost.spins` / `ghost.stations`. Real Athens RDS decoded (see findings).
- **V4 spectrum scanner** (rtl_tcp :1234): 88–470 MHz sweep + airband preset,
  4000+ rows in `spectrum.scans`, 49 peaks. Ran concurrently with the V3 — the
  two-dongle setup is proven.
- **Debunk A/B**: `..._fx.wav` generated from the dry sweep (slapback 90 ms / fb 0.3
  + reverb) to show "spirit voice" = dry radio + a delay plugin.

## Measured findings (carry these into the persistent config)

- **Gain 12 on both dongles.** V3 at gain 20 clipped 8.4 % on strong Athens FM; V4
  at gain 20 clipped 29 % on airband even with the bandstop on. Gain 12 → zero
  clipping, healthy levels (V3 peak −15 dBFS, V4 airband peaks +4 dBFS pre-drop).
  Set `SCAN_GAIN=12` in `/etc/rtl-scanner/v4-01.env` and use `GHOST_GAIN=12`.
- **Bandstop confirmed working on the V4**: 0 detected peaks in 88–108 MHz, strong
  airband peaks at 119–126 MHz. The filter does its job.
- **Dongle index is not stable**: this session enumerated V3 (`v3-01`, R820T) as
  index 0 and V4 (`v4-01`, R828D, "Blog V4") as index 1, but that can flip on
  replug. Always address by serial (the templated systemd units do; manual
  `rtl_tcp -d N` is a per-session convenience only).
- **RDS PS is thin at 2 s dwell, good at 4 s.** 2 s recovered PI on 2 stations and a
  full PS ("105.8 FM") on one. 4 s recovered 5 stations plus RadioText
  ("TEL 8 FM", "105,8 MHz &", "91,6 MHz ATH"). PS also wobbles between runs (needs
  several clean 0A groups). Use `GHOST_RDS_DWELL_S=4 GHOST_RDS_TOP_N=20` for a
  well-labelled sweep. Burst-error correction is a known follow-up.

## Bugs fixed live (committed)

- `ghost/migrate.py` hardcoded `rds.schema_migrations` (clone leftover) → PHASE 2h
  bootstrap failed `ACCESS_DENIED`, ghost tables never created. Retargeted to
  `ghost`. `infra/up.sh` now boots clean.
- `ops/udev/99-rtl-sdr.rules` used `GROUP="plugdev"`, which does not exist on
  openSUSE → the whole rule voided, no `/dev/rtl_sdr_v*` symlinks and no MODE 0666
  (rtl_tcp needed sudo). Changed to `GROUP="users"`.

## Host prerequisites learned

- **DVB blacklist is required** before rtl_tcp/rtl_eeprom work cleanly:
  `blacklist dvb_usb_rtl28xxu` (+ `rtl2832`, `rtl2830`) in
  `/etc/modprobe.d/blacklist-rtlsdr.conf`, then `sudo modprobe -r dvb_usb_rtl28xxu`.
- EEPROM serials were already `v3-01` / `v4-01`; no write needed. `iSerial`
  descriptors read correctly.

## Current state at handoff

- Data layer (Docker) is up. `ghost` and `spectrum` hold this session's rows.
- Two manual `sudo rtl_tcp` instances may still be running (V3 :1235, V4 :1234). They
  are NOT persistent — a reboot loses them. Nothing is installed under systemd yet.
- Antennas: V4 ~57 cm/arm (VHF compromise), V3 ~65 cm/arm (4× Nothing 3a Pro, good
  for FM). Vertical, suction-mounted on double-aluminium sunroom windows, ~1.3 m
  feedpoint. UHF (>300 MHz) will be weak indoors through the aluminium frames.

## Next (not done this session)

1. **Persistence** — `bash ops/rtl-tcp/install.sh`, create `/etc/rtl-scanner/v4-01.env`
   (`SCAN_GAIN=12`) and `v3-01.env`, `systemctl --user enable --now rtl-tcp@v4-01
   rtl-tcp@v3-01 rtl-scanner@v4-01`. Then `ops/install-trip-hardening.sh`. See
   `ghost/HOSTPREP.md`.
2. **Backups** — `ops/clickhouse-backup/` with `BACKUP_DIR=/data/...` before real
   collection.
3. Ghost follow-ups: REPORT.md (GR+EN), blind-test HTML from real WAV fragments,
   Grafana Ghost panels tuned to real rows, K-II EMF module (hardware).
