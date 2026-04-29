# Frequency Investigation Checklist

## How to use
1. Open SDR++ (or SDR Console) on Windows
2. Source: RTL-SDR, sample rate 2.048 MHz
3. For each frequency below:
   - Tune to the listed frequency
   - Set the demodulation mode as indicated
   - Listen for 30-60 seconds (some signals are bursty)
   - Note what you hear in the "Heard" column
   - Mark the checkbox when done

## Demod quick reference
| Mode | Use for | Bandwidth |
|------|---------|-----------|
| **AM** | Airband voice (118-137 MHz) | 8.33 kHz |
| **NFM** | Land mobile, marine, business | 12.5 or 25 kHz |
| **WFM** | FM broadcast (88-108 MHz) | 150 kHz |

## Scanner config
```
Run:     run_20260405_095339
Gain:    12 dB
Antenna: rooftop_tripod, 57 cm arms
Date:    2026-04-05 10:40 UTC
```

---

## 1. Strong Unknown Signals (auto-detected)

Loudest signals not in the known_frequencies table. DVB-T band (174-230) excluded.

| # | Freq (MHz) | Power | Demod | BW | What to listen for | Heard |
|---|-----------|-------|-------|-----|-------------------|-------|
| 1 | **146.4** | -9.5 | NFM | 12.5k | 2m ham or military — voice? digital? | |
| 2 | **148.4** | -9.5 | NFM | 12.5k | 2m ham or military — voice? digital? | |
| 3 | **148.1** | -11 | NFM | 12.5k | 2m ham or military — voice? digital? | |
| 4 | **146.1** | -11.4 | NFM | 12.5k | 2m ham or military — voice? digital? | |
| 5 | **152.2** | -11.9 | NFM | 12.5k | Military/business — dispatch? repeater? | |
| 6 | **164.7** | -13.4 | NFM | 12.5k | Business/taxi — dispatch? repeater? | |
| 7 | **150.2** | -14.3 | NFM | 12.5k | Military/business — dispatch? repeater? | |
| 8 | **170.9** | -14.7 | NFM | 12.5k | Business/taxi — dispatch? repeater? | |
| 9 | **166.8** | -15.1 | NFM | 12.5k | Business/taxi — dispatch? repeater? | |
| 10 | **129.3** | -15.5 | AM | 8.33k | Airband — ATC voice? ATIS? Pilot? | |
| 11 | **140** | -15.9 | NFM | 12.5k | Gov/military — voice? data bursts? repeater? | |
| 12 | **147.2** | -16.7 | NFM | 12.5k | 2m ham or military — voice? digital? | |
| 13 | **165.3** | -16.7 | NFM | 12.5k | Business/taxi — dispatch? repeater? | |
| 14 | **142.1** | -17.5 | NFM | 12.5k | Gov/military — voice? data bursts? repeater? | |
| 15 | **135.5** | -17.5 | AM | 8.33k | Airband — ATC voice? ATIS? Pilot? | |
| 16 | **138** | -17.6 | NFM | 12.5k | Gov/military — voice? data bursts? repeater? | |
| 17 | **144.1** | -18.1 | NFM | 12.5k | Gov/military — voice? data bursts? repeater? | |
| 18 | **133.4** | -18.1 | AM | 8.33k | Airband — ATC voice? ATIS? Pilot? | |
| 19 | **131.4** | -18.8 | AM | 8.33k | Airband — ATC voice? ATIS? Pilot? | |
| 20 | **173** | -19.1 | NFM | 12.5k | Business/taxi — dispatch? repeater? | |
| 21 | **149.2** | -19.2 | NFM | 12.5k | Military/business — dispatch? repeater? | |
| 22 | **157.5** | -19.5 | NFM | 25k | Marine VHF — ship traffic? coast station? | |
| 23 | **172.9** | -19.6 | NFM | 12.5k | Business/taxi — dispatch? repeater? | |
| 24 | **151.9** | -19.9 | NFM | 12.5k | Military/business — dispatch? repeater? | |
| 25 | **153.3** | -19.9 | NFM | 12.5k | Military/business — dispatch? repeater? | |
| 26 | **153.6** | -20.4 | NFM | 12.5k | Military/business — dispatch? repeater? | |
| 27 | **167.4** | -21.2 | NFM | 12.5k | Business/taxi — dispatch? repeater? | |
| 28 | **160.7** | -21.4 | NFM | 25k | Marine VHF — ship traffic? coast station? | |
| 29 | **154.8** | -21.6 | NFM | 12.5k | Military/business — dispatch? repeater? | |
| 30 | **164.1** | -21.6 | NFM | 12.5k | Business/taxi — dispatch? repeater? | |

---

## 2. Airband Verification (confirm real ATC)

These frequencies are strong in the airband. We believe they are real
Athens ATC signals (not IMD artifacts). Tune AM, 8.33 kHz.

| # | Freq (MHz) | Avg Power | Variability | Expected | Heard |
|---|-----------|-----------|-------------|----------|-------|
| 1 | **135.454** | -15.8 | 1.5 dB | Semi-continuous (ATIS/VOLMET?) | |
| 2 | **131.358** | -16.4 | 3.9 dB | Bursty voice (pilot/controller) | |
| 3 | **133.406** | -16.8 | 1.8 dB | Semi-continuous (ATIS/VOLMET?) | |
| 4 | **129.31** | -17.4 | 4.5 dB | Bursty voice (pilot/controller) | |
| 5 | **126.614** | -19.5 | 4.1 dB | Bursty voice (pilot/controller) | |
| 6 | **124.566** | -21.2 | 5.8 dB | Bursty voice (pilot/controller) | |
| 7 | **124.666** | -21.4 | 5 dB | Bursty voice (pilot/controller) | |
| 8 | **122.518** | -22.2 | 5.1 dB | Bursty voice (pilot/controller) | |
| 9 | **123.766** | -22.3 | 3.5 dB | Bursty voice (pilot/controller) | |
| 10 | **125.714** | -22.4 | 3.7 dB | Bursty voice (pilot/controller) | |
| 11 | **121.718** | -22.7 | 6.1 dB | Bursty voice (pilot/controller) | |
| 12 | **122.618** | -22.9 | 6.2 dB | Bursty voice (pilot/controller) | |

---

## 3. Known Signals — Quick Verify

Already in the database but worth a quick listen to confirm.

| # | Freq (MHz) | Name | Demod | BW | What to confirm | OK? |
|---|-----------|------|-------|-----|----------------|-----|
| 1 | **136.125** | Athens ATIS | AM | 8.33k | Automated weather in English? | |
| 2 | **144.775** | Greek 2m Ham | NFM | 12.5k | Greek amateur voice? | |
| 3 | **148.440** | Military/Gov VHF | NFM | 12.5k | Greek military? Encrypted? Repeater? | |
| 4 | **150.490** | Military/Gov VHF | NFM | 12.5k | Same as above or different service? | |
| 5 | **152.540** | Business Radio | NFM | 12.5k | Commercial dispatch? Taxi? Security? | |
| 6 | **156.800** | Marine Ch16 | NFM | 25k | Coast guard? Securite calls? | |
| 7 | **156.650** | Marine Ch13 | NFM | 25k | Piraeus port? Bridge-to-bridge? | |

---

## 4. Baseline Mysteries

Flagged in the April 5 baseline report — strong, unidentified.

| # | Freq (MHz) | Power | Demod | BW | Hypothesis | Heard |
|---|-----------|-------|-------|-----|-----------|-------|
| 1 | **164.730** | -13.1 | NFM | 12.5k | Taxi / business dispatch (strong, bursty) | |
| 2 | **166.770** | -13.9 | NFM | 12.5k | Same service as 164.73? Related? | |
| 3 | **168.820** | -13.9 | NFM | 12.5k | EU 169 MHz business allocation | |
| 4 | **156.030** | -15.1 | NFM | 25k | Marine Ch1 — Piraeus port ops? | |
| 5 | **158.080** | -15.7 | NFM | 25k | Marine coast station (Piraeus Radio?) | |
| 6 | **160.130** | -15.9 | NFM | 25k | Coast station duplex TX freq | |
| 7 | **160.730** | -16.7 | NFM | 25k | Piraeus coast radio repeater? | |

---

## Reporting Template

After investigating, copy this for each signal:

```
Freq:     ___ MHz
Heard:    (voice / data bursts / tone / silence / digital noise)
Language: (Greek / English / N/A)
Content:  (what was said, or type of traffic)
Pattern:  (continuous / bursty / periodic / silence)
ID:       (proposed name)
Category: (gov / business / marine / airband / ham / unknown)
```

---
*Generated 2026-04-05 10:40 UTC | Run: run_20260405_095339*
