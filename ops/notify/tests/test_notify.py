#!/usr/bin/env python3
"""Self-test for notify.py's desktop path. stdlib only, no pytest required.

Run bare:   python3 ops/notify/tests/test_notify.py   (prints PASS lines, exit 0)
Or:         pytest ops/notify/tests/test_notify.py

The regression this guards: root-level callers (escalator, probes) have no
desktop session. The popup must run as the desktop user on that user's bus,
and a failed popup must report False instead of "sent".
"""

import importlib.util
import os
import pwd
import subprocess
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "notify", Path(__file__).resolve().parent.parent / "notify.py")
notify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify)


class _Result:
    def __init__(self, returncode):
        self.returncode = returncode


def _capture(euid, returncode):
    """Call _desktop_notify with a faked euid and subprocess result.
    Returns (return value, command that would have run)."""
    seen = []
    real_geteuid, real_run = os.geteuid, subprocess.run
    os.geteuid = lambda: euid
    subprocess.run = lambda cmd, **kw: seen.append(cmd) or _Result(returncode)
    try:
        ok = notify._desktop_notify("CRITICAL", "t", "m", os.getuid())
    finally:
        os.geteuid, subprocess.run = real_geteuid, real_run
    return ok, seen[0]


def test_root_runs_as_desktop_user_on_their_bus():
    ok, cmd = _capture(euid=0, returncode=0)
    user = pwd.getpwuid(os.getuid()).pw_name
    assert ok is True
    assert cmd[:4] == ["runuser", "-u", user, "--"]
    assert f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{os.getuid()}/bus" in cmd
    assert "busctl" in cmd
    # CRITICAL maps to urgency byte 2
    assert cmd[cmd.index("urgency") + 2] == "2"


def test_user_calls_busctl_directly():
    _, cmd = _capture(euid=1000, returncode=0)
    assert cmd[0] == "busctl"


def test_failed_popup_is_not_reported_as_sent():
    ok, _ = _capture(euid=1000, returncode=1)
    assert ok is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
