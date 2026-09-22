# Dongle identity — how the host distinguishes V3 from V4

> Port layout note (native Omen, two dongles): the FM-notched **V4** runs the
> spectrum scanner on rtl_tcp **:1234**; the bare **V3** is the ghost-pipeline
> dongle on rtl_tcp **:1235**. This is the reverse of leap's V3:1234/V4:1235
> split (leap ran the scanner on the V3). The rest of this doc is the original
> leap-era procedure; the serial scheme and the EEPROM VBUS gotcha are unchanged.

## Why this document exists

The RTL-SDR enumeration order on the USB bus is non-deterministic. `-d 0` binds to "whichever dongle the kernel found first", which flips on boot, on watchdog rebinds, and whenever USB power-saving unplugs one device. With two dongles we need a stable identity independent of index.

This is solved by writing a unique serial number into each dongle's EEPROM, letting librtlsdr and udev match on that serial, and giving everything downstream (rtl_tcp instance, scanner env file, ClickHouse `dongle_id` column, Grafana filter variable) a consistent name.

## Naming

| Purpose | Value |
|---|---|
| V3 dongle serial (current) | `v3-01` (set during setup — see "V3 serial-read procedure" below) |
| V4 dongle serial | `v4-01` |
| udev symlink (V3) | `/dev/rtl_sdr_v3` |
| udev symlink (V4) | `/dev/rtl_sdr_v4` |
| rtl_tcp instance (V3) | `rtl-tcp@v3-01.service`, port 1235 (ghost dongle) |
| rtl_tcp instance (V4) | `rtl-tcp@v4-01.service`, port 1234 (spectrum scanner) |
| scanner instance (V3) | `rtl-scanner@v3-01.service` |
| scanner instance (V4) | `rtl-scanner@v4-01.service` |
| ClickHouse `dongle_id` (V3) | `'v3-01'` (LowCardinality(String)) |
| ClickHouse `dongle_id` (V4) | `'v4-01'` |

Serial convention: 8 characters max (RTL-SDR EEPROM limit), lowercase, hyphen-separated. The `-01` suffix leaves room for a `v3-02` replacement dongle without renaming everything.

---

## V4 serial-write procedure — recommended (OFF leap)

**This is the path to use.** V4 arrives with default serial `00000001` from the factory. So does any other RTL-SDR dongle. If V4 is plugged into leap alongside V3 before its serial is changed, udev cannot distinguish them — and bringing V3 offline to fix it defeats the whole point of this infrastructure prep.

### Steps (dev laptop with V4 alone on USB)

```bash
# 1. Plug V4 into any Linux host. Must be the ONLY RTL-SDR on that host's USB bus.
lsusb | grep RTL          # expect one 0bda:2838 entry
rtl_eeprom -d 0           # inspect current EEPROM — serial likely "00000001"

# 2. Write new serial
rtl_eeprom -d 0 -s v4-01

# 3. Verify (requires unplug/replug for the new EEPROM to re-read)
# Unplug V4, wait 3s, plug back in
lsusb | grep RTL
rtl_eeprom -d 0 | grep -i serial
# Expect:  Serial number:        v4-01
```

After this, V4 can be shipped to leap and plugged in at any time alongside V3 without enumeration ambiguity.

---

## V4 serial-write procedure — fallback (ON leap, if V4 arrives pre-plugged)

Only use this if V4 has already been plugged into leap with its default serial. Doing this requires powering leap down and interrupting V3.

```bash
# 1. Power leap down fully
sudo systemctl poweroff

# 2. Physically unplug V3. Leave ONLY V4 plugged in.

# 3. Boot leap. Services will start but rtl_tcp will bind to whatever dongle it
#    finds (V4, since it's the only one). Stop the scanner pipeline temporarily
#    to free the device:
systemctl --user stop rtl-scanner@v3-01 rtl-tcp@v3-01

# 4. Confirm only one device is visible
lsusb | grep RTL          # expect one 0bda:2838

# 5. Write serial
rtl_eeprom -d 0 -s v4-01
rtl_eeprom -d 0 | grep -i serial   # verify

# 6. Power leap down again
sudo systemctl poweroff

# 7. Physically plug V3 back in alongside V4, then boot
```

Total V3 downtime: ~10–15 minutes (boot + shutdown cycles). Budget one to two missed full sweeps.

---

## V3 serial-read procedure (ON leap)

If V3's current serial is already set and documented, skip this. Otherwise:

```bash
# Stop the scanner pipeline briefly — rtl_eeprom needs exclusive access to the
# USB device, which rtl_tcp holds while a sweep is in progress. The airband
# preset fires every 60s, so there is no 230s idle window on this host.
#
# Budget: ~15s total.  One missed airband sweep at worst.

systemctl --user stop rtl-scanner@v3-01 rtl-tcp@v3-01
rtl_eeprom -d 0 | grep -i serial
systemctl --user start rtl-tcp@v3-01 rtl-scanner@v3-01
```

Record the serial in this document's "Naming" table.  If the V3 serial is still the factory default `00000001`, write a new one:

```bash
systemctl --user stop rtl-scanner@v3-01 rtl-tcp@v3-01
rtl_eeprom -d 0 -s v3-01
# Unplug V3 and plug it back in (or reboot) — EEPROM needs re-enumeration.
# The udev rule below will create /dev/rtl_sdr_v3 automatically on re-enumeration.
systemctl --user start rtl-tcp@v3-01 rtl-scanner@v3-01
```

---

## udev rules

**File:** `ops/udev/99-rtl-sdr.rules` (committed in repo, installed to `/etc/udev/rules.d/` on leap)

```udev
# RTL-SDR Blog V3/V4 stable-name symlinks by serial.
# Match 0bda:2838 (RTL2838UHIDIR, used by Blog V3 and V4) and create /dev
# symlinks keyed on the serial written via rtl_eeprom. Downstream tools
# (rtl_tcp wrapper, rtl-usb-reset) resolve the current USB index via these.
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", ATTRS{serial}=="v3-01", SYMLINK+="rtl_sdr_v3", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", ATTRS{serial}=="v4-01", SYMLINK+="rtl_sdr_v4", MODE="0666", GROUP="plugdev"
```

### Installation

```bash
sudo cp ops/udev/99-rtl-sdr.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=usb
```

### Verification

```bash
# symlinks exist (after next USB re-enumeration — unplug/replug or reboot)
ls -la /dev/rtl_sdr_v3 /dev/rtl_sdr_v4

# kernel agrees the serial is what we wrote
lsusb -v -d 0bda:2838 2>/dev/null | grep -i iSerial

# librtlsdr agrees
rtl_test 2>&1 | grep -E 'SN|Found'
```

Expected output (`rtl_test`):

```
Found 2 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: v3-01
  1:  Realtek, RTL2838UHIDIR, SN: v4-01
```

The order of indices (0/1) is still non-deterministic across reboots. That is expected. rtl_tcp starts with `-d <serial>`, so the index is never used.

---

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

---

## Collision mitigation

**Precondition for V4 install** (hard requirement, not a nice-to-have):

V4's serial must be `v4-01` *before* V4's USB connector is plugged into leap alongside V3.  If both dongles enumerate with default serial `00000001`:

- udev cannot tell them apart — only one of `/dev/rtl_sdr_v3` and `/dev/rtl_sdr_v4` will appear, bound to whichever dongle the kernel saw first.
- `rtl_test` reports two devices with identical `SN: 00000001`; the wrapper script will pick index 0 for both serials.
- Fixing it on leap requires running `rtl_eeprom -d <i> -s v4-01` against the correct `<i>` — but which index is V4? No way to tell without physical unplug.
- The only recovery path is the "ON leap fallback" procedure above, which takes V3 down for ~15 minutes.

This is why the recommended procedure puts V4 on a dev laptop first. No exceptions without explicit approval.

---

## Go/no-go check before starting the physical install

Run this checklist immediately before plugging V4 into leap. If any item fails, do not proceed.

- [ ] V3 serial on leap matches `v3-01`:
  `rtl_eeprom -d 0 | grep -i serial` shows `v3-01` (with scanner stopped briefly per V3 procedure above)
- [ ] V4 serial (on dev laptop, with V4 alone on USB) shows `v4-01`:
  `rtl_eeprom -d 0 | grep -i serial` shows `v4-01`
- [ ] udev rules installed on leap:
  `ls -la /etc/udev/rules.d/99-rtl-sdr.rules` exists
- [ ] `rtl-tcp-by-serial` wrapper on leap:
  `command -v rtl-tcp-by-serial` prints a path
- [ ] `/etc/rtl-scanner/v4-01.env` exists on leap (populated with real values, no remaining TODO markers):
  `grep -c TODO /etc/rtl-scanner/v4-01.env` returns `0`

If all five pass, V4 can be plugged in. Run `sudo udevadm trigger --subsystem-match=usb` after plug-in to force symlink creation without waiting for natural re-enumeration.
