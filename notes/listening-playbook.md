# Athens Listening Playbook

Standing reference for what to expect across the spectrum and how to identify new finds. Companion to:

- `QUICKREF.md` (root) — terminal cheat sheet, command-line one-liners.
- `notes/signal-log.txt` — free-form journal of individual finds, dated.
- `spectrum/logging/index.html` + `spectrum.listening_log` table — structured per-session form (Grafana dashboard at `http://scanner:3003` → Listening Playbook).

This file is the "what should I expect at frequency X" + "I heard Y, what is it?" reference. Update when new emitters are identified or new modes are encountered.

---

## Demod Mode Primer

| Mode | Bandwidth | What it sounds like | When to pick it |
|---|---|---|---|
| **WFM** | 150-200 kHz | Hi-fi music + voice, stereo | Broadcast FM 88-108 MHz |
| **NFM** | 12.5-25 kHz | Compressed voice, "walkie-talkie" | Marine, PMR, business radio, amateur 2m/70cm, public-safety analog |
| **AM** | 6-15 kHz | Voice with carrier hiss, AM broadcast | Airband (118-137 MHz), shortwave broadcast (HF), CB |
| **USB** | 2.4-3 kHz | Voice, no carrier; "Donald Duck" if mistuned | HF amateur SSB voice 14/21/28 MHz, marine HF, utility |
| **LSB** | 2.4-3 kHz | Voice, no carrier (lower sideband) | HF amateur SSB on 80m / 40m (3.5 / 7 MHz) |
| **CW** | 50-500 Hz | Pure tone, on/off keying = morse | HF beacons, time signals (RWM), amateur morse, weak-signal work. **In SDR++: select CW mode → BFO offset 600-800 Hz so the tone is audible.** A continuous tone with no on/off pattern = unmodulated carrier (broadcast off-air, beacon, intermod product). |
| **APT** | 34 kHz | Tick-tick-tick from satellite | NOAA 15/18/19 (137 MHz). Use `noaa-apt` to convert WAV to image. |
| **OFDM / digital** | 7-8 MHz / 25 kHz | White noise / hash | DVB-T (Hymettus muxes), DAB (169 MHz), TETRA (380-400). Cannot demodulate to audio — visual-only. |

CW note: many "raspy" or "high-pitched continuous" HF signals are actually unmodulated AM carriers from broadcast stations between scheduled programs. CW mode with the offset is the easiest way to hear them at all (AM mode would just give silence + hiss).

---

## Frequency Catalog by Band

### HF (0-30 MHz) — direct sampling on V3 (Q-branch) or native on V4

Direct sampling reminder for V3: SDR++ → Source → RTL-SDR → Direct Sampling → **Q-branch**. V4 has built-in HF reception, no toggle. Best after sunset for ionospheric propagation.

| Freq | Mode | Name | Sound | Decoder / notes |
|---|---|---|---|---|
| 4.625 MHz | AM (or USB) | UVB-76 "The Buzzer" | Continuous buzz, occasional voice | Russian number station. Always-on. Tune to confirm dongle/HF setup is working. |
| 4.996 / 9.996 MHz | CW | RWM time signal | Pulsed CW tones, callsign in morse | Russian time/freq standard. Useful for verifying TCXO frequency drift on V3. |
| 9.410 MHz | AM | BBC World Service | English voice, music | 31m band shortwave broadcast |
| 9.420 / 9.935 MHz | AM | Voice of Greece | Greek voice, music | National shortwave service |
| 10.000 MHz | AM | WWV (NIST USA) | Voice ID + tones every minute | Time signal from Colorado. Hearable in Athens at night. |

### VHF Low (30-88 MHz)

Mostly ham 6m, some legacy paging, intermittent. Not actively catalogued yet — log finds in `signal-log.txt` and we'll promote.

### FM Broadcast (88-108 MHz)

Saturated by Lycabettus/Hymettus transmitters. **Use V4 (no filter).** WFM mode, 200 kHz channels.

| Freq | Name | Notes |
|---|---|---|
| 99.6 | Kosmos FM | Strong, good first-tune test |
| 105.8 | Skai | Strong |

V3 is filtered — 88-108 is heavily attenuated by design. Don't use V3 for FM broadcast listening.

### VHF Airband (108-137 MHz)

AM mode, 8.33 kHz channel spacing. **V3 (filtered) gives much better reception** — FM intermod was masking weak ATC.

| Freq | Name | Notes |
|---|---|---|
| 118.100 | Athens Tower | Final approach + departure |
| 118.575 | Athens Approach | Vectors to final |
| 119.100 | Athens Approach (alt) | |
| 121.500 | Guard / Emergency | International distress, AM. Always-on monitoring. |
| 136.125 | Athens ATIS | Continuous automated weather/runway broadcast |

### VHF Satcom (137-138 MHz) — patio only

Need sky view; indoor reception will not work. APT mode for analog NOAAs, LRPT for Meteor (digital, use `satdump`).

| Freq | Name | Schedule |
|---|---|---|
| 137.100 | NOAA 19 / Meteor M2-3 | Use `gpredict` for pass times |
| 137.620 | NOAA 15 | |
| 137.9125 | NOAA 18 | |

Recording: `bash scripts/satellite-pass.sh noaa15` — see script header.

### VHF Marine + AIS (156-162 MHz)

NFM mode for voice channels (12.5-25 kHz). AIS Ch87/88 are digital — use `multimon-ng -a AIS` or run the AIS pipeline (`ais/docker-compose.yml`). Best with antenna pointed SW toward Piraeus.

| Freq | Name | Mode | Notes |
|---|---|---|---|
| 156.050 | Marine Ch1 | NFM | Piraeus port operations |
| 156.650 | Marine Ch13 | NFM | Bridge-to-bridge, often English |
| 156.800 | Marine Ch16 | NFM | Distress / calling, monitored |
| 158.080 | Marine Coast Stn | NFM | Piraeus Radio coast station |
| 160.130 | Marine Coast TX | NFM | Coast station duplex TX |
| 160.730 | Marine Coast Rpt | NFM | Piraeus coast repeater |
| 161.975 | AIS Ch87 | digital | Ship positions — fed to AIS pipeline |
| 162.025 | AIS Ch88 | digital | Ship positions — fed to AIS pipeline |

### VHF Business / Utility (162-174 MHz)

Lots of activity post-filter that wasn't audible before. This is where the V3 filter pays off most visibly: V3 noise floor ~-46 dBFS clean, V4 noise floor in the same band ~-10 dBFS hash from FM intermod.

NFM mode, 12.5-25 kHz channels. Mostly business PMR, utility telemetry, paging. See "Active investigations" below for unconfirmed emitters.

### VHF Gov / Military (380-400 MHz) — Greek TETRA

Digital, encrypted. Show up as wideband hash on the waterfall. **Listening to encrypted comms is illegal in Greece and most EU countries** even though receiving the RF is fine. We catalog the channels (384 MHz center) but do not attempt decode.

### UHF ISM (433.92 MHz)

Mixed protocols (OOK, FSK, Manchester) — `rtl_433` handles all of them. Don't try to hear it on speakers; pipe to `rtl_433` and read the JSON.

```bash
bash scripts/ism-monitor.sh        # live decoded events
```

Pipeline lives at `ism/docker-compose.yml` for continuous ingest into ClickHouse.

### UHF PMR446 (446.0-446.2 MHz)

License-free walkie-talkies. NFM, 12.5 kHz channels. Often hear delivery riders, neighbors testing.

| Freq | Name |
|---|---|
| 446.00625 | PMR446 Ch1 |
| 446.01875 | PMR446 Ch2 |
| 446.03125 | PMR446 Ch3 |
| 446.21000 | observed activity (maybe DMR variant) |

### L-band (1090 MHz) — ADS-B

Aircraft transponders. Don't try to hear; run the ADS-B pipeline:

```bash
cd adsb && docker compose up -d
# tar1090 map: http://localhost:8080
# Grafana:     http://localhost:3000
```

---

## Identification Flow — "I heard X, what is it?"

Walk through these in order:

**1. What band are you in?** → narrows the universe.

**2. What demod gives intelligible audio?**
- WFM works → broadcast FM only.
- AM works → airband, shortwave, CB, aviation HF.
- NFM works → land mobile (marine, PMR, business, ham), public safety analog.
- USB/LSB works → HF voice (amateur, marine HF, utility).
- CW gives a clean tone → HF beacon, time signal, off-air carrier, or unmodulated intermod.
- Nothing works (just hash) → digital signal: DMR/dPMR/DVB-T/TETRA/AIS/POCSAG. Capture IQ and run through a decoder.

**3. What's the cadence?**
- Continuous → broadcast, repeater, control channel, beacon.
- Periodic (1 sec to 15 min) → telemetry, paging, scheduled net, beacon.
- Bursty (random) → human voice, packet data.
- One-shot then gone → opportunistic emitter, mobile.

**4. What's the texture?**
- Voice (any language) → land mobile (NFM), airband (AM), HF (USB/LSB), broadcast (AM/WFM).
- Tones / warbling → POCSAG paging, modem, telemetry.
- Raspy hash → digital voice (DMR, dPMR, P25), TETRA, packet data.
- Clean tone → CW beacon, unmodulated carrier, intermod product.

**5. Worked example — 164.025 MHz NFM 1-sec raspy:**
- Band: VHF business/utility (162-174).
- Demod: NFM works, but hash not voice.
- Cadence: periodic, very fast (1 sec).
- Texture: raspy digital.
- → candidate set: utility SCADA polling, DMR control channel, automated telemetry. See "Active investigations."

**6. Worked example — 9.7097 MHz CW high-pitch continuous:**
- Band: HF, 31m broadcast (9400-9900 kHz region).
- Demod: CW gives clean audible tone; AM gives silence + hiss = unmodulated carrier.
- Cadence: continuous (no on/off keying = not real morse).
- → candidate set: shortwave broadcaster off-air carrier between programs, utility beacon, intermod from a strong nearby station. See "Active investigations."

---

## Capture & Decode Workflow

### IQ recording in SDR++ (recommended for unknown signals)

In SDR++: ☰ menu → Recorder → Mode: **Baseband**. Tune to the signal, set sample rate to 250-500 kHz around the carrier (smaller = smaller file). Press record. 30 seconds is enough for most identification work.

Output: `.wav` file containing IQ samples (not audio). Open back in SDR++ as a "File" source for offline analysis, or feed to specialized decoders.

### Audio recording

In SDR++: Recorder → Mode: **Audio**. Records the demod output as a normal WAV. Useful for documenting unusual sounds or running through `multimon-ng`.

### Offline decoders — what's installed (per `setup/install-wsl.sh`)

| Tool | What it decodes | One-liner |
|---|---|---|
| `multimon-ng` | POCSAG, FLEX, AIS, DTMF, EAS, AFSK1200 | `rtl_fm -M fm -f 466.075M -s 22050 -g 40 - \| multimon-ng -t raw -a POCSAG1200 -` |
| `rtl_433` | 200+ ISM-band protocols | `rtl_433 -g 40 -f 433.92M -F json` |
| `dump1090` / `readsb` | ADS-B (1090 MHz) | `dump1090 --interactive --gain 40` (for one-shot tests; pipeline is the right tool for ongoing) |
| `noaa-apt` | NOAA APT satellite imagery | `noaa-apt recording.wav -o output.png` |
| `satdump` | Meteor LRPT, many satellites | GUI-driven; see `scripts/satellite-pass.sh` |
| `sox` / `ffmpeg` | Audio post-processing | `sox in.wav -r 11025 out.wav` for resampling before noaa-apt |
| GNU Radio | Custom DSP pipelines | `gnuradio-companion` |

### Not installed but worth installing for specific finds

- **dsd-fme** — DMR / dPMR / NXDN / P25 voice decoder. Needed for confirming the 164.025 MHz hypothesis. Build from source.
- **WSJT-X** — FT8/FT4/JT65 weak-signal HF amateur modes. Useful on HF.
- **fldigi** — multimode HF data (RTTY, PSK31, etc.).

---

## V3 vs V4 Picking Guide

**V3 (FM bandstop filter inline)** — use for:
- Anything 110-200 MHz where FM intermod was previously the dominant noise source. The biggest practical wins are airband (118-137), marine (156-162), and the 162-174 utility/business band.
- Clean weak-signal work — narrowband CW, faint NFM voice, weak AM airband.
- Anywhere you see "pollution everywhere" on V4's spectrum but expect a real signal — the filter strips the IMD products and lets the real carrier stand out.

**V4 (raw, no filter)** — use for:
- FM broadcast itself (88-108 MHz) — V3 cannot hear this band by design.
- Native HF reception — V4 has built-in HF coverage with no direct-sampling toggle. V3 can do HF but only via Q-branch direct sampling, which is ~20 dB less sensitive.
- Wideband survey / spectrum exploration where you want to see what's actually present in the unfiltered RF environment.
- Signals stronger than -40 dBFS where dynamic range is fine and you want raw front-end performance.

Concrete data point from 2026-04-28: in the 163.5-165.5 MHz band, V3 noise floor sits around **-46 dBFS clean**; V4 in the same band sits around **-10 dBFS** of broadband hash from FM intermod. That's ~30 dB of effective dynamic range improvement for V3 in this band.

---

## Active Investigations

Running list of unconfirmed emitters. Move to the catalog (and `spectrum/clickhouse/migrations/`) once identified.

### 164.025 MHz — periodic raspy NFM bursts

- **First heard:** 2026-04-28, V3 + filter
- **Mode used:** NFM
- **Observed:** raspy ~1-second periodic bursts, near-identical signature each cycle
- **Spectrum data:** V4 sees 164.026 MHz bin peaking around -26 dBFS during bursts; V3 sees ~-46 dBFS in the wideband bin (signal is too narrowband relative to the 100 kHz scanner bin to register at scanner resolution, but is clearly audible at narrow-demod bandwidth)
- **Candidates ranked:**
  1. **Utility SCADA / telemetry polling** — Greek utilities (EYDAP water, DEDDIE electricity) license narrowband VHF in this region for talking to remote terminal units. 1-second polling fits a high-priority infrastructure monitor.
  2. **DMR / dPMR control channel** — trunked digital business radio. Continuous frame transmission can sound 1-sec periodic through NFM.
  3. **Harbor / port telemetry** — Piraeus is line-of-sight; some port operations ride VHF outside the marine band.
- **Next step:** capture 30s baseband IQ in SDR++ at 164.025 MHz, then either install `dsd-fme` and attempt DMR/dPMR lock, or run through `multimon-ng -a POCSAG*` to rule out paging. Result determines the right catalog class_id.

### 9.7097 MHz — CW high-pitch continuous tone

- **First heard:** 2026-04-28, V3 + direct sampling
- **Mode used:** CW (BFO offset audible)
- **Observed:** continuous high-pitched tone, no on/off keying = not real morse
- **Candidates ranked:**
  1. **Off-air shortwave carrier** — 9.7097 MHz is in the 31m broadcast band (9400-9900 kHz). Many broadcasters keep their carrier on between scheduled programs. The audible tone in CW mode is just the carrier offset by your BFO setting.
  2. **Utility / military beacon** — narrowband always-on transmitter for propagation reference.
  3. **Intermod from a strong nearby broadcaster** — nonlinear mixing of two HF transmitters can produce a third tone at the sum/difference.
- **Next step:** monitor across an hour — does the tone come on/off on schedule? If yes → broadcaster carrier; check shortwave schedules (eibispace.de) for 9.7097 MHz around the heard time. If continuous 24h → likely beacon or fixed utility.
