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
