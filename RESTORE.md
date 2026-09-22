# RESTORE: rebuild the rf_luv stack on a Linux host

How to bring the rf_luv stack up on a native Linux host with one or two
RTL-SDR dongles, from a fresh clone. The reference host is the HP Omen
(openSUSE Tumbleweed, user `dio_nysi`, repo at `~/dev/rf_luv`): the **V4**
(`v4-01`) runs the spectrum scanner on rtl_tcp :1234 with its FM bandstop on,
and the **V3** (`v3-01`) is the `ghost/` pipeline dongle on rtl_tcp :1235 with
no filter. One dongle works too: it becomes the scanner.

Written because the 2026-06 leap disk failure had no runbook. The first
two-dongle bring-up (2026-09-19/20) then showed the mechanical steps were
spread over five documents and two installers. They now live in one script,
`ops/install-host.sh`. This file is the manual for that script plus the
physical steps it cannot do.

> **Applying edits:** the reliability stack runs its **installed** copies, not
> the repo files. Editing `ops/*.py`, `ops/*.sh` or `ops/*.service` changes
> nothing on the host until you re-run `bash ops/install-host.sh ...`, which
> re-runs each component installer. Unit-file changes also need
> `systemctl --user daemon-reload`.

## What the repo restores vs what it does not

| Recovers from the repo | Lost unless backed up / on hardware |
|------------------------|-------------------------------------|
| All ClickHouse schema (migrations + `migrate.py`) | All ClickHouse row data (Docker volumes) |
| All Grafana datasources + dashboards (provisioned) | Ad-hoc UI-only dashboard edits |
| All systemd units + helper binaries (via `install-host.sh`) | `/var/log/rtl-recovery.log` failure history |
| udev rules, sudoers, DVB blacklist | Tuned values in the gitignored `/etc/rtl-scanner/*.env` files |
| Dongle EEPROM serials (stored on the dongle, not the disk) | |

The data gap is what `ops/clickhouse-backup/` closes. If backups were running,
step 7 restores the data. For the 2026-06 leap incident there were no backups.

---

## Step 0: rescue data from leap's old disk (history only)

Skip this for a normal setup. It applies only if leap's failed disk is still
readable. After consolidation all databases lived in one Docker named volume,
`rf_luv_infra_ch-data`. If the old disk mounts, copy that volume off
read-only before wiping it:

```bash
tar czf /mnt/rescue/rf_luv_infra_ch-data.tgz \
    -C /var/lib/docker/volumes/rf_luv_infra_ch-data/_data .
```

A 2026-06-era disk may instead carry the old per-pipeline volumes
(`spectrum_clickhouse-data`, `acars_clickhouse-data`, ...). Tar each one you
find. If the disk will not read, image it with `ddrescue` first. If nothing
reads, accept the loss and continue.

---

## Step 1: OS packages

`python3` must be 3.10 or newer with numpy. Pick your distro:

```bash
# openSUSE Tumbleweed (the reference host)
sudo zypper install rtl-sdr docker docker-compose python3-numpy
# Debian / Ubuntu
sudo apt install rtl-sdr docker.io docker-compose-v2 python3-numpy
# Fedora
sudo dnf install rtl-sdr docker docker-compose python3-numpy
# Arch
sudo pacman -S rtl-sdr docker docker-compose python-numpy

sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"      # log out and in again
# NOAA scheduler only:  pip install --user orbit-predictor
```

`install-host.sh` checks for `rtl_tcp`, `rtl_eeprom`, `rtl_test`, `docker`
and numpy and prints the matching install line if any is missing.

## Step 2: clone the repo

```bash
mkdir -p ~/dev && cd ~/dev
git clone <your-remote> rf_luv
cd rf_luv
```

## Step 3: dongle serials (physical, one dongle at a time)

Every dongle ships with the factory serial `00000001`. Two dongles with the
same serial cannot be told apart. Write a serial to each dongle **with only
that dongle plugged in**:

```bash
# Only the V4 plugged in:
rtl_eeprom -d 0 -s v4-01
# Unplug the V4, wait 3 s, plug it back in.

# Only the V3 plugged in:
rtl_eeprom -d 0 -s v3-01
# Unplug the V3, wait 3 s, plug it back in.
```

The EEPROM serial is re-read only on a real VBUS power cycle. A sysfs unbind
or `rtl-usb-reset` is not enough. Verify with `rtl_test 2>&1 | grep SN`.
`install-host.sh` refuses to continue while any dongle still reads `00000001`.

If the DVB kernel driver holds the dongle (`usb_claim_interface error -6`),
run `sudo modprobe -r dvb_usb_rtl28xxu` first. The installer blacklists it
permanently in step 5.

## Step 4: shared data layer

Bring up ClickHouse, Grafana and the schema before the installer, because the
first backup and the scanner both write into ClickHouse:

```bash
docker network create rf_luv_net          # idempotent
bash infra/up.sh                           # ClickHouse 8123/9000, Grafana 3000, logging-form 8084
```

The `ch-bootstrap` one-shot creates all eight databases (adsb, ais, ism,
spectrum, acars, noaa, rds, ghost), their users, the schema and the Athens
known-frequencies seed.

## Step 5: run the host installer

One command does the DVB blacklist, the udev rule (with a group that exists
on your distro), the rtl_tcp wrapper and watchdog, the per-dongle env files
with the live USB index, the systemd units, and the backup timer:

```bash
# Two dongles (the reference layout):
bash ops/install-host.sh --scanner v4-01 --ghost v3-01 --gain 12 \
     --backup-dir /data/rf-clickhouse-backups

# One dongle:
bash ops/install-host.sh --scanner v4-01 --backup-dir /data/rf-clickhouse-backups
```

Flags:
- `--scanner SERIAL` the dongle for the spectrum scanner (rtl_tcp :1234). Required.
- `--ghost SERIAL` the second dongle for the ghost pipeline (rtl_tcp :1235).
- `--gain N` `SCAN_GAIN` for new env files. Default 12. Gain 20 clips on Athens FM and airband.
- `--backup-dir DIR` daily ClickHouse snapshots go here. Put it on a different physical disk. On the Omen that is the second NVMe at `/data`.
- `--dry-run` print every command that would change the host, change nothing.
- `--verify-only` skip install, run the PASS/FAIL checks only.

The script needs sudo, so run it in a terminal. It is idempotent. On a re-run
it keeps a tuned env file. Dongles are addressed by serial, so you can unplug
and replug them at any time: udev starts `rtl-tcp@<serial>` on plug and
systemd stops it on unplug.

Then confirm:

```bash
bash ops/install-host.sh --verify-only --scanner v4-01 --ghost v3-01 \
     --backup-dir /data/rf-clickhouse-backups
```

Every line must read PASS: `rtl-tcp@` active and listening per dongle, the
watchdog timers, the `/dev/rtl_sdr_*` symlinks, the scanner unit, the DVB
driver unloaded, fresh `spectrum.scans` rows, and a backup snapshot.

## Step 6: antennas and the bandstop

Screw the FM bandstop filter inline on the **scanner** dongle. Leave the ghost
dongle bare (the spirit box needs the FM band open). Dipole arm lengths from
the formula `arm_cm = 7125 / freq_MHz`: about 57 cm per arm for the wideband
scanner, about 73 cm for FM work. The antenna table in `CLAUDE.md` has the
rest.

## Step 7: restore ClickHouse data (only if a backup exists)

If snapshots exist on the backup disk, restore after step 4 so the schema is
already there:

```bash
bash ops/clickhouse-backup/restore.sh --db spectrum --latest
bash ops/clickhouse-backup/restore.sh --db ghost --latest
docker exec clickhouse clickhouse-client --user spectrum \
  --password spectrum_local --query "SELECT count(), min(timestamp), max(timestamp) FROM spectrum.scans"
```

## Step 8: the rest of the ops layer

`install-host.sh` covers the dongles, the scanner, `rf-mode` and the backups.
If any client is connected (loopback included), the watchdog skips its probe.
SDR++ over RTL-TCP needs no mode switch. Use `rf-mode listen` and `rf-mode
scan` only for direct-USB tools (`rtl_fm`, `rtl_433`, `rtl_eeprom`).

The alerting, probes and intelligence timers are separate installers:

```bash
bash ops/install-trip-hardening.sh        # escalator, freshness + signal-quality probes, notify, heartbeat
bash ops/spectrum-features/install.sh
bash ops/spectrum-classifier/install.sh
bash ops/spectrum-classifier-health/install.sh
bash ops/spectrum-acars-feedback/install.sh
bash ops/rtl-coordinator/install.sh
```

## Step 9: gitignored config

Only `*.example` files are in git. `install-host.sh` creates the first four.
Recreate the rest by hand:

| Live file | Source | Created by |
|-----------|--------|------------|
| `/etc/rtl-scanner/v4-01.env`, `v3-01.env` | `ops/rtl-scanner/env.*.example` | `install-host.sh` (gain, port, index filled in) |
| `/etc/rtl-scanner/escalator.env` | `ops/rtl-tcp/escalator.env.example` | `install-host.sh` (`SERIALS` filled in) |
| `/etc/rtl-scanner/clickhouse-backup.env` | `ops/clickhouse-backup/clickhouse-backup.env.example` | `install-host.sh` (`BACKUP_DIR` uncommented) |
| `/etc/rtl-scanner/notify.env` | `ops/notify/notify.env.example` | you. **Generate a new random `NTFY_TOPIC`**; the old one leaked into a committed doc |
| `/etc/rtl-scanner/noaa-scheduler.env` | none | you, optional: RX lat/lon/alt |
| `spectrum/.env` | `spectrum/.env.example` | you |
| `acars/.env` | `acars/env.v4-01.example` | you (step 10) |

## Step 10: optional pipelines

```bash
# ACARS on the V4 (stops the scanner while it runs; see acars/DEPLOY.md):
systemctl --user stop rtl-scanner@v4-01
cd acars && cp env.v4-01.example .env && cd .. && bash pipeline.sh up acars

bash ops/noaa-pass-scheduler/install.sh   # NOAA scheduler (recorder is a scaffold)
bash ops/remote-desktop/enable-xrdp.sh    # xrdp + icewm
```

---

## Known transients

- **`rtl_tcp` can SIGABRT under reconnect churn.** The scanner reconnects on
  every sweep. On 2026-09-20 the V4's `rtl_tcp` core-dumped once after 13
  minutes and systemd restarted it in 11 s. The unit allows 100 restarts per
  10 minutes and `rtl-reset-failed.timer` clears failure state every 5
  minutes, so this self-heals. If the restart counter climbs, look at
  `coredumpctl info rtl_tcp` and `journalctl --user -u rtl-tcp@v4-01`.
- **`udevadm trigger` re-attaches the DVB driver** on a live host. Both
  installers now run `modprobe -r dvb_usb_rtl28xxu` after the trigger.
- **A loose dongle vanishes from `lsusb`.** Before debugging software, check
  `lsusb | grep 2838` shows every dongle.

## Verification checklist

- [ ] `bash ops/install-host.sh --verify-only ...` prints all PASS
- [ ] `lsusb | grep 2838` shows every dongle
- [ ] Grafana at :3000 renders the Spectrum folder with fresh scans
- [ ] `ls <backup-dir>/spectrum/latest` exists and `MANIFEST.tsv` has non-zero rows
- [ ] ntfy topic rotated (or unset for a desktop-only setup)
- [ ] ghost (if `--ghost`): `python3 ghost/spiritbox.py --mode forward --dwell-ms 150` writes a labelled WAV

## Reference

- Host installer: `ops/install-host.sh --help`
- Component installers: each `ops/*/install.sh` header
- Dongle serials: `spectrum/docs/dongle_identity.md`
- Ghost specifics: `ghost/HOSTPREP.md`, `ghost/docs/session-handoff-20260919.md`
- ACARS deploy: `acars/DEPLOY.md`
- Backups: `ops/clickhouse-backup/README.md`
- Ports, dongle policy, reliability stack: `CLAUDE.md`
