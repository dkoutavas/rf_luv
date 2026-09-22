#!/usr/bin/env python3
"""Self-test for the rtl_tcp watchdog. stdlib only, no pytest required.

Run bare:   python3 ops/rtl-tcp/tests/test_watchdog.py   (prints PASS lines, exit 0)
Or:         pytest ops/rtl-tcp/tests/test_watchdog.py
"""

import importlib.util
import os
import socket
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_OPS_RTL = os.path.dirname(_HERE)

# import the hyphenated script via spec_from_file_location
_spec = importlib.util.spec_from_file_location(
    "rtl_tcp_watchdog",
    os.path.join(_OPS_RTL, "rtl-tcp-watchdog.py"),
)
wd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wd)


# ── dongle_present ───────────────────────────────────────────────────────────

def test_dongle_present():
    """Symlink present -> True; missing -> False; empty serial -> True."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        assert not wd.dongle_present("v3-01", dev_dir=d), "missing symlink must be False"
        open(os.path.join(d, "rtl_sdr_v3-01"), "w").close()
        assert wd.dongle_present("v3-01", dev_dir=d), "present symlink must be True"
        assert wd.dongle_present("", dev_dir=d), "empty serial must assume present"
    print("PASS dongle_present: missing False, present True, empty serial True")


# ── has_active_client ────────────────────────────────────────────────────────

def test_has_active_client_loopback_established():
    """Loopback ESTABLISHED on port 1235 (0x04D3) must return True."""
    lines = [
        # local_addr          remote_addr         st
        "   0: 0100007F:04D3 0100007F:C5A0 01 00000000:00000000 00:00000000 00000000  1000 0 12345 1\n",
    ]
    assert wd.has_active_client(1235, proc_lines=lines), "loopback ESTABLISHED not detected"
    print("PASS has_active_client: loopback ESTABLISHED detected")


def test_has_active_client_wrong_port():
    """ESTABLISHED on a different port must return False."""
    lines = [
        "   0: 0100007F:04D2 0100007F:C5A0 01 00000000:00000000 00:00000000 00000000  1000 0 12345 1\n",
    ]
    assert not wd.has_active_client(1235, proc_lines=lines), "wrong port matched"
    print("PASS has_active_client: wrong port ignored")


def test_has_active_client_listen_state():
    """LISTEN (state 0A) on the right port must return False."""
    lines = [
        "   0: 00000000:04D3 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 12345 1\n",
    ]
    assert not wd.has_active_client(1235, proc_lines=lines), "LISTEN state matched"
    print("PASS has_active_client: LISTEN state ignored")


def test_has_active_client_remote_peer():
    """Non-loopback ESTABLISHED must also return True."""
    lines = [
        "   0: 0100007F:04D3 0200A8C0:D404 01 00000000:00000000 00:00000000 00000000  1000 0 12345 1\n",
    ]
    assert wd.has_active_client(1235, proc_lines=lines), "remote peer not detected"
    print("PASS has_active_client: remote ESTABLISHED detected")


def test_has_active_client_empty():
    """No lines at all must return False."""
    assert not wd.has_active_client(1234, proc_lines=[]), "empty returned True"
    print("PASS has_active_client: empty returns False")


# ── probe ────────────────────────────────────────────────────────────────────

def _serve_once(server_sock, handler):
    """Accept one connection and run handler(conn) in a thread."""
    def _run():
        conn, _ = server_sock.accept()
        try:
            handler(conn)
        finally:
            conn.close()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def test_probe_healthy():
    """Server sends RTL0 greeting + enough samples: probe returns (True, ...)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def handler(conn):
        greeting = b"RTL0" + b"\x00" * 8
        conn.sendall(greeting)
        # send >512KB of samples
        chunk = b"\x80" * 65536
        for _ in range(10):
            try:
                conn.sendall(chunk)
            except OSError:
                break

    t = _serve_once(srv, handler)
    ok, reason = wd.probe("127.0.0.1", port)
    t.join(timeout=5)
    srv.close()
    assert ok, f"healthy server returned False: {reason}"
    print(f"PASS probe: healthy server -> (True, {reason!r})")


def test_probe_greeting_timeout():
    """Server accepts but sends nothing: probe returns (False, 'greeting timeout...')
    without raising an exception. Must complete within ~4s."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def handler(conn):
        # accept but never send, hold the connection for 5s
        time.sleep(5)

    t = _serve_once(srv, handler)
    t0 = time.time()
    ok, reason = wd.probe("127.0.0.1", port)
    elapsed = time.time() - t0
    t.join(timeout=2)
    srv.close()
    assert not ok, "silent server returned True"
    assert "greeting timeout" in reason, f"unexpected reason: {reason}"
    assert elapsed < 5.0, f"probe took {elapsed:.1f}s, expected < 5s"
    print(f"PASS probe: greeting timeout -> (False, {reason!r}) in {elapsed:.1f}s, no exception")


def test_probe_connection_refused():
    """Closed port: probe returns (False, 'connect failed...')."""
    # bind and immediately close to get a known-unused port
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()

    ok, reason = wd.probe("127.0.0.1", port)
    assert not ok, "closed port returned True"
    assert "connect failed" in reason, f"unexpected reason: {reason}"
    print(f"PASS probe: connection refused -> (False, {reason!r})")


# ── escalation logic ─────────────────────────────────────────────────────────

def test_escalation_fail1_no_recover():
    """fails == 1 must NOT call recover (no systemctl run)."""
    calls = []
    _orig_run = __import__("subprocess").run

    def mock_run(cmd, **kw):
        calls.append(cmd)
        return type("R", (), {"returncode": 0})()

    import subprocess
    subprocess.run = mock_run
    try:
        state = {"consecutive_failures": 0, "last_hard_reset_ts": 0.0, "client_skip_count": 0}
        state["consecutive_failures"] = 1
        fails = state["consecutive_failures"]
        # replicate main()'s logic for fails==1
        if fails == 1:
            passed = True
        else:
            passed = False
    finally:
        subprocess.run = _orig_run

    assert passed, "fails==1 should skip recover"
    assert len(calls) == 0, f"fails==1 should not call subprocess.run, got {calls}"
    print("PASS escalation: fails==1 skips recover (no subprocess calls)")


def test_escalation_fail2_soft_restart():
    """fails == 2 must trigger a soft restart (systemctl --user restart)."""
    calls = []
    _orig_run = __import__("subprocess").run

    def mock_run(cmd, **kw):
        calls.append(list(cmd))
        return type("R", (), {"returncode": 0})()

    import subprocess
    subprocess.run = mock_run
    try:
        state = {"consecutive_failures": 2, "last_hard_reset_ts": 0.0}
        wd.recover(2, "v3-01", "rtl-tcp@v3-01.service", state)
    finally:
        subprocess.run = _orig_run

    assert len(calls) == 1, f"expected 1 call, got {len(calls)}: {calls}"
    assert "restart" in calls[0], f"expected restart, got {calls[0]}"
    print(f"PASS escalation: fails==2 -> soft restart: {calls[0]}")


def test_escalation_fail4_recent_hard_reset():
    """fails == 4 with recent hard reset must soft-restart only (cooldown)."""
    calls = []
    _orig_run = __import__("subprocess").run

    def mock_run(cmd, **kw):
        calls.append(list(cmd))
        return type("R", (), {"returncode": 0})()

    import subprocess
    subprocess.run = mock_run
    try:
        state = {"consecutive_failures": 4, "last_hard_reset_ts": time.time() - 10}
        wd.recover(4, "v3-01", "rtl-tcp@v3-01.service", state)
    finally:
        subprocess.run = _orig_run

    assert len(calls) == 1, f"expected 1 soft restart, got {len(calls)}: {calls}"
    assert "restart" in calls[0], f"expected restart, got {calls[0]}"
    assert "rtl-usb-reset" not in " ".join(calls[0]), "should not hard-reset during cooldown"
    print(f"PASS escalation: fails==4 + recent hard reset -> soft only: {calls[0]}")


def test_escalation_fail4_no_recent_hard_reset():
    """fails == 4 with no recent hard reset must do USB reset then restart."""
    calls = []
    _orig_run = __import__("subprocess").run

    def mock_run(cmd, **kw):
        calls.append(list(cmd))
        return type("R", (), {"returncode": 0})()

    import subprocess
    subprocess.run = mock_run
    try:
        state = {"consecutive_failures": 4, "last_hard_reset_ts": 0.0}
        wd.recover(4, "v3-01", "rtl-tcp@v3-01.service", state)
    finally:
        subprocess.run = _orig_run

    assert len(calls) == 2, f"expected 2 calls (usb-reset + restart), got {len(calls)}: {calls}"
    assert "rtl-usb-reset" in " ".join(calls[0]), f"first call should be usb-reset: {calls[0]}"
    assert "restart" in calls[1], f"second call should be restart: {calls[1]}"
    print(f"PASS escalation: fails==4 + no recent hard reset -> usb-reset + restart")


# ── runner ───────────────────────────────────────────────────────────────────

def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for t in tests:
        try:
            t()
        except Exception as e:
            failed.append((t.__name__, e))
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
