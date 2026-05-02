# NOAA / Meteor weather satellite pipeline (Tier 2 #6) — SCAFFOLD

> ⚠️ This is a **scaffold**, not a deployable pipeline. The schema, the
> directory structure, the scheduler skeleton, and the recorder skeleton
> are in place; key orchestration pieces are intentionally stubbed and
> need operator review before going live.
>
> What's deployable today: just the ClickHouse + Grafana stack
> (`docker compose up -d`). The migrator container will create the schema
> and exit 0. The recorder will not actually record yet.

## Why a scaffold

The roadmap budget for this is ~3 days. This commit lays the high-leverage
primitives — schema, recorder/scheduler design, integration with the
`rtl-coordinator` lock primitive (commit 7432e8d) — so the remaining
implementation can land in clean, reviewable chunks rather than as one
giant unreviewable diff.

## Architecture

```
┌──────────────┐  hourly   ┌────────────────┐
│  scheduler   ├──────────▶│ noaa.passes    │   pending rows for the next 12h of passes
│  (host       │           │  (ClickHouse)  │
│  systemd     │           └────────────────┘
│  timer,      │                    ▲
│  reads TLE)  │                    │ recording / recorded / decoded / failed
└─────┬────────┘                    │
      │ for each pass:              │
      │ systemd-run --on-calendar=… │
      │      noaa-record-pass …     │
      ▼                             │
┌──────────────────────────────────┐│
│  recorder (host, systemd-run)    ││
│  1. acquire rtl-coordinator lock ┃│
│     for V3 dongle                ││
│  2. stop rtl-tcp@v3-01.service   ││
│  3. rtl_fm record (12 min)       ││
│  4. start rtl-tcp@v3-01.service  ││
│  5. decode (noaa-apt / satdump)  ┼┘
│  6. INSERT decoded row           │
└──────────────────────────────────┘
```

The wideband scanner on V3 sees the lock and skips its sweeps during the
pass (when the spectrum scanner is wired into `spectrum/coordinator.py`,
which is staged but not yet active — see
`ops/rtl-coordinator/README.md`).

## What ships in this scaffold

| File | Status | Notes |
|---|---|---|
| `clickhouse/migrations/001_init.sql` | ✅ Production-ready | passes table + pass_latest + monthly_summary |
| `migrate.py` | ✅ Production-ready | Cloned from `acars/migrate.py` with database name swap |
| `scheduler.py` | 🟡 Scaffold | TLE reading, orbit-predictor integration, INSERT logic all in place. **NOAA_DRY_RUN=1 by default** so it doesn't fire systemd-run yet. |
| `recorder.py` | 🟡 Scaffold | CLI parsing, status updates, lock-acquisition stub all in place. **rtl-tcp service stop/start orchestration is intentionally NOT implemented** — needs operator review. Current behaviour: marks the pass as `failed` with a clear scaffold note. |
| `Dockerfile.ingest` | ✅ Production-ready | Just runs migrations. No long-lived container. |
| `docker-compose.yml` | ✅ Production-ready | ClickHouse :8128 / 9005, Grafana :3005, migrator one-shot. |
| `grafana/provisioning/` | ✅ Production-ready | NOAA Overview dashboard (passes table, daily count, by-satellite bar chart, decode rate). |
| `tle_refresh.sh` | ❌ TODO | Should pull NOAA + METEOR TLEs from celestrak weekly. |
| `env.v3-01.example` | ❌ TODO | Per-host overrides (RX lat/lon, gain, decoder choice). |
| `DEPLOY.md` | ❌ TODO | The V3 takeover dance is more delicate than ACARS — has to coordinate with the running scanner. |
| `../ops/noaa-pass-scheduler/` | ❌ TODO | Hourly systemd timer that runs `scheduler.py`. |

## Status (lifecycle of a pass row)

```
pending     →   scheduled but not yet at AOS
recording   →   rtl_fm started
recorded    →   WAV exists, awaiting decode
decoded     →   image_path set + SNR computed
failed      →   see notes for why
```

Each transition is a fresh INSERT into `passes`; the `pass_latest`
ReplacingMergeTree picks up the newest state. So the dashboard always
shows the truth, even if the scheduler/recorder restart mid-pass.

## TODO before this is deployable

In rough dependency order:

1. **`tle_refresh.sh`** — weekly cron pulling fresh TLEs from
   `celestrak.org/NORAD/elements/weather.txt` to `/var/lib/noaa/tles.txt`.
2. **rtl-tcp orchestration** in `recorder.py` — the actual
   `systemctl stop rtl-tcp@v3-01.service` / `rtl_fm` / `systemctl start`
   sequence. Needs operator review of the failure modes (what if rtl_fm
   crashes mid-pass and we can't restart rtl-tcp?). Suggest a context
   manager that always re-starts rtl-tcp on exit.
3. **Wire `spectrum/coordinator.py`** into `spectrum/scanner.py` so the
   scanner skips sweeps during locked windows. Without this, scanner crashes
   while rtl-tcp is stopped during a pass and the watchdog churns.
4. **`tle_refresh.sh` + `noaa-pass-scheduler.{service,timer}`** under
   `ops/noaa-pass-scheduler/` — hourly invocation of `scheduler.py` with
   `NOAA_DRY_RUN=0`.
5. **`scheduler.py`'s systemd-run** — currently prints intended commands
   in dry-run mode; flip to actually invoking `systemd-run --user
   --on-calendar=...` so passes auto-fire at AOS-30s.
6. **Decoder output parsing** — capture `noaa-apt`'s SNR estimate (or
   compute one from the WAV) and write it to `snr_db`.
7. **Image gallery in Grafana** — embed the decoded PNGs (Grafana 11
   supports image links in tables; needs a static file server or absolute
   paths reachable from the browser).
8. **`DEPLOY.md` runbook** — V3 takeover sequence + first-pass validation.

## Local smoke test

```bash
cd noaa/
docker compose up -d clickhouse grafana
docker compose run --rm noaa-migrator      # creates the schema and exits
docker exec clickhouse-noaa clickhouse-client --user noaa --password noaa_local \
  --database noaa --query "SHOW TABLES"
# Expected: passes, pass_latest (+ inner_id), monthly_summary (+ inner_id), schema_migrations

# Inject a synthetic decoded pass to verify the dashboard renders:
docker exec clickhouse-noaa clickhouse-client --user noaa --password noaa_local \
  --database noaa --query "
    INSERT INTO passes (pass_start, pass_end, satellite, freq_mhz, max_elevation,
                        duration_s, decoder, snr_db, image_path, status, dongle_id)
    VALUES (now() - INTERVAL 30 MINUTE, now() - INTERVAL 18 MINUTE,
            'NOAA-19', 137.100, 64.5, 720, 'noaa-apt', 28.4,
            '/srv/noaa/2026-05-02_noaa19.png', 'decoded', 'v3-01')"

# Open http://localhost:3005 — NOAA Overview should show 1 row.

# Tear down:
docker compose down -v
```

## Related

- `acars/` — the Tier 1 #1 pipeline; design template for ports + schema layout.
- `ops/rtl-coordinator/` — the flock primitive this pipeline depends on.
- `scripts/satellite-pass.sh` — the existing one-shot recorder this pipeline will eventually wrap.
