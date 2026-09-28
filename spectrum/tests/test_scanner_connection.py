"""Tests for the scanner's rtl_tcp connection lifecycle.

The regression this guards: the scanner used to open a new rtl_tcp connection
for every sweep. rtl_tcp tears down its USB stream on each disconnect, and that
teardown races the command thread (libusb SIGABRT, about twice a day at ~180
connects/hour). The scanner now keeps one connection across sweeps and only
drops it when another consumer holds the dongle lock.
"""

import contextlib
import socket
import threading

import pytest

import scanner


class FakeRtlTcp:
    """Minimal rtl_tcp stand-in: counts accepts, sends the 12-byte greeting,
    then streams mid-scale samples (0x80 = zero signal, never clips) until the
    client disconnects. Commands from the client are ignored."""

    def __init__(self):
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.accepts = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.accepts += 1
            threading.Thread(target=self._stream, args=(conn,), daemon=True).start()

    @staticmethod
    def _stream(conn):
        try:
            conn.sendall(b"RTL0" + bytes(8))
            chunk = b"\x80" * 65536
            while True:
                conn.sendall(chunk)
        except OSError:
            conn.close()


@pytest.fixture
def fake_server(monkeypatch):
    server = FakeRtlTcp()
    monkeypatch.setattr(scanner, "RTL_HOST", "127.0.0.1")
    monkeypatch.setattr(scanner, "RTL_PORT", server.port)
    # Both presets always due, so every loop tick runs a sweep
    monkeypatch.setattr(scanner, "FULL_INTERVAL", 0)
    monkeypatch.setattr(scanner, "AIRBAND_INTERVAL", 0)
    monkeypatch.setattr(scanner, "running", True)
    yield server
    server.server.close()


def _stub_sweep(monkeypatch, clients, stop_after):
    """Replace sweep() so no DSP runs. Records the client of each call and
    stops the main loop after `stop_after` sweeps. One bin is returned because
    main() takes max() over the first full sweep's bins."""
    def fake_sweep(client, freq_start, freq_end):
        clients.append(client)
        if len(clients) >= stop_after:
            scanner.running = False
        bins = [{"freq_hz": 100_000_000, "power_dbfs": -50.0}]
        clipping = {"max_clip_fraction": 0.0, "worst_clip_freq_hz": 0,
                    "clipped_captures": 0, "total_captures": 1, "clipped": False}
        return bins, clipping
    monkeypatch.setattr(scanner, "sweep", fake_sweep)


def test_one_connection_across_sweeps(monkeypatch, fake_server, capsys):
    """Two sweeps share one client object and one server-side accept."""
    monkeypatch.setattr(scanner, "dongle_lock",
                        lambda *a, **kw: contextlib.nullcontext(True))
    clients = []
    _stub_sweep(monkeypatch, clients, stop_after=2)

    scanner.main()

    assert len(clients) == 2
    assert clients[0] is clients[1]
    assert fake_server.accepts == 1


def test_releases_connection_when_lock_is_taken(monkeypatch, fake_server, capsys):
    """When another consumer holds the lock, the scanner closes its socket
    so rtl_tcp can accept that consumer, then reconnects afterwards."""
    # Lock results per tick: sweep, refused (another consumer), sweep
    lock_results = iter([True, False, True])
    monkeypatch.setattr(scanner, "dongle_lock",
                        lambda *a, **kw: contextlib.nullcontext(next(lock_results)))
    monkeypatch.setattr(scanner.time, "sleep", lambda s: None)
    clients = []
    _stub_sweep(monkeypatch, clients, stop_after=2)

    scanner.main()

    assert len(clients) == 2
    assert clients[0] is not clients[1]
    assert fake_server.accepts == 2


def test_idle_scanner_lets_another_consumer_take_the_lock(monkeypatch, fake_server, tmp_path):
    """Between sweeps the scanner must leave the lock free long enough for a
    consumer polling it (iq_capture, the NOAA recorder) to win. It used to hold
    the lock through its 1 s idle drain and release it only for microseconds,
    so iq_capture timed out after 60 s whenever the scanner ran (2026-09-28)."""
    import time as _time
    import coordinator

    monkeypatch.setattr(coordinator, "LOCK_DIR", tmp_path)   # real flock
    monkeypatch.setattr(scanner, "FULL_INTERVAL", 3600)      # sweep once, then idle
    monkeypatch.setattr(scanner, "AIRBAND_INTERVAL", 3600)
    clients = []
    _stub_sweep(monkeypatch, clients, stop_after=99)
    result = {}

    def contender():
        while not clients:                                    # wait until the scanner idles
            _time.sleep(0.05)
        t0 = _time.monotonic()
        with coordinator.dongle_lock(scanner.DONGLE_ID, mode="timeout", timeout=5) as ok:
            result["ok"], result["wait"] = ok, _time.monotonic() - t0
        scanner.running = False

    th = threading.Thread(target=contender)
    th.start()
    scanner.main()
    th.join()

    assert result["ok"], "a waiting consumer never got the lock from an idle scanner"
    assert result["wait"] < 2.5, result["wait"]
