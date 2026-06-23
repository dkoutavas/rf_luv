"""Tests for coordinator.dongle_lock — flock dongle coordination + the
missing-coordinator graceful-degradation path.

The regression this guards: dongle_lock is a @contextmanager, so its body (and
any raise) runs at __enter__ (the caller's `with`), not at the dongle_lock()
call. dongle_lock used to raise CoordinatorMissing when the lock dir was
absent, which escaped callers that wrapped only the dongle_lock() call in
try/except (scanner.py did exactly that) and crashed the scanner. It now yields
a no-op lock instead.
"""

import importlib

import pytest


def _coordinator_with_lock_dir(monkeypatch, path):
    """Reload coordinator with RTL_COORDINATOR_LOCK_DIR=path so module-level
    LOCK_DIR + the once-warned flag are recomputed per test."""
    if path is None:
        monkeypatch.delenv("RTL_COORDINATOR_LOCK_DIR", raising=False)
    else:
        monkeypatch.setenv("RTL_COORDINATOR_LOCK_DIR", str(path))
    import coordinator
    return importlib.reload(coordinator)


def test_missing_lock_dir_yields_noop_not_raises(monkeypatch, tmp_path):
    """The bug: a non-existent lock dir must degrade to acquired=True, NOT raise
    at __enter__. This is what crashed scanner.py before the fix."""
    missing = tmp_path / "does-not-exist"
    coord = _coordinator_with_lock_dir(monkeypatch, missing)
    with coord.dongle_lock("v4-01", mode="nonblock") as got_lock:
        assert got_lock is True  # would raise CoordinatorMissing before the fix


def test_present_lock_dir_acquires_and_releases(monkeypatch, tmp_path):
    """A real lock dir gives a genuine flock that acquires then releases."""
    coord = _coordinator_with_lock_dir(monkeypatch, tmp_path)
    with coord.dongle_lock("v4-01", mode="nonblock") as got_lock:
        assert got_lock is True
    # Released on exit -> a second nonblock acquire succeeds.
    with coord.dongle_lock("v4-01", mode="nonblock") as got_again:
        assert got_again is True


def test_nonblock_second_holder_is_refused(monkeypatch, tmp_path):
    """While one holder has the lock, a second nonblock acquire of the SAME
    serial returns False (the whole point of the coordinator)."""
    coord = _coordinator_with_lock_dir(monkeypatch, tmp_path)
    with coord.dongle_lock("v4-01", mode="nonblock") as first:
        assert first is True
        with coord.dongle_lock("v4-01", mode="nonblock") as second:
            assert second is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
