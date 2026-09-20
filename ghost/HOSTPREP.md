# ghost/ host prep — owner-gated steps

These are the physical and root steps to bring the two-dongle station up so the
ghost pipeline can run. They cannot be done headless (they need a plugged-in
dongle, EEPROM writes with a physical replug, and sudo). This extends
[`../RESTORE.md`](../RESTORE.md) for two dongles on the native Tumbleweed Omen.

The layout: V4 (`v4-01`) runs the spectrum scanner on rtl_tcp **:1234** with its FM
notch on. V3 (`v3-01`) is the ghost dongle on rtl_tcp **:1235** with the notch off.

> **Validated live 2026-09-19.** Serials were already `v3-01`/`v4-01`, the
> bandstop suppresses the FM band on the V4 (0 peaks in 88–108 MHz), and both
> dongles ran concurrently. Findings and the exact commands are in
> [`docs/session-handoff-20260919.md`](docs/session-handoff-20260919.md). Use
> **gain 12** on both dongles (gain 20 clips hard on Athens FM/airband).

## 1. Dongle serials (physical replug required)

Write each serial on a host where that dongle is the only RTL-SDR (both ship as
`00000001`, so udev cannot tell them apart until they differ):

```bash
# With ONLY the V4 plugged in:
rtl_eeprom -d 0 -s v4-01
# Unplug the V4, wait 3 s, plug it back in (a sysfs reset is NOT enough; the
# EEPROM serial only re-reads on a real VBUS power cycle).

# Then with ONLY the V3 plugged in:
rtl_eeprom -d 0 -s v3-01
# Unplug/replug the V3 the same way.
```

Verify: `rtl_test 2>&1 | grep -i serial` shows `v4-01` and `v3-01`.

## 1b. DVB kernel blacklist (sudo, one-time)

The kernel DVB driver claims the RTL2832U otherwise. Required before rtl_tcp /
rtl_eeprom work as a normal user.

```bash
sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf >/dev/null << 'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null
```

## 2. udev symlinks (sudo)

`ops/udev/99-rtl-sdr.rules` has both the v4 and v3 lines active and uses
`GROUP="users"` (openSUSE has no `plugdev` group — the old value voided the rule).
Install:

```bash
sudo cp ops/udev/99-rtl-sdr.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=usb
ls -l /dev/rtl_sdr_v4 /dev/rtl_sdr_v3     # both symlinks must resolve
```

## 3. Two rtl_tcp instances + watchdogs (sudo, then user systemd)

```bash
bash ops/rtl-tcp/install.sh          # units, wrapper, watchdog, udev, sudoers, linger

# Per-dongle env files (gitignored; copy from the examples):
sudo install -m 0644 ops/rtl-scanner/env.v4-01.example /etc/rtl-scanner/v4-01.env
sudo install -m 0644 ops/rtl-scanner/env.v3-01.example /etc/rtl-scanner/v3-01.env
# v4-01.env keeps RTL_TCP_PORT=1234; v3-01.env already has 1235. Set SCAN_GAIN=12
# on the V4 (gain 20 clipped 29% on Athens airband even with the bandstop on).

# Bring up both rtl_tcp servers and their watchdogs:
systemctl --user enable --now rtl-tcp@v4-01 rtl-tcp-watchdog@v4-01.timer
systemctl --user enable --now rtl-tcp@v3-01 rtl-tcp-watchdog@v3-01.timer
# The spectrum scanner (V4 only):
systemctl --user enable --now rtl-scanner@v4-01
ss -tlnp | grep -E ':1234|:1235'     # both listening
```

The escalator watches both serials once you install its override:

```bash
sudo install -m 0644 ops/rtl-tcp/escalator.env.example /etc/rtl-scanner/escalator.env
# Confirm XHCI_PCI in that file matches this host (lspci) before trusting the escalator.
bash ops/install-trip-hardening.sh
```

## 4. Shared data layer + the ghost database (Docker)

```bash
docker network create rf_luv_net       # idempotent
bash infra/up.sh                       # ch-bootstrap creates all 8 databases incl. ghost
# Confirm the ghost schema:
docker exec clickhouse clickhouse-client --user ghost --password ghost_local \
  --query "SHOW TABLES FROM ghost"     # spins, stations, segments
```

## 5. Backups OFF the primary disk, before collecting data

The Omen has a second NVMe at `/data`. Point backups there so a primary-disk
failure is not another total loss:

```bash
bash ops/clickhouse-backup/install.sh
sudo $EDITOR /etc/rtl-scanner/clickhouse-backup.env   # BACKUP_DIR=/data/rf-clickhouse-backups
# DATABASES already includes ghost (and rds). Confirm the first snapshot has rows.
```

## 6. First live spirit-box run

```bash
# V3 FM notch OFF (unscrew the inline SMA filter). Then:
GHOST_GAIN=12 python3 ghost/spiritbox.py --mode forward --dwell-ms 150 --duration 60
# Check the WAV under /data/rf_luv/ghost/recordings and the labels:
docker exec clickhouse clickhouse-client --user ghost --password ghost_local \
  --query "SELECT round(freq_hz/1e6,1) mhz, ps FROM ghost.stations ORDER BY freq_hz"
```

If the V3 front end overloads on strong Athens FM (ghost signals, moving carriers),
drop `GHOST_GAIN` first; if that is not enough, swap the dongles' roles (put the
bandstop and the scanner on the V3, ghost on the V4).

## Quick manual path (no systemd) — what the first live run used

Faster than the systemd stack for a one-off session. After steps 1b + 2:

```bash
# Start rtl_tcp per dongle (index is per-session; confirm with `rtl_eeprom -d N`).
# This session enumerated V3 as index 0, V4 as index 1.
nohup rtl_tcp -d 0 -a 127.0.0.1 -p 1235 -s 2048000 >/tmp/rtl_v3.log 2>&1 &   # V3, ghost
nohup rtl_tcp -d 1 -a 127.0.0.1 -p 1234 -s 2048000 >/tmp/rtl_v4.log 2>&1 &   # V4, scanner

# Ghost spirit box on the V3:
RTL_TCP_HOST=127.0.0.1 RTL_TCP_PORT=1235 GHOST_GAIN=12 GHOST_RDS_DWELL_S=4 \
  GHOST_RDS_TOP_N=20 CLICKHOUSE_HOST=127.0.0.1 python3 ghost/spiritbox.py \
  --mode forward --dwell-ms 150 --no-lock

# Spectrum scanner on the V4 (from spectrum/, so the flat imports resolve):
cd spectrum && RTL_TCP_HOST=127.0.0.1 RTL_TCP_PORT=1234 SCAN_DONGLE_ID=v4-01 \
  SCAN_GAIN=12 CLICKHOUSE_HOST=127.0.0.1 CLICKHOUSE_DB=spectrum \
  CLICKHOUSE_USER=spectrum CLICKHOUSE_PASSWORD=spectrum_local \
  bash -c 'python3 scanner.py | python3 scan_ingest.py'
```

If udev perms are not applied yet, prepend `sudo` to the `rtl_tcp` lines. This path
does not survive a reboot; use the systemd stack above for that.
