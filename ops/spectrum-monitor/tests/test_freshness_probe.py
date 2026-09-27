#!/usr/bin/env python3
"""Self-test for the freshness probe's session handling. stdlib only.

Run bare:   python3 ops/spectrum-monitor/tests/test_freshness_probe.py
Or:         pytest ops/spectrum-monitor/tests/test_freshness_probe.py

The regression this guards: collection on the Omen is session-based, so the
dongles are unplugged between sessions. An unplugged dongle must not alert,
and a freshly plugged dongle must not alert on data from the last session.
A dongle that stays plugged in with no new data must still alert.
"""

import importlib.util
import os
import tempfile
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "freshness_probe", Path(__file__).resolve().parent.parent / "freshness-probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

SIX_HOURS = 6 * 3600


def _run(plugged_seconds_ago, stale_sec, ticks=((),)):
    """Run main() for dongle v4-01, once per entry in `ticks`, sharing one
    state file. Each tick is the list of foreign rtl_tcp clients seen then
    (empty = only our own scanner, or nobody). plugged_seconds_ago=None means
    the dongle is unplugged. Returns (last level in state, alert levels sent)."""
    alerts = []
    with tempfile.TemporaryDirectory() as tmp:
        dev_dir = Path(tmp, "dev")
        dev_dir.mkdir()
        if plugged_seconds_ago is not None:
            link = dev_dir / "rtl_sdr_v4-01"
            link.symlink_to("bus/usb/001/002")
            t = time.time() - plugged_seconds_ago
            os.utime(link, (t, t), follow_symlinks=False)
        Path(tmp, "v4-01.env").write_text("RTL_TCP_PORT=1234\n")
        cfg = dict(probe.DEFAULTS,
                   DEV_DIR=str(dev_dir),
                   DONGLE_ENV_DIR=tmp,
                   STATE_FILE=str(Path(tmp, "state.json")),
                   ACTION_LOG=str(Path(tmp, "actions.log")))
        probe.load_env = lambda: cfg
        probe.query_freshness = lambda cfg: {"v4-01": stale_sec}
        probe.notify = lambda cfg, level, title, message="": alerts.append(level)
        for clients in ticks:
            probe.foreign_clients = lambda port, clients=clients: list(clients)
            probe.main()
        state = probe.load_state(cfg)
    return state["dongles"]["v4-01"]["level"], alerts


def test_unplugged_dongle_does_not_alert():
    level, alerts = _run(plugged_seconds_ago=None, stale_sec=SIX_HOURS)
    assert level == "ABSENT"
    assert alerts == []


def test_just_plugged_dongle_starts_ok():
    # Last session's data is 6 h old, but the dongle was plugged in 30 s ago
    level, alerts = _run(plugged_seconds_ago=30, stale_sec=SIX_HOURS)
    assert level == "OK"
    assert alerts == []


def test_plugged_dongle_without_data_still_alerts():
    # Plugged in for an hour, no rows for 6 h: the pipeline is broken
    level, alerts = _run(plugged_seconds_ago=3600, stale_sec=SIX_HOURS)
    assert level == "CRITICAL"
    assert alerts == ["CRITICAL"]


def test_listening_session_does_not_alert():
    # SDR++ holds rtl_tcp for hours, so the scanner writes nothing
    level, alerts = _run(plugged_seconds_ago=3600, stale_sec=SIX_HOURS,
                         ticks=[["sdrpp"]])
    assert level == "IN_USE"
    assert alerts == []


def test_scanner_connected_without_data_still_alerts():
    # Only the scanner holds rtl_tcp (not foreign), yet no rows arrive:
    # the ingest path is broken and must alert
    level, alerts = _run(plugged_seconds_ago=3600, stale_sec=SIX_HOURS,
                         ticks=[[]])
    assert level == "CRITICAL"
    assert alerts == ["CRITICAL"]


def test_first_tick_after_listening_session_starts_ok():
    # SDR++ closed; the last scanner row is still from before the session.
    # The scanner gets the normal warn window to reconnect.
    level, alerts = _run(plugged_seconds_ago=3600, stale_sec=SIX_HOURS,
                         ticks=[["sdrpp"], []])
    assert level == "OK"
    assert alerts == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
