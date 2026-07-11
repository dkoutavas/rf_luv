# rds — FM RDS subcarrier decoder

This pipeline decodes the RDS (Radio Data System) data subcarrier that FM
broadcast stations transmit at 57 kHz on their MPX composite, and writes the
public station metadata into ClickHouse for Grafana. From a single FM carrier
it recovers the PS station name (so the dashboard can say "you are tuned to
Kosmos FM"), the PI code, RadioText, PTY (programme type), the TP/TA traffic
flags, and the broadcaster's clock-time — all in a numpy-only DSP chain (WFM
discriminator → pilot-locked coherent 57 kHz demod → 1187.5 bps biphase/DBPSK
→ differential decode → CRC-10 block sync → group parsing). RDS is public
broadcast metadata; nothing private is decoded. Reference station is 99.6 MHz
Kosmos FM (see `notes/listening-playbook.md`).

What it can't do: it needs a **stereo** signal — the coherent demod locks to
the 19 kHz stereo pilot to regenerate the 57 kHz reference, so a mono or very
weak station gives no pilot and nothing is emitted. Multipath in the Athens
urban canyon shows up as `block_errors` (CRC-failed blocks) climbing on the
dashboard; a fringe station may only assemble PS/RadioText intermittently. The
full EN 50067 Annex E character set is not implemented — non-ASCII code points
render as spaces (fine for Greek stations, which mostly send basic ASCII). And
crucially this **must run on the V4 with the FM broadcast notch REMOVED**: the
V3 is FM-bandstopped by design and will never see RDS.

## Run

Rotating V4 decoder (shares `:1235`; rotate other V4 pipes down first):

```bash
cp rds/env.v4-01.example rds/.env      # edit RDS_FREQ_HZ / gain as needed
./pipeline.sh rotate rds               # or: ./pipeline.sh up rds
./pipeline.sh logs rds
```

## Verify against redsea (external cross-check, not a dependency)

Capture once, decode with both tools and compare PS/PI:

```bash
# capture ~30 s of IQ at 99.6 MHz (signed 8-bit interleaved)
rtl_sdr -f 99600000 -s 228000 -g 29.7 -n 6840000 kosmos.cs8

# this decoder, offline mode
python3 rds/rds_reader.py --file kosmos.cs8

# redsea (if installed) on the same capture
cat kosmos.cs8 | redsea -r 228000 -f s8
```

PS and PI should match. The offline `--file` path is also the D2 synergy path:
any `iq_capture` `.cs8` decodes without a live dongle.

## Self-test

Numpy-only, no hardware, no network, no pytest required:

```bash
python3 rds/tests/test_rds_decoder.py
```

It encodes a known PI/PS/RadioText/clock into a synthetic MPX, FM-modulates it
to CU8, runs the full `RDSDemodulator`, and asserts the metadata round-trips.
The same file exposes `def test_*()` so pytest discovers it later.
