# RTL-SDR Quick Reference - Athens

## Two-dongle layout (Omen)

| Dongle | Serial | Port | Role | Filter |
|--------|--------|------|------|--------|
| V4 | `v4-01` | :1234 | spectrum scanner (systemd, 24/7) | FM bandstop ON |
| V3 | `v3-01` | :1235 | listening / ghost (on demand) | none |

Both run as `rtl-tcp@<serial>.service` under systemd with a watchdog. The
scanner writes to ClickHouse continuously. The V3 is free for live listening
or the ghost pipeline.

## Live listening with SDR++ (the primary path)

SDR++ connects to the V3 over TCP while the scanner keeps running on the V4.

1. Open SDR++ (`sdrpp`)
2. Source dropdown → **RTL-TCP**
3. Host: `127.0.0.1`, Port: `1235`
4. Click **Start**
5. Tune to 99.6 MHz (Kosmos FM), mode WFM → confirm audio

The watchdog knows SDR++ is connected (it detects non-loopback TCP clients) and
will not restart the V3's rtl_tcp while you are listening.

Use `notes/listening-playbook.md` for what to expect at each frequency. Log
findings via the form at http://localhost:8084 (writes to
`spectrum.listening_log` in ClickHouse).

## First boot checklist

1. Plug in dongles
2. Native Linux: `bash ops/install-host.sh --scanner v4-01 --ghost v3-01 --gain 12 --backup-dir /data/rf-clickhouse-backups`
3. `docker network create rf_luv_net && bash infra/up.sh`
4. Open Grafana at http://localhost:3000 (admin/admin), Spectrum folder
5. Open SDR++ on V3 :1235, tune FM → confirm audio

Full rebuild: `RESTORE.md`. Ghost-specific: `ghost/HOSTPREP.md`.

## Common commands

### Continuous spectrum scanner (already running under systemd)
```bash
systemctl --user status rtl-scanner@v4-01     # is it running?
journalctl --user -u rtl-scanner@v4-01 -f     # live log
# Grafana: http://localhost:3000 (Spectrum folder)
# Full 88–470 MHz sweep every ~5 min, airband every 60s
```

### Quick one-shot spectrum scan (V3, stop rtl_tcp first)
```bash
systemctl --user stop rtl-tcp@v3-01           # free the V3
rtl_power -f 80M:500M:10k -i 10 -g 12 -e 300 -d 0 scan.csv
systemctl --user start rtl-tcp@v3-01          # give it back
```

### Listen to FM (via SDR++, not rtl_fm)

SDR++ is the right tool. `rtl_fm` cannot use `rtl_tcp` on rtl-sdr v2.0.3 (the
`-d tcp:` syntax is a keenerd fork feature not present in mainline). Use SDR++
on :1235 (V3) or stop rtl_tcp and use `rtl_fm` directly:

```bash
# Only if rtl_tcp is stopped on the V3:
systemctl --user stop rtl-tcp@v3-01
rtl_fm -M wfm -f 99.6M -s 200000 -r 48000 -d 0 - | aplay -r 48000 -f S16_LE
systemctl --user start rtl-tcp@v3-01
```

### Shell scripts (airband, AIS, ISM, satellite)

The scripts in `scripts/` use direct USB via `rtl_fm` / `rtl_433` / `rtl_ais`.
They need the V3's rtl_tcp stopped first:

```bash
systemctl --user stop rtl-tcp@v3-01
bash scripts/airband-listen.sh approach       # Athens Approach 118.575 AM
bash scripts/ais-monitor.sh                   # AIS ships 161.975/162.025
bash scripts/ism-monitor.sh                   # ISM 433 MHz sensors
bash scripts/satellite-pass.sh noaa19         # NOAA 19 APT (patio, V-dipole)
systemctl --user start rtl-tcp@v3-01          # give it back when done
```

### Decode pagers (POCSAG, stop rtl_tcp first)
```bash
rtl_fm -M fm -f 466.075M -s 22050 -g 12 -d 0 - | multimon-ng -t raw -a POCSAG512 -a POCSAG1200 -a POCSAG2400 -
```

### Record raw IQ
```bash
rtl_sdr -f 137.1M -s 2048000 -g 12 -d 0 -n 20480000 noaa_iq.raw   # ~10 sec
```

## SDR++ direct sampling (HF / shortwave)

1. Source → RTL-SDR (not RTL-TCP, needs direct USB — stop rtl_tcp first)
2. Direct Sampling → Q-branch
3. Sample rate: 2.048 MHz (view window 0–1 MHz)
4. Tune to target:
   - UVB-76 "The Buzzer": 4.625 MHz
   - BBC World Service: 9.410 MHz
   - Voice of Greece: 9.420 / 9.935 MHz
   - WWV time signal: 10.000 MHz
5. Demod: USB for SSB voice, AM for broadcast
6. Best after sunset (20:00–04:00 local), needs a long wire antenna (10–20 m)

## Gain

Validated at Polygono: **gain 12** on both dongles. Gain 20 clips on strong
Athens FM and airband. Start low, raise by 5 dB. Ghost copies of strong signals
at odd frequencies = gain too high (intermodulation from the 8-bit ADC).

## Antenna arm lengths (dipole, per arm)

```
FM Radio    100 MHz  →  75.0 cm     Formula: 7125 / freq_MHz = arm_cm
NOAA Sat    137 MHz  →  52.0 cm
VHF Marine  156 MHz  →  45.7 cm
AIS         162 MHz  →  44.0 cm
TETRA       390 MHz  →  18.3 cm
UHF         446 MHz  →  16.0 cm
ADS-B      1090 MHz  →   6.5 cm
```

## Key Athens frequencies

```
FM Broadcast      88–108 MHz        Strong, good first test
Athens Approach   118.575 MHz       Airport ATC (AM mode)
Athens Tower      118.1 MHz         ATC (AM)
ATIS              136.125 MHz       Airport weather
NOAA 15           137.620 MHz       Weather satellite (patio)
NOAA 18           137.9125 MHz      Weather satellite
NOAA 19 / Meteor  137.100 MHz       Weather satellite
Marine Ch16       156.800 MHz       Distress/calling
AIS Ch87          161.975 MHz       Ship positions
AIS Ch88          162.025 MHz       Ship positions
Greek TETRA       380–400 MHz       Emergency services (encrypted)
ISM Band          433.920 MHz       Sensors, remotes, weather stations
PMR446            446.0–446.2 MHz   Walkie-talkies
ADS-B             1090 MHz          Aircraft positions
UVB-76 (HF)       4.625 MHz        Number station (direct sampling)
```

## Troubleshooting

- **No signal**: antenna disconnected, or DVB driver claimed the dongle (`sudo modprobe -r dvb_usb_rtl28xxu`)
- **Wrong demod mode**: WFM for FM broadcast, NFM for comms, AM for airband
- **Ghost signals everywhere**: gain too high, reduce by 10 dB
- **`usb_claim_interface error -6`**: DVB driver or another rtl_tcp holds the dongle
- **rtl_tcp connection refused**: `systemctl --user is-active rtl-tcp@v4-01` and `ss -tlnp | grep 1234`
- **Dongle visible in `lsusb` but not `rtl_test`**: another process holds it (librtlsdr hides claimed devices)
- **Shell scripts fail**: stop `rtl-tcp@v3-01` first — the scripts use direct USB
- **SDR++ cannot connect**: check port (V3 = 1235, V4 = 1234) and that rtl_tcp is running
