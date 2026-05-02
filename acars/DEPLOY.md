# ACARS Deploy Runbook — V4 Takeover

Deploy procedure for the ACARS pipeline (Tier 1 #1 from the decoding-roadmap), built and smoke-tested 2026-05-02. End-to-end test on leap passed; no live decoder run yet because that requires V4 takeover.

This runbook is written for remote execution: every step has a verify-before-act check. Reverse a step at any point with the rollback section.

---

## Pre-flight (run from anywhere — read-only)

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
#  expect: 0f864e9 (or later) on top — pull if not:
#  ssh dio_nysis@192.168.2.10 'cd ~/dev/rf_luv && git pull'

# 4. Target ports still free (no conflict crept in)
ssh dio_nysis@192.168.2.10 'ss -tln | grep -E ":(8127|9004|3004) " || echo "ports free"'
#  expect: "ports free"

# 5. V3 scanner happy (we are NOT touching V3)
ssh dio_nysis@192.168.2.10 'systemctl --user is-active rtl-tcp@v3-01.service rtl-scanner@v3-01.service'
#  expect: active, active

# 6. V4 currently scanning (we ARE replacing this with ACARS)
ssh dio_nysis@192.168.2.10 'systemctl --user is-active rtl-tcp@v4-01.service rtl-scanner@v4-01.service'
#  expect: active, active. The scanner is the consumer we're replacing.
```

If any check fails: stop, investigate, do not proceed. Most likely failure is git not up-to-date — `git pull` and recheck.

---

## V4 takeover (~3 minutes)

Run from leap (`ssh dio_nysis@192.168.2.10` first, or prefix each line with `ssh dio_nysis@192.168.2.10`).

```bash
# Step 1 — stop and disable the V4 wideband scanner.
# rtl-tcp@v4-01 stays running because acarsdec is its new consumer.
systemctl --user stop rtl-scanner@v4-01.service
systemctl --user disable rtl-scanner@v4-01.service

# Verify rtl-tcp@v4-01 still up and serving
systemctl --user is-active rtl-tcp@v4-01.service     # expect: active
ss -tlnp | grep :1235                                # expect: rtl_tcp PID listening

# Step 2 — deploy
cd ~/dev/rf_luv/acars
cp env.v4-01.example .env
# .env defaults: SOAPYSDR=driver=rtltcp,rtltcp=192.168.2.10:1235  (correct as-is)
docker compose up -d --build
# Brings up 4 services: acarsdec + acars-ingest + clickhouse + grafana

# Step 3 — wait for ClickHouse healthy + first decode
docker compose ps
# All four should show "Up", clickhouse-acars should be "(healthy)" within ~30s
```

---

## First-hour validation

Run on leap. The order matters — each check builds on the previous.

```bash
# Check 1: migrations applied
docker exec clickhouse-acars clickhouse-client --user acars --password acars_local \
  --database acars --query "SELECT version, name FROM schema_migrations ORDER BY version"
#  expect: 001 | 001_init

# Check 2: acarsdec is talking to rtl_tcp via SoapyRTLTCP
docker compose logs --tail=50 acarsdec
#  Look for "Connecting to ..." or similar SoapySDR connect message.
#  ERROR signs: "Cannot find SoapySDR module rtltcp", or repeated reconnect loops.

# Check 3: ingest is bound and ClickHouse-ready
docker compose logs --tail=10 acars-ingest
#  expect:
#    "ClickHouse is ready"
#    "Listening for acarsdec JSON on UDP :5550, dongle_id=v4-01"

# Check 4: live message count climbing (give it 5-15 min)
docker exec clickhouse-acars clickhouse-client --user acars --password acars_local \
  --database acars --query "SELECT count() FROM messages"
#  expect: > 0 within 15 min during daytime LGAV traffic

# Check 5: first message inspection
docker exec clickhouse-acars clickhouse-client --user acars --password acars_local \
  --database acars --query "SELECT timestamp, freq_mhz, level_db, label, flight, tail, text FROM messages ORDER BY timestamp DESC LIMIT 5 FORMAT Vertical"
#  expect: real callsigns (AEE..., RYR..., TRA..., etc.), real LGAV-area tails (SX-...)

# Check 6: Grafana renders
# Open http://192.168.2.10:3004 (admin / admin)
# Default dashboard: ACARS Overview. Should show non-zero panels within 30 min.
```

### Health signals during the first hour

| Symptom | Likely cause | Fix |
|---|---|---|
| acarsdec restarts repeatedly | SoapyRTLTCP can't reach :1235 | `ss -tlnp \| grep :1235` — confirm rtl_tcp@v4-01 alive |
| `Code: 60. UNKNOWN_TABLE` in ingest logs | migration 001 didn't apply | `docker compose logs acars-ingest \| grep migrate` |
| 0 messages after 30 min in busy hours | gain too low / antenna issue | Bump ACARS_GAIN in .env from 30 → 35 → 40, restart |
| `avg(err_count) > 0.5` after 1 hour | gain too high (over-driving) | Drop ACARS_GAIN by 5 |
| `freq_activity` empty for one of the 3 freqs | acarsdec tuner bw shifted off-center | Likely benign — the 131.825 is on the edge of acarsdec's 2 MHz window. Watch a few hours before adjusting |

---

## 1-week soak success criteria

Per the decoding-roadmap, no second decoder starts until ACARS has soaked clean for a week. Success looks like:

- [ ] No acars-ingest restarts in 7 days (`docker inspect acars-ingest --format '{{.RestartCount}}'`)
- [ ] No acarsdec restarts in 7 days, OR restarts only correlate with rtl-tcp@v4-01 escalator events (expected — chip-lockup recovery)
- [ ] Daily message counts > 100 during weekday operations (LGAV is busy)
- [ ] At least 5 unique flights cross-correlated with ADS-B `aircraft_latest` for the same time window (proves the join works)
- [ ] Grafana ACARS Overview dashboard remains usable (no panels stuck on "No Data" during traffic hours)
- [ ] `freq_activity` has rows for at least 2 of the 3 ACARS freqs

A failed criterion isn't a stop — it's a diagnostic. Update the README with what you learned, then iterate.

---

## Rollback (any time, ~1 minute)

```bash
# On leap:
cd ~/dev/rf_luv/acars
docker compose down -v            # stop, remove volumes (clears collected data)

# Restore V4 to wideband scanning
systemctl --user enable rtl-scanner@v4-01.service
systemctl --user start rtl-scanner@v4-01.service

# Verify
systemctl --user is-active rtl-tcp@v4-01.service rtl-scanner@v4-01.service
#  expect: active, active
```

`docker compose down -v` removes the `clickhouse-data` volume — collected messages are lost. If you want to keep them across a stop/start cycle, omit `-v`.

---

## What this build leaves on the host

- Docker images: `acars-acars-ingest` (custom build, ~50 MB), `ghcr.io/sdr-enthusiasts/docker-acarsdec:4.1.6Build1494` (~250 MB)
- Docker volumes: `acars_clickhouse-data`, `acars_grafana-data` (each grows ~50-200 MB/week with active LGAV traffic)
- Nothing modified outside `~/dev/rf_luv/acars/`
- No new systemd units (the existing `rtl-tcp@v4-01.service` is reused)

## Reference

- Pipeline build: commit `4b57bf1` (acars: add ACARS aircraft messaging pipeline (Tier 1 #1))
- Backport hardening: commit `b9f3dd2` (hardening: backport ClickHouse fixes from acars to adsb/ais/ism)
- Documentation: commit `0f864e9` (docs: register ACARS pipeline in CLAUDE.md)
- Decoding roadmap: `~/.claude/plans/i-d-like-to-start-iterative-bear.md` (or whatever path it lives at on the host you're reading from)
