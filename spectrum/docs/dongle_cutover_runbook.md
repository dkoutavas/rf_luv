# Dongle-aware cutover runbook (V3-only → template units + dongle_id)

## Scope

Execute on `leap` (192.168.2.10) to migrate the running single-dongle (V3)
pipeline onto the new dual-dongle-capable infrastructure:

- ClickHouse: apply migrations 017 → 020 (add `dongle_id`, recreate RMT
  tables, rebuild `hourly_baseline` per-dongle, add `dongle_comparison_view`).
- Scanner: deploy dongle-aware `scanner.py` + `scan_ingest.py`.
- Systemd: cut over from `rtl_tcp.service` + docker-compose `spectrum-scanner`
  to template units `rtl-tcp@v3-01.service` + `rtl-scanner@v3-01.service`.
- Watchdog: cut over to per-instance `rtl-tcp-watchdog@v3-01.service/.timer`.
- udev: install serial-based symlinks.
- V4 stub: install `rtl-tcp@v4-01.service` in expected-failing state.

**This runbook does NOT plug in V4.** V4 install is a separate step, gated
by `spectrum/docs/second_dongle_preflight.md`.

## Pre-execution checks

Before starting, verify:

```bash
# Working tree on leap matches what's in git (including the new files)
cd ~/dev/rf_luv && git status && git log -3 --oneline

# What is actually running right now?
systemctl --user list-units --all 'rtl_tcp*' 'rtl-tcp*' 'spectrum-scanner*' 2>/dev/null || true
docker compose -f spectrum/docker-compose.yml ps 2>&1 || true

# ClickHouse reachable?
curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" --data-binary "SELECT version()"

# schema_migrations state
curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
    --data-binary "SELECT version, applied_at FROM spectrum.schema_migrations ORDER BY version FORMAT TSV"
```

Record the current state (which scanner is running — docker-compose or
systemd singleton?) in your session notes. Steps below branch on that.

## Downtime budget

Total V3 downtime during this runbook: **60–90 seconds**, spread across:

- Phase 4 (scanner cutover): ~30s
- Phase 5 (hourly_baseline rebuild): ~30–60s, in the same stop window

At most 1–2 missed sweeps (airband preset fires every 60s; full preset every
280s). This is an acceptable cost for fixing the dongle infrastructure
before V4 arrives.

---

## Phase 0 — Identify what's currently running

```bash
systemctl --user is-active rtl_tcp.service 2>/dev/null || true
systemctl --user is-active rtl-tcp@v3-01.service 2>/dev/null || true
docker compose -f ~/dev/rf_luv/spectrum/docker-compose.yml ps --format json 2>/dev/null | jq -r '.[] | select(.Service=="spectrum-scanner") | .State' || true
```

**Decision tree**:

- `rtl_tcp.service` active, no template: → go through all phases in order.
- `rtl-tcp@v3-01.service` already active: → skip Phase 3 (rtl_tcp cutover done).
- docker-compose `spectrum-scanner` running: → Phase 4 will stop it explicitly.
- neither scanner running: → confirm this is expected; may be an incomplete
  prior cutover. Resume from Phase 1.

Write your finding in the session notes. Every phase below is idempotent for
the normal cases; surprising state halts the runbook.

---

## Phase 1 — Apply migrations 017 + 018

These are additive. Scanner stays running. Takes ~1–2 min total.

```bash
cd ~/dev/rf_luv/spectrum
python3.11 migrate.py
```

Verify:

```bash
curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
    --data-binary "SELECT version FROM spectrum.schema_migrations WHERE version IN ('017','018') ORDER BY version FORMAT TSV"
# Expect both 017 and 018 listed.

# Confirm columns exist:
curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
    --data-binary "SELECT count() FROM spectrum.scans WHERE dongle_id = 'v3-01'"
# Expect a large number (> 10M). Newly added rows use the default even before
# the ALTER UPDATE mutations finish, so this works immediately.
```

**If 018 fails partway**: the `_new` tables may exist without the swap having
completed. Inspect:

```bash
curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
    --data-binary "SHOW TABLES FROM spectrum" | grep -E '_new|_old'
```

Drop any leftover `*_new` or `*_old` tables manually, then re-run `migrate.py`.
See `spectrum/docs/followups/dongle_id_downstream.md` for recovery context.

---

## Phase 2 — Deploy dongle-aware scanner/ingest code

Pull the new `scanner.py` + `scan_ingest.py`. No service action yet — the old
scanner is still running with the old code. The new code is what Phase 4 will
start.

```bash
cd ~/dev/rf_luv && git pull --ff-only
python3.11 -m py_compile spectrum/scanner.py spectrum/scan_ingest.py
```

Verify the new code has dongle_id support:

```bash
grep -c "dongle_id" spectrum/scanner.py spectrum/scan_ingest.py
# Expect non-zero counts in both files.
```

---

## Phase 3 — Install templates and udev; cut over rtl_tcp

### 3a. Install systemd templates + wrapper + udev

```bash
cd ~/dev/rf_luv
bash ops/rtl-tcp/install.sh
bash ops/rtl-scanner/install.sh
```

Idempotent. Doesn't stop anything running. Installs:

- Template units in `~/.config/systemd/user/`
- Wrapper at `/usr/local/bin/rtl-tcp-by-serial`
- Updated watchdog + per-serial USB reset at `/usr/local/{bin,sbin}/`
- udev rules at `/etc/udev/rules.d/99-rtl-sdr.rules`
- Env-file examples at `/etc/rtl-scanner/*.env.example`

### 3b. Populate real env files

```bash
# v3-01: copy the example and make sure values match current docker-compose
sudo install -m 0644 /etc/rtl-scanner/v3-01.env.example /etc/rtl-scanner/v3-01.env
sudo $EDITOR /etc/rtl-scanner/v3-01.env
# Review SCAN_DONGLE_ID, SCAN_GAIN, SCAN_GAIN_MIN against the current
# spectrum-scanner env in docker-compose.yml. Adjust if different.

# v4-01: copy but leave TODOs until V4 arrives
sudo install -m 0644 /etc/rtl-scanner/v4-01.env.example /etc/rtl-scanner/v4-01.env
# Do not edit v4-01.env yet — it stays in TODO state until V4 is plugged in.
```

### 3c. Verify V3 serial is `v3-01`

If `rtl_tcp.service` is running, it holds the USB device. To run `rtl_eeprom`
we need to release it briefly:

```bash
systemctl --user stop rtl_tcp.service 2>/dev/null || true
# Short window — do the checks quickly:
rtl_test 2>&1 | grep -E 'Found|SN:'
rtl_eeprom -d 0 | grep -i serial
```

**If the serial shows `v3-01`**: good. Proceed.

**If the serial is still the factory default (`00000001`) or something else**:
write it now:

```bash
rtl_eeprom -d 0 -s v3-01
# Re-enumerate for the new serial to take effect:
sudo /usr/local/sbin/rtl-usb-reset
sleep 3
rtl_eeprom -d 0 | grep -i serial    # verify v3-01
```

### 3d. Start `rtl-tcp@v3-01`

```bash
systemctl --user daemon-reload
systemctl --user enable --now rtl-tcp@v3-01.service
journalctl --user -u rtl-tcp@v3-01 -n 20 --no-pager
# Expect: "rtl-tcp-by-serial: v3-01 → index 0" and the standard rtl_tcp
# "listening ..." log line.

systemctl --user status rtl-tcp@v3-01 --no-pager | head -10
ls -la /dev/rtl_sdr_v3
```

### 3e. Disable the old singleton

```bash
systemctl --user disable rtl_tcp.service     # unit file stays installed
# DO NOT DELETE rtl_tcp.service — keep for rollback. Just disable.
```

---

## Phase 4 + 5 — Scanner cutover + hourly_baseline rebuild (combined stop window)

These two actions share a single scanner-stop window to keep downtime at ~60s
and preserve the `hourly_baseline` aggregate from gap-sampling.

### 4/5a. Stop the current scanner

Case A — docker-compose is running the scanner:
```bash
docker compose -f ~/dev/rf_luv/spectrum/docker-compose.yml stop spectrum-scanner
```

Case B — a systemd unit is running the scanner (seen only on leap if a
pre-existing unit exists; not in the repo):
```bash
systemctl --user stop rtl-scanner 2>/dev/null || true
systemctl --user disable rtl-scanner 2>/dev/null || true
```

**The V3 pipeline is now down. Budget: 60s.**

### 4/5b. Run migration 019 (hourly_baseline rebuild)

```bash
cd ~/dev/rf_luv/spectrum
python3.11 migrate.py
# 019 should apply in 30–60s depending on scans volume. Verify:
curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
    --data-binary "SELECT version FROM spectrum.schema_migrations WHERE version='019' FORMAT TSV"
```

If migration 019 fails:

```bash
# Check what happened
curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
    --data-binary "SHOW TABLES FROM spectrum LIKE '%hourly_baseline%' FORMAT TSV"
# Clean up any _new leftovers; confirm hourly_baseline exists (either old or new form).
# See followups/dongle_id_downstream.md recovery section.
```

### 4/5c. Start `rtl-scanner@v3-01`

```bash
systemctl --user enable --now rtl-scanner@v3-01.service
journalctl --user -u rtl-scanner@v3-01 -n 30 --no-pager
# Expect: "Run run_v3-01_... started", sweeps begin firing.
```

### 4/5d. Verify dongle_id flows end-to-end

```bash
# Wait ~5 min for a full sweep + next airband, then:
curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
    --data-binary "SELECT dongle_id, count() FROM spectrum.scans WHERE timestamp > now() - INTERVAL 10 MINUTE GROUP BY dongle_id FORMAT TSV"
# Expect: single row, "v3-01  <count>"

curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
    --data-binary "SELECT run_id, dongle_id FROM spectrum.scan_runs WHERE started_at > now() - INTERVAL 30 MINUTE FORMAT TSV"
# Expect: run_id starts with "run_v3-01_"; dongle_id = "v3-01"
```

If the scanner isn't producing `v3-01` rows:

- Scanner log should show `dongle=v3-01` in its startup banner. If it says
  default, the env file isn't being loaded — check `systemctl --user
  show rtl-scanner@v3-01 | grep EnvironmentFile`.
- Rollback: `systemctl --user stop rtl-scanner@v3-01 && docker compose -f
  ~/dev/rf_luv/spectrum/docker-compose.yml start spectrum-scanner`. The old
  docker scanner still works because dongle_id has a `DEFAULT 'v3-01'` in
  every table.

---

## Phase 6 — Cut over the watchdog

### 6a. Enable per-instance watchdog

```bash
systemctl --user enable --now rtl-tcp-watchdog@v3-01.timer
systemctl --user list-timers --no-pager rtl-tcp-watchdog@v3-01.timer
# Expect: a future fire within the next 30s.
```

### 6b. Disable the singleton watchdog

```bash
systemctl --user stop rtl-tcp-watchdog.timer rtl-tcp-watchdog.service 2>/dev/null || true
systemctl --user disable rtl-tcp-watchdog.timer 2>/dev/null || true
# Unit files stay installed for rollback.
```

### 6c. Exercise the watchdog (optional but recommended)

```bash
# Manually trigger a probe; on a healthy system it logs "recovered" or
# "N bytes in Ns" on stderr, exit 0.
/usr/local/bin/rtl-tcp-watchdog --serial v3-01 --unit rtl-tcp@v3-01.service
echo "exit=$?"
```

---

## Phase 7 — V4 stub (expected-failing)

```bash
# Ensure the v4-01 env file exists with port 1235
cat /etc/rtl-scanner/v4-01.env | grep RTL_TCP_PORT   # expect 1235

systemctl --user enable rtl-tcp@v4-01.service
systemctl --user start  rtl-tcp@v4-01.service 2>/dev/null || true
systemctl --user status rtl-tcp@v4-01.service --no-pager | head -15
journalctl --user -u rtl-tcp@v4-01 -n 15 --no-pager
# Expect: status=failed, exit code 1, journal shows
#   "rtl-tcp-by-serial: serial 'v4-01' not found on bus"
# This is the intended pre-V4 behavior. Do NOT enable the scanner for v4-01.
```

Verify it is NOT a config error or unit-not-found:

```bash
systemctl --user is-enabled rtl-tcp@v4-01    # should say "enabled"
systemctl --user is-active  rtl-tcp@v4-01    # should say "failed"
```

---

## Phase 8 — Apply migration 020 (comparison view)

```bash
cd ~/dev/rf_luv/spectrum
python3.11 migrate.py    # idempotent; applies 020
```

Verify:

```bash
curl -s "http://localhost:8126/?user=spectrum&password=spectrum_local" \
    --data-binary "SELECT count() FROM spectrum.dongle_comparison_view WHERE hour > now() - INTERVAL 1 HOUR FORMAT TSV"
# Expect: >0 (V3 rows). v4 columns will be NULL until V4 ingests.
```

---

## Phase 9 — Preflight verification

Run through `spectrum/docs/second_dongle_preflight.md` → "Before V4 plug-in"
checklist. Every item must pass before V4 can be plugged in.

---

## Rollback

Each phase is independently rollback-able:

- **Phase 8 (020)**: `DROP VIEW spectrum.dongle_comparison_view` — harmless.
- **Phase 7 (V4 stub)**: `systemctl --user disable rtl-tcp@v4-01` —
  stub is inert if not enabled.
- **Phase 6 (watchdog)**: `systemctl --user disable --now
  rtl-tcp-watchdog@v3-01.timer && systemctl --user enable --now
  rtl-tcp-watchdog.timer` — reverts to singleton watchdog.
- **Phase 4/5 (scanner + 019)**: `systemctl --user stop rtl-scanner@v3-01
  && docker compose start spectrum-scanner` recovers scanner; 019 is
  one-way schema change, no rollback. If 019 failed to produce a usable
  per-dongle baseline, manually drop and recreate the MV from
  `spectrum/clickhouse/init.sql`'s definition.
- **Phase 3 (rtl_tcp cutover)**: `systemctl --user stop rtl-tcp@v3-01
  && systemctl --user enable --now rtl_tcp.service` — both unit files
  still installed.
- **Phase 2 (code)**: `git checkout <prev>` — old scanner still works
  against the new schema because `dongle_id` has a default.
- **Phase 1 (017/018)**: `ALTER TABLE ... DROP COLUMN dongle_id` on each
  table; RMT recreation reversal requires re-running with the old schema
  from init.sql. Expensive but possible.

---

## Post-runbook

Once all phases are complete and `preflight` passes:

1. Commit cutover completion to notes (`notes/2026-04-XX_cutover_complete.md`
   or equivalent).
2. Remove the `spectrum-scanner` service from `spectrum/docker-compose.yml`
   after ~1 week of stable native-systemd operation. Covered as a followup.
3. Plan V4 physical installation (`spectrum/docs/second_dongle_preflight.md`).
