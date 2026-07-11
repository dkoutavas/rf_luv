#!/usr/bin/env python3
"""
RDS reader — thin runner around the pure decoder in rds_decoder.py.

Two modes, both emitting one JSON line per decoded RDS group to stdout so
rds_ingest.py can consume them (the ism entrypoint pipe shape):

  live:    connect to rtl_tcp, tune RDS_FREQ_HZ at 228000 S/s, feed 1-s IQ
           chunks to RDSDemodulator.

  offline: --file capture.cs8   (signed 8-bit interleaved I/Q, as produced
           by rtl_sdr / an IQ capture) decodes a file and exits. This is the
           redsea cross-check path and the D2 iq_capture synergy path.

All DSP lives in rds_decoder.py; this file only moves bytes, so the decoder
core stays independently testable and the reader stays thin.
"""

import os
import sys
import json
import time
import socket
import struct
import signal
import logging
from datetime import datetime, timezone

import numpy as np

from rds_decoder import RDSDemodulator, FS_DEFAULT

# ─── Config ──────────────────────────────────────────────

RTL_HOST = os.environ.get("RTL_TCP_HOST", "host.docker.internal")
RTL_PORT = int(os.environ.get("RTL_TCP_PORT", "1234"))
FREQ_HZ = int(os.environ.get("RDS_FREQ_HZ", "99600000"))
GAIN_DB = float(os.environ.get("RDS_GAIN", "29.7"))
DONGLE_ID = os.environ.get("RDS_DONGLE_ID", "v4-01")
SAMPLE_RATE = int(os.environ.get("RDS_SAMPLE_RATE", str(FS_DEFAULT)))
# Pilot gate: median |pilot| below this => no stereo pilot => nothing emitted.
# 0.0 (off) by default so a marginal capture still decodes; raise it if a
# mono neighbour bleeds in. See rds_decoder.RDSDemodulator.
PILOT_THRESH = float(os.environ.get("RDS_PILOT_THRESH", "0.0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("rds-reader")

running = True


def handle_signal(signum, frame):
    global running
    log.info(f"Received signal {signum}, shutting down...")
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# ─── rtl_tcp client ──────────────────────────────────────
# Minimal verbatim copy of spectrum/scanner.py::RTLTCPClient (the source of
# truth). Inlined, not imported, because the Docker build context is rds/ and
# cannot COPY ../spectrum, and a sys.path hack that only works outside the
# container is a trap. Keep in sync with scanner.py if that protocol changes.

class RTLTCPClient:
    CMD_SET_FREQ = 0x01
    CMD_SET_SAMPLE_RATE = 0x02
    CMD_SET_GAIN_MODE = 0x03
    CMD_SET_GAIN = 0x04
    CMD_SET_AGC = 0x08

    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=10)
        header = self._read_exact(12)
        if header[:4] != b"RTL0":
            raise ConnectionError(f"Invalid rtl_tcp header: {header[:4]}")
        log.info(f"Connected to rtl_tcp at {host}:{port}")

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("rtl_tcp connection closed")
            buf.extend(chunk)
        return bytes(buf)

    def send_command(self, cmd_id: int, value: int):
        self.sock.sendall(struct.pack(">BI", cmd_id, value))

    def set_frequency(self, freq_hz: int):
        self.send_command(self.CMD_SET_FREQ, freq_hz)

    def set_sample_rate(self, rate: int):
        self.send_command(self.CMD_SET_SAMPLE_RATE, rate)

    def set_gain(self, gain_db: float):
        self.send_command(self.CMD_SET_GAIN_MODE, 1)
        self.send_command(self.CMD_SET_AGC, 0)
        self.send_command(self.CMD_SET_GAIN, int(gain_db * 10))

    def read_samples(self, num_bytes: int) -> bytes:
        return self._read_exact(num_bytes)

    def discard(self, num_bytes: int):
        self._read_exact(num_bytes)

    def close(self):
        self.sock.close()


# ─── Emit ────────────────────────────────────────────────

def emit(group: dict):
    """Tag a decoded group with freq/dongle/timestamp and print as JSON."""
    group["freq_hz"] = FREQ_HZ
    group["dongle_id"] = DONGLE_ID
    group["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(json.dumps(group), flush=True)


# ─── Live mode ───────────────────────────────────────────

def run_live():
    demod = RDSDemodulator(fs=SAMPLE_RATE, pilot_thresh=PILOT_THRESH)
    client = RTLTCPClient(RTL_HOST, RTL_PORT)
    try:
        client.set_sample_rate(SAMPLE_RATE)
        client.set_frequency(FREQ_HZ)
        client.set_gain(GAIN_DB)
        client.discard(SAMPLE_RATE)  # ~0.5 s of settling bytes (SAMPLE_RATE bytes)
        log.info(f"Decoding RDS at {FREQ_HZ/1e6:.3f} MHz, {SAMPLE_RATE} S/s, gain {GAIN_DB} dB")

        total = 0
        while running:
            raw = client.read_samples(SAMPLE_RATE * 2)  # 1 s of CU8 (I,Q interleaved)
            buf = np.frombuffer(raw, dtype=np.uint8).astype(np.float64)
            iq = ((buf[0::2] - 127.5) + 1j * (buf[1::2] - 127.5)) / 127.5
            for group in demod.process(iq):
                emit(group)
                total += 1
    finally:
        client.close()
        log.info(f"Shutdown complete. Emitted {total} groups")


# ─── Offline mode ────────────────────────────────────────

def run_file(path: str):
    demod = RDSDemodulator(fs=SAMPLE_RATE, pilot_thresh=PILOT_THRESH)
    raw = np.fromfile(path, dtype=np.int8).astype(np.float64)
    iq = (raw[0::2] + 1j * raw[1::2]) / 127.5
    log.info(f"Decoding {len(iq)} IQ samples from {path} at {SAMPLE_RATE} S/s")

    total = 0
    chunk = SAMPLE_RATE  # 1 s of complex samples
    for start in range(0, iq.size, chunk):
        if not running:
            break
        for group in demod.process(iq[start:start + chunk]):
            emit(group)
            total += 1
    log.info(f"Done. Emitted {total} groups from {path}")


# ─── Main ────────────────────────────────────────────────

def main():
    if "--file" in sys.argv:
        idx = sys.argv.index("--file")
        if idx + 1 >= len(sys.argv):
            log.error("--file requires a path")
            sys.exit(2)
        run_file(sys.argv[idx + 1])
        return

    while running:
        try:
            run_live()
            break
        except (ConnectionError, OSError) as e:
            if not running:
                break
            log.error(f"rtl_tcp error: {e}; reconnecting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
