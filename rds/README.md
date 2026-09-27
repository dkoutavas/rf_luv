# rds — FM RDS subcarrier decoder

This pipeline decodes the RDS (Radio Data System) data subcarrier that FM
broadcast stations transmit at 57 kHz on their MPX composite, and writes the
public station metadata into ClickHouse for Grafana. From a single FM carrier
it recovers the PS station name (so the dashboard can say "you are tuned to
BEST 92.6"), the PI code, RadioText, PTY (programme type), the TP/TA traffic
flags, and the broadcaster's clock-time — all in a numpy-only DSP chain (WFM
discriminator → pilot-locked coherent 57 kHz demod → 1187.5 bps biphase/DBPSK
→ differential decode → CRC-10 block sync → group parsing). RDS is public
broadcast metadata; nothing private is decoded. Reference station is BEST
92.6, which decodes at the full group rate on the patio antenna.

Live capture runs at 1.824 MS/s and is filtered and decimated to 228 kS/s in
software (`rds_decoder.Decimator`). Capturing at 228 kS/s directly lets
neighbouring FM stations alias into the window, which hid RDS completely on the
outdoor antenna.

What it can't do: it needs a **stereo** signal — the coherent demod locks to
the 19 kHz stereo pilot to regenerate the 57 kHz reference, so a mono or very
weak station gives no pilot and nothing is emitted. Multipath in the Athens
urban canyon shows up as `block_errors` (CRC-failed blocks) climbing on the
dashboard; a fringe station may only assemble PS/RadioText intermittently. The
full EN 50067 Annex E character set is not implemented — non-ASCII code points
render as spaces (fine for Greek stations, which mostly send basic ASCII). And it
**needs a dongle with no FM notch**: with the notch screwed in, the 88-108 MHz
FM band is attenuated and RDS is invisible. Since 2026-09-26 that is the V3.

## Run

`pipeline.sh` pauses the dongle's scanner, points the decoder at that dongle's
rtl_tcp port, and resumes the scanner on `down`:

```bash
cp rds/env.example rds/.env            # optional: station and gain
./pipeline.sh up rds v3-01
./pipeline.sh logs rds
./pipeline.sh down rds v3-01
```

## Verify against redsea (external cross-check, not a dependency)

Capture once, decode with both tools and compare PS/PI:

```bash
# capture ~30 s of IQ at 92.6 MHz (signed 8-bit interleaved). rtl_sdr needs
# direct USB: `ops/rf-mode listen v3-01` first, `ops/rf-mode scan v3-01` after.
# 228 kS/s direct can alias neighbours in; use a quiet station for this check.
rtl_sdr -d v3-01 -f 92600000 -s 228000 -g 3.7 -n 6840000 best.cs8

# this decoder, offline mode
python3 rds/rds_reader.py --file best.cs8

# redsea (if installed) on the same capture
cat best.cs8 | redsea -r 228000 -f s8
```

PS and PI should match. The offline `--file` path also decodes a D2 `iq_capture`
`.cs8` — set `RDS_SAMPLE_RATE` to match the capture's rate (`--file` reads samples
at `RDS_SAMPLE_RATE`, default 228000; a mismatch silently garbles the decode).

## Self-test

Numpy-only, no hardware, no network, no pytest required:

```bash
python3 rds/tests/test_rds_decoder.py
```

It encodes a known PI/PS/RadioText/clock into a synthetic MPX, FM-modulates it
to CU8, runs the full `RDSDemodulator`, and asserts the metadata round-trips.
The same file exposes `def test_*()` so pytest discovers it later.
