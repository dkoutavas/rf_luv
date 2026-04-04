#!/usr/bin/env python3
"""
RTL-TCP Spectrum Scanner

Connects to an rtl_tcp server, sweeps across a frequency range by
retuning and capturing IQ samples at each step, computes FFT power
spectrum, and outputs one JSON line per frequency bin to stdout.

This replaces rtl_power for setups where the dongle is accessed via
rtl_tcp (e.g., RTL-SDR on Windows, processing in WSL/Docker).

The rtl_tcp protocol is simple:
  - 12-byte header on connect: "RTL0" + tuner_type(u32) + gain_count(u32)
  - Client sends 5-byte commands: cmd_id(u8) + value(u32 big-endian)
  - Server streams unsigned 8-bit IQ pairs: I0, Q0, I1, Q1, ...

DSP notes (for audio engineers):
  - IQ samples are like mid/side stereo — two channels encoding amplitude + phase
  - FFT on IQ gives a two-sided spectrum (negative and positive frequencies)
  - fftshift centers DC, just like centering 0 Hz in a frequency analyzer
  - Hann window reduces spectral leakage, same as in audio spectrograms
  - dBFS = dB relative to full scale of the 8-bit ADC
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

# ─── Config ──────────────────────────────────────────────

RTL_HOST = os.environ.get("RTL_TCP_HOST", "host.docker.internal")
RTL_PORT = int(os.environ.get("RTL_TCP_PORT", "1234"))
FREQ_START = int(os.environ.get("SCAN_FREQ_START", "88000000"))
FREQ_END = int(os.environ.get("SCAN_FREQ_END", "470000000"))
BIN_WIDTH = int(os.environ.get("SCAN_BIN_WIDTH", "100000"))
GAIN_DB = float(os.environ.get("SCAN_GAIN", "40"))
FFT_SIZE = int(os.environ.get("SCAN_FFT_SIZE", "1024"))
SAMPLE_RATE = int(os.environ.get("SCAN_SAMPLE_RATE", "2048000"))
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SECONDS", "280"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,  # logs to stderr, data to stdout
)
log = logging.getLogger("scanner")

# ─── Graceful shutdown ───────────────────────────────────

running = True


def handle_signal(signum, frame):
    global running
    log.info(f"Received signal {signum}, shutting down...")
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# ─── rtl_tcp client ──────────────────────────────────────


class RTLTCPClient:
    """Low-level rtl_tcp protocol client."""

    CMD_SET_FREQ = 0x01
    CMD_SET_SAMPLE_RATE = 0x02
    CMD_SET_GAIN_MODE = 0x03
    CMD_SET_GAIN = 0x04
    CMD_SET_AGC = 0x08

    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=10)
        # Read 12-byte header: "RTL0" + tuner_type(u32) + gain_count(u32)
        header = self._read_exact(12)
        magic = header[:4]
        if magic != b"RTL0":
            raise ConnectionError(f"Invalid rtl_tcp header: {magic}")
        log.info(f"Connected to rtl_tcp at {host}:{port}")

    def _read_exact(self, n: int) -> bytes:
        """Read exactly n bytes from the socket."""
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
        self.send_command(self.CMD_SET_GAIN_MODE, 1)  # manual gain
        self.send_command(self.CMD_SET_AGC, 0)  # disable AGC
        self.send_command(self.CMD_SET_GAIN, int(gain_db * 10))

    def read_samples(self, num_bytes: int) -> bytes:
        return self._read_exact(num_bytes)

    def discard(self, num_bytes: int):
        """Read and discard bytes (settling time after retune)."""
        self._read_exact(num_bytes)

    def close(self):
        self.sock.close()


# ─── DSP ─────────────────────────────────────────────────


def compute_power_spectrum(iq_bytes: bytes, fft_size: int, window: np.ndarray) -> np.ndarray:
    """
    Convert raw unsigned 8-bit IQ bytes to power spectrum in dBFS.

    Like computing one frame of a spectrogram:
    raw samples → complex IQ → window → FFT → |X|² → 10·log10
    """
    raw = np.frombuffer(iq_bytes, dtype=np.uint8).astype(np.float32)

    # Interleaved I,Q → complex, remove DC offset (127.5 is center of 0-255)
    iq = (raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)
    iq /= 127.5  # normalize to [-1, 1]

    # Window to reduce spectral leakage
    iq[:fft_size] *= window

    # FFT → shift DC to center → power
    spectrum = np.fft.fftshift(np.fft.fft(iq[:fft_size], n=fft_size))
    power = np.abs(spectrum) ** 2 / fft_size

    # To dBFS
    return 10.0 * np.log10(np.maximum(power, 1e-20))


def downsample_bins(
    power_db: np.ndarray, center_freq: int, sample_rate: int, output_bin_hz: int
) -> list[dict]:
    """
    Average FFT bins into wider output bins.

    FFT gives sample_rate/fft_size Hz per bin (e.g., 2 kHz at 2.048 MS/s with 1024-pt FFT).
    We average groups of bins to produce output_bin_hz-wide bins (e.g., 100 kHz).
    """
    fft_size = len(power_db)
    fft_bin_hz = sample_rate / fft_size
    bins_per_output = max(1, int(output_bin_hz / fft_bin_hz))

    freq_start = center_freq - sample_rate // 2
    results = []

    for i in range(0, fft_size, bins_per_output):
        chunk = power_db[i : i + bins_per_output]
        avg_db = float(np.mean(chunk))
        bin_center = int(freq_start + (i + bins_per_output // 2) * fft_bin_hz)
        results.append({"freq_hz": bin_center, "power_dbfs": round(avg_db, 1)})

    return results


# ─── Sweep ───────────────────────────────────────────────


NUM_AVERAGES = int(os.environ.get("SCAN_NUM_AVERAGES", "8"))


def sweep(client: RTLTCPClient) -> list[dict]:
    """Perform one full frequency sweep, return list of {freq_hz, power_dbfs}."""
    window = np.hanning(FFT_SIZE)
    capture_bytes = FFT_SIZE * 2  # I + Q interleaved

    all_bins = []
    center = FREQ_START + SAMPLE_RATE // 2

    while center < FREQ_END + SAMPLE_RATE // 2:
        client.set_frequency(center)

        # Let PLL settle after retuning. The R820T needs 1-5ms,
        # but we also need to drain stale samples from the TCP buffer.
        # Sleep first (PLL lock), then discard buffered stale data.
        time.sleep(0.005)  # 5ms PLL settle
        client.discard(32768)  # drain ~8ms of stale buffered IQ

        # Average multiple FFT frames for stable power measurement.
        # Like averaging multiple spectrogram frames in audio — reduces
        # variance from noise and transient effects.
        power_sum = np.zeros(FFT_SIZE)
        for _ in range(NUM_AVERAGES):
            iq_data = client.read_samples(capture_bytes)
            power_sum += compute_power_spectrum(iq_data, FFT_SIZE, window)
        power_db = power_sum / NUM_AVERAGES

        bins = downsample_bins(power_db, center, SAMPLE_RATE, BIN_WIDTH)

        for b in bins:
            if FREQ_START <= b["freq_hz"] <= FREQ_END:
                all_bins.append(b)

        center += SAMPLE_RATE

    return all_bins


# ─── Main loop ───────────────────────────────────────────


def main():
    global running

    log.info(
        f"Spectrum scanner: {FREQ_START/1e6:.1f} - {FREQ_END/1e6:.1f} MHz, "
        f"{BIN_WIDTH/1e3:.0f} kHz bins, gain {GAIN_DB} dB, "
        f"FFT {FFT_SIZE} pts, interval {SCAN_INTERVAL}s"
    )

    while running:
        try:
            # Connect fresh for each sweep — avoids stale IQ data
            # accumulating in the TCP socket buffer during sleep.
            # rtl_tcp streams continuously; a 280s sleep would buffer
            # ~1.1 GB of stale samples that pollute the next sweep.
            client = RTLTCPClient(RTL_HOST, RTL_PORT)
            client.set_sample_rate(SAMPLE_RATE)
            client.set_gain(GAIN_DB)
            # Warmup: discard initial samples while PLL settles after connect
            client.discard(65536)

            sweep_id = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            t0 = time.monotonic()

            bins = sweep(client)
            elapsed = time.monotonic() - t0

            client.close()

            # Output JSON lines to stdout (picked up by scan_ingest.py)
            for b in bins:
                b["sweep_id"] = sweep_id
                print(json.dumps(b), flush=True)

            # Emit flush marker so ingest flushes the final batch
            print(json.dumps({"flush": True}), flush=True)

            log.info(f"Sweep complete: {len(bins)} bins in {elapsed:.1f}s")

            # Sleep until next sweep
            for _ in range(SCAN_INTERVAL):
                if not running:
                    break
                time.sleep(1)

        except (ConnectionRefusedError, ConnectionError, socket.error, OSError) as e:
            log.warning(f"Connection error: {e} — retrying in 10s...")
            time.sleep(10)

    log.info("Scanner shutdown complete")


if __name__ == "__main__":
    main()
