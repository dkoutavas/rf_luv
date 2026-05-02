# rtl-coordinator — flock-based dongle access coordinator

Lets multiple short-lived rtl_tcp consumers (NOAA APT recorder, Meteor LRPT, scheduled decoders) share a dongle without stepping on each other or on the wideband scanner. Implemented as a thin `flock(1)` wrapper — no daemon, no IPC, no central process.

```
                       ┌──────────────────────────┐
                       │   /var/lib/rtl-coordinator/<serial>.lock
                       │     ↑       ↑       ↑    │
            wrapper script ─┘       │       │    │
            scanner.py (via         │       │    │
                spectrum/coordinator.py)    │    │
            other decoder ─────────────────┘    │
                       └──────────────────────────┘
                  flock(2) — advisory, in-kernel
```

The lock file path is the rendezvous point. Every consumer that wants exclusive use of a dongle takes the lock for that dongle's serial.

## When you actually need this

Most decoders DON'T. The two-dongle layout already gives parallelism (V3 + V4); a decoder that owns a dongle exclusively (like ACARS owning V4) just runs continuously without ever touching the coordinator.

You need the coordinator when:

1. **A decoder runs in scheduled bursts on a dongle that's also used for something else.** Canonical case: NOAA APT pass recording at 137 MHz. The pass lasts ~12 min and needs full sample rate. The wideband scanner on the same dongle has to step aside for that window.
2. **Two decoders share a dongle by time-division.** E.g., POCSAG sweep and marine voice activity logging both want V4 but on different schedules.
3. **A maintenance task needs the dongle.** E.g., the rtl-tcp escalator's `rtl-usb-reset` — currently doesn't take this lock, but could.

If you're building a decoder that needs continuous access to a dongle (like ACARS), do NOT use the coordinator — give it the dongle outright and disable the wideband scanner on that dongle (see `acars/DEPLOY.md`).

## Files

- `rtl-coordinator-run` — flock wrapper script. Takes a dongle serial, a mode (`--wait` / `--nonblock` / `--timeout <sec>`), and a command. Acquires the lock, runs the command, releases on exit.
- `rtl-coordinator-status` — debug helper. Lists all locks and their current holder PID/comm.
- `rtl-coordinator@.service` — templated systemd unit for systemctl-driven launches. Reads `/etc/rtl-coordinator/<serial>.env` for `COORDINATOR_CMD` etc.
- `install.sh` — idempotent installer. Creates `/var/lib/rtl-coordinator/`, installs the binaries to `/usr/local/bin/`, registers the systemd template.

## Usage from a decoder

Bash:
```bash
rtl-coordinator-run v3-01 --timeout 30 -- \
    /home/dio_nysis/dev/rf_luv/scripts/satellite-pass.sh noaa18
# Exits 75 if the lock can't be acquired in 30s.
```

Python (for tools that already have a long-lived process):
```python
# spectrum/coordinator.py provides a context manager
from spectrum.coordinator import dongle_lock

with dongle_lock("v3-01", mode="nonblock") as ok:
    if not ok:
        log.info("Skipping sweep — coordinator lock held by another consumer")
        return
    do_the_sweep()
```

## Integration with the wideband scanner

`spectrum/scanner.py` is the canonical "I want the dongle for ~30 seconds" consumer. To make it lock-aware (NOT done in this commit — needs a real test on leap):

1. Add `from coordinator import dongle_lock` to scanner.py.
2. Wrap the per-sweep `RTLTCPClient(...)` connect in `with dongle_lock(DONGLE_ID, mode="nonblock") as ok:` — skip the sweep on miss.

The Python helper `spectrum/coordinator.py` exists in this commit; the scanner.py change does NOT, intentionally. Wire it in when the first scheduled decoder (#6 NOAA) lands and there's a real opportunity to test the integration end-to-end.

## Lock semantics

- **Advisory.** Locks only matter if every consumer takes them. A consumer that bypasses the coordinator and connects to rtl_tcp directly will succeed (rtl_tcp itself only enforces single-client; new connection kicks the previous one).
- **One lock per dongle.** Lock file = `/var/lib/rtl-coordinator/<serial>.lock`. Use the same serial as `SCAN_DONGLE_ID` (`v3-01`, `v4-01`, etc.) for consistency with the existing systemd template names.
- **Auto-released on process exit.** flock holds the lock as long as FD 9 is open in the wrapper (or the FD passed to `flock()` in Python). When the wrapped command exits, the lock goes away.
- **Non-blocking is preferred for the scanner**, blocking is preferred for decoders. Scanner wants to skip and try again next sweep; decoders want to wait their turn.

## Audit trail

Every lock event is appended to `/var/log/rtl-recovery.log` (the same log the watchdog/escalator stack uses) as one JSON line. Events: `lock_wait_start`, `lock_try`, `lock_busy`, `lock_acquired`, `lock_released`, `lock_timeout`. Inspect with `tail -f /var/log/rtl-recovery.log | jq 'select(.layer=="coordinator")'`.

## Failure modes the coordinator does NOT cover

- **Bypass.** Any process that opens rtl_tcp without taking the lock breaks the model. Scanner + every documented decoder is on the honor system.
- **rtl_tcp single-client kick.** rtl_tcp itself drops the previous client when a new one connects, regardless of any flock state. The coordinator prevents this happening but doesn't react if it does. If you see scanner sweeps with truncated data after a coordinator-bypassing consumer connects, that's why.
- **Stale lock from a crashed process.** flock auto-releases on FD close, so even SIGKILL releases the lock. Truly stuck only if the kernel itself is hung.

## Related

- `ops/rtl-tcp/` — the per-dongle rtl_tcp service + watchdog stack
- `ops/rtl-tcp-escalator/` — escalator that resets USB on prolonged failure
- `acars/DEPLOY.md` — example of a decoder that owns a dongle outright (no coordinator needed)
