# RESTORE: rebuild the rf_luv stack on the local host

How to bring the rf_luv stack up on the local **openSUSE Tumbleweed PC (WSL2)**
with one **RTL-SDR V4**, from a fresh clone. Written because the 2026-06 leap
disk failure had no runbook and the recovery knowledge was scattered across
CLAUDE.md, `spectrum/docs/dongle_identity.md`, and each `install.sh` header.
This is the single ordered path. leap itself is retired (see CLAUDE.md); this
rebuilds onto the local host, not leap.

> **Applying edits:** the reliability stack runs its **installed** copies, not
> the repo files. `ops/*/install.sh` copies scripts into `/usr/local/bin` and
> `/usr/local/sbin` and unit files into `~/.config/systemd/user/`. Editing a repo
> `ops/*.py` or `ops/*.service` changes **nothing** on the running host until you
> re-run that pipeline's `install.sh` (and `systemctl --user daemon-reload` for
> unit changes). Re-run the relevant installer after any Stage 0 edit.

## What the repo restores vs what it does not

| Recovers from the repo | Lost unless backed up / on hardware |
|------------------------|-------------------------------------|
| All ClickHouse schema (migrations + `migrate.py`) | All ClickHouse row data (Docker volumes) |
| All Grafana datasources + dashboards (provisioned) | Ad-hoc UI-only dashboard edits |
| All systemd units + helper binaries (`install.sh`) | `/var/log/rtl-recovery.log` failure history |
| udev rules, sudoers, xrdp/tailscale config | Tuned values in gitignored `.env` files |
| Dongle EEPROM serials (stored on the dongle, not the disk) | |

The data gap is what `ops/clickhouse-backup/` exists to close. **Going forward,
if backups are running, Step 6 restores the data.** For the 2026-06 leap
incident there were no backups; leap's `spectrum.scans` and the FM-bandstop A/B
baseline are gone, and the local host starts with an empty ClickHouse. Step 0
below is the only thing that could ever have salvaged them, and it applies only
if leap's failed disk is still around to read.

---

## Step 0: rescue data from leap's old disk (history / only if that disk survives)

**Skip this for a normal local setup — start at Step 1.** This step only applies
if leap's failed disk is still readable and you want to attempt a salvage; the
local rebuild does not depend on it. After consolidation all six databases lived
in a **single** ClickHouse Docker named volume, `rf_luv_infra_ch-data` (the
`ch-data` volume of compose project `rf_luv_infra`). If the old disk still mounts
at all, copy it off read-only before wiping it.

```bash
# One shared volume now holds all six databases.
#   rf_luv_infra_ch-data  (was: per-pipeline spectrum_clickhouse-data, acars_..., etc.)
# Mount the old disk read-only (or boot a live USB), then:
tar czf /mnt/rescue/rf_luv_infra_ch-data.tgz \
    -C /var/lib/docker/volumes/rf_luv_infra_ch-data/_data .
```

A 2026-06-era disk may instead carry the *old* per-pipeline volumes
(`spectrum_clickhouse-data`, `acars_clickhouse-data`, ...). If so, tar each one
you find; they are imported into the new shared server per database, not by
dropping the directory in place.

Prioritise the spectrum data (months of scans + the one-shot FM-bandstop A/B
baseline, which cannot be re-measured). If the disk will not read, use
`ddrescue` to image the partition first, then loop-mount the image. If nothing
reads, accept the loss and continue.

To re-import a rescued shared volume on the new disk: stop the stack, extract the
tgz back into `/var/lib/docker/volumes/rf_luv_infra_ch-data/_data`, start the
stack. Then verify row counts before trusting it. (A clean logical restore via
Step 6 is preferred when a real backup exists.)

---

## Step 1: base OS and packages

The local host is openSUSE Tumbleweed (WSL2), user `dio_nysi`, repo at
`~/dev/rf_luv`. Tumbleweed's default `python3` is 3.13, which is what the
pipelines and ops scripts run on (no separate `python311` needed — that was
leap, where the distro `python3` was 3.6).

```bash
# Docker + compose plugin
sudo zypper install docker docker-compose
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"      # log out/in for group to take effect

# RTL-SDR userland (python3 3.13 is already the Tumbleweed default)
sudo zypper install rtl-sdr
# rtl_tcp, rtl_test, rtl_eeprom must be in PATH (rtl-sdr package)

# Optional CLI toolchain for ad-hoc experiments (not needed for the pipelines):
#   bash setup/install-wsl.sh   (adapts to the local package set)

# NOAA scheduler dependency (only if running noaa/):
pip install --user orbit-predictor
```

## Step 2: clone the repo

```bash
mkdir -p ~/dev && cd ~/dev
git clone <your-remote> rf_luv
cd rf_luv
```

## Step 3: dongle identity (EEPROM serials)

The serial->instance scheme is what makes the templated units reproducible.
Serials live on the dongle hardware, so they survive a disk failure. The local
host has one dongle; verify it still reports serial `v4-01` (a second V3, if you
add one, should be `v3-01`):

```bash
rtl_test 2>&1 | grep -i 'SN\|serial'
```

If the dongle shows a different serial, rewrite it per
`spectrum/docs/dongle_identity.md`:

```bash
rtl_eeprom -d <index> -s v4-01     # v3-01 for an optional second dongle
```

Gotcha (documented in that file): an EEPROM serial change needs a real VBUS
power-cycle. A sysfs unbind/bind (`rtl-usb-reset.sh`) is NOT enough; the kernel
keeps showing the old serial until a physical replug or reboot. On the local
host the V4 (`v4-01`) is the scanner dongle; its FM notch is a removable inline
SMA filter (screw it in for wideband scanning, unscrew it for FM-band work).

## Step 4: host-side rtl_tcp reliability stack

Order matters. The rtl-tcp installer lays the foundation; it does NOT start
anything (start is explicit).

```bash
bash ops/rtl-tcp/install.sh        # units + wrapper + watchdog + USB reset + udev + sudoers + linger
bash ops/rtl-scanner/install.sh    # rtl-scanner@ template + env.*.example references
```

Create the real per-dongle env files (gitignored, so not in the clone):

```bash
# V4 is the local scanner dongle:
sudo install -m 0644 /etc/rtl-scanner/v4-01.env.example /etc/rtl-scanner/v4-01.env
sudo $EDITOR /etc/rtl-scanner/v4-01.env     # recalibrate gain/antenna for the local RF environment
# Optional second V3 dongle only:
# sudo install -m 0644 /etc/rtl-scanner/v3-01.env.example /etc/rtl-scanner/v3-01.env
# sudo $EDITOR /etc/rtl-scanner/v3-01.env
```

Bring up the V4 chain and confirm the udev symlink resolved:

```bash
ls -la /dev/rtl_sdr_v4 2>/dev/null
systemctl --user enable --now rtl-tcp@v4-01 rtl-scanner@v4-01
journalctl --user -u rtl-tcp@v4-01 -n 20
```

Then install the unattended-ops layer (freshness + signal-quality probes, local
desktop / ntfy notify, daily heartbeat, escalator, reset-failed safety net):

```bash
bash ops/install-trip-hardening.sh        # idempotent, one sudo prompt
systemctl --user enable --now rtl-reset-failed.timer
```

## Step 5: bring up the shared data layer + pipelines

The schema and the Athens known-frequencies seed apply automatically when the
infra `ch-bootstrap` one-shot runs, recreating all six databases, their users
and grants, and the provisioned Grafana dashboards and folders.

```bash
docker network create rf_luv_net          # idempotent; up.sh also creates it
cd ~/dev/rf_luv && bash infra/up.sh        # shared ClickHouse 8123/9000 + Grafana 3000 + logging-form 8084
# Rotating V4 decoders only if wanted:
# bash pipeline.sh up adsb   (note: adsb CH historically ran on the Windows host)
# bash pipeline.sh up ais
# bash pipeline.sh up ism
```

Note: the spectrum scanner runs natively under systemd (Step 4), not in a
container. It writes to the shared ClickHouse on `127.0.0.1:8123` (formerly the
per-pipeline `:8126`). `bash infra/up.sh` brings up the single ClickHouse,
Grafana, the listening-log nginx form (`:8084`), and the `ch-bootstrap`
one-shot. The classifier, feature extractor, and health monitor run as the
systemd timers installed below, not as containers. Dashboards land in the single
Grafana on `:3000`, one folder per pipeline (see the CLAUDE.md "Port Allocation"
section).

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
# restore.sh targets the shared container via CH_CONTAINER=clickhouse
bash ops/clickhouse-backup/restore.sh --db spectrum --latest
bash ops/clickhouse-backup/restore.sh --db acars --latest
# Verify against the single shared server:
docker exec clickhouse clickhouse-client --user spectrum \
  --password '<pw>' --query "SELECT count(), min(timestamp), max(timestamp) FROM spectrum.scans"
```

leap's data had no backup and is gone, so on a fresh local host there is nothing
to restore here — skip to Step 7 and start collecting clean, with backups on.

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
| `/etc/rtl-scanner/v4-01.env` | `ops/rtl-scanner/env.v4-01.example` | scanner dongle: copy + recalibrate gain/antenna for the local RF environment |
| `/etc/rtl-scanner/v3-01.env` | `ops/rtl-scanner/env.v3-01.example` | only for an optional second V3 dongle |
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

ACARS deploys clean on the local V4 (its soak restarts from zero). Follow
`acars/DEPLOY.md` end to end. In short:

```bash
systemctl --user stop rtl-scanner@v4-01.service     # hand the V4 to ACARS
systemctl --user disable rtl-scanner@v4-01.service
cd ~/dev/rf_luv/acars && cp env.v4-01.example .env
cd ~/dev/rf_luv && bash pipeline.sh up acars        # against the always-on infra
```

## Step 10: optional services

```bash
bash ops/noaa-pass-scheduler/install.sh   # NOAA scheduler (recorder still a scaffold)
bash ops/remote-desktop/enable-xrdp.sh    # xrdp + icewm
bash ops/remote-desktop/trust-tailscale-iface.sh
```

---

## Verification checklist

- [ ] `lsusb | grep -i RTL` shows the V4 (and a V3 if you added one)
- [ ] `/dev/rtl_sdr_v4` symlink exists (+ v3 if used)
- [ ] `systemctl --user is-active rtl-tcp@v4-01 rtl-scanner@v4-01` both `active`
- [ ] Grafana at :3000 renders the Spectrum folder, scans flowing (freshness probe not alerting)
- [ ] `clickhouse-backup.timer` enabled, first snapshot has non-zero rows, BACKUP_DIR is off-host
- [ ] ntfy topic rotated (or unset for a desktop-only setup); 09:00 UTC heartbeat fires
- [ ] ACARS (if deployed): messages climbing during daytime LGAV traffic

## Reference

- Install-order detail: each `ops/*/install.sh` header
- Dongle serials: `spectrum/docs/dongle_identity.md`
- Dongle cutover (V3/V4 swap): `spectrum/docs/dongle_cutover_runbook.md`
- ACARS deploy: `acars/DEPLOY.md`
- Backups: `ops/clickhouse-backup/README.md`
- Ports, dongle policy, reliability stack: `CLAUDE.md`
