# ghost/ host prep

The mechanical host setup (DVB blacklist, udev, rtl_tcp units, env files,
backups) is one command now, `ops/install-host.sh`. Its manual is
[`../RESTORE.md`](../RESTORE.md) step 5. This file keeps only what is specific
to the ghost pipeline.

> Validated live 2026-09-19/20. Findings and the exact commands are in
> [`docs/session-handoff-20260919.md`](docs/session-handoff-20260919.md).

## Layout

| Dongle | Serial | rtl_tcp | Filter | Role |
|--------|--------|---------|--------|------|
| V4 | `v4-01` | :1234 | FM bandstop **on** | spectrum scanner |
| V3 | `v3-01` | :1235 | none | ghost spirit box (needs the FM band open) |

Ghost holds the V3 for a whole session. It is not a `pipeline.sh` rotation.

## 1. Serials

Both dongles must carry their serial before anything else. See RESTORE.md
step 3 (one dongle on the bus at a time, physical replug after each write).

## 2. Install

```bash
bash infra/up.sh                                   # data layer first (creates the ghost DB)
bash ops/install-host.sh --scanner v4-01 --ghost v3-01 --gain 12 \
     --backup-dir /data/rf-clickhouse-backups
bash ops/install-host.sh --verify-only --scanner v4-01 --ghost v3-01 \
     --backup-dir /data/rf-clickhouse-backups
```

## 3. Antennas

- V4: about 57 cm per arm (VHF compromise), bandstop screwed in.
- V3: about 73 cm per arm ideal for FM (65 cm measured on the Omen works).
- Vertical, window-mounted. Double aluminium window frames attenuate UHF hard;
  FM and VHF pass fine.

## 4. First live ghost run

```bash
GHOST_GAIN=12 GHOST_RDS_DWELL_S=4 GHOST_RDS_TOP_N=20 \
  python3 ghost/spiritbox.py --mode forward --dwell-ms 150
docker exec clickhouse clickhouse-client --user ghost --password ghost_local \
  --query "SELECT round(freq_hz/1e6,1) mhz, ps, radiotext FROM ghost.stations ORDER BY freq_hz"
```

The WAV and its JSON sidecar land in `/data/rf_luv/ghost/recordings/`
(`GHOST_WAV_DIR`). Gain 12 is the validated value: gain 20 clipped 8 % on
Athens FM. A 4 s RDS dwell recovers PS and RadioText; 2 s recovers PI only.
The coordinator lock is on by default; add `--no-lock` if
`/var/lib/rtl-coordinator` is not installed yet.

If the V3 front end overloads on strong Athens FM (moving ghost carriers),
drop `GHOST_GAIN` first. If that is not enough, swap the dongles' roles.

## Quick manual path (no systemd, one-off)

For a one-off session without the installer. Does not survive a reboot.

```bash
sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null
rtl_eeprom -d 0 2>&1 | grep Serial      # confirm which index is which
rtl_tcp -d 0 -a 127.0.0.1 -p 1235 -s 2048000 &   # V3, ghost   (index as probed)
rtl_tcp -d 1 -a 127.0.0.1 -p 1234 -s 2048000 &   # V4, scanner
RTL_TCP_PORT=1235 GHOST_GAIN=12 python3 ghost/spiritbox.py --mode forward --dwell-ms 150 --no-lock
```
