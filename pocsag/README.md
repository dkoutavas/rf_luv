# POCSAG paging pipeline

Receive-only decoding of POCSAG pager traffic.

```
RTL-SDR (USB) -> rtl_fm (FM demod) -> multimon-ng (POCSAG) -> pocsag_ingest.py -> ClickHouse -> Grafana
```

## Privacy and legal posture

Listening is legal in Greece and across the EU. POCSAG is unencrypted, so it
decodes with no key. This is the lesson: a critical messaging protocol with no
authentication and no encryption, in the clear.

POCSAG can carry private messages, for example hospital or on-call pagers. This
project receives, it never transmits. The decoded text is written only to the
local `pocsag` ClickHouse database on this host. It must never be committed to
git, put in a shared screenshot, pushed off-host, or published in any form.
Every test in this directory uses synthetic capcodes and made-up text. Treat
the captured content as private data that stays on the machine.

## How it runs

USB mode, like ADS-B. `rtl_fm` cannot read rtl_tcp on this hardware, so the
container opens the dongle directly. `pipeline.sh` frees the dongle first.

```bash
bash infra/up.sh                                   # shared data layer
bash pipeline.sh up pocsag v3-01                   # tunes POCSAG_FREQ (default 169.6 MHz)
POCSAG_FREQ=466.075M bash pipeline.sh up pocsag v3-01   # a different channel
bash pipeline.sh down pocsag v3-01                 # rtl_tcp and the scanner come back
```

`up pocsag v3-01` stops the V3 scanner for the session. ADS-B on the V4 keeps
running. Antenna: a quarter-wave dipole for the chosen channel (arm cm = 7125 /
MHz), vertical. 169 MHz wants ~42 cm arms, and 466 MHz wants ~15 cm.

## Finding the channel

Athens paging frequency is not confirmed yet. The two candidates are 169 MHz
(the ERMES paging band) and 466.075 MHz (on-site paging). Bring the pipeline up
on one, watch `docker logs pocsag` for `POCSAG` decode lines, and switch
`POCSAG_FREQ` until pages appear. Then record the working channel in the
spectrum `known_frequencies` seed.

## Schema

`pocsag.messages` (one row per page: capcode, function, msg_type, message,
protocol, freq_hz, dongle_id), plus `hourly_stats` and `capcode_latest`
materialized views. Numbered migrations in `clickhouse/migrations/`, applied by
`infra/migrate.py pocsag` from the infra bootstrap.

## Tests

`python3 -m pytest pocsag/tests` runs the multimon-ng line parser against
synthetic alpha, numeric, tone and malformed lines.
