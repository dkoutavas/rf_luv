# Deployment status report — 2026-04-23 evening

**Written for:** a Claude Code agent (or a senior engineer) picking up the work and discussing next steps. **Target outcome of that discussion:** turn the rf_luv deployment into something reliable, straightforward, and fun to operate.

## What we built (the last two sessions, 2026-04-22 and 2026-04-23)

A dual-dongle spectrum scanner on leap (192.168.2.10, openSUSE Tumbleweed, user `dio_nysis`, default shell fish):

- **V3** — RTL-SDR Blog V3 (R820T2 tuner), rooftop tripod, 57 cm stock dipole, 3 m up, gain 12 dB, FM bandstop filter inline since `2026-04-22 14:35:12 UTC`. Serial EEPROM-written to `v3-01`. This is the production dongle — the one we care about for the self-A/B experiment.
- **V4** — RTL-SDR Blog V4 (R828D tuner), patio-window short vertical whip (5 cm per element, 1.5 m up, 165° orient), gain 10 dB (adaptive, started at 12), no filter. Serial `v4-01`. Role: antenna-diversity feed, not A/B control (different antenna makes dual-dongle A/B confounded).

Plumbing layer:

- systemd user template units: `rtl-tcp@<serial>.service` + `rtl-scanner@<serial>.service` + `rtl-tcp-watchdog@<serial>.timer`. Port 1234 for V3, 1235 for V4.
- `rtl-tcp-by-serial` wrapper resolves serial → librtlsdr index via `rtl_eeprom` probe (not `rtl_test`, which deadlocks — see gotchas).
- `rtl-usb-reset` helper unbinds/rebinds a specific dongle via sysfs (per-serial).
- `rtl-reset-failed.timer` added today — clears StartLimitBurst state every 5 min so permanent-failed never sticks.
- udev rules for stable `/dev/rtl_sdr_v3` and `/dev/rtl_sdr_v4` symlinks.

Data path: rtl_tcp → TCP to scanner.py → FFT + peak detection → JSON lines → scan_ingest.py → ClickHouse. Downstream: feature_extractor, classifier, detect_compression timers all operate per-dongle (commit `9d66c0d`).

Grafana dashboard on :3003, spectrum-overview.json, 23 panels. Dongle Health Summary at the top (added today — shows both dongles regardless of `$dongle` selector; color-coded "seconds since last write").

## Where we are right now (live numbers, ~17:56 UTC 2026-04-23)

**Ingest in last 30 minutes** (post-recovery):

| dongle | rows | rows/sec | range |
|--------|------|----------|-------|
| v3-01 | 78,908 | 43.8 | 17:34 – 17:56 |
| v4-01 | 27,987 | 15.5 | 17:34 – 17:56 |

The v3 rate is inflated — multiple restart cycles during recovery accumulated data quickly. Normal steady-state is ~17 rows/sec per dongle (equal on both when healthy).

**Sweep health last 30 min:**

| dongle | preset | sweeps | gain | avg_clip | max_clip | avg_max_pwr | avg_dur_ms |
|--------|--------|--------|------|----------|----------|-------------|------------|
| v3-01 | airband | 23 | 11.6 | 0.005 | 0.068 | -39.5 | 144 |
| v3-01 | full | 19 | 11.7 | 0.000 | 0.000 | -26.4 | 2262 |
| v4-01 | airband | 23 | 10.2 | **0.032** | **0.373** | -39.1 | 142 |
| v4-01 | full | 6 | 10.0 | 0.000 | 0.000 | -14.0 | 2262 |

V4 airband clips 3.2% on average, max 37% in a single sweep. V4 full-sweep peaks are 12 dB hotter than V3 (−14 vs −26 dBFS) — the filter on V3 is clearly suppressing something V4 sees.

**Storage (ClickHouse `spectrum` database):** 13.7M scan rows in 52 MiB. Totally fine, 180-day TTL is way oversized for the current throughput.

**Failure counters last 24h (from journal):**

| unit | fail events |
|------|-------------|
| rtl-tcp@v3-01 | 71 |
| rtl-tcp@v4-01 | **1420** |
| rtl-scanner@v3-01 | 0 |
| rtl-scanner@v4-01 | 0 |
| rtl-tcp-watchdog@v4-01 | **1459** |

**This is the headline number.** V4's rtl_tcp failed ~1420 times in 24 hours — one failure per 60 seconds on average. The watchdog fired recovery 1459 times. V3's 71 events are all from two cascading outages where V3 got taken down alongside V4; V3 itself wasn't flapping.

**Ingest continuity (hourly row counts):**

```
hour                 v3        v4
17:00 (today)        78908     27987   ← post-recovery, healthy
16:00 — 02:00                            (outage window, 15h of no data)
01:00 (today)        112372    0       ← v3 spiked as it died, v4 already dead
00:00                66733     33854   ← v4 first flap (00:32 UTC)
23:00 (yesterday)    62621     62426   ← normal
22:00                62621     62816   ← normal
21:00                62621     66538   ← V4 first ingest hour
20:00                47165     33464   ← V3 cutover + V4 plug-in
```

**V4 ran clean for 10 hours** (21:00–23:00 UTC), then flapped, then failed hard. Over 15 hours of no data until today's recovery.

## V3 issues — what actually happened

V3 is the reliable dongle. Its failures were all **cascaded**, not root-caused in V3:

1. **First cascade** (00:32 → 01:36 UTC 2026-04-23): V4 started flapping around 00:32. The V4 watchdog's `rtl-usb-reset v4-01` only targets V4's USB device, but the hammering (1420 resets in a few hours) disturbed the shared USB bus enough that V3's librtlsdr enumeration occasionally failed. V3's `rtl-tcp@v3-01` wrapper returned "serial 'v3-01' not found on bus" on each try, hit `StartLimitBurst=20`, went permanent-failed. Couldn't recover without manual `reset-failed`.

2. **Second cascade** (today, during recovery): an old system-level `/etc/systemd/system/rtl-tcp.service` (Restart=always, runs as `dio_nysis`) that we THOUGHT we disabled yesterday had come back as `active` (not `enabled` — someone/something had started it manually or the disable didn't stick). It was holding V3's USB device. User-level `rtl-tcp@v3-01.service` fighting for device 0 kept getting `usb_claim_interface error -6`, which the wrapper mis-categorized as "serial not found." Same StartLimitBurst cascade.

V3's real failure mode is **the system is intolerant of shared-bus disturbance** and the error messages actively mislead the operator about the cause.

## V4 issues — what actually happened

V4 is the unreliable dongle. Root cause still uncertain, but symptoms are clear:

- Worked fine for ~10 hours after plug-in.
- Then: "all threads dead", "Connection reset by peer", "socket timeout" — the rtl_tcp→USB read pipeline stops producing samples while rtl_tcp's TCP socket stays open.
- Watchdog probe detects "starvation" (< 512 KB in 2 seconds where there should be ~8 MB).
- Watchdog triggers `rtl-usb-reset v4-01` which does sysfs unbind/bind on the 0bda:2838 device matching serial v4-01. Sometimes recovers, sometimes the next probe fails again in 30 seconds.
- After hundreds of cycles, the USB subsystem state gets weird enough that neither dongle can be enumerated cleanly. Both dongles die.

**Likely physical root causes (none confirmed):**
- Marginal USB power delivery — two dongles on one root hub drawing ~400 mA each plus peaks. Could need a powered USB hub.
- Cable/connection noise at V4 (patio window, possibly exposed to RF or temperature variation).
- V4 firmware issue (RTL-SDR Blog V4 with R828D is newer hardware — possibly just flakier).
- Thermal — V4 at the patio window may run hotter than V3 at rooftop tripod.

**Known unknown:** we've never actually unplugged V4 to inspect its cable, feel its temperature, or try a different port. Physical replug today "fixed" it (10 hours later we don't know how long this state will hold).

## Did we plan correctly? Honest retrospective

Short answer: no — the plan underestimated the messiness of production SDR deployments. Here's the gap-by-gap:

### Things the plan missed

1. **The hidden system-level `rtl-tcp.service`.** Never grep'd `/etc/systemd/system/` during the initial inventory. Cost ~4 hours of cutover debugging the first time; another ~1 hour today when it came back (today finally masked it with `/dev/null` symlink).
2. **RTL2838 EEPROM requires VBUS cycle.** `rtl_eeprom -s` only updates the EEPROM; kernel still sees the old serial until the device is power-cycled. `rtl-usb-reset` (sysfs unbind/bind) is NOT equivalent. We learned this mid-cutover when `/dev/rtl_sdr_v3` wouldn't appear. Not anywhere in the original plan.
3. **`rtl_test` deadlock on librtlsdr 2.0+.** The original wrapper used `rtl_test | awk ... exit` assuming awk's exit would SIGPIPE rtl_test. Librtlsdr 2.0's sample loop doesn't write stderr after the device list, so SIGPIPE never triggers. Wrapper held the device forever. Required full rewrite of the probe to `rtl_eeprom`.
4. **ClickHouse 24.3 `MODIFY ORDER BY` restriction.** Migration 017 had `ALTER TABLE scans MODIFY ORDER BY (..., dongle_id)` that worked in theory but fails when `dongle_id` was added in a separate prior statement of the same migration file. Hotfix dropped the ORDER BY change entirely.
5. **`numpy` missing on leap's bare-metal Python.** Docker container had it; the systemd template unit using `/usr/bin/python3.11` didn't. Scanner crashed on first startup. Hotfix: `pip3.11 install --user numpy`. Not in any dependency list.
6. **StartLimitBurst=20 too strict.** systemd default felt reasonable; in reality, a 2-minute USB hiccup burns through it and puts the unit in permanent-failed state with zero auto-recovery. Raised to 100 today.
7. **Watchdog had no circuit breaker.** Designed to keep trying forever. In practice, a broken dongle causes 1000+ reset storms that (a) punish the USB subsystem, (b) cascade to the other dongle, (c) generate no useful signal to an operator beyond "it's been trying for hours." Added circuit breaker today.
8. **Watchdog had no hard-reset cooldown.** Every 30 seconds, hard-reset the USB device. USB devices need time to settle. Hammered them. Added 5-min cooldown today.
9. **Wrapper couldn't distinguish "device missing" from "device busy".** Error message "serial 'v3-01' not found on bus" was misleading every time the real cause was "another process has the handle." Today's update adds busy detection and a different error.
10. **No proactive observability.** We had per-dongle data in ClickHouse but no at-a-glance "is this working?" panel. The entire V3+V4 outage from 00:32 to 20:00 UTC was silent — user discovered it only when they happened to look at Grafana and saw empty charts. Added Dongle Health Summary panel today.
11. **Fish shell on leap.** Every multi-line SSH heredoc needed `bash -l` wrapping. Forgot multiple times. Small friction that adds up over a long session.
12. **`rtl_tcp.service` (singleton user-level) vs `rtl-tcp.service` (system-level) vs `rtl-tcp@<serial>.service` (user-level template).** Three different services, two have hyphens one has underscore, two run as the same user. Maximum confusion potential.
13. **ADS-B, AIS, ISM pipelines silently offline all session.** They share one dongle — whichever we give them — and the dual-dongle cutover took that dongle for spectrum scanning. Original project had 4 pipelines; current state has 1.5 (spectrum × 2 dongles). Users may not realize ADS-B map at :8080 is stale.
14. **A/B methodology pivoted mid-cutover.** Plan assumed splitter; reality had two antennas. We pivoted to self-A/B, which is correct — but should have asked about hardware layout before writing the cutover plan.
15. **No USB power/bus audit.** Two dongles on one root hub, drawing bus power. V4 flapped. We don't know if it's power-starved. A $10 powered hub might fix it. Not planned, not tested.

### Things the plan got right

- EEPROM-serial-based identity scheme → dongle enumeration is stable across reboots.
- Template systemd units → scaling to N dongles is "copy env file, start instance."
- Per-dongle downstream code (feature_extractor, classifier, detect_compression) → data is cleanly separated in ClickHouse.
- Migrations were additive enough that the partial 017 failure didn't corrupt anything.
- `rtl-reset-failed.timer` (today) → safety net that makes many classes of failure self-healing.

### What this says about the plan's quality

The **individual plans were fine**. What was missing is the **"what could go wrong in production"** audit. Most of today's firefighting was things a pre-deployment chaos test would have surfaced:
- Pull V4's USB cable, see if V3 keeps working → we would have caught the bus interference.
- Kill `rtl_tcp` 30 times in 10 minutes → we would have hit StartLimitBurst.
- Put something else on the USB bus that holds the device → we would have caught the misleading wrapper error.

The plan read the code correctly; it didn't stress the code.

## What's stable now vs still fragile

### Stable
- V3 ingest path with the new hardened wrapper + watchdog + reset-failed timer.
- Both dongles enumerating correctly after physical V4 replug and system-service mask.
- Scanner code per-dongle (no data mixing).
- Grafana Dongle Health Summary — silent ingest stops are now visible.
- Migration state settled (015 through 020).

### Still fragile
- **V4 physical reliability is untested.** It worked 10 hours yesterday; might work 10 hours, might work a week. We haven't touched cables, power, or mounting.
- **Shared USB bus.** If V4 flaps severely again, the watchdog circuit breaker will stop hammering — but V4's rtl_tcp will fail-restart 100 times before the new StartLimitBurst ceiling kicks in, and that churn may still nudge V3. Untested.
- **The V3 filter self-A/B has been poisoned twice.** Data pre-filter (2026-04-22 before 14:35 UTC) is clean; post-filter data has huge gaps now (15-hour outage today). Re-running the criterion-based thresholds is not going to be clean until V3 runs continuously for 24-48h.
- **No alerting.** Dongle Health Summary tells you what's wrong if you look. Nothing pings you when something breaks. The next outage will again be discovered by chance.
- **ADS-B / AIS / ISM pipelines have been offline** (~18 hours for ADS-B stopping to matter — airframe coverage drifts fast).
- **Fish / bash / sudo interactive dance.** Every SSH-driven deployment trips over the same three wrappings. Automation is manual.

## What "reliable, straightforward, fun" might mean

*Framing for the conversation, not a plan — these are observations to discuss.*

### Reliable

1. **Audit and fix V4 physical reliability before running the A/B again.** Options: powered USB hub (+$10, most likely win), replacement V4 unit, or accept V4 as a "best-effort diversity" feed where flapping is tolerated.
2. **Decouple the dongles physically.** Today they share a root hub. Separate USB controllers (one via hub on a front port, one on a rear port, or via an add-in card) would eliminate the shared-bus-noise cascade.
3. **Add alerting** — the simplest win: have the rtl-reset-failed.timer OR the dashboard fire a notification when `seconds_since_last_write > 300` for any dongle. Discord webhook, email, signal — whatever the user actually checks. Without this, silence = unknown.
4. **Test the recovery paths.** Simulate a failure, watch the system auto-recover. We've built the mechanisms; we haven't exercised them in a controlled way.
5. **Automate the reboot test.** Template units have `WantedBy=default.target`. We've never rebooted leap to confirm the whole stack comes up clean.

### Straightforward

1. **Delete the old singleton units.** `rtl_tcp.service`, `rtl-tcp-watchdog.service`, `rtl-tcp-watchdog.timer` — all legacy, all sitting in `~/.config/systemd/user/` as "disabled/failed" but confusing. One source of truth per function.
2. **Retire `docker-compose.yml`'s `spectrum-scanner` block** — it's been exited for 26+ hours. Leaving it in the compose file just invites `docker compose up` to accidentally resurrect a conflict. Keep clickhouse + grafana containerized, drop the scanner.
3. **Document shell expectations once, loudly.** A `CLAUDE.md`-level note that "leap default shell is fish; always `bash -l` for SSH heredocs" would save every future session 10 minutes.
4. **`install.sh` is a god-script that half-does things without confirmation** — break it into `install-units.sh`, `install-udev.sh`, `install-sudoers.sh`. Each idempotent, each can be re-run safely.
5. **Deployment today was manual-sudo-heavy.** Could be a single `make deploy` that SSH-copies files, prompts once for sudo, applies all changes. Worth it if we're going to iterate fast.

### Fun

1. **The A/B is still waiting.** Get V3 running continuously for 48h, run the criterion queries, get a data-backed answer to the original question (does the FM filter earn its keep). That was the whole point of this work a week ago.
2. **ADS-B / AIS / ISM are offline.** Those are the parts that are visibly fun — maps, ships, weather sensors decoded in the living room. Consider sharing the V4 dongle with ADS-B on a schedule (e.g. nights) or adding a third dongle dedicated to 1090 MHz.
3. **V4 at the patio window is poorly matched for VHF but great for UHF.** Point it at 1.4 GHz (mil UHF, ADS-B, even cellular IMSI catching for fun) — the 5 cm whip is actually near-optimal for that band. Right now it's mirroring V3's scan range which is wasteful.
4. **Grafana should be narrated, not just numerical.** A "what's happening right now" text panel that says "V3 is quiet, V4 is picking up an active airband signal at 120 MHz" based on current classifications. More inviting than numbers.
5. **Fewer alerts, more invitation.** When a new signal appears that doesn't match `known_frequencies`, push a notification: "unknown carrier at 169.8 MHz — want to investigate?" That turns the tool from monitoring into discovery.

## Quick reference for the next agent

- **Plan file from this session:** `/home/dio_nysi/.claude/plans/could-you-please-check-ticklish-wozniak.md` (post-cutover hardening).
- **Handoff from yesterday:** `spectrum/docs/followups/20260423_dongle_cutover_next_steps.md`.
- **This report:** you're reading it.
- **Memory entries worth reading first:**
  - `project_setup_status.md`
  - `project_antenna_config.md`
  - `reference_leap_machine.md`
  - `project_rtl_tcp_reliability.md`
  - `project_fm_filter_v3_install.md` (has the A/B cutoff timestamp)
- **Last 4 commits:**
  - `08498c8` hardening: tolerate flaps, break runaway watchdog, add health summary  **(today)**
  - `16cbae9` docs: rewrite ab_comparison for V3 self-A/B; mark dongle_id followups done
  - `4f83243` rtl-tcp-by-serial: replace rtl_test enumerate with rtl_eeprom probes
  - `919efc6` 017: drop unsupported MODIFY ORDER BY on scans
- **One-liner health check (paste in SSH):**
  ```bash
  ssh dio_nysis@192.168.2.10 "bash -l" <<'EOF'
  systemctl --user is-active rtl-tcp@v3-01 rtl-scanner@v3-01 rtl-tcp@v4-01 rtl-scanner@v4-01 rtl-tcp-watchdog@v3-01.timer rtl-tcp-watchdog@v4-01.timer rtl-reset-failed.timer
  curl -s 'http://localhost:8126/?user=spectrum&password=spectrum_local&database=spectrum' \
    --data "SELECT dongle_id, count() FROM scans WHERE timestamp > now() - INTERVAL 5 MINUTE GROUP BY dongle_id FORMAT TabSeparated"
  EOF
  ```
  Expected: 7 × `active`, then two rows with v3-01/v4-01 both in the 20k+ range.
