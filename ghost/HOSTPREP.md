# ghost/ host prep — owner-gated steps

These are the physical and root steps to bring the two-dongle station up so the
ghost pipeline can run. They cannot be done headless (they need a plugged-in
dongle, EEPROM writes with a physical replug, and sudo). This extends
[`../RESTORE.md`](../RESTORE.md) for two dongles on the native Tumbleweed Omen.

The layout: V4 (`v4-01`) runs the spectrum scanner on rtl_tcp **:1234** with its FM
notch on. V3 (`v3-01`) is the ghost dongle on rtl_tcp **:1235** with the notch off.

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

## 2. udev symlinks (sudo)

`ops/udev/99-rtl-sdr.rules` now has both the v4 and v3 lines active. Install:

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
# v4-01.env keeps RTL_TCP_PORT=1234; v3-01.env already has 1235. Recalibrate
# SCAN_GAIN per dongle for the local RF environment.

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
python3 ghost/spiritbox.py --mode forward --dwell-ms 150 --duration 60
# Check the WAV under /data/rf_luv/ghost/recordings and the labels:
docker exec clickhouse clickhouse-client --user ghost --password ghost_local \
  --query "SELECT round(freq_hz/1e6,1) mhz, ps FROM ghost.stations ORDER BY freq_hz"
```

If the V3 front end overloads on strong Athens FM (ghost signals, moving carriers),
drop `GHOST_GAIN` first; if that is not enough, swap the dongles' roles (put the
bandstop and the scanner on the V3, ghost on the V4).
