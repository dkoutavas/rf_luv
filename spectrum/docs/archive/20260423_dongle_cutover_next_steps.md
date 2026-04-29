# Next steps after dual-dongle cutover — handoff

**Written:** 2026-04-23, morning after the 2026-04-22 dual-dongle cutover.
**Target reader:** a Claude Code agent (or human) picking up where we left off.

## TL;DR state

- V3 (R820T2) + FM bandstop filter + rooftop tripod + 57cm dipole, gain=12, serial `v3-01`, running via `rtl-tcp@v3-01` + `rtl-scanner@v3-01` systemd user templates on leap (192.168.2.10). Filter inline since **`2026-04-22 14:35:12 UTC`** — that timestamp is the A/B cutoff.
- V4 (R828D) + no filter + patio window + 5cm vertical whip, adaptive gain settled at 10 dB, serial `v4-01`, running via `rtl-tcp@v4-01` + `rtl-scanner@v4-01`. Role: antenna-diversity feed, NOT the filter A/B (separate antennas make that comparison confounded).
- Both dongles writing to `spectrum.scans` with `dongle_id` column (migrations 015–020 all applied). Downstream: `feature_extractor.py`, `classifier.py`, `detect_compression.py`, Grafana dashboard — all per-dongle aware (commit `9d66c0d`).
- Singleton `rtl_tcp.service` (user) and `rtl-tcp.service` (system, `/etc/systemd/system/`) both stopped + disabled. Docker `spectrum-scanner` container stopped (not removed).

## Context for the next agent

Before acting, load:
- `spectrum/docs/dongle_cutover_runbook.md` — the original 9-phase plan
- `spectrum/docs/ab_comparison.md` — **needs rewriting** (see pending item #2); describes the abandoned dual-dongle-with-splitter A/B, not the current self-A/B approach
- `spectrum/docs/followups/dongle_id_downstream.md` — mark completed items
- `/home/dio_nysi/.claude/projects/-home-dio-nysi-dev-rf-luv/memory/project_fm_filter_v3_install.md` — canonical record of the filter-install timestamp and query examples
- This file

The user prefers runbook execution over autonomous sudo-heavy work. Drive leap over SSH (`ssh dio_nysis@192.168.2.10 "bash -l" <<'REMOTE'` — leap's default shell is fish, always wrap in `bash -l`). Interactive sudo on leap isn't passwordless; factor anything needing sudo as a handoff to the user.

## Pending work, in priority order

### 1. Phase 8 — V3 self-A/B (wait until ≥24h of post-filter data)

Earliest meaningful run: **`2026-04-23 14:35:12 UTC`** (24h after install). Ideally wait 48h for diurnal-cycle coverage.

Query stub (timestamp cutoff approach — no scan_runs.filter column yet):

```sql
WITH filter_install AS (SELECT toDateTime64('2026-04-22 14:35:12', 3, 'UTC') AS t)
SELECT
  (timestamp < (SELECT t FROM filter_install)) AS pre_filter,
  count() AS n,
  round(avg(power_dbfs), 2) AS avg_p,
  round(quantile(0.1)(power_dbfs), 2) AS nf_q10,
  round(quantile(0.99)(power_dbfs), 2) AS peak_q99
FROM spectrum.scans
WHERE dongle_id = 'v3-01'
  AND freq_hz BETWEEN 88000000 AND 108000000   -- FM band
  AND timestamp > toDateTime64('2026-04-20 00:00:00', 3, 'UTC')
  AND timestamp < toDateTime64('2026-04-24 14:35:12', 3, 'UTC')  -- 48h post
GROUP BY pre_filter
ORDER BY pre_filter DESC
```

Decision thresholds (from `ab_comparison.md`, still apply for self-A/B):

| Criterion | Target | Interpretation |
|---|---|---|
| FM noise floor drop, 88–108 MHz | `delta_avg_power_dbfs ≤ -3.0 dB` | Filter is attenuating FM by ≥3 dB |
| Airband insertion loss, 118–137 MHz | `delta_avg_power_dbfs ≥ -1.0 dB` | No more than 1 dB passband hit |
| Clip rate reduction, FM band | `post/pre ≤ 0.2` (via sweep_health.max_clip_fraction) | Filter relieved front-end saturation |
| Unknown signal loss | Manual review of peaks present pre but absent post, outside 88–108 MHz | Filter isn't eating legit signals |

**Early A/B preview (45 min post-filter, not meaningful yet):**
- FM: avg -46.35 post vs -46.6 pre (delta ~+0.25 dB) — essentially flat, diurnal noise
- Airband: avg -45.25 post vs -42.3 pre (delta -2.9 dB) — also too-small sample

Do NOT make the permanent-filter decision on less than 24h of data. If after 48h the FM delta is still < 3 dB, investigate filter quality / installation before concluding "filter ineffective."

### 2. Phase 9 — doc updates

Rewrite `spectrum/docs/ab_comparison.md`'s methodology section:
- Current doc describes dual-dongle A/B assuming a splitter. Reality is two antennas, no splitter, so the filter went on V3 directly and the A/B is self-comparison via timestamp cutoff.
- `dongle_comparison_view` still exists but its role changed — document it as "antenna diversity" not "filter A/B".
- Decision thresholds (noise floor, airband loss, clip rate) still apply; just the data source changes.

Close out items in `spectrum/docs/followups/dongle_id_downstream.md`:
- `detect_compression.py` — **done** in commit `9d66c0d`
- `feature_extractor.py` — **done**
- `classifier.py` — **done**
- `classifier_health.py` — intentionally deferred (doc marked optional)
- Grafana `spectrum-overview.json` dongle variable — **done**
- Migration 021 `scan_runs.filter` column — **deferred** (timestamp-cutoff works; revisit if Phase 8 queries get awkward)
- Docker-compose `spectrum-scanner` service removal — **deferred** (wait 1 week of stable native runs per original plan, earliest 2026-04-29)

### 3. V4 airband clipping — monitor, maybe tune

V4's airband preset has `avg_clip_fraction ≈ 1.6%` ongoing with spikes to 30%. Adaptive gain settled at 10 dB. Options if the clip rate keeps creeping up:
- Lower `SCAN_GAIN_MIN` from 2 to 0 so adaptive can go further down
- Lower `SCAN_GAIN` starting point from 12 to 8 to skip the overshoot
- Investigate what signal near 131.312 MHz is driving the clip (air traffic control? strong digital mode?)

Edit: `/etc/rtl-scanner/v4-01.env` on leap (needs sudo). Restart: `systemctl --user restart rtl-scanner@v4-01.service`.

### 4. Leftover singleton cleanup (low priority)

- `~/.config/systemd/user/rtl_tcp.service` — unit file still present, in "failed + disabled" state. OK to delete after confirming template units survive a reboot.
- `/etc/systemd/system/rtl-tcp.service` — the hidden system-level zombie source. Disabled, but the file still exists. `sudo rm /etc/systemd/system/rtl-tcp.service && sudo systemctl daemon-reload` would remove it entirely. **Coordinate with the user** — they're the one who'd have set it up originally and there may be context we're missing about why it existed.
- `rtl-tcp-watchdog.timer` (singleton) — disabled, OK to delete.

### 5. Reboot survival check

Template units have `WantedBy=default.target`. Should auto-start after a reboot. **This hasn't been tested yet.** Schedule a reboot at a convenient time; have a recovery plan ready.

## Known gotchas

- **librtlsdr 2.0+ `rtl_test` deadlock**: if someone edits `rtl-tcp-by-serial.sh` again, do NOT use `rtl_test` for enumeration — its sample loop holds the device forever when `awk ... exit` closes the pipe. See commit `4f83243`. Use `rtl_eeprom -d N` probes instead.
- **Migration 017 `MODIFY ORDER BY`**: ClickHouse 24.3 won't accept `ALTER TABLE ... MODIFY ORDER BY (existing_column, added_column)` even inside a multi-statement migration file, because `migrate.py` splits statements and sends each separately. Combined ALTERs would work but aren't supported by the splitter. See commit `919efc6`.
- **RTL2838 EEPROM re-read**: writing a new serial via `rtl_eeprom -s X` does NOT take effect via `rtl-usb-reset` or `authorized=0/1`. Only a physical replug power-cycles the chip. If you ever rewrite a serial, plan for the physical action.
- **numpy on leap**: scanner.py needs `numpy` in the system `python3.11` (not just the docker container). Installed via `pip3.11 install --user numpy`. Don't assume containerized deps transfer to the bare-metal template units.
- **Two rtl_tcp services fought each other for ~5 hours of debugging**: if orphan `rtl_tcp` PIDs reappear with ppid=1, check BOTH `systemctl --user status` AND `sudo systemctl status` for hidden system-level units matching `rtl-tcp*` or `rtl_tcp*`. Unfortunate naming clash (hyphen vs underscore).
- **Leap's default shell is fish**, not bash. Always wrap multi-line SSH scripts in `bash -l` or you'll get `Unsupported use of '='` on the first variable assignment.
- **`SCAN_GAIN` is an initial value, `SCAN_GAIN_MIN` is the adaptive floor.** Scanner auto-reduces on clip; don't set initial gain higher than the safe-known value.

## Quick state-check commands

```bash
# one-liner health check (paste in one SSH call)
ssh dio_nysis@192.168.2.10 "bash -l" <<'EOF'
systemctl --user is-active rtl-tcp@v3-01 rtl-scanner@v3-01 rtl-tcp@v4-01 rtl-scanner@v4-01
curl -s 'http://localhost:8126/?user=spectrum&password=spectrum_local&database=spectrum' \
  --data "SELECT dongle_id, count() FROM scans WHERE timestamp > now() - INTERVAL 5 MINUTE GROUP BY dongle_id FORMAT TabSeparated"
EOF
```

Expected: 4 `active` lines, then a table with two rows (v3-01 and v4-01) each in the ~24000 range (4892 rows/5min × ~5 = ~24k is wrong — it's 4892/5min so expect ~4892-5000 per dongle).

## Unpushed? No — everything is on origin/main through `4f83243`.

Last 4 commits in order:
```
4f83243 rtl-tcp-by-serial: replace rtl_test enumerate with rtl_eeprom probes
919efc6 017: drop unsupported MODIFY ORDER BY on scans
9d66c0d dongle_id downstream consumers: per-dongle queries before V4 ingests
91d3860 Prepare leap for second RTL-SDR dongle (V4 + FM bandstop A/B week)
```
