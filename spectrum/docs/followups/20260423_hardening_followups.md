# Hardening followups — 2026-04-23

**Opened:** 2026-04-23

## Context

Running list of items discovered during the 2026-04-23 leap-machine hardening session (plan: `rf-luv-leap-machine-hardening-giggly-sonnet`). Each entry is either (a) deferred because the owner was unavailable for a sudo password, (b) explicitly out-of-scope for this session, or (c) a read-only recommendation surfaced by the hardening work but not executed.

---

## 1. Mask `rtl-tcp-refresh.{service,timer}` on leap (closed 2026-04-29)

**Status:** Closed. Folded into the trip-hardening installer — `ops/install-trip-hardening.sh` masks both units via `/dev/null` symlink (idempotent; detects already-masked state and only acts on the timer). Confirmed against installer dry-run output 2026-04-29: `rtl-tcp-refresh.service` was already masked from a previous pass, the `.timer` gets masked when the trip-hardening installer runs.

**Context.** Two system-level units created on 2026-04-16 were surfaced by the Phase 1 audit:

```
/etc/systemd/system/rtl-tcp-refresh.service   static
/etc/systemd/system/rtl-tcp-refresh.timer     enabled — fires daily at 04:00
```

The service body is `ExecStart=/usr/bin/systemctl restart rtl-tcp`. Its target (`rtl-tcp.service`, the system-level singleton) was masked to `/dev/null` earlier today. The refresh timer now fires at 04:00 daily against a masked target — harmless (silent no-op) but dead code. The live units it was presumably meant to touch (`rtl-tcp@v3-01`, `rtl-tcp@v4-01`) are user-level templates that this service can't reach.

**Proposed action.** Mask both units via `/dev/null` symlink (same pattern as today's other masks — single undo point, `systemctl unmask` restores):

```bash
ssh -t -o IdentitiesOnly=yes dio_nysis@192.168.2.10 \
  'sudo bash -c "ln -sf /dev/null /etc/systemd/system/rtl-tcp-refresh.service && \
                 ln -sf /dev/null /etc/systemd/system/rtl-tcp-refresh.timer && \
                 systemctl daemon-reload && \
                 ls -la /etc/systemd/system/rtl-tcp-refresh.*"'
```

**Urgency:** low. The timer's daily no-op is cosmetic journal noise, not a functional risk. Fold in with the next sudo-requiring leap change.

---

## 2. ClickHouse `spectrum.scans` TTL — hold at 180 days for now

**Status:** No action recommended. Notes captured for future reference.

**Current state (2026-04-23 22:30 UTC).** `system.parts` shows 13.9M rows across 52.39 MiB on disk for the 19-day span 2026-04-05 → 2026-04-23. Compression is ~3.8 bytes/row. Historical average is ~730K rows/day, but dual-dongle steady-state (started 2026-04-22) is closer to ~2.9M rows/day (17 rows/sec/dongle × 2).

**Projection at 180-day TTL, dual-dongle steady-state.** ~520M rows, ~2.0 GiB on disk. Entirely reasonable for the 1 TB rootfs.

**Why not shorten.** V3 pre-vs-post filter self-A/B data points span 2026-04-05 onward. Any TTL shortening drops historical rows on the next merge, poisoning the comparison windows the owner is still collecting. The 180-day horizon is also useful for seasonal RF-environment baselines.

**When to revisit.** After the self-A/B comparison is locked in and the owner confirms which historical windows they want to retain. A 60-day `spectrum.scans` TTL with 180-day rollups (`hourly_baseline`, `freq_latest`) would still preserve all analytical use cases at ~700 MiB.

---

## 3. (Reserved for Phase 2 thermal report, Phase 3 deferrals, etc.)

To be appended as discoveries accumulate.

---

## 4. Trip-hardening layer — added 2026-04-29 (`please-audit-the-following-sequential-octopus` plan)

**Status:** Code in `ops/`, deployed via `ops/install-trip-hardening.sh`. Owner leaving on a 2-week trip; existing watchdog stack stops at the circuit breaker (silent CB-open at fail #10), no out-of-band alerting, no ClickHouse-level freshness probe, no reboot fallback. Trip-hardening adds three layers:

- **`rtl-tcp-escalator`** (root system service, every 5 min): reads watchdog state files; if a serial has been CB-open ≥10 min, runs the proven `unwedge-v4.sh` recipe (per-device USB reset → xHCI bounce → restart user unit). After 3 failed unwedges in 24h on the same serial OR both serials CB-open ≥30 min, triggers `systemctl reboot` (max 1 reboot per 6h).
- **`rf-freshness-probe`** (root system service, every 5 min): queries `spectrum.scans` per `dongle_id`, alerts at WARN >10 min stale and CRITICAL >25 min stale. Independent layer — catches scanner/Docker/ClickHouse failures invisible to the per-process watchdog.
- **`rf-notify`** (stdlib `urllib`): POST to ntfy.sh. Topic configured in `/etc/rtl-scanner/notify.env` (placeholder installed; owner must set `NTFY_TOPIC=` before leaving). Idempotent within a 5-min window per (level,title).
- **`rf-heartbeat`** (daily at 09:00 UTC): one-line status ntfy — confirms the alert pipe is alive even when nothing is wrong.
- **`/var/log/rtl-recovery.log`**: append-only JSON action log. Owner reads on return: `tail /var/log/rtl-recovery.log | jq`.

**V3 recovery folded into install.** As of install-time, V3 is in firmware-starvation state (process alive, RTL0 greeting flows, 0B IQ samples, watchdog CB at fail #96). Per-device unbind/rebind alone didn't unwedge it (same pattern as 2026-04-23 V4 case). The new escalator's first scheduled run within 5 min of install will execute the full unwedge sequence (per-device + xHCI bounce + restart) as root and recover V3 automatically.

**Risks not covered (verbatim from plan):**
1. Chip-lockup signature (2026-04-28 type) remains unrecoverable in software — needs per-port-power hub or smart plug.
2. Kernel panic / OOM kill — `systemctl reboot` won't help.
3. Outbound network failure for ≥24h — ntfy.sh alerts won't reach owner.
4. Both dongles flapping simultaneously — escalator handles each independently and will reboot per the both-CB threshold.
5. Watchdog timer itself fails to fire — freshness probe catches via stale data; reboot path eventually triggers.

**Plan file:** `~/.claude/plans/please-audit-the-following-sequential-octopus.md`.
