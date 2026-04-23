# Hardening followups — 2026-04-23

**Opened:** 2026-04-23

## Context

Running list of items discovered during the 2026-04-23 leap-machine hardening session (plan: `rf-luv-leap-machine-hardening-giggly-sonnet`). Each entry is either (a) deferred because the owner was unavailable for a sudo password, (b) explicitly out-of-scope for this session, or (c) a read-only recommendation surfaced by the hardening work but not executed.

---

## 1. Mask `rtl-tcp-refresh.{service,timer}` on leap (deferred — needs sudo)

**Status:** Deferred. Ready to apply; owner was unavailable for password prompt during the 2026-04-23 cleanup pass.

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

## 2. ClickHouse `spectrum.scans` TTL is ~10× oversized (read-only recommendation)

**Status:** Recommendation only. No ALTER to be executed without explicit review.

**Context.** As of 2026-04-23 17:56 UTC: 13.7M rows / 52 MiB / 180-day TTL on `spectrum.scans`. At the current steady-state throughput (~17 rows/sec per dongle × 2 dongles = ~2.9M rows/day), the 180-day window would permit ~520M rows — roughly 40× the current footprint. TTL is not causing problems, but the headroom is disproportionate.

**Proposed action (review required).** Drop scan-level retention to 30 days; keep 180-day horizons on the rolled-up materialized views (`hourly_baseline`, `freq_latest`) where per-bin granularity isn't needed for long-term analysis.

```sql
-- NOT TO BE EXECUTED without explicit approval — included for review only
ALTER TABLE spectrum.scans MODIFY TTL timestamp + INTERVAL 30 DAY;
-- hourly_baseline and freq_latest materialized views keep existing TTLs
```

**Why not now.** Any TTL shortening drops historical data on the next merge, which would poison the V3 pre-vs-post filter self-A/B windows the owner is still collecting. Revisit once the A/B comparison is locked in.

---

## 3. (Reserved for Phase 2 thermal report, Phase 3 deferrals, etc.)

To be appended as discoveries accumulate.
