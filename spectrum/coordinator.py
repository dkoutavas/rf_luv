#!/usr/bin/env python3
"""
spectrum.coordinator — Python context manager for the rtl-coordinator
flock-based dongle access scheme.

Pairs with the bash side at ops/rtl-coordinator/. See that directory's
README for the architecture; this module just exposes an importable
dongle_lock() context manager that scanner.py and other Python decoders
can use without shelling out to the wrapper script.

Usage:

    from coordinator import dongle_lock

    with dongle_lock("v3-01", mode="nonblock") as got_lock:
        if not got_lock:
            log.info("Skipping sweep — another consumer holds the lock")
            return
        do_the_thing()

The lock is released automatically on context exit. fcntl(2) flock
(LOCK_EX | LOCK_NB / LOCK_EX) is what we use under the hood — same
kernel state as the bash flock(1) helper, so they interoperate.

NOT WIRED INTO scanner.py YET. See ops/rtl-coordinator/README.md
section "Integration with the wideband scanner" for the planned
integration points; do that when the first scheduled decoder (#6 NOAA)
lands and we can test the handoff end-to-end on leap.
"""

from __future__ import annotations

import os
import fcntl
import logging
import contextlib
from pathlib import Path
from typing import Iterator

log = logging.getLogger("coordinator")

LOCK_DIR = Path(os.environ.get("RTL_COORDINATOR_LOCK_DIR", "/var/lib/rtl-coordinator"))


class CoordinatorMissing(RuntimeError):
    """Raised if /var/lib/rtl-coordinator doesn't exist — installer wasn't run."""


@contextlib.contextmanager
def dongle_lock(serial: str, *, mode: str = "wait", timeout: float = 0.0) -> Iterator[bool]:
    """Acquire the per-dongle coordinator lock.

    Args:
        serial: dongle serial, e.g. "v3-01" or "v4-01". Same value as
            SCAN_DONGLE_ID and the systemd instance name.
        mode: "wait" (block until acquired), "nonblock" (return False if
            held), or "timeout" (return False if not acquired in `timeout`
            seconds).
        timeout: seconds, only used when mode="timeout".

    Yields:
        True if the lock was acquired, False if not (only possible in
        "nonblock" or "timeout" modes). The lock is released automatically
        on context exit if it was acquired.

    Example:
        with dongle_lock("v3-01", mode="nonblock") as ok:
            if not ok:
                return
            ...

    Notes:
        - Advisory only; consumers that bypass this break the model.
        - Falls back to a no-op if LOCK_DIR doesn't exist (raises
          CoordinatorMissing). Set RTL_COORDINATOR_LOCK_DIR=/tmp/... in
          tests, or guard callers with `try: ... except CoordinatorMissing`.
    """
    if not LOCK_DIR.is_dir():
        raise CoordinatorMissing(
            f"{LOCK_DIR} not present — run ops/rtl-coordinator/install.sh"
        )

    lock_path = LOCK_DIR / f"{serial}.lock"
    lock_path.touch(exist_ok=True)

    fd = os.open(str(lock_path), os.O_RDWR)
    acquired = False
    try:
        if mode == "wait":
            fcntl.flock(fd, fcntl.LOCK_EX)
            acquired = True
        elif mode == "nonblock":
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
        elif mode == "timeout":
            # Python's fcntl.flock doesn't expose a native timeout. Emulate
            # via SIGALRM in single-thread context, or via short-poll loop
            # for thread safety. Loop is simpler and matches what flock(1) -w
            # does in practice for this short-window use case.
            import time
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        acquired = False
                        break
                    time.sleep(0.1)
        else:
            raise ValueError(f"unknown mode: {mode}")

        log.debug(
            "coordinator: %s lock for %s",
            "acquired" if acquired else "not_acquired",
            serial,
        )
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
