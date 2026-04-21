# Forensic capture — design

**Status: design only. No code or infra changes for immediate merge. Requires Phase 3's rtl_tcp concurrency verification before any implementation begins.**

## Goal

When a compression event or operator-requested target fires, **acquire 2 MHz of raw IQ at the estimated emitter center frequency for a few seconds** so the modulation can be inspected offline (SDR++, `baudline`, `inspectrum`, Python demod). The 2026-04-21 11:55:04 event was a pure FFT spectrum snapshot; we could estimate the emitter frequency but not its modulation, symbol rate, or payload. Forensic capture closes that gap.

Constraint: **single RTL-SDR dongle**, shared via rtl_tcp on the leap box. The scanner owns it 24/7 for baseline sweeps.

## Recommended architecture — single-dongle priority queue with flock

```
  ┌─────────────────────────────┐           ┌───────────────────────┐
  │ detect_compression sidecar  │           │  Listening Playbook    │
  │ (or operator manual)        │           │   Grafana panel        │
  └──────────────┬──────────────┘           └──────────┬────────────┘
                 │ INSERT trigger row                    │
                 ▼                                       ▼
          ┌──────────────────────────────────────────────────┐
          │   spectrum.forensic_trigger                      │
          │   (freq_hz, span_hz, duration_s, requested_at,   │
          │    rate_limited, status)                         │
          └──────────────┬───────────────────────────────────┘
                         │ polled between sweeps
                         ▼
          ┌──────────────────────────────────────────────────┐
          │   scanner.py main loop                           │
          │   1. grab /run/rtl_tcp.lock  (flock)             │
          │   2. claim oldest pending trigger                │
          │   3. tune + capture N seconds IQ                 │
          │   4. write /var/lib/spectrum/iq_captures/...cs8  │
          │   5. update trigger.status = 'captured'          │
          │   6. update scan_tiles.iq_capture_path          │
          │   7. release /run/rtl_tcp.lock                   │
          │   8. resume sweep                                │
          └──────────────┬───────────────────────────────────┘
                         │
                         ▼                              (operator pulls .cs8
                  /var/lib/spectrum/iq_captures          into SDR++/baudline
                  └── 2026-04-21T11:55:04_304190.cs8     for offline analysis)
```

### Why flock

rtl_tcp is **typically single-client** (verify in Phase 3 before building). If the scanner's next sweep attempts to open a second connection during a forensic capture, either (a) rtl_tcp refuses the second connection, (b) rtl_tcp accepts but sends both clients the same stream (so both get corrupted), or (c) rtl_tcp kicks the first client. All three are bad.

A file lock on `/run/rtl_tcp.lock` (or `/var/run/rtl_tcp.lock`) serializes access regardless of what rtl_tcp itself does. The scanner uses this pattern:

```python
import fcntl
LOCK_PATH = "/run/rtl_tcp.lock"
with open(LOCK_PATH, 'w') as lk:
    fcntl.flock(lk.fileno(), fcntl.LOCK_EX)  # blocks until acquired
    # ...rtl_tcp work here...
    # lock released when 'with' exits
```

Both the regular sweep loop AND the forensic capture code hold the lock for their duration. They're cooperating processes, so the lock is the coordination primitive.

### Why rate-limit

Trigger storms during a real broadband disturbance (lightning, a close-range emitter keyed for minutes, a misconfigured neighbor radio) could fire triggers every minute. Each trigger steals a full sweep plus a few seconds, starving the baseline scan and exhausting disk. Rate-limit enforced by the trigger-inserter, not the scanner:

```python
# In sidecar, before inserting a trigger row:
q = "SELECT count() FROM forensic_trigger WHERE requested_at > now() - INTERVAL 1 HOUR AND status != 'cancelled'"
n = int(ch_query(q))
if n >= MAX_FORENSIC_CAPTURES_PER_HOUR:
    ch_query(f"INSERT INTO forensic_trigger_rate_limited VALUES (now(), {freq_hz}, {reason!r})")
    return
```

Default `MAX_FORENSIC_CAPTURES_PER_HOUR = 3`. Operator can override via env var if investigating a known storm.

### Drop on floor if queue > 1 deep

Second rule, complementary to rate limit. If a trigger lands while one is already pending, drop the new one silently (logged only). Catching up on missed triggers is never the right move — the emitter is gone, and the captured IQ would be of a later event, misattributed to the earlier trigger.

### Disk rotation

`/var/lib/spectrum/iq_captures/` capped at 2 GB (≈ 100 captures at 20 MB each). Rotation:

```python
# Ran before each capture:
files = sorted(glob.glob(IQ_DIR + "/*.cs8"), key=os.path.getmtime)
total = sum(os.path.getsize(f) for f in files)
while total > 2_000_000_000 and files:
    f = files.pop(0)
    total -= os.path.getsize(f)
    os.unlink(f)
```

Captures older than 90 days that weren't manually archived are deleted anyway via a separate TTL cron. Operator who wants to keep one forever moves it out of `iq_captures/` into `iq_captures/archive/` (not subject to rotation).

## Schema

```sql
-- New migration 016_add_forensic_trigger.sql
CREATE TABLE IF NOT EXISTS spectrum.forensic_trigger (
    trigger_id       String DEFAULT generateUUIDv4(),
    requested_at     DateTime64(3) DEFAULT now64(3),
    freq_hz          UInt32,
    span_hz          UInt32 DEFAULT 2000000,
    duration_s       Float32 DEFAULT 5.0,
    gain_db          Float32 DEFAULT 2.0,
    source           String,                          -- 'compression_event:<sweep_id>' / 'operator:<name>'
    status           Enum8('pending'=0, 'captured'=1, 'failed'=2, 'rate_limited'=3, 'cancelled'=4) DEFAULT 'pending',
    captured_at      Nullable(DateTime64(3)),
    capture_path     String DEFAULT '',
    capture_bytes    UInt64 DEFAULT 0,
    error            String DEFAULT ''
) ENGINE = MergeTree()
ORDER BY (requested_at, trigger_id)
TTL toDateTime(requested_at) + INTERVAL 90 DAY;
```

## Capture format

`.cs8` = complex signed 8-bit (raw I then Q, both as signed bytes in range -127..127 after centering). This is the native RTL-SDR output shifted from unsigned 0..255. 2 MHz sample rate × 5 s × 2 bytes = **20 MB per capture**.

Filename convention: `YYYY-MM-DDThh:mm:ss_<freq_khz>_<duration>s.cs8` — sortable and self-describing. Example: `2026-04-21T11:55:04_304190_5s.cs8`.

Why `.cs8` not `.wav`:
- SDR++ and `baudline` open `.cs8` natively.
- wav headers add complication for the operator and the Python writer.
- Storage is identical — IQ is IQ.

## Downstream classification — deliberately manual

Operator workflow:
1. Compression event fires, trigger queued, scanner captures, Grafana table lights up with new row in `forensic_trigger` at status=`captured`.
2. Operator clicks `capture_path` link (served via a tiny static file server, separate container; `iq_captures/` mounted read-only) — browser downloads the .cs8.
3. Drag into SDR++ with appropriate center/sample-rate; inspect spectrum/waterfall/audio.
4. If identified, operator adds a row to `listening_log` with `confirmed_freq_hz` and `class_id` — classifier prior updates automatically.

No programmatic demod in v1. A tiny Python helper script to strip AM or NFM envelope from a .cs8 can be added later if it becomes a common need (`scripts/demod_am.py infile.cs8 outfile.wav`).

## Second-dongle mode — sketch only, deferred

Once compression event rate exceeds ~10/day or the operator finds they want to monitor specific frequencies continuously while keeping baseline sweeps going, add a second RTL-SDR dongle:

- Separate `docker-compose` service `spectrum-forensic` with its own `rtl_tcp` instance on the second USB (port 1235 instead of 1234).
- Forensic capture service uses the secondary rtl_tcp only; scanner untouched.
- No flock coordination needed.
- Second dongle dedicated to forensic mode means no single-event-skipped-sweep penalty.

Cost: ~$35 dongle + USB port. Not needed until the pipeline proves the case. Based on Phase 2 backfill (at time of writing, 0-15 events in 16 days), single-dongle with 3/hr rate limit is overkill for current event rates. Do not build this until the event rate demands it.

## What NOT to build in v1

- Remote alerting (email, Slack, SMS). The operator can check Grafana.
- Automatic modulation classification (AM vs FM vs FSK vs OFDM). The operator's SDR++ session does this faster than a heuristic classifier can.
- Cross-device triangulation. We have one antenna in Polygono; triangulation requires two receivers.
- Long-tail archival to cloud storage. 20 MB/capture × <1000 captures/year is under 20 GB/year, fits on leap's disk.
- Trigger-replay / backtest mode. A captured .cs8 can be replayed into rtl_tcp via `rtl_fm_emulator` if we ever need this.

## Verification before wiring up (pre-Phase-5 gate)

1. **rtl_tcp concurrency test** (per signature_detection.md) — confirmed what happens with 2 concurrent clients before choosing lock strategy.
2. **Scanner lock overhead** — instrument a flock-acquire round trip on leap, confirm <1 ms (it'll be negligible but measure).
3. **Disk write latency** — write 20 MB to leap disk repeatedly, confirm <500 ms so capture time dominates, not filesystem flush.
4. **ClickHouse trigger poll cost** — `SELECT ... FROM forensic_trigger WHERE status = 'pending' LIMIT 1` between sweeps is cheap, but measure anyway.
5. **TTL rotation works** — manually fill `iq_captures/` past 2 GB and confirm rotation.

Only then: implement scanner changes + sidecar trigger emitter. All five verifications above are < 1 hour of work.

## Verification after implementation

End-to-end smoke test: operator inserts a manual trigger row targeting a known continuous carrier (e.g. 99.6 MHz Kosmos FM) and confirms:

1. Next sweep completes normally.
2. Between sweeps, `iq_captures/<ts>_99600_5s.cs8` appears with size ≈ 20 MB.
3. `forensic_trigger` row status becomes 'captured', `capture_path` populated.
4. Drag into SDR++, tune to 99.6 MHz center, hear FM audio — confirms IQ integrity end-to-end.
5. Run scanner sweep proceeds normally after capture releases the lock.

If any of 1-5 fails, the architecture has a flaw the verification tests didn't catch — pause and re-investigate.
