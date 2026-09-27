# Dongle identity: how the host tells the dongles apart

Each dongle carries its own serial in its EEPROM, and everything on the host
keys on that serial, never on the USB index. Which dongle does which job (the
scanner, the ghost pipeline) changes with the hardware; the current roles are in
`CLAUDE.md` under "Current Project State".

## Why this document exists

The RTL-SDR enumeration order on the USB bus is non-deterministic. `-d 0` binds to "whichever dongle the kernel found first", which flips on boot, on watchdog rebinds, and whenever USB power-saving unplugs one device. With two dongles we need a stable identity independent of index.

This is solved by writing a unique serial number into each dongle's EEPROM, letting librtlsdr and udev match on that serial, and giving everything downstream (rtl_tcp instance, scanner env file, ClickHouse `dongle_id` column, Grafana filter variable) a consistent name.

## Naming

| Purpose | V3 | V4 |
|---|---|---|
| EEPROM serial | `v3-01` | `v4-01` |
| udev symlinks | `/dev/rtl_sdr_v3-01`, `/dev/rtl_sdr_v3` | `/dev/rtl_sdr_v4-01`, `/dev/rtl_sdr_v4` |
| rtl_tcp instance | `rtl-tcp@v3-01.service`, port 1235 | `rtl-tcp@v4-01.service`, port 1234 |
| scanner instance | `rtl-scanner@v3-01.service` | `rtl-scanner@v4-01.service` |
| ClickHouse `dongle_id` | `'v3-01'` | `'v4-01'` |

Serial convention: 8 characters at most (the EEPROM limit), lowercase,
hyphen-separated. The `-01` suffix leaves room for a `v3-02` replacement dongle
without renaming everything.

## Writing a serial

Follow `RESTORE.md` step 3: write each serial with only that dongle plugged in,
then power-cycle it by unplugging (the EEPROM is re-read only on a real VBUS
cycle). Never plug in two dongles that still carry the factory serial
`00000001`: udev and librtlsdr cannot tell them apart, and the only way to find
out which is which is to unplug one. `ops/install-host.sh` refuses to continue
while any dongle reads `00000001`.

## udev rules

`ops/udev/99-rtl-sdr.rules`, installed by `ops/rtl-tcp/install.sh` (which fills
in a group that exists on this distro; openSUSE has no `plugdev`). It gives every
RTL2838 dongle `/dev/rtl_sdr_<serial>` plus the short `/dev/rtl_sdr_v3` and
`/dev/rtl_sdr_v4` names, and starts `rtl-tcp@<serial>` on plug (see "Plug and
play" below).

Verify:

```bash
ls -la /dev/rtl_sdr_*
rtl_test 2>&1 | grep -E 'SN|Found'
```

The USB index order (0/1) changes between boots. That is expected: rtl_tcp
starts with `-d <serial>`, so the index is never used.

## Why rtl_tcp needs a wrapper

It barely does. `rtl_tcp -d` accepts a serial string: librtlsdr's
`verbose_device_search` tries an index first, then an exact serial match, then
a serial suffix match. It reads the USB string descriptors without claiming
the interface, so the lookup works while the other dongle is held by its own
`rtl_tcp`. Tested on rtl-sdr 2.0.3 on 2026-09-22:

```
$ rtl_tcp -d v4-01 -p 1299      # while another rtl_tcp holds the V4
Found 1 device(s):
  0:  RTLSDRBlog, Blog V4, SN: v4-01
Using device 0: Generic RTL2832U OEM
usb_claim_interface error -6    # expected: the sibling holds it
$ rtl_tcp -d nope-99
No matching devices found.
```

`ops/rtl-tcp/rtl-tcp-by-serial.sh` is therefore one line: `exec rtl_tcp -d "$SERIAL" "$@"`.
It stays as a wrapper only so the systemd template has one ExecStart and one
log prefix.

History: until 2026-09-22 the wrapper probed each index with `rtl_eeprom`,
which claims the interface and therefore cannot read the serial of a dongle
another process holds. That was misread as "librtlsdr hides claimed devices",
and the workaround was a fixed `RTL_TCP_DEVICE_INDEX` per env file. On
2026-09-22 the host rebooted with the V3 unplugged; `rtl-tcp@v3-01` (index 0)
opened the V4 and `rtl-tcp@v4-01` (index 1) restart-looped for 40 minutes,
writing 116 empty `scan_runs` rows. The index path is gone.

## Plug and play

- `ops/udev/99-rtl-sdr.rules` tags the dongle `systemd` and sets
  `SYSTEMD_USER_WANTS=rtl-tcp@<serial>.service`, so a plug starts the unit.
- `ops/install-host.sh` writes `~/.config/systemd/user/rtl-tcp@<serial>.service.d/10-device.conf`
  with `BindsTo=` and `After=` the device unit (for example
  `dev-rtl_sdr_v4\x2d01.device`), so an unplug stops the unit and it does not
  restart until the dongle returns. The template cannot express this itself:
  `%i` keeps the dash, the device unit name escapes it.
- The scanner dongle also gets `20-scanner.conf` with `Wants=rtl-scanner@<serial>.service`.
  The scanner has `Requires=rtl-tcp@`, so it follows the dongle down and up.
  One `scan_runs` row per plug cycle.
- `rtl-tcp-watchdog.py` skips its tick while `/dev/rtl_sdr_<serial>` is missing.
