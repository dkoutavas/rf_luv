# 164–168 MHz session briefing

_Generated 2026-04-18 13:04 UTC from live ClickHouse on leap. Numbers are live — if you want fresher, regenerate; otherwise aperol and go._

## 1. At a glance

| Freq (MHz) | Class · conf | Duty 24h | Burst p50/p95 | Peak hr (Athens) | Power | Allocation | BW |
|---|---|---:|---|---|---:|---|---|
| 164.73 | nfm_voice_repeater · 0.80 | 0.134 | 1650 s / 2310 s | 11:00 | -1.6 dBFS | land_mobile_safety | 12.5 kHz |
| 166.77 | nfm_voice_repeater · 0.80 | 0.122 | 1200 s / 2220 s | flat | -1.3 dBFS | land_mobile_safety | 12.5 kHz |
| 168.82 | nfm_voice_repeater · 0.80 | 0.122 | 1800 s / 2340 s | flat | -1.4 dBFS | land_mobile_safety | 12.5 kHz |

## 2. Tuning recipes

Copy-paste mentally into SDR++.

- **164.73 MHz** · NFM, filter 12.5 kHz, squelch -40 dBFS (tighten if it chatters on noise) · best odds: flat across the day · weekday pattern undersampled
- **166.77 MHz** · NFM, filter 12.5 kHz, squelch -40 dBFS (tighten if it chatters on noise) · best odds: flat across the day · weekday pattern undersampled
- **168.82 MHz** · NFM, filter 12.5 kHz, squelch -40 dBFS (tighten if it chatters on noise) · best odds: flat across the day · weekday pattern undersampled

## 3. What you'll probably hear

### 164.73 MHz
Long sustained bursts (~28 min p50), narrow FM, power -1.6 dBFS. This is NOT momentary dispatch — it's either a keyed-up idle repeater carrier (hiss + tail, no voice), a data/telemetry link, or a long conversation that ran through the sample window. Tune in: silence-with-squelch-open = idle carrier; packet-modem hiss = data; continuous voice = a hot mic or a very busy channel. Duty 0.13 means about 193 min/day active.

### 166.77 MHz
Long sustained bursts (~20 min p50), narrow FM, power -1.3 dBFS. This is NOT momentary dispatch — it's either a keyed-up idle repeater carrier (hiss + tail, no voice), a data/telemetry link, or a long conversation that ran through the sample window. Tune in: silence-with-squelch-open = idle carrier; packet-modem hiss = data; continuous voice = a hot mic or a very busy channel. Duty 0.12 means about 175 min/day active.

### 168.82 MHz
Long sustained bursts (~30 min p50), narrow FM, power -1.4 dBFS. This is NOT momentary dispatch — it's either a keyed-up idle repeater carrier (hiss + tail, no voice), a data/telemetry link, or a long conversation that ran through the sample window. Tune in: silence-with-squelch-open = idle carrier; packet-modem hiss = data; continuous voice = a hot mic or a very busy channel. Duty 0.12 means about 175 min/day active.

## 4. Confirmation template

Paste into the Listening Playbook form (or the logging-form page at http://192.168.2.10:8084). Filled-in fields are my best guess — adjust as needed. A confirmation flips the classifier override to confidence 1.0 on its next run.

**164.73 MHz**
```
Frequency (MHz): 164.73
Mode: NFM
Heard: [yes / no / briefly]
Class: nfm_voice_repeater     (adjust if something different)
Language: [Greek / English / data / unclear]
Notes: [callsigns, vessel names, language, anything distinctive]
```

**166.77 MHz**
```
Frequency (MHz): 166.77
Mode: NFM
Heard: [yes / no / briefly]
Class: nfm_voice_repeater     (adjust if something different)
Language: [Greek / English / data / unclear]
Notes: [callsigns, vessel names, language, anything distinctive]
```

**168.82 MHz**
```
Frequency (MHz): 168.82
Mode: NFM
Heard: [yes / no / briefly]
Class: nfm_voice_repeater     (adjust if something different)
Language: [Greek / English / data / unclear]
Notes: [callsigns, vessel names, language, anything distinctive]
```

## 5. Back pocket

**Antenna:** rooftop dipole, 57 cm arms, ~3 m height, 165° (roughly S).

**Reference signals** (tune these if 164–168 is dead to confirm the chain works):
- 136.254 MHz · AM · Athens ATIS — continuous weather broadcast, should always be there
- ~100 MHz · WFM · nearest strong FM broadcaster (Kosmos 99.6, Skai 105.8)
- 156.800 MHz · NFM · Marine Ch16 — sporadic but characteristic when it keys up

**Confirmation in the Listening Playbook triggers a classifier override** at confidence 1.0 on next run (within 5 min). The override matches by nearest-bin ±150 kHz so you don't need to hit the scanner's exact bin.

**Dongle is single-client.** SDR++ and the scanner-on-leap can't share `rtl_tcp` at the same time. To free the dongle for listening:
```
# on leap, pause the scanner (ingest and migrations stop cleanly)
cd ~/dev/rf_luv/spectrum && docker compose stop spectrum-scanner

# tune/listen on SDR++ against rtl_tcp at 192.168.2.10:1234

# resume after the session
docker compose start spectrum-scanner
```
The feature extractor and classifier keep running on their own timers; they just see a gap in `scans` and don't produce new peaks for the gap window. ClickHouse stays up. Nothing downstream breaks.

## 6. If it's dead air for 20 min

For 164.73 MHz, 166.77 MHz, 168.82 MHz the diurnal pattern is flat — activity is spread across the day, so there's no "best hour". Come back in a few hours and tune again, or leave SDR++ recording the baseband and scrub through later.

**Partial ID is fine.** If you hear something but can't pin language or purpose, log notes like `unclear, bursts every ~N min, sounds like [dispatch / modem / carrier]` — the classifier only needs the class_id to flip the override; notes are for you-later.
