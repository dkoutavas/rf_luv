# Session report — 2026-04-29

**Target reader:** future you, or a collaborator picking up the work.
**One-paragraph summary:** today bolted three new things onto the rf_luv platform — (1) a trip-hardening reliability layer ([escalator + freshness probe + ntfy](#1-trip-hardening-recap-installed-earlier-today)) before a planned absence; (2) a [three-phase spectrum/ audit](#2-spectrum-platform-audit-todays-main-work) that removed bloat, consolidated DRY violations, and made the codebase deployable by other RF enthusiasts; (3) a [signal-quality probe](#3-signal-quality-probe-incident-driven) added in response to a V3 RF-chain failure that exposed a monitoring gap. All three are committed, pushed, and deployed to leap. Production is currently healthy.

---

## TL;DR — what's new on disk and on leap

| Item | Repo state | Leap deployed | First-fire validated |
|---|---|---|---|
| Trip-hardening (escalator + freshness probe + ntfy + heartbeat) | `baa5ac1` | yes | yes — auto-recovered V3 from firmware-starvation 20:06 UTC |
| Spectrum platform audit (bloat / DRY / sharing prep) | `ad1fd9c` | code on disk | scanner units NOT yet restarted to pick up refactored code |
| Signal-quality probe (deaf-scanner detection) | `e96b947` | yes (timer active) | yes — first tick at 22:29 EEST showed both dongles OK |

`origin/main` matches leap's `main`. Local + leap working trees clean. 64 tests passing.

---

## 1. Trip-hardening recap (installed earlier today)

Three layers added on top of the existing `rtl-tcp-watchdog@<serial>` user-level stack:

1. **`rtl-tcp-escalator`** (root system service, every 5 min) — catches CB-open at watchdog fail #10. Runs the proven manual recipe: `rtl-usb-reset <serial>` → xHCI bounce on PCI `0000:00:14.0` → restart user unit. After 3 failed unwedges/24h on one serial OR both serials CB-open ≥30 min, triggers `systemctl reboot` (rate-limited 1/6h). Validated end-to-end against the 2026-04-29 V3 firmware-starvation incident — auto-recovered in 22 s.
2. **`rf-freshness-probe`** (root, every 5 min) — queries `spectrum.scans` per dongle; WARN >10 min stale, CRITICAL >25 min stale. Catches Docker / ClickHouse / ingest failures invisible to the per-process watchdog.
3. **`rf-notify` + `rf-heartbeat`** — stdlib `urllib.request` POST to ntfy.sh; topic `rf_luv_mnz10pds` configured at `/etc/rtl-scanner/notify.env`. Daily heartbeat at 09:00 UTC confirms the alert pipe is alive.

Action log at `/var/log/rtl-recovery.log` (logrotate weekly, 4 weeks retention) captures every layer's transitions as JSON.

Memory entry: `project_trip_hardening_20260429.md`.

---

## 2. Spectrum platform audit (today's main work)

Plan file: `~/.claude/plans/please-audit-the-following-sequential-octopus.md`. Three phases, one squashed commit (`ad1fd9c`).

### Phase 1 — bloat
- `spectrum/exports/` cleared: 3 markdown reports moved to `spectrum/docs/archive/` (24-day-old, superseded by Grafana). Raw CSV dir gitignored.
- `spectrum/docs/followups/` collapsed from 10 → 1 file:
  - 5 `20260421_*.md` research notes folded into a single `phase-4-research.md` roadmap
  - 4 closed-by-completion items deleted (`verify_must_fixes`, `dongle_id_downstream`, `dongle_cutover_next_steps`, `deployment_status` — last two preserved as historical retrospectives in `archive/`)
  - 1 active item kept (`20260423_hardening_followups.md`)
- `Dockerfile.scanner` resurrected in `docker-compose.yml` behind `profiles: ["scanner"]` — plain `docker compose up -d` keeps leap's behavior (CH + Grafana + nginx-logging only); contributors get the scanner via `docker compose --profile scanner up -d`.
- `spectrum/README.md` documents the three classifier batch jobs running on leap as systemd user timers (~5 min cadence).

### Phase 2 — DRY consolidation
Three new shared modules under `spectrum/`, each replacing repeated boilerplate:

| New module | Replaces | Saves |
|---|---|---|
| `spectrum/db.py` | 6 hand-rolled urllib wrappers (scan_ingest, migrate, feature_extractor, classifier, classifier_health, analysis/detect_compression) | ~150 LOC, single error-handling path |
| `spectrum/config.py` | 6 duplicate env-var blocks; magic numbers in tests | one frozen `Config` dataclass; tests override via `dataclasses.replace` |
| `spectrum/messages.py` | string-literal marker contract maintained in parallel by scanner.py + scan_ingest.py | typo on either side now becomes ImportError instead of silent skip |

7 new contract tests in `tests/test_messages.py`. Net diff: 428 LOC removed, 248 added across 11 files. Imports use the bare-name pattern (`import db, messages`) so production scripts continue to launch directly via systemd `python3 /path/to/scanner.py`; only `analysis/detect_compression.py` (one directory deeper) needs a one-line `sys.path.insert`.

`CH_PORT` inconsistency (8123 vs 8126 default in `scan_ingest.py`) resolved by routing all defaults through `config.py`.

**Caveat (important):** `rtl-scanner@v3-01.service` and `rtl-scanner@v4-01.service` on leap are **still running the pre-refactor scanner.py / scan_ingest.py code** (the units pick up the new code only on restart). Behavior is byte-identical to the new code (validated by tests + the messages.PEAK = "peak" string equivalence), but the units should be restarted at a convenient window to actually run the refactored code path.

### Phase 3 — sharing readiness
- `spectrum/.env.example` documents every override knob; `docker-compose.yml` now uses `${VAR:-default}` substitution so `cp .env.example .env` flows values through.
- Athens-specific `known_frequencies` seed split out of `init.sql` into `spectrum/clickhouse/seeds/known_frequencies_athens.sql`. A `*.sql.example` template + `seeds/README.md` walk a contributor through writing their own. `init.sql` now creates empty tables only.
- `spectrum/docs/CUSTOMIZE.md` — four-section guide (dongle / location / band / remote rtl_tcp).
- `spectrum/README.md` setup section rewritten to document the **Docker quick-start** vs **systemd watchdog stack** paths.
- `.gitignore` covers `spectrum/.env` (via `*.env`) and `spectrum/exports/*` with `.gitkeep` exception. Secrets sweep clean.

### What was deferred (intentionally)
- **Phase 2.5** — moving `classifier.py` / `feature_extractor.py` / `classifier_health.py` to `spectrum/jobs/`. Needs lockstep updates to `~/.config/systemd/user/spectrum-*.service` ExecStart paths on leap; better as a separate maintenance window with eyes on the deploy.
- **Phase 0** — the X server SSH warnings (4× `xinput disable` in `~/.bashrc` running unguarded). Patch identified, blocked by a safety check that requires explicit authorization to edit shell-profile files.

---

## 3. Signal-quality probe (incident-driven)

**Why this exists:** at 18:15 UTC today, V3's full-sweep `max_power` dropped 30 dB (from -14 dBFS to -43 dBFS) and stayed there for ~1 hour. *None* of the existing layers caught it: rtl_tcp kept streaming, scanner kept sweeping, rows kept landing in ClickHouse, freshness probe stayed green. The whole pipeline was structurally healthy while V3 was effectively useless. Cause was almost certainly a connector backing off (most likely the F-connector on the inline FM bandstop filter — one week of thermal cycling at the rooftop tripod). Replug at ~22:00 EEST restored the signal within one bucket boundary.

**What was added:** `ops/spectrum-monitor/signal-quality-probe.py` + matching `.service` + `.timer`. Queries `spectrum.sweep_health` for `max(max_power)` per dongle over the last 30 min, full sweeps only (airband is normally ~10 dB quieter and would false-WARN). Thresholds env-overridable:

```
SIGNAL_WARN_FLOOR_DBFS=-35    # WARN if sustained
SIGNAL_CRIT_FLOOR_DBFS=-40    # CRITICAL if sustained
SIGNAL_WINDOW_S=1800           # 30 min
SIGNAL_MIN_SWEEPS=5            # guard against false alerts after restart
```

State at `/var/lib/spectrum-monitor/signal_quality.json` (sibling to `freshness.json`). Notify path shared with the freshness probe / escalator. Action log entries are written **only on state transitions** — so currently empty (probe started OK, both dongles OK).

`install-trip-hardening.sh` updated and re-run on leap; timer is enabled and ticking every 5 min. First tick at 22:29 EEST: `v3-01:-15.5dBFS(OK,n=6) v4-01:-4.2dBFS(OK,n=7)`.

**The 2026-04-29 V3 deafness would have produced this WARN at ~21:45 EEST** (instead of being noticed by chance ~22:00):

> rf_luv: v3-01 signal weak (deaf scanner?)
> max_power -43.2 dBFS over last 30min (6 full sweeps), floor is -35 dBFS. Check antenna/coax/filter.

Memory entry: `project_v3_failure_modes.md` — a taxonomy of the three V3 failure modes seen this month (firmware-starvation / chip-lockup / RF-chain), each with detection layer + recovery path + cross-references.

---

## Production state right now (22:30 EEST 2026-04-29)

```
rtl-tcp@v3-01            active   (samples flowing)
rtl-tcp@v4-01            active
rtl-scanner@v3-01        active   (running pre-refactor code; new code on disk)
rtl-scanner@v4-01        active
rtl-tcp-watchdog@v3-01   active, consecutive_failures=0
rtl-tcp-watchdog@v4-01   active
rtl-tcp-escalator        active (root)
rf-freshness-probe       active (root) — v3:60s, v4:51s, both OK
rf-signal-quality-probe  active (root) — v3:-15.5dBFS, v4:-4.2dBFS, both OK
rf-heartbeat             active (root) — next fire 09:00 UTC tomorrow
spectrum-classifier      active (user) — every 5 min
spectrum-features        active (user) — every 5 min
spectrum-classifier-health active (user) — every 5 min
ClickHouse + Grafana + logging-form (Docker) — all healthy
```

ClickHouse `spectrum.scans`: ~17 rows/sec/dongle steady-state, both dongles writing. V3 max_power back to ~-15 dBFS post-replug; peak counts recovering (52 in last 15 min, was 0 during deafness).

---

## Outstanding items / decision points

### Quick wins (15-60 min each)

1. **Restart `rtl-scanner@{v3,v4}-01.service`** to pick up refactored scanner.py / scan_ingest.py. Risk: low — code is functionally identical (validated by tests + JSON-marker equivalence) but never run in production. Pick a freshness-tolerant window; freshness-probe will WARN at 10 min if the restart takes longer.
2. **Silence X server warnings on SSH login** — root cause is `~/.bashrc` running `xinput disable` 4× unguarded. Fix: replace the four lines with one `[[ -n "$DISPLAY" ]] && xinput disable "AT Translated Set 2 keyboard" 2>/dev/null`. Blocked by a safety check requiring explicit authorization to edit shell profile.
3. **Test the signal-quality probe alert path** — temporarily disconnect V3's antenna for 30+ min and confirm a WARN ntfy lands. Or set `SIGNAL_WARN_FLOOR_DBFS=-10` in `/etc/rtl-scanner/signal-quality-probe.env` and restart the timer; both dongles will WARN immediately, then revert.

### Medium (multi-hour)

4. **Phase 2.5 — move classifier / feature_extractor / classifier_health to `spectrum/jobs/`**. Needs lockstep edit of three `~/.config/systemd/user/spectrum-*.service` files (ExecStart paths). Update unit files, `systemctl --user daemon-reload`, verify timers fire.
5. **Tune signal-quality thresholds** for the actual leap RF environment. Defaults assume strong-signal availability above -35 dBFS; if V3 with FM filter sees natural quiet stretches, raise the floor. Right now V3 sees ~-15 dBFS so plenty of margin — but the next location-change or filter-swap might need re-tuning.
6. **Add a Grafana panel** surfacing `/var/lib/spectrum-monitor/signal_quality.json` content alongside the existing dongle-health summary. Would let "deaf V3" be visible at a glance, not just via push notification.
7. **Hardware mitigations:**
   - Per-port-power USB hub (e.g. YEPKIT YKUSH3) — software-driven power cycle for chip-lockup recovery (mode #2 of the V3 taxonomy)
   - Smart plug on leap — whole-machine cycle as nuclear fallback
   - Weatherproof rooftop tripod connectors — butyl rubber tape on SMA / F-connector chain. Prevention for mode #3 deafness.

### Bigger / strategic

8. **Add ingest tests** — currently 64 tests cover DSP only. `scan_ingest.py` (JSON routing) and `migrate.py` (SQL splitting) are untested. Would need either mocking ClickHouse or a docker-compose-for-tests harness.
9. **Public-release polish** — if you do want to share publicly: LICENSE, CONTRIBUTING.md, demo data set (or a "first-time installer that seeds a sample-data ClickHouse"), distro-agnostic installer (currently `setup/install-wsl.sh` is openSUSE-only).
10. **Phase 4 research items** in `spectrum/docs/phase-4-research.md` — RANGE_HASHED dictionary, emitter_null subtyping, fs/4 spur hypothesis, infrastructure noise correlation, principled threshold calibration. Each is self-contained; pick whichever feels timely.

### Open decision points

- **V3 root cause confirmation:** the 2026-04-29 deafness is *very likely* an FM-filter F-connector but not 100% confirmed. Could be the antenna SMA, the coax to dongle, or even a partial cable break. Worth a hands-on inspection with a multimeter (DC continuity through the filter) when next on the rooftop.
- **Trip status:** the trip-hardening was installed in anticipation of an absence. If the absence is happening soon, item #1 above (restart scanner units to run refactored code) should land *before* the trip — running new code only when watched is the safer pattern.

---

## Files / commits / references

**Commits (both pushed):**
- `ad1fd9c` audit: bloat removal, DRY consolidation, sharing readiness
- `e96b947` hardening: add signal-quality probe (deaf-scanner detection)

**Plan file:** `~/.claude/plans/please-audit-the-following-sequential-octopus.md`.

**Memory entries (auto-loaded next session):**
- `project_v3_failure_modes.md` — 3-mode V3 failure taxonomy (new today)
- `project_trip_hardening_20260429.md` — trip-hardening installation report
- `project_v3_hot_lockup_20260428.md` — mode #2 detail
- `project_fm_filter_v3_install.md` — A/B cutoff timestamp
- `reference_leap_machine.md`, `reference_leap_shell.md`

**Where things live:**
```
ops/
  rtl-tcp/                 watchdog + escalator + USB-reset + unwedge
  spectrum-monitor/        freshness-probe + signal-quality-probe (NEW)
  notify/                  ntfy helper + heartbeat
  install-trip-hardening.sh installs all of the above

spectrum/
  scanner.py scan_ingest.py migrate.py    pipeline core (refactored)
  config.py db.py messages.py             shared modules (NEW)
  classifier.py feature_extractor.py classifier_health.py  batch jobs
  analysis/detect_compression.py          one-shot archaeology + future live timer
  clickhouse/init.sql + migrations/       schema (Athens seed split out)
  clickhouse/seeds/                       optional location seeds (NEW)
  docs/CUSTOMIZE.md                       location customization guide (NEW)
  docs/phase-4-research.md                consolidated research roadmap (NEW)
  docs/followups/20260423_hardening_followups.md  the only open followup
  docs/archive/                           historical reports + retrospectives
  tests/                                  64 passing tests (incl. test_messages.py NEW)
```
