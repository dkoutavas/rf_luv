#!/usr/bin/env python3
"""Standalone self-test for spectrum/iq_capture.py (feature D2).

Runs with numpy + stdlib ONLY — no pytest, no scipy. Two ways to run:

    python3 spectrum/tests/test_iq_capture.py     # bare, prints OK, exit 0
    pytest spectrum/tests/test_iq_capture.py       # also discoverable

Every test is a plain assert-based function with no fixtures/monkeypatch. The
__main__ block calls each one explicitly. DB access in iq_capture is stubbed by
attribute assignment on the four _ch_* shims, so no ClickHouse is needed. The
headline test (5) exercises REAL flock contention against a tempdir lock dir —
the first time coordinator.dongle_lock(mode="timeout") is driven under a held
lock.
"""

import os
import sys
import time
import glob
import json
import importlib
import tempfile
import threading
from pathlib import Path

import numpy as np

# Make `import iq_capture`, `import coordinator`, `import db` resolve when run
# bare (the pytest conftest only helps under pytest).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import iq_capture  # noqa: E402


# ─── helpers ─────────────────────────────────────────────

class _StubDB:
    """Context-managed stub of iq_capture's four _ch_* shims.

    scalar_router(sql)->value, rows_router(sql)->list, and it records every
    insert as (table, rows) and every exec sql string.
    """

    def __init__(self, scalar=None, rows=None):
        self._scalar = scalar or (lambda sql: 0)
        self._rows = rows or (lambda sql: [])
        self.inserts = []   # list of (table, [rows])
        self.execs = []     # list of sql strings

    def __enter__(self):
        self._orig = (iq_capture._ch_scalar, iq_capture._ch_rows,
                      iq_capture._ch_insert, iq_capture._ch_exec)
        iq_capture._ch_scalar = self._scalar
        iq_capture._ch_rows = self._rows
        iq_capture._ch_insert = lambda table, rows: self.inserts.append((table, list(rows)))
        iq_capture._ch_exec = lambda sql: self.execs.append(sql)
        return self

    def __exit__(self, *exc):
        (iq_capture._ch_scalar, iq_capture._ch_rows,
         iq_capture._ch_insert, iq_capture._ch_exec) = self._orig
        return False

    def inserted_rows(self, table):
        out = []
        for t, rows in self.inserts:
            if t == table:
                out.extend(rows)
        return out


class FakeRTLTCPClient:
    """Deterministic rtl_tcp stand-in: serves a continuous CU8 ramp
    (byte value == stream position mod 256). discard() and read_samples() both
    advance the same cursor, so the test can predict exactly which bytes land in
    the .cs8 (the warmup/discard bytes are excluded)."""

    def __init__(self, host, port):
        self.pos = 0
        self.calls = []

    def _next(self, n):
        vals = (np.arange(self.pos, self.pos + n) & 0xFF).astype(np.uint8)
        self.pos += n
        return vals.tobytes()

    def set_sample_rate(self, rate):
        self.calls.append(("rate", rate))

    def set_gain(self, gain):
        self.calls.append(("gain", gain))

    def set_frequency(self, freq):
        self.calls.append(("freq", freq))

    def discard(self, n):
        self._next(n)

    def read_samples(self, n):
        return self._next(n)

    def close(self):
        self.calls.append(("close", None))


# ─── (1) CU8 -> CS8 exactness ────────────────────────────

def test_cu8_to_cs8_exact():
    out = iq_capture.cu8_to_cs8(bytes([0, 127, 128, 129, 255]))
    assert np.frombuffer(out, np.int8).tolist() == [-128, -1, 0, 1, 127], out

    # length preserved
    assert len(iq_capture.cu8_to_cs8(bytes(range(256)))) == 256

    # I/Q interleave order preserved on a counting pattern
    src = bytes([10, 200, 11, 201])
    got = np.frombuffer(iq_capture.cu8_to_cs8(src), np.int8).tolist()
    assert got == [10 - 128, 200 - 128, 11 - 128, 201 - 128], got

    # full 0..255 domain cross-check against the XOR-0x80 identity
    full = bytes(range(256))
    ref = (np.frombuffer(full, np.uint8) ^ 0x80).view(np.int8).tobytes()
    assert iq_capture.cu8_to_cs8(full) == ref


# ─── (2) rate-limit + drop + blocklist ───────────────────

def _router(recent, pending):
    def scalar(sql):
        if "INTERVAL 1 HOUR" in sql:
            return recent
        if "status='pending'" in sql:
            return pending
        return 0
    return scalar


def test_insert_trigger_rate_limited():
    with _StubDB(scalar=_router(recent=3, pending=0)) as sdb:
        assert iq_capture.insert_trigger(99_600_000) == "rate_limited"
        rows = sdb.inserted_rows("forensic_trigger")
        assert len(rows) == 1 and rows[0]["status"] == "rate_limited"


def test_insert_trigger_dropped_when_pending():
    with _StubDB(scalar=_router(recent=0, pending=1)) as sdb:
        assert iq_capture.insert_trigger(99_600_000) == "dropped"
        assert sdb.inserted_rows("forensic_trigger") == []  # log only, no row


def test_insert_trigger_pending_ok():
    with _StubDB(scalar=_router(recent=0, pending=0)) as sdb:
        assert iq_capture.insert_trigger(99_600_000) == "pending"
        rows = sdb.inserted_rows("forensic_trigger")
        assert len(rows) == 1 and rows[0]["status"] == "pending"
        assert rows[0]["freq_hz"] == 99_600_000


def test_blocklist_auto_source_refused():
    # 390 MHz is inside TETRA (380-400). Auto source is always refused.
    with _StubDB(scalar=_router(0, 0)) as sdb:
        assert iq_capture.insert_trigger(
            390_000_000, source="compression_event:abc") == "blocked"
        rows = sdb.inserted_rows("forensic_trigger")
        assert rows[0]["status"] == "blocked" and "TETRA" in rows[0]["error"]


def test_blocklist_operator_refused_without_override():
    saved = iq_capture.IQ_ALLOW_BLOCKED_BANDS
    iq_capture.IQ_ALLOW_BLOCKED_BANDS = False
    try:
        with _StubDB(scalar=_router(0, 0)):
            assert iq_capture.insert_trigger(
                390_000_000, source="operator:me") == "blocked"
    finally:
        iq_capture.IQ_ALLOW_BLOCKED_BANDS = saved


def test_blocklist_operator_allowed_with_override():
    saved = iq_capture.IQ_ALLOW_BLOCKED_BANDS
    iq_capture.IQ_ALLOW_BLOCKED_BANDS = True
    try:
        with _StubDB(scalar=_router(0, 0)):
            assert iq_capture.insert_trigger(
                390_000_000, source="operator:me") == "pending"
    finally:
        iq_capture.IQ_ALLOW_BLOCKED_BANDS = saved


def test_blocklist_clear_freq_allowed():
    # 99.6 MHz (Kosmos FM) is nowhere near a blocked band.
    assert iq_capture.freq_blocked(99_600_000, "operator:me") is None
    assert iq_capture.freq_blocked(99_600_000, "compression_event:x") is None
    # sanity: each blocked band middle is caught for the auto path
    for lo, hi, label in iq_capture.BLOCKED_BANDS:
        mid = (lo + hi) / 2
        assert iq_capture.freq_blocked(mid, "compression_event:x") == label


# ─── (3) oldest-first 2 GB rotation ──────────────────────

def test_rotate_capture_dir_oldest_first():
    with tempfile.TemporaryDirectory() as d:
        archive = os.path.join(d, "archive")
        os.makedirs(archive)
        # 5 files of 100 bytes each, staggered mtimes (f0 oldest .. f4 newest).
        for i in range(5):
            p = os.path.join(d, f"2026-07-11T10-00-0{i}_99600_5s.cs8")
            with open(p, "wb") as fh:
                fh.write(b"\x00" * 100)
            with open(p[:-4] + ".json", "w") as fh:
                json.dump({"i": i}, fh)
            os.utime(p, (1000 + i, 1000 + i))
        # A file inside archive/ must be immune (non-recursive glob).
        arch_file = os.path.join(archive, "keep_99600_5s.cs8")
        with open(arch_file, "wb") as fh:
            fh.write(b"\x00" * 100)

        saved = iq_capture.IQ_DIR_MAX_BYTES
        iq_capture.IQ_DIR_MAX_BYTES = 250  # keep only newest 2 (2*100 <= 250)
        try:
            iq_capture.rotate_capture_dir(d)
        finally:
            iq_capture.IQ_DIR_MAX_BYTES = saved

        remaining = sorted(os.path.basename(f) for f in glob.glob(os.path.join(d, "*.cs8")))
        assert remaining == [
            "2026-07-11T10-00-03_99600_5s.cs8",
            "2026-07-11T10-00-04_99600_5s.cs8",
        ], remaining
        total = sum(os.path.getsize(f) for f in glob.glob(os.path.join(d, "*.cs8")))
        assert total <= 250
        # sidecars of deleted files gone; sidecars of survivors kept
        assert not os.path.exists(os.path.join(d, "2026-07-11T10-00-00_99600_5s.json"))
        assert os.path.exists(os.path.join(d, "2026-07-11T10-00-04_99600_5s.json"))
        # archive/ untouched
        assert os.path.exists(arch_file)


# ─── (4) capture-to-.cs8 with a MOCKED rtl_tcp client ────

def test_perform_capture_writes_cs8_and_row():
    rate = 2048
    dur = 0.5
    total_bytes = int(dur * rate) * 2  # 2048 bytes
    with tempfile.TemporaryDirectory() as d, _StubDB(scalar=_router(0, 0)) as sdb:
        trigger = {
            "trigger_id": "tid-abc",
            "freq_hz": 99_600_000,
            "span_hz": rate,
            "duration_s": dur,
            "gain_db": 20.0,
            "source": "operator:cli",
        }
        path = iq_capture.perform_capture(
            trigger, client_factory=FakeRTLTCPClient, capture_dir=d)
        assert path is not None and path.endswith(".cs8")
        assert os.path.getsize(path) == total_bytes

        # Expected bytes: the ramp AFTER the discarded warmup window.
        start = iq_capture.WARMUP_BYTES
        expected_cu8 = (np.arange(start, start + total_bytes) & 0xFF).astype(np.uint8).tobytes()
        expected_cs8 = iq_capture.cu8_to_cs8(expected_cu8)
        with open(path, "rb") as fh:
            assert fh.read() == expected_cs8

        # manifest sidecar
        with open(path[:-4] + ".json") as fh:
            man = json.load(fh)
        assert man["freq_hz"] == 99_600_000
        assert man["sample_rate_hz"] == rate
        assert man["format"] == "cs8"
        assert man["trigger_id"] == "tid-abc"

        # iq_captures row saw freq/rate/size
        rows = sdb.inserted_rows("iq_captures")
        assert len(rows) == 1
        assert rows[0]["freq_hz"] == 99_600_000
        assert rows[0]["sample_rate_hz"] == rate
        assert rows[0]["size_bytes"] == total_bytes
        # trigger flipped to captured via a mutation
        assert any("status='captured'" in s for s in sdb.execs)
        # no .part left behind
        assert glob.glob(os.path.join(d, "*.part")) == []


def test_perform_capture_refuses_blocked_band():
    with tempfile.TemporaryDirectory() as d, _StubDB() as sdb:
        saved = iq_capture.IQ_ALLOW_BLOCKED_BANDS
        iq_capture.IQ_ALLOW_BLOCKED_BANDS = False
        try:
            trigger = {
                "trigger_id": "tid-blocked",
                "freq_hz": 390_000_000,       # TETRA
                "span_hz": 2048,
                "duration_s": 0.1,
                "gain_db": 20.0,
                "source": "compression_event:x",
            }
            res = iq_capture.perform_capture(
                trigger, client_factory=FakeRTLTCPClient, capture_dir=d)
            assert res is None
            assert glob.glob(os.path.join(d, "*.cs8")) == []
            assert any("status='blocked'" in s for s in sdb.execs)
        finally:
            iq_capture.IQ_ALLOW_BLOCKED_BANDS = saved


# ─── (5) HEADLINE: real coordinator contention (real flock) ──

def _reload_coordinator(lock_dir):
    os.environ["RTL_COORDINATOR_LOCK_DIR"] = str(lock_dir)
    import coordinator
    return importlib.reload(coordinator)


def test_coordinator_contention_real_flock():
    saved_env = os.environ.get("RTL_COORDINATOR_LOCK_DIR")
    tmp = tempfile.TemporaryDirectory()
    try:
        coord = _reload_coordinator(tmp.name)

        # (5a) timeout acquire FAILS while a holder holds the lock, but the poll
        # loop actually gave up (did not block forever, did not fail instantly).
        held = threading.Event()
        release = threading.Event()

        def holder():
            with coord.dongle_lock("v4-01", mode="nonblock") as got:
                assert got is True
                held.set()
                release.wait(5)

        t = threading.Thread(target=holder)
        t.start()
        assert held.wait(2)

        # scanner-style nonblock acquire is refused while the recorder-style
        # holder has it (the live hand-off, refusal direction).
        with coord.dongle_lock("v4-01", mode="nonblock") as sc:
            assert sc is False

        t0 = time.monotonic()
        with coord.dongle_lock("v4-01", mode="timeout", timeout=0.5) as got:
            elapsed = time.monotonic() - t0
            assert got is False, "timeout acquire must fail while held"
        assert 0.4 <= elapsed <= 2.0, f"poll loop timing off: {elapsed:.3f}s"

        release.set()
        t.join(2)

        # (5b) release mid-wait: a timeout acquire started while held must grab
        # the lock the instant the holder releases (the hand-off, grab direction).
        held2 = threading.Event()
        release2 = threading.Event()

        def holder2():
            with coord.dongle_lock("v4-01", mode="nonblock") as got:
                assert got is True
                held2.set()
                release2.wait(5)

        t2 = threading.Thread(target=holder2)
        t2.start()
        assert held2.wait(2)
        # release 0.4s into the main thread's 3.0s timeout acquire
        threading.Timer(0.4, release2.set).start()
        t1 = time.monotonic()
        with coord.dongle_lock("v4-01", mode="timeout", timeout=3.0) as got:
            elapsed2 = time.monotonic() - t1
            assert got is True, "must acquire once holder releases"
        assert elapsed2 < 1.5, f"did not grab promptly on release: {elapsed2:.3f}s"
        t2.join(2)

        # (5c) uncontended: timeout acquire returns True fast.
        t3 = time.monotonic()
        with coord.dongle_lock("v4-01", mode="timeout", timeout=1.0) as got:
            assert got is True
        assert time.monotonic() - t3 < 0.2
    finally:
        tmp.cleanup()
        if saved_env is None:
            os.environ.pop("RTL_COORDINATOR_LOCK_DIR", None)
        else:
            os.environ["RTL_COORDINATOR_LOCK_DIR"] = saved_env
        import coordinator
        importlib.reload(coordinator)  # restore module state for other tests


# ─── runner ──────────────────────────────────────────────

def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
        passed += 1
    print(f"\nOK — {passed}/{len(tests)} tests passed")


if __name__ == "__main__":
    _run_all()
    sys.exit(0)
