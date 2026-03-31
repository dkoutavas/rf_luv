# RTL-SDR Quick Reference — Athens

## First Boot Checklist
1. Plug in dongle
2. Windows: Zadig → Bulk-In Interface 0 → WinUSB → Replace Driver
3. Open SDR++ → Source: RTL-SDR → Sample Rate: 2.048 MHz → Gain: 30 dB
4. Tune to 99.6 MHz (Kosmos FM) or 105.8 MHz (Skai) → confirm audio
5. If no signal: check gain, check Zadig targeted the right interface

## rtl_tcp Bridge (Windows → WSL)
```
# Windows CMD/PowerShell:
rtl_tcp -a 0.0.0.0 -p 1234 -s 2048000

# WSL tools connect to:
rtl_fm -d tcp:127.0.0.1:1234 ...
```

## Common Commands

### Listen to FM station
```bash
rtl_fm -M wfm -f 99.6M -s 200000 -r 48000 - | aplay -r 48000 -f S16_LE
```

### Quick spectrum scan (5 min)
```bash
rtl_power -f 80M:500M:10k -i 10 -g 40 -e 300 scan.csv
python3 ~/.local/bin/heatmap.py scan.csv scan.png
```

### Decode pagers (POCSAG)
```bash
rtl_fm -M fm -f 466.075M -s 22050 -g 40 - | multimon-ng -t raw -a POCSAG512 -a POCSAG1200 -a POCSAG2400 -
```

### ISM band device scan (weather stations, sensors, remotes)
```bash
rtl_433 -g 40 -f 433.92M -F json
```

### ADS-B quick test (no Docker)
```bash
rtl_power -f 1090M:1090M:1M -i 1 -g 50 -e 10 /dev/null  # just check signal
# Or if dump1090 is installed:
dump1090 --interactive --gain 40
```

### Record raw IQ (for later analysis in GNU Radio / SDR++)
```bash
rtl_sdr -f 137.1M -s 2048000 -g 40 -n 20480000 noaa_iq.raw  # ~10 sec
```

### AIS ship tracking
```bash
rtl_fm -M fm -f 161.975M -s 12500 -g 40 - | multimon-ng -t raw -a AIS -
```

## SDR++ Direct Sampling (HF/Shortwave)
1. Source → RTL-SDR → Direct Sampling → Q-branch
2. Sample rate: 2.048 MHz (gives you 0–1 MHz view window)
3. Tune to target — remember frequencies are in kHz:
   - UVB-76: 4625 kHz = 4.625 MHz
   - BBC World Service: 9410 kHz
   - Voice of Greece: 9420 kHz / 9935 kHz
   - Time signal WWV: 10000 kHz
4. Use USB (Upper Sideband) for SSB voice, AM for broadcast
5. Best reception: after sunset, ideally 20:00–04:00 local

## Gain Tuning Method
1. Set gain to 0 → note noise floor level on waterfall
2. Increase gain in steps of 5 dB
3. Watch for target signal AND noise floor
4. Stop when: signal improves but noise floor starts rising fast
5. Sweet spot is usually 28–42 dB depending on band and local interference
6. If you see "ghost" signals that move when you retune → gain too high (intermod)

## Antenna Arm Lengths (dipole, per arm)
```
FM Radio    100 MHz  →  75.0 cm
NOAA Sat    137 MHz  →  52.0 cm
VHF Marine  156 MHz  →  45.7 cm
AIS         162 MHz  →  44.0 cm
TETRA       390 MHz  →  18.3 cm
UHF         446 MHz  →  16.0 cm
ADS-B      1090 MHz  →   6.5 cm

Formula: 7125 / freq_MHz = arm_cm
```

## Key Athens Frequencies
```
FM Broadcast      88–108 MHz        Strong, good first test
Athens Approach   118.575 / 119.1   Airport ATC (AM mode)
NOAA 15           137.620 MHz       Weather satellite
NOAA 18           137.9125 MHz      Weather satellite
NOAA 19 / Meteor  137.100 MHz       Weather satellite
Marine Ch16       156.800 MHz       Distress/calling
AIS Ch87          161.975 MHz       Ship positions
AIS Ch88          162.025 MHz       Ship positions
Greek TETRA       380–400 MHz       Emergency services
ISM Band          433.920 MHz       Sensors, remotes, weather stations
PMR446            446.0–446.2 MHz   Walkie-talkies
ADS-B             1090 MHz          Aircraft positions
UVB-76 (HF)       4.625 MHz        Number station (direct sampling)
```

## Troubleshooting
- **No signal at all**: Zadig wrong interface, or antenna not connected
- **Signal but no audio**: check demod mode (WFM for broadcast, NFM for comms, AM for airband)
- **Ghost signals everywhere**: gain too high, reduce by 10 dB
- **Signals drift**: normal for non-TCXO dongles, your V3 has TCXO so drift should be <1 ppm
- **USB drops / glitches**: try lower sample rate (1.024 MS/s), shorter USB cable
- **rtl_tcp connection refused**: check Windows firewall, or use 127.0.0.1 not localhost
- **WSL can't see USB**: need usbipd-win, or use rtl_tcp bridge instead
