# Dongle identity — how leap distinguishes V3 from V4

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
| rtl_tcp instance (V3) | `rtl-tcp@v3-01.service`, port 1234 |
| rtl_tcp instance (V4) | `rtl-tcp@v4-01.service`, port 1235 |
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

The order of indices (0/1) is still non-deterministic across reboots. That's expected — the wrapper script resolves serial→index on each start.

---

## Why rtl_tcp needs a wrapper

`rtl_tcp` takes `-d <device_index>` only. Depending on librtlsdr version it might accept `-d :<serial>` prefix syntax, but that's unreliable (various distribution builds strip it, Debian packaging handled it differently for years). We use a thin wrapper script that:

1. Runs `rtl_test 2>&1` to list devices with their serials and indices.
2. Picks the index matching the serial we asked for, or exits 1 with a clear "Serial X not found" message.
3. Calls `rtl_eeprom -d <index>` to verify the device behind that index actually has the serial we expect (guards against mid-lifecycle USB rebinds that reshuffle indices).
4. `exec`s `/usr/local/bin/rtl_tcp -d <index> <args>`.

If the dongle's USB connection flaps after wrapper exit, `systemctl Restart=always` will restart the unit, which re-runs the wrapper, which re-resolves serial→index cleanly. Stale index binding cannot persist.

Script lives at `ops/rtl-tcp/rtl-tcp-by-serial.sh`, installed to `/usr/local/bin/rtl-tcp-by-serial` on leap.

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
