# ghost/ — replicating and debunking "paranormal" investigation gear

A mechanism-level rebuild of the equipment a "paranormal investigation" uses (a
spirit box, an EMF meter, a Bluetooth speaker), built from first principles on the
rf_luv SDR station, plus offline forensics on public video. The goal is a
reproducible write-up about how the equipment works. It is about the mechanism, not
about any person, and it decodes no message content.

Everything here is reception, self-generated audio in the flat, or analysis of
public video. No transmit, no interference with anyone's session, no payload decode.
RDS is public broadcast metadata only.

## Where this runs

The two-dongle Omen (native openSUSE Tumbleweed): the FM-notched **V4** stays on the
spectrum scanner (rtl_tcp :1234), and the bare **V3** (rtl_tcp :1235) is the ghost
dongle, with the FM band wide open. Because there are two dongles, ghost does not
time-share with the scanner and is not a `pipeline.sh` rotation; it holds the V3 for
the length of a session through the coordinator. Host bring-up is in
[`HOSTPREP.md`](HOSTPREP.md).

## What is built

| File | What it does |
|------|--------------|
| `spiritbox.py` | Spirit-box replica: sweeps the FM band with no squelch/lock, dwelling ~150 ms per channel, and writes a labelled WAV plus one `ghost.spins` row per step. |
| `dsp.py` | The demod chain (WFM discriminator, 50 us de-emphasis, low-pass, decimate to 48 kHz), the sweep planner, and the slapback/reverb post-fx. Reuses `fm_discriminate` and `sinc_lpf` from the RDS pipeline. |
| `forensics/audio_io.py` | `yt-dlp` fetch + `ffmpeg` decode of public video audio to a numpy array. |
| `forensics/delay_estimate.py` | Measures the slapback echo (ms + feedback) on a "voice" segment (autocorrelation + cepstrum). A consistent tap across episodes is a plugin setting. |
| `forensics/bandlimit.py` | Measures the effective upper audio bandwidth. A hard shelf the room audio lacks points to a Bluetooth speaker rendering the "voice". |
| `forensics/fingerprint.py` | Chromaprint (`fpcalc`) fingerprints; a near-identical fingerprint across episodes proves the clip was pre-recorded, not captured live. |
| `forensics/spectrogram.py` | STFT time-frequency view (terminal heatmap + stdlib grayscale PNG) of a voice segment vs room tone. |
| `forensics/reverb_match.py` | RT60 (Schroeder decay) of the room's claps vs the reverb tail on a "voice"; a mismatch means the tail was added in the box. |
| `forensics/emf_sync.py` | Lines up logged EMF-LED timestamps with impulsive bursts / 217 Hz GSM buzz in the audio; a coincidence means a radio, not a ghost. |
| `migrate.py`, `clickhouse/migrations/` | The `ghost` database schema (`spins`, `stations`, `segments`). |

## Spirit-box quick start

```bash
# Offline, no hardware: demod the bundled synthetic capture to a WAV.
python3 ghost/spiritbox.py --file ghost/samples/spirit_demo.cs8 --out /tmp/demo.wav

# Live (after HOSTPREP): sweep the FM band, RDS-label each fragment, write to ClickHouse.
python3 ghost/spiritbox.py --mode forward --dwell-ms 150 --duration 60

# The same, dry (WAV + sidecar only, no ClickHouse), with the "creepy voice" fx on:
python3 ghost/spiritbox.py --mode random --steps 200 --fx --dry-run
```

Each run writes `<session>.wav` and a `<session>.json` sidecar under `GHOST_WAV_DIR`
(default `/data/rf_luv/ghost/recordings`). The sidecar carries, per step:
`step_idx, t_start, t_end, freq_hz, dwell_ms, rssi_db, rds_ps, rds_rt`. That is the
receipt: every "word" in the WAV is tied to the station and frequency it came from.

## Forensics quick start

```bash
python3 ghost/forensics/delay_estimate.py <file-or-url> --start 12.0 --dur 4.0
python3 ghost/forensics/bandlimit.py <file-or-url> --start 12.0 --dur 4.0
python3 ghost/forensics/fingerprint.py ep1.opus ep2.opus   # flag reused clips
```

`fingerprint.py` shells out to `fpcalc` (chromaprint), installed via `sudo zypper
install chromaprint-fpcalc`; every other forensic tool is numpy + stdlib.

## Tests (headless, no hardware)

```bash
python3 ghost/tests/test_spiritbox.py     # demod, sweep planner, WAV, RDS pre-pass
python3 ghost/tests/test_forensics.py     # slapback delay, bandwidth, ffmpeg decode
python3 ghost/migrate.py --dry-run        # schema (needs the shared ClickHouse up)
```

## Scope

Built now: the spirit box, RDS labels, and the full forensics suite (slapback delay,
audio bandlimit, chromaprint reuse, spectrogram, RT60 reverb match, EMF/RF sync).
Designed but not yet built: the K-II EMF replication (module 3,
hardware-gated), the Grafana Ghost dashboards, the perception blind test, and
`REPORT.md`. See the plan for the full design.
