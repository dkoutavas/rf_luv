# RESTORE: rebuild leap from bare metal

How to bring the rf_luv stack back on a fresh disk or a new machine. Written
because the 2026-06 disk failure had no runbook and the recovery knowledge was
scattered across CLAUDE.md, MORNING-CHECK, `spectrum/docs/dongle_identity.md`,
and each `install.sh` header. This is the single ordered path.

## What the repo restores vs what it does not

| Recovers from the repo | Lost unless backed up / on hardware |
|------------------------|-------------------------------------|
| All ClickHouse schema (migrations + `migrate.py`) | All ClickHouse row data (Docker volumes) |
| All Grafana datasources + dashboards (provisioned) | Ad-hoc UI-only dashboard edits |
| All systemd units + helper binaries (`install.sh`) | `/var/log/rtl-recovery.log` failure history |
| udev rules, sudoers, xrdp/tailscale config | Tuned values in gitignored `.env` files |
| Dongle EEPROM serials (stored on the dongle, not the disk) | |

The data gap is what `ops/clickhouse-backup/` exists to close. **If backups
were running before the failure, Step 6 restores the data. For the 2026-06
incident specifically there were no backups, so Step 0 (old-disk rescue) is the
only chance, and after that the historical data is accepted as lost.**

---

## Step 0: rescue data from the old disk (do this BEFORE wiping it)

Highest-value action. The ClickHouse data lived only in Docker named volumes on
the failing disk. If the disk still mounts at all, copy them off read-only
before replacing it.

```bash
# Docker named volumes live here. Project prefix = pipeline dir name.
#   spectrum_clickhouse-data, acars_clickhouse-data, noaa_clickhouse-data, ...
# Mount the old disk read-only (or boot a live USB), then:
tar czf /mnt/rescue/spectrum_clickhouse-data.tgz \
    -C /var/lib/docker/volumes/spectrum_clickhouse-data/_data .
# Repeat for acars_clickhouse-data and any others.
```

Prioritise `spectrum_clickhouse-data` (months of scans + the one-shot
FM-bandstop A/B baseline, which cannot be re-measured). If the disk will not
read, use `ddrescue` to image the partition first, then loop-mount the image.
If nothing reads, accept the loss and continue.

To re-import a rescued volume on the new disk: stop the stack, extract the tgz
back into `/var/lib/docker/volumes/<name>/_data`, start the stack. Then verify
row counts before trusting it. (A clean logical restore via Step 6 is preferred
when a real backup exists.)

---

## Step 1: base OS and packages

leap is openSUSE Leap 15.6, user `dio_nysis`, repo at `~/dev/rf_luv`.

```bash
# Docker + compose plugin
sudo zypper install docker docker-compose
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"      # log out/in for group to take effect

# RTL-SDR userland + Python 3.11 (Leap's default python3 is 3.6)
sudo zypper install rtl-sdr python311
# rtl_tcp, rtl_test, rtl_eeprom must be in PATH (rtl-sdr package)

# Optional CLI toolchain for ad-hoc experiments (not needed for the pipelines):
#   bash setup/install-wsl.sh   (adapts to the local package set)

# NOAA scheduler dependency (only if running noaa/):
pip3.11 install --user orbit-predictor
```

## Step 2: clone the repo

```bash
mkdir -p ~/dev && cd ~/dev
git clone <your-remote> rf_luv
cd rf_luv
```

## Step 3: dongle identity (EEPROM serials)

The serial->instance scheme is what makes the templated units reproducible.
Serials live on the dongle hardware, so they survive a disk failure. Verify
they are still `v3-01` and `v4-01`:

```bash
rtl_test 2>&1 | grep -i 'SN\|serial'
```

If a dongle was replaced and shows a different serial, rewrite it per
`spectrum/docs/dongle_identity.md`:

```bash
rtl_eeprom -d <index> -s v3-01     # or v4-01
```

Gotcha (documented in that file): an EEPROM serial change needs a real VBUS
power-cycle. A sysfs unbind/bind (`rtl-usb-reset.sh`) is NOT enough; the kernel
keeps showing the old serial until a physical replug or reboot. Decide which
physical dongle is V3 (FM-bandstopped, wideband) and which is V4 (narrowband,
ACARS) before writing serials.

## Step 4: host-side rtl_tcp reliability stack

Order matters. The rtl-tcp installer lays the foundation; it does NOT start
anything (start is explicit).

```bash
bash ops/rtl-tcp/install.sh        # units + wrapper + watchdog + USB reset + udev + sudoers + linger
bash ops/rtl-scanner/install.sh    # rtl-scanner@ template + env.*.example references
```

Create the real per-dongle env files (gitignored, so not in the clone):

```bash
sudo install -m 0644 /etc/rtl-scanner/v3-01.env.example /etc/rtl-scanner/v3-01.env
sudo $EDITOR /etc/rtl-scanner/v3-01.env     # v3-01 example carries production values
# V4 only if running a V4 decoder/scanner:
sudo install -m 0644 /etc/rtl-scanner/v4-01.env.example /etc/rtl-scanner/v4-01.env
sudo $EDITOR /etc/rtl-scanner/v4-01.env     # WARNING: example has TODO gain/antenna, recalibrate
```

Bring up the V3 chain and confirm the udev symlinks resolved:

```bash
ls -la /dev/rtl_sdr_v3 /dev/rtl_sdr_v4 2>/dev/null
systemctl --user enable --now rtl-tcp@v3-01 rtl-scanner@v3-01
journalctl --user -u rtl-tcp@v3-01 -n 20
```

Then install the unattended-ops layer (freshness + signal-quality probes, ntfy
notify, daily heartbeat, escalator, reset-failed safety net):

```bash
bash ops/install-trip-hardening.sh        # idempotent, one sudo prompt
systemctl --user enable --now rtl-reset-failed.timer
```

## Step 5: bring up the pipelines

Migrations / init.sql apply automatically at container start, recreating the
full schema and the provisioned Grafana dashboards.

```bash
cd ~/dev/rf_luv/spectrum && docker compose up -d     # primary; scanner native, NOT in compose
# Companions only if wanted:
# cd ../adsb && docker compose up -d   (note: adsb CH historically ran on the Windows host)
# cd ../ais  && docker compose up -d
# cd ../ism  && docker compose up -d
```

Note: the spectrum scanner runs natively under systemd (Step 4), not in the
Docker stack (the compose `spectrum-scanner` service is gated behind
`profiles: ["scanner"]` and stays off). Plain `docker compose up -d` brings up
ClickHouse, Grafana, and the listening-log nginx form (:8084). The classifier,
feature extractor, and health monitor run as the systemd timers installed
below, not as compose services. Dashboards land at the ports in the CLAUDE.md
"Port Allocation" table (spectrum :3003, etc.).

Install the spectrum intelligence + feedback timers and the coordinator:

```bash
bash ops/spectrum-features/install.sh
bash ops/spectrum-classifier/install.sh
bash ops/spectrum-classifier-health/install.sh
bash ops/spectrum-acars-feedback/install.sh
bash ops/rtl-coordinator/install.sh
```

## Step 6: restore ClickHouse data (only if a backup exists)

If `ops/clickhouse-backup/` was running before the failure and snapshots are on
off-host storage, restore after the stack is up (so migrations have created the
schema):

```bash
# Make the off-host snapshot location available, point the env at it:
sudo $EDITOR /etc/rtl-scanner/clickhouse-backup.env   # set BACKUP_DIR
bash ops/clickhouse-backup/restore.sh --db spectrum --latest
bash ops/clickhouse-backup/restore.sh --db acars --latest
# Verify:
docker exec clickhouse-spectrum clickhouse-client --user spectrum \
  --password '<pw>' --query "SELECT count(), min(timestamp), max(timestamp) FROM spectrum.scans"
```

For the 2026-06 incident there is no backup; skip this step and rely on whatever
Step 0 rescued.

## Step 7: turn ON backups before collecting new data

So the next disk failure is not another total loss. Do this even if Step 6 had
nothing to restore.

```bash
bash ops/clickhouse-backup/install.sh
sudo $EDITOR /etc/rtl-scanner/clickhouse-backup.env   # BACKUP_DIR must be OFF-HOST
```

See `ops/clickhouse-backup/README.md`. Confirm the first snapshot wrote a
`MANIFEST.tsv` with non-zero row counts.

## Step 8: recreate the other gitignored config

Only `*.example` files are in git. Recreate the live ones:

| Live file | Source | Action |
|-----------|--------|--------|
| `/etc/rtl-scanner/v3-01.env` | `ops/rtl-scanner/env.v3-01.example` | copy + edit (full values in example) |
| `/etc/rtl-scanner/v4-01.env` | `ops/rtl-scanner/env.v4-01.example` | copy + recalibrate gain/antenna (example is TODO) |
| `/etc/rtl-scanner/notify.env` | `ops/notify/notify.env.example` | **generate a NEW random NTFY_TOPIC** (old one leaked, see below) |
| `/etc/rtl-scanner/clickhouse-backup.env` | `ops/clickhouse-backup/clickhouse-backup.env.example` | set BACKUP_DIR off-host |
| `/etc/rtl-scanner/noaa-scheduler.env` | none (no example) | optional: RX lat/lon/alt for Polygono |
| `spectrum/.env` | `spectrum/.env.example` | CH password + per-dongle metadata |
| `acars/.env` | `acars/env.v4-01.example` | `cp` (Step 9) |

Security: rotate the ntfy topic. The old value leaked into a committed doc
(`spectrum/docs/session-handoff-20260429.md`), so anyone who read the repo can
read and spoof alerts on it. Pick a new random topic, set it in
`/etc/rtl-scanner/notify.env`, and scrub the old value from that doc.

## Step 9: redeploy ACARS fresh

ACARS deploys clean on the recovered V4 (its soak restarts from zero). Follow
`acars/DEPLOY.md` end to end. In short:

```bash
systemctl --user stop rtl-scanner@v4-01.service     # if V4 was scanning
systemctl --user disable rtl-scanner@v4-01.service
cd ~/dev/rf_luv/acars && cp env.v4-01.example .env
docker compose up -d --build
```

## Step 10: optional services

```bash
bash ops/noaa-pass-scheduler/install.sh   # NOAA scheduler (recorder still a scaffold)
bash ops/remote-desktop/enable-xrdp.sh    # xrdp + icewm
bash ops/remote-desktop/trust-tailscale-iface.sh
```

---

## Verification checklist

- [ ] `lsusb | grep -i RTL` shows both dongles
- [ ] `/dev/rtl_sdr_v3` (+ v4 if used) symlinks exist
- [ ] `systemctl --user is-active rtl-tcp@v3-01 rtl-scanner@v3-01` both `active`
- [ ] spectrum Grafana at :3003 renders, scans flowing (freshness probe not alerting)
- [ ] `clickhouse-backup.timer` enabled, first snapshot has non-zero rows, BACKUP_DIR is off-host
- [ ] ntfy topic rotated; 09:00 UTC heartbeat reaches the phone
- [ ] If restoring data: row counts and min/max timestamps look right per pipeline
- [ ] ACARS (if deployed): messages climbing during daytime LGAV traffic

## Reference

- Install-order detail: each `ops/*/install.sh` header
- Dongle serials: `spectrum/docs/dongle_identity.md`
- Dongle cutover (V3/V4 swap): `spectrum/docs/dongle_cutover_runbook.md`
- ACARS deploy: `acars/DEPLOY.md`
- Backups: `ops/clickhouse-backup/README.md`
- Ports, dongle policy, reliability stack: `CLAUDE.md`
