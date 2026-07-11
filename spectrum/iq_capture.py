#!/usr/bin/env python3
"""
spectrum.iq_capture — forensic raw-IQ recorder (feature D2).

A STANDALONE consumer of the single local RTL-SDR V4 that grabs N seconds of
raw IQ at a target center frequency, time-sharing the dongle with the running
scanner through the flock coordinator (spectrum/coordinator.py). It writes a
`.cs8` file + a `.json` manifest sidecar + a `spectrum.iq_captures` ClickHouse
row. This is the time-domain layer that unblocks blind decoding (D3) and the
FIRST real exercise of coordinator contention (until now scanner.py was the
only lock-taker).

Design: spectrum/docs/forensic_capture.md (adapted; see that doc + the D2 plan).

SAFETY / CORRECTNESS crux (read before editing):
  - Lock is ALWAYS taken with mode="timeout" (bounded), NEVER mode="wait".
    mode="wait" is a blocking LOCK_EX with no deadline — against a 24/7 scanner
    it can hang the recorder forever (the NOAA scaffold's deadlock trap). The
    scanner takes the same lock per-sweep with mode="nonblock" and simply skips
    a sweep while we hold it, so we MUST yield promptly: the lock is held only
    around connect+tune+settle+read+rename (~duration + ~1 s), never around DB
    writes, disk rotation, or the poll loop.
  - CU8 -> CS8: rtl_tcp streams UNSIGNED 8-bit interleaved I/Q (0..255, DC at
    ~127.5); .cs8 is SIGNED int8 = byte - 128 (range -128..127). See cu8_to_cs8.
  - LEGAL BLOCKLIST: auto-triggers in TETRA / cellular bands are always refused;
    operator/CLI triggers there are refused unless IQ_ALLOW_BLOCKED_BANDS=1.

Deliberate deviations from the doc (see forensic_capture.md / the plan):
  - Writes ClickHouse rows directly via spectrum/db.py, NOT through the
    scanner's messages.py / scan_ingest.py stdout-marker path (that pipe is the
    scanner's; a standalone consumer uses db.py).
  - Standalone process using coordinator.dongle_lock, not capture inline in
    scanner.py's loop on a raw blocking flock.
  - CLI/manual + operator triggers only in D2; detect_compression auto-wiring is
    a later two-line follow-up that calls insert_trigger().
"""

from __future__ import annotations

import os
import sys
import glob
import json
import time
import uuid
import signal
import logging
import argparse
from datetime import datetime, timezone

import numpy as np

# scanner.py is import-safe (__main__-guarded, no I/O at import) but DOES
# register its own SIGTERM/SIGINT handlers at module level (scanner.py L105-106)
# and calls logging.basicConfig. We therefore import it FIRST and register our
# own signal handlers only later (in run_daemon), after this import has run.
# Guarded so the mocked-client tests run even on a host where scanner's deps
# are unavailable — perform_capture always accepts a client_factory.
try:
    from scanner import RTLTCPClient  # type: ignore
except Exception:  # pragma: no cover - defensive
    RTLTCPClient = None  # type: ignore

import coordinator
from coordinator import dongle_lock
import db

log = logging.getLogger("iq_capture")

# ─── Config (env) ────────────────────────────────────────

IQ_CAPTURE_DIR = os.environ.get("IQ_CAPTURE_DIR", "/var/lib/spectrum/iq_captures")
IQ_MAX_CAPTURES_PER_HOUR = int(os.environ.get("IQ_MAX_CAPTURES_PER_HOUR", "3"))
IQ_DIR_MAX_BYTES = int(os.environ.get("IQ_DIR_MAX_BYTES", "2000000000"))
IQ_LOCK_TIMEOUT_S = float(os.environ.get("IQ_LOCK_TIMEOUT_S", "30"))
IQ_POLL_INTERVAL_S = float(os.environ.get("IQ_POLL_INTERVAL_S", "5"))
IQ_MAX_DURATION_S = float(os.environ.get("IQ_MAX_DURATION_S", "30"))
# Operator override for the legal blocklist. Default OFF. Read as a module
# global so tests can flip iq_capture.IQ_ALLOW_BLOCKED_BANDS at runtime.
IQ_ALLOW_BLOCKED_BANDS = os.environ.get("IQ_ALLOW_BLOCKED_BANDS", "0") == "1"

# Host-run recorder talks to the local rtl_tcp directly (NOT the scanner's
# Docker host.docker.internal default).
RTL_HOST = os.environ.get("RTL_TCP_HOST", "127.0.0.1")
RTL_PORT = int(os.environ.get("RTL_TCP_PORT", "1234"))
DONGLE_ID = os.environ.get("SCAN_DONGLE_ID", "v4-01")

DEFAULT_SAMPLE_RATE = int(os.environ.get("SCAN_SAMPLE_RATE", "2048000"))
DEFAULT_DURATION_S = float(os.environ.get("IQ_DURATION_S", "5.0"))
DEFAULT_GAIN_DB = float(os.environ.get("IQ_GAIN_DB", "20.0"))

# rtl_tcp read tuning (byte-for-byte the scanner main-loop warmup pattern).
WARMUP_BYTES = 131072      # discard(): PLL settle + stale-buffer drain
CHUNK_BYTES = 262144       # 256 KiB reads keep memory flat, socket un-starved
MAX_LOCK_ATTEMPTS = 5      # consecutive lock timeouts -> mark trigger failed

# LEGAL BLOCKLIST — capturing to decode protected TETRA / cellular systems is
# out of scope. (lo_hz, hi_hz, label). Labeling these from sweep data is fine;
# grabbing raw IQ to demod them is not.
BLOCKED_BANDS = [
    (380e6, 400e6, "TETRA"),
    (791e6, 862e6, "LTE800"),
    (880e6, 960e6, "GSM/UMTS900"),
    (1710e6, 1880e6, "GSM1800"),
]

# Sentinel: perform_capture could not acquire the dongle within the timeout.
# Distinct from None (a terminal outcome) so the daemon can retry / age out.
LOCK_MISS = object()


# ─── Thin ClickHouse shims ───────────────────────────────
# All DB access funnels through these four module-level functions precisely so
# the self-test can stub them by attribute assignment (stdlib "mocking") without
# a live ClickHouse. Do not call db.* directly elsewhere in this module.

def _ch_scalar(sql: str):
    return db.query_scalar(sql)


def _ch_rows(sql: str):
    return db.query_rows(sql)


def _ch_insert(table: str, rows):
    db.insert(table, rows)


def _ch_exec(sql: str):
    return db.query(sql)


def _sql_str(value: str) -> str:
    """Escape a Python str for a single-quoted ClickHouse SQL literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ─── CU8 -> CS8 ──────────────────────────────────────────

def cu8_to_cs8(buf: bytes) -> bytes:
    """Convert rtl_tcp's unsigned 8-bit interleaved I/Q to signed .cs8.

    rtl_tcp streams uint8 samples 0..255 with DC at ~127.5. .cs8 wants signed
    int8 centered at 128, i.e. byte - 128 (range -128..127). Interleave order
    (I,Q,I,Q,...) and length are preserved 1:1.

    Note the off-by-one vs the doc's "-127..127": 0x00 maps to -128, not -127.
    """
    u8 = np.frombuffer(buf, dtype=np.uint8)
    return (u8.astype(np.int16) - 128).astype(np.int8).tobytes()


# ─── Legal blocklist ─────────────────────────────────────

def _band_for(freq_hz: float):
    for lo, hi, label in BLOCKED_BANDS:
        if lo <= freq_hz < hi:
            return label
    return None


def freq_blocked(freq_hz: float, source: str):
    """Return the blocked-band label if this (freq, source) must be REFUSED,
    else None.

    Auto-path sources (source startswith 'compression_event:') in a blocked band
    are ALWAYS refused. Operator/CLI sources are refused too, unless the explicit
    IQ_ALLOW_BLOCKED_BANDS=1 override is set. Checked twice (insert_trigger AND
    perform_capture) as defense in depth — anyone can INSERT a row directly.
    """
    band = _band_for(freq_hz)
    if band is None:
        return None
    is_auto = source.startswith("compression_event:")
    if is_auto:
        return band
    if IQ_ALLOW_BLOCKED_BANDS:
        return None
    return band


# ─── Disk rotation ───────────────────────────────────────

def rotate_capture_dir(capture_dir: str = None) -> None:
    """Oldest-first delete .cs8 (+ its .json sidecar) until dir <= IQ_DIR_MAX_BYTES.

    Non-recursive glob so iq_captures/archive/ is immune. Runs BEFORE each
    capture (outside the dongle lock).
    """
    capture_dir = capture_dir or IQ_CAPTURE_DIR
    files = glob.glob(os.path.join(capture_dir, "*.cs8"))
    files.sort(key=lambda f: os.path.getmtime(f))  # oldest first
    total = sum(os.path.getsize(f) for f in files)
    i = 0
    while total > IQ_DIR_MAX_BYTES and i < len(files):
        f = files[i]
        try:
            size = os.path.getsize(f)
            os.unlink(f)
            total -= size
            side = f[:-4] + ".json"
            if os.path.exists(side):
                os.unlink(side)
            log.info("rotated out %s (%d bytes)", os.path.basename(f), size)
        except OSError as e:  # pragma: no cover - defensive
            log.warning("rotation could not remove %s: %s", f, e)
        i += 1


# ─── Trigger insertion (rate-limit + drop + blocklist) ───

def insert_trigger(
    freq_hz: int,
    *,
    duration_s: float = None,
    span_hz: int = None,
    gain_db: float = None,
    source: str = "operator:cli",
) -> str:
    """Insert a forensic_trigger row, enforcing the legal blocklist, the hourly
    rate limit, and the drop-if-queue-already-pending rule. Returns the resulting
    disposition string: 'blocked' | 'rate_limited' | 'dropped' | 'pending'.

    Importable — the future detect_compression auto-path calls this. Rate limit
    lives HERE (the inserter), not on the capture path, per the design.
    """
    duration_s = DEFAULT_DURATION_S if duration_s is None else duration_s
    span_hz = DEFAULT_SAMPLE_RATE if span_hz is None else span_hz
    gain_db = DEFAULT_GAIN_DB if gain_db is None else gain_db

    band = freq_blocked(freq_hz, source)
    if band:
        _ch_insert("forensic_trigger", [{
            "trigger_id": str(uuid.uuid4()),
            "dongle_id": DONGLE_ID,
            "freq_hz": int(freq_hz),
            "span_hz": int(span_hz),
            "duration_s": float(duration_s),
            "gain_db": float(gain_db),
            "source": source,
            "status": "blocked",
            "error": f"blocked band {band}",
        }])
        log.warning("refused trigger @ %d Hz — blocked band %s (source=%s)",
                    int(freq_hz), band, source)
        return "blocked"

    # Rate limit: count only rows that consumed (or will consume) a capture
    # slot. Excluding rate_limited/blocked/cancelled avoids the doc snippet's
    # bug where counting rate_limited rows locks the queue out forever.
    recent = _ch_scalar(
        "SELECT count() FROM forensic_trigger "
        "WHERE requested_at > now() - INTERVAL 1 HOUR "
        "AND status IN ('pending','captured','failed')"
    ) or 0
    if int(recent) >= IQ_MAX_CAPTURES_PER_HOUR:
        _ch_insert("forensic_trigger", [{
            "trigger_id": str(uuid.uuid4()),
            "dongle_id": DONGLE_ID,
            "freq_hz": int(freq_hz),
            "span_hz": int(span_hz),
            "duration_s": float(duration_s),
            "gain_db": float(gain_db),
            "source": source,
            "status": "rate_limited",
            "error": f"rate limit {IQ_MAX_CAPTURES_PER_HOUR}/hour exceeded",
        }])
        log.warning("rate-limited trigger @ %d Hz (%d in last hour)",
                    int(freq_hz), int(recent))
        return "rate_limited"

    # Drop-if-queue: never catch up on missed triggers — a late capture would be
    # misattributed. Log only, no row.
    pending = _ch_scalar(
        "SELECT count() FROM forensic_trigger WHERE status='pending'"
    ) or 0
    if int(pending) >= 1:
        log.warning("dropping trigger @ %d Hz — %d already pending",
                    int(freq_hz), int(pending))
        return "dropped"

    _ch_insert("forensic_trigger", [{
        "trigger_id": str(uuid.uuid4()),
        "dongle_id": DONGLE_ID,
        "freq_hz": int(freq_hz),
        "span_hz": int(span_hz),
        "duration_s": float(duration_s),
        "gain_db": float(gain_db),
        "source": source,
        "status": "pending",
    }])
    log.info("queued trigger @ %d Hz (source=%s)", int(freq_hz), source)
    return "pending"


# ─── Trigger status transitions ──────────────────────────

def _set_trigger_status(
    trigger_id: str,
    status: str,
    *,
    error: str = "",
    captured_at: str = "",
    capture_path: str = "",
    capture_bytes: int = 0,
) -> None:
    """Synchronous (mutations_sync=1) ALTER UPDATE so the next poll cannot
    re-claim a just-finished trigger. At <=3 mutations/hour this is trivial."""
    sets = [f"status='{status}'"]
    if error:
        sets.append(f"error='{_sql_str(error)}'")
    if capture_path:
        sets.append(f"capture_path='{_sql_str(capture_path)}'")
    if capture_bytes:
        sets.append(f"capture_bytes={int(capture_bytes)}")
    if captured_at:
        sets.append(f"captured_at='{captured_at}'")
    _ch_exec(
        "ALTER TABLE forensic_trigger UPDATE "
        + ", ".join(sets)
        + f" WHERE trigger_id='{_sql_str(trigger_id)}' SETTINGS mutations_sync=1"
    )


# ─── Capture ─────────────────────────────────────────────

def _capture_filename(freq_hz: int, duration_s: float, when: datetime) -> str:
    # Hyphens (not colons) in the time so the file survives being dragged onto
    # Windows SDR++ from WSL2.
    ts = when.strftime("%Y-%m-%dT%H-%M-%S")
    return f"{ts}_{freq_hz // 1000}_{duration_s:g}s.cs8"


def _do_capture(client_factory, freq_hz, rate_hz, gain_db, total_bytes,
                capture_dir, duration_s) -> str:
    """Take the raw IQ inside the dongle lock. Writes to a .part temp file then
    atomically renames. Returns the final .cs8 path."""
    fname = _capture_filename(freq_hz, duration_s, datetime.now(timezone.utc))
    final = os.path.join(capture_dir, fname)
    part = final + ".part"

    client = client_factory(RTL_HOST, RTL_PORT)
    try:
        client.set_sample_rate(rate_hz)
        client.set_gain(gain_db)
        client.set_frequency(freq_hz)
        time.sleep(0.010)              # PLL settle
        client.discard(WARMUP_BYTES)   # drain stale buffer
        written = 0
        with open(part, "wb") as f:
            while written < total_bytes:
                n = min(CHUNK_BYTES, total_bytes - written)
                buf = client.read_samples(n)
                if not buf:
                    break
                f.write(cu8_to_cs8(buf))
                written += len(buf)
    finally:
        client.close()
    if written < total_bytes:
        # rtl_tcp stream ended early — don't pass off a truncated file as a
        # clean capture. Drop the .part; perform_capture marks the trigger failed.
        try:
            os.unlink(part)
        except OSError:
            pass
        raise IOError(f"short capture: {written}/{total_bytes} bytes (rtl_tcp stream ended)")
    os.replace(part, final)
    return final


def _write_manifest(cs8_path, *, freq_hz, rate_hz, duration_s, gain_db,
                    trigger_id, captured_at) -> str:
    manifest_path = cs8_path[:-4] + ".json"
    with open(manifest_path, "w") as f:
        json.dump({
            "freq_hz": int(freq_hz),
            "sample_rate_hz": int(rate_hz),
            "duration_s": float(duration_s),
            "gain_db": float(gain_db),
            "dongle_id": DONGLE_ID,
            "trigger_id": trigger_id,
            "captured_at": captured_at,
            "format": "cs8",
        }, f, indent=2)
    return manifest_path


def perform_capture(trigger: dict, *, client_factory=None, capture_dir: str = None):
    """Capture one trigger: blocklist re-check -> rotate -> timeout-lock ->
    tune+read+write .cs8 -> manifest + iq_captures row + status='captured'.

    Returns the .cs8 path on success, LOCK_MISS if the dongle could not be
    acquired within IQ_LOCK_TIMEOUT_S (trigger left pending for retry), or None
    on a terminal non-capture (blocked, or capture error -> status='failed').

    The dongle lock is held ONLY around connect+tune+settle+read+rename — never
    around rotation or DB writes — so the scanner's per-sweep nonblock acquire
    is deferred by at most ~duration_s + ~1 s.
    """
    if client_factory is None:
        client_factory = RTLTCPClient
    capture_dir = capture_dir or IQ_CAPTURE_DIR

    trigger_id = trigger.get("trigger_id", "")
    source = trigger.get("source", "")
    freq_hz = int(trigger["freq_hz"])
    rate_hz = int(trigger.get("span_hz") or DEFAULT_SAMPLE_RATE)
    duration_s = float(trigger.get("duration_s") or DEFAULT_DURATION_S)
    gain_db = float(trigger["gain_db"]) if trigger.get("gain_db") is not None else DEFAULT_GAIN_DB

    # Defense in depth: re-check the blocklist at the capture path too.
    band = freq_blocked(freq_hz, source)
    if band:
        log.warning("perform_capture refusing %d Hz — blocked band %s", freq_hz, band)
        if trigger_id:
            _set_trigger_status(trigger_id, "blocked", error=f"blocked band {band}")
        return None

    # Hard-cap duration to bound the lock hold.
    duration_s = min(duration_s, IQ_MAX_DURATION_S)
    total_bytes = int(duration_s * rate_hz) * 2  # 2 bytes (I+Q) per sample pair

    rotate_capture_dir(capture_dir)  # outside the lock

    with dongle_lock(DONGLE_ID, mode="timeout", timeout=IQ_LOCK_TIMEOUT_S) as got:
        if not got:
            log.warning("dongle %s lock timeout after %.1fs — leaving trigger pending",
                        DONGLE_ID, IQ_LOCK_TIMEOUT_S)
            return LOCK_MISS
        try:
            path = _do_capture(client_factory, freq_hz, rate_hz, gain_db,
                               total_bytes, capture_dir, duration_s)
        except Exception as e:  # capture failed — mark and bail
            log.exception("capture failed @ %d Hz", freq_hz)
            if trigger_id:
                _set_trigger_status(trigger_id, "failed", error=str(e)[:200])
            return None
    # Lock released. Everything below is off the dongle.

    size_bytes = os.path.getsize(path)
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    _write_manifest(path, freq_hz=freq_hz, rate_hz=rate_hz, duration_s=duration_s,
                    gain_db=gain_db, trigger_id=trigger_id, captured_at=captured_at)

    _ch_insert("iq_captures", [{
        "trigger_id": trigger_id,
        "dongle_id": DONGLE_ID,
        "freq_hz": freq_hz,
        "sample_rate_hz": rate_hz,
        "duration_s": duration_s,
        "gain_db": gain_db,
        "format": "cs8",
        "path": path,
        "size_bytes": size_bytes,
        "source": source,
    }])
    if trigger_id:
        _set_trigger_status(trigger_id, "captured", captured_at=captured_at,
                            capture_path=path, capture_bytes=size_bytes)
    log.info("captured %s (%d bytes) @ %d Hz", os.path.basename(path), size_bytes, freq_hz)
    return path


# ─── Poll / claim ────────────────────────────────────────

def claim_oldest_pending():
    """Return the oldest pending trigger row as a dict, or None."""
    rows = _ch_rows(
        "SELECT trigger_id, freq_hz, span_hz, duration_s, gain_db, source "
        "FROM forensic_trigger WHERE status='pending' ORDER BY requested_at LIMIT 1"
    )
    return rows[0] if rows else None


# ─── Daemon ──────────────────────────────────────────────

RUNNING = True


def _handle_signal(signum, frame):
    global RUNNING
    log.info("received signal %s — shutting down", signum)
    RUNNING = False


def _install_signal_handlers():
    # Registered HERE, after the module-level `from scanner import RTLTCPClient`
    # (scanner installs its own handlers at import), so ours win in the daemon.
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


def _lock_dir_ready() -> bool:
    """coordinator.dongle_lock degrades to a no-op (yields True) when its lock
    dir is absent — safe for the scanner as the sole consumer, but iq_capture is
    a SECOND consumer, so capturing unlocked would corrupt the scanner's in-flight
    sweep. Require the real dir before any live-dongle path runs."""
    if coordinator.LOCK_DIR.is_dir():
        return True
    log.error("coordinator lock dir %s absent — refusing to run unlocked as a "
              "second dongle consumer. Run ops/rtl-coordinator/install.sh, or "
              "set RTL_COORDINATOR_LOCK_DIR to a writable dir.", coordinator.LOCK_DIR)
    return False


def run_daemon():
    _install_signal_handlers()
    if not _lock_dir_ready():
        return
    log.info("iq_capture daemon starting (dongle=%s, poll=%.1fs, dir=%s)",
             DONGLE_ID, IQ_POLL_INTERVAL_S, IQ_CAPTURE_DIR)
    processed = set()   # belt-and-braces within one lifetime; real claim is the mutation
    misses = {}         # trigger_id -> consecutive lock-timeout count
    while RUNNING:
        try:
            trig = claim_oldest_pending()
            if trig is not None and trig.get("trigger_id") not in processed:
                tid = trig.get("trigger_id")
                res = perform_capture(trig)
                if res is LOCK_MISS:
                    misses[tid] = misses.get(tid, 0) + 1
                    if misses[tid] >= MAX_LOCK_ATTEMPTS:
                        _set_trigger_status(tid, "failed", error="lock timeout")
                        processed.add(tid)
                        misses.pop(tid, None)
                else:
                    processed.add(tid)
                    misses.pop(tid, None)
        except Exception:  # pragma: no cover - never let the loop die
            log.exception("daemon poll cycle error")
        # Sleep in short slices so SIGTERM is responsive.
        slept = 0.0
        while RUNNING and slept < IQ_POLL_INTERVAL_S:
            time.sleep(min(0.5, IQ_POLL_INTERVAL_S - slept))
            slept += 0.5
    log.info("iq_capture daemon stopped")


# ─── CLI ─────────────────────────────────────────────────

def _cli_capture(args) -> int:
    """--capture: manual one-shot. Inserts an operator:cli audit trigger, then
    captures immediately under the timeout lock."""
    freq = int(args.capture)
    dur = args.duration if args.duration is not None else DEFAULT_DURATION_S
    rate = args.rate if args.rate is not None else DEFAULT_SAMPLE_RATE
    gain = args.gain if args.gain is not None else DEFAULT_GAIN_DB
    source = "operator:cli"

    band = freq_blocked(freq, source)
    if band:
        log.error("refusing manual capture @ %d Hz — blocked band %s "
                  "(set IQ_ALLOW_BLOCKED_BANDS=1 to override)", freq, band)
        return 2

    if not _lock_dir_ready():
        return 1

    tid = str(uuid.uuid4())
    trigger = {
        "trigger_id": tid,
        "freq_hz": freq,
        "span_hz": rate,
        "duration_s": dur,
        "gain_db": gain,
        "source": source,
    }

    # No pre-inserted 'pending' row: a running daemon would double-claim it, and
    # a CLI failure would leave it pending and jam drop-if-queue for everyone.
    # Capture first, then write the audit row with its FINAL status
    # (perform_capture's status UPDATE simply no-ops while no row exists yet).
    res = perform_capture(trigger)

    def _audit(status, **extra):
        _ch_insert("forensic_trigger", [{
            "trigger_id": tid, "dongle_id": DONGLE_ID, "freq_hz": freq,
            "span_hz": rate, "duration_s": dur, "gain_db": gain,
            "source": source, "status": status, **extra,
        }])

    if res is LOCK_MISS:
        _audit("failed", error=f"lock timeout after {IQ_LOCK_TIMEOUT_S:g}s")
        log.error("could not acquire dongle within %.1fs", IQ_LOCK_TIMEOUT_S)
        return 1
    if res is None:
        _audit("failed", error="capture failed (see log)")
        log.error("capture failed (see log)")
        return 1
    _audit("captured", capture_path=res, capture_bytes=os.path.getsize(res),
           captured_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
    print(res)
    return 0


def _cli_trigger(args) -> int:
    """--trigger: insert a trigger row (rate-limit/drop/blocklist) and exit."""
    disp = insert_trigger(
        int(args.trigger),
        duration_s=args.duration,
        span_hz=args.rate,
        gain_db=args.gain,
        source=args.source or "operator:cli",
    )
    print(disp)
    return 0 if disp in ("pending",) else 3


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    p = argparse.ArgumentParser(description="Forensic raw-IQ recorder (D2)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--daemon", action="store_true",
                   help="poll forensic_trigger and capture (default if no mode given)")
    g.add_argument("--capture", metavar="FREQ_HZ",
                   help="one-shot manual capture at FREQ_HZ, then exit")
    g.add_argument("--trigger", metavar="FREQ_HZ",
                   help="insert a forensic_trigger row at FREQ_HZ, then exit")
    p.add_argument("--duration", type=float, help="capture seconds (cap %.0fs)" % IQ_MAX_DURATION_S)
    p.add_argument("--rate", type=int, help="sample rate / span Hz")
    p.add_argument("--gain", type=float, help="tuner gain dB")
    p.add_argument("--source", help="trigger source label (--trigger only)")
    args = p.parse_args(argv)

    if args.capture is not None:
        return _cli_capture(args)
    if args.trigger is not None:
        return _cli_trigger(args)
    run_daemon()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
