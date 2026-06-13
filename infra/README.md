# infra: shared rf_luv data layer

This directory is the consolidation of what used to be six separate
ClickHouse + Grafana stacks (one per pipeline, on ports 8123-8128 / 9001-9005 /
3000-3005) into **one** always-on data layer. The rotating decoder pipelines now
attach to it instead of carrying their own database.

## Two lifecycles on one network

Everything talks over a single external Docker network, `rf_luv_net`, created
out of band (`docker network create rf_luv_net`) or by `infra/up.sh` if absent.

- **Always-on data layer** = compose project `rf_luv_infra` (this directory's
  `compose.yml`): one ClickHouse, one Grafana, the logging form, and the
  one-shot bootstrap. Brought up with `infra/up.sh`. It is never torn down by
  the pipeline tooling.
- **Rotating pipelines** = compose projects `rf_luv_<pipe>` (each pipeline's
  `compose.overlay.yml`): the decoders. Managed by the top-level `pipeline.sh`.

Every compose file declares `networks: { rf_luv_net: { external: true } }` and
every service attaches to it, so a decoder reaches ClickHouse simply at the
network alias `clickhouse` (HTTP `clickhouse:8123`, native `clickhouse:9000`).

Host-side access (anything not on the docker network: probes, ad-hoc CLI) uses
`127.0.0.1:8123` (HTTP) and `127.0.0.1:9000` (native). Those are the only
ClickHouse ports published now; the old per-pipeline ports are retired.

## What runs here

| Service | Image | Host port | Role |
|---|---|---|---|
| clickhouse | `clickhouse/clickhouse-server:24.3-alpine` | 8123 (HTTP), 9000 (native) | the one database server |
| grafana | `grafana/grafana:11.1.0` | 3000 | all six dashboards |
| logging-form | `nginx:1.27-alpine` | 8084 | the spectrum logging form |
| ch-bootstrap | built from `Dockerfile.bootstrap` | (none, one-shot) | creates identities + schema |

## Identities and the cross-grant

One server hosts six databases. Each pipeline keeps its own database and its own
least-privilege user, created by `clickhouse/bootstrap.sql` (PHASE 1):

- six databases + six users; **user name == db name**, **password ==
  `<db>_local`**, `GRANT ALL ON <db>.*`. This is the same credential convention
  every pipeline's config already defaults to, so nothing downstream changes.
- one extra cross-grant: `GRANT SELECT ON acars.* TO spectrum`, so the
  spectrum-acars feedback path can read `acars.messages` directly now that both
  live on one server (no more cross-instance HTTP hop).

The built-in `default` user is the admin/bootstrap identity only. Its password
is `${CH_ADMIN_PASSWORD}`, supplied from the **gitignored** `infra/.env`
(documented by `infra/.env.example`). There is deliberately **no committed
admin-password fallback**: if `.env` is missing, compose substitutes an empty
string and the healthcheck + bootstrap fail loudly rather than booting blank.

## Bootstrap order (and why athens-seed-before-021)

`bootstrap.sh` runs inside `ch-bootstrap` once ClickHouse is healthy:

1. **PHASE 1** apply `clickhouse/bootstrap.sql` as `default` (databases, users,
   grants). This is load-bearing: the acars and noaa migrators do **not** create
   their own database, and every per-db user must exist before its schema runs.
2. **PHASE 2**, each schema applied as its own per-db user:
   - `adsb` -> `adsb/clickhouse/init.sql`
   - `ism` -> `ism/clickhouse/init.sql`
   - `ais` -> `ais/clickhouse/bootstrap.sql` (the consolidated idempotent file)
   - `spectrum` -> `init.sql`, **then** the Athens seed, **then** `migrate.py`
   - `acars` -> `migrate.py`
   - `noaa` -> `migrate.py`

The spectrum sub-order matters. The 27-row Athens `known_frequencies` catalog
(`spectrum/clickhouse/seeds/known_frequencies_athens.sql`) is guarded by
`WHERE (SELECT count() FROM spectrum.known_frequencies) = 0`. Spectrum migration
**021** inserts HF rows into the same table. If `migrate.py` ran first, 021 would
make the table non-empty and the seed's count guard would skip the **entire**
catalog. So: seed first, migrate second.

The migrators are stdlib urllib HTTP clients reading
`CLICKHOUSE_HOST/PORT/DB/USER/PASSWORD`, so the script points them at
`clickhouse:8123` with the matching per-db user. spectrum's `migrate.py` imports
`db.py`/`config.py`, so it is invoked with `cwd=spectrum`.

Everything is idempotent (CREATE ... IF NOT EXISTS, count-guarded seeds,
migrate.py skips applied versions), so a `ch-bootstrap` restart re-runs cleanly.

## Bootstrap image choice

`Dockerfile.bootstrap` needs **both** `clickhouse-client` (to apply raw `.sql`
as a specific user) and `python3` (to run the migrators). The alpine
clickhouse-server image is musl-based and will not run inside a glibc python
image, so the Dockerfile is multi-stage: it copies the single `clickhouse`
binary out of the **non-alpine** (glibc) `clickhouse/clickhouse-server:24.3`
image into `python:3.12-slim` (Debian/glibc) and symlinks `clickhouse-client`
(same binary, dispatched on `argv[0]`). Both tags are pinned.
`PYTHONDONTWRITEBYTECODE=1` because the repo is bind-mounted read-only.

## Grafana datasources

Grafana auto-provisions one ClickHouse datasource per database over the
**native** protocol (`clickhouse:9000`), with UIDs `P_<DBUPPER>_CLICKHOUSE`
(`P_ADSB_CLICKHOUSE`, `P_AIS_CLICKHOUSE`, `P_ISM_CLICKHOUSE`,
`P_SPECTRUM_CLICKHOUSE`, `P_ACARS_CLICKHOUSE`, `P_NOAA_CLICKHOUSE`). Exactly one
is the default: spectrum. (The provisioning tree itself is owned by the grafana
unit; `compose.yml` only binds it in.)

## pipeline.sh guardrail

`../pipeline.sh up|down|rotate <pipe>` operates only on `rf_luv_<pipe>` projects.
It **refuses the name `infra`** and has an internal guard that never targets
project `rf_luv_infra`, so it cannot drop the data layer. `rotate` derives the
currently-up pipeline live from `docker ps` compose-project labels (not a state
file), downs it, and brings the target up. `noaa` is a no-op reminder that it
records via host systemd (`ops/noaa-pass-scheduler`).

## Backups: do this before collecting new data

All six databases now share one `ch-data` volume on one disk. The 2026-06 leap
disk failure is the cautionary tale: months of data, zero backups, total loss.
This consolidation makes that single point of failure more concentrated, not
less. **Strongly recommended** (not a hard gate): deploy `ops/clickhouse-backup`
pointed at off-host storage **before** collecting new data on this volume, so the
next disk failure is recoverable. On recovery from the old disk, attempt a
read-only rescue of the ClickHouse volume before wiping it.
