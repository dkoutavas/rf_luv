# ACARS Deploy Runbook - V4 Takeover

Deploy procedure for the ACARS pipeline (Tier 1 #1 from the decoding-roadmap), built and smoke-tested 2026-05-02. End-to-end test on leap passed; no live decoder run yet because that requires V4 takeover.

This runbook is written for remote execution: every step has a verify-before-act check. Reverse a step at any point with the rollback section.

---

## Pre-flight (run from anywhere - read-only)

Before touching anything, confirm the host is in the expected state.

```bash
# 1. SSH reachable, leap responsive
ssh dio_nysis@192.168.2.10 'uptime; free -h | head -2'
#  expect: load < 1.0, swap usage may show some pressure (~800M is normal)

# 2. Both dongles present and serving rtl_tcp
ssh dio_nysis@192.168.2.10 'ss -tlnp | grep -E ":1234|:1235"'
#  expect: both 1234 (V3) and 1235 (V4) listening with rtl_tcp PIDs

# 3. ACARS code already on leap
ssh dio_nysis@192.168.2.10 'cd ~/dev/rf_luv && git log --oneline -3'
#  expect: 0f864e9 (or later) on top - pull if not:
#  ssh dio_nysis@192.168.2.10 'cd ~/dev/rf_luv && git pull'

# 4. Shared data layer is up (ACARS writes into it; no ACARS-specific ports anymore)
ssh dio_nysis@192.168.2.10 'ss -tln | grep -E ":(8123|9000|3000) " && echo "infra up"'
#  expect: 8123 (CH HTTP), 9000 (CH native), 3000 (Grafana) all listening.
#  If not: ssh in and run `docker network create rf_luv_net && bash infra/up.sh` first.

# 5. V3 scanner happy (we are NOT touching V3)
ssh dio_nysis@192.168.2.10 'systemctl --user is-active rtl-tcp@v3-01.service rtl-scanner@v3-01.service'
#  expect: active, active

# 6. V4 currently scanning (we ARE replacing this with ACARS)
ssh dio_nysis@192.168.2.10 'systemctl --user is-active rtl-tcp@v4-01.service rtl-scanner@v4-01.service'
#  expect: active, active. The scanner is the consumer we're replacing.
```

If any check fails: stop, investigate, do not proceed. Most likely failure is git not up-to-date - `git pull` and recheck.

---

## V4 takeover (~3 minutes)

Run from leap (`ssh dio_nysis@192.168.2.10` first, or prefix each line with `ssh dio_nysis@192.168.2.10`).

```bash
# Step 1 - stop and disable the V4 wideband scanner.
# rtl-tcp@v4-01 stays running because acarsdec is its new consumer.
systemctl --user stop rtl-scanner@v4-01.service
systemctl --user disable rtl-scanner@v4-01.service

# Verify rtl-tcp@v4-01 still up and serving
systemctl --user is-active rtl-tcp@v4-01.service     # expect: active
ss -tlnp | grep :1235                                # expect: rtl_tcp PID listening

# Step 2 - deploy the decoder against the always-on infra
cd ~/dev/rf_luv/acars
cp env.v4-01.example .env
# .env defaults: SOAPYSDR=driver=rtltcp,rtltcp=192.168.2.10:1235  (correct as-is)
cd ~/dev/rf_luv && bash pipeline.sh up acars
# Brings up 2 services on the shared rf_luv_net: acarsdec + acars-ingest
# (ClickHouse + Grafana are already running from infra/up.sh)

# Step 3 - confirm the decoder + ingest are up
bash pipeline.sh ps acars
# Both acarsdec and acars-ingest should show "Up"; the shared clickhouse
# container is already "(healthy)" from the infra stack.
```

---

## First-hour validation

Run on leap. The order matters - each check builds on the previous.

```bash
# Check 1: schema present (applied by the infra ch-bootstrap, not a per-pipeline step)
docker exec clickhouse clickhouse-client --user acars --password acars_local \
  --database acars --query "SELECT version, name FROM schema_migrations ORDER BY version"
#  expect: 001 | 001_init

# Check 2: acarsdec is talking to rtl_tcp via SoapyRTLTCP
bash pipeline.sh logs acars | grep -i acarsdec | tail -50
#  Look for "Connecting to ..." or similar SoapySDR connect message.
#  ERROR signs: "Cannot find SoapySDR module rtltcp", or repeated reconnect loops.

# Check 3: ingest is bound and ClickHouse-ready
bash pipeline.sh logs acars | grep -i acars-ingest | tail -10
#  expect:
#    "ClickHouse is ready"
#    "Listening for acarsdec JSON on UDP :5550, dongle_id=v4-01"

# Check 4: live message count climbing (give it 5-15 min)
docker exec clickhouse clickhouse-client --user acars --password acars_local \
  --database acars --query "SELECT count() FROM messages"
#  expect: > 0 within 15 min during daytime LGAV traffic

# Check 5: first message inspection
docker exec clickhouse clickhouse-client --user acars --password acars_local \
  --database acars --query "SELECT timestamp, freq_mhz, level_db, label, flight, tail, text FROM messages ORDER BY timestamp DESC LIMIT 5 FORMAT Vertical"
#  expect: real callsigns (AEE..., RYR..., TRA..., etc.), real LGAV-area tails (SX-...)

# Check 6: Grafana renders
# Open http://192.168.2.10:3000 (admin / admin), ACARS folder
# Dashboard: ACARS Overview. Should show non-zero panels within 30 min.
```

### Health signals during the first hour

| Symptom | Likely cause | Fix |
|---|---|---|
| acarsdec restarts repeatedly | SoapyRTLTCP can't reach :1235 | `ss -tlnp \| grep :1235` - confirm rtl_tcp@v4-01 alive |
| `Code: 60. UNKNOWN_TABLE` in ingest logs | ch-bootstrap didn't apply the acars schema | check `docker logs ch-bootstrap` from the infra stack |
| 0 messages after 30 min in busy hours | gain too low / antenna issue | Bump ACARS_GAIN in .env from 30 → 35 → 40, restart |
| `avg(err_count) > 0.5` after 1 hour | gain too high (over-driving) | Drop ACARS_GAIN by 5 |
| `freq_activity` empty for one of the 3 freqs | acarsdec tuner bw shifted off-center | Likely benign - the 131.825 is on the edge of acarsdec's 2 MHz window. Watch a few hours before adjusting |

---

## 1-week soak success criteria

Per the decoding-roadmap, no second decoder starts until ACARS has soaked clean for a week. Success looks like:

- [ ] No acars-ingest restarts in 7 days (`docker inspect acars-ingest --format '{{.RestartCount}}'`)
- [ ] No acarsdec restarts in 7 days, OR restarts only correlate with rtl-tcp@v4-01 escalator events (expected - chip-lockup recovery)
- [ ] Daily message counts > 100 during weekday operations (LGAV is busy)
- [ ] At least 5 unique flights cross-correlated with ADS-B `aircraft_latest` for the same time window (proves the join works)
- [ ] Grafana ACARS Overview dashboard remains usable (no panels stuck on "No Data" during traffic hours)
- [ ] `freq_activity` has rows for at least 2 of the 3 ACARS freqs

A failed criterion isn't a stop - it's a diagnostic. Update the README with what you learned, then iterate.

---

## Rollback (any time, ~1 minute)

```bash
# On leap:
cd ~/dev/rf_luv && bash pipeline.sh down acars   # stop + remove the acarsdec + ingest containers

# Restore V4 to wideband scanning
systemctl --user enable rtl-scanner@v4-01.service
systemctl --user start rtl-scanner@v4-01.service

# Verify
systemctl --user is-active rtl-tcp@v4-01.service rtl-scanner@v4-01.service
#  expect: active, active
```

`pipeline.sh down acars` only tears down the rotating decoder. The collected
messages live in the **shared** ClickHouse (`acars` database) on the always-on
infra stack and are NOT removed; to drop them, run a `DROP TABLE`/`TRUNCATE` on
the `acars` database, or `DROP DATABASE acars` and re-bootstrap.

---

## What this build leaves on the host

- Docker images: the custom `acars-ingest` build (~50 MB) and the `@sha256`-pinned sdr-enthusiasts acarsdec image (~250 MB, the soak build was `4.1.6Build1494`)
- No ACARS-specific volumes: collected messages land in the shared `rf_luv_infra_ch-data` volume (the `acars` database), which grows ~50-200 MB/week with active LGAV traffic
- Two containers on the `rf_luv_net` network (`acarsdec`, `acars-ingest`); nothing modified outside `~/dev/rf_luv/acars/` and the shared ClickHouse
- No new systemd units (the existing `rtl-tcp@v4-01.service` is reused)

## Reference

- Pipeline build: commit `4b57bf1` (acars: add ACARS aircraft messaging pipeline (Tier 1 #1))
- Backport hardening: commit `b9f3dd2` (hardening: backport ClickHouse fixes from acars to adsb/ais/ism)
- Documentation: commit `0f864e9` (docs: register ACARS pipeline in CLAUDE.md)
- Decoding roadmap: `~/.claude/plans/i-d-like-to-start-iterative-bear.md` (or whatever path it lives at on the host you're reading from)
