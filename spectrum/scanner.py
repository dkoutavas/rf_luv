#!/usr/bin/env python3
"""
RTL-TCP Spectrum Scanner

Connects to an rtl_tcp server, sweeps across configurable frequency ranges,
computes FFT power spectrum, detects peaks and transient events, and outputs
JSON lines to stdout for ClickHouse ingestion.

Features:
  - Multi-preset sweeps: full band (88-470 MHz) every 5 min,
    airband (118-137 MHz) every 60s for fast ATC capture
  - Automatic peak detection: finds bins above their neighbors
  - Transient event logging: detects signals appearing/disappearing

DSP notes (for audio engineers):
  - IQ samples are like mid/side stereo — two channels encoding amplitude + phase
  - FFT on IQ gives a two-sided spectrum (negative and positive frequencies)
  - Hann window reduces spectral leakage, same as in audio spectrograms
  - Peak detection is like peak-picking in a spectral analyzer
  - Transient detection is like an envelope follower edge detector
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
GAIN_DB = float(os.environ.get("SCAN_GAIN", "20"))
FFT_SIZE = int(os.environ.get("SCAN_FFT_SIZE", "1024"))
SAMPLE_RATE = int(os.environ.get("SCAN_SAMPLE_RATE", "2048000"))
NUM_AVERAGES = int(os.environ.get("SCAN_NUM_AVERAGES", "8"))

# Sweep intervals
FULL_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SECONDS", "280"))
AIRBAND_INTERVAL = int(os.environ.get("SCAN_AIRBAND_INTERVAL", "60"))
AIRBAND_START = int(os.environ.get("SCAN_AIRBAND_START", "118000000"))
AIRBAND_END = int(os.environ.get("SCAN_AIRBAND_END", "137000000"))

# Detection thresholds
PEAK_THRESHOLD_DB = float(os.environ.get("SCAN_PEAK_THRESHOLD", "10"))
PEAK_NEIGHBOR_BINS = int(os.environ.get("SCAN_PEAK_NEIGHBORS", "5"))
TRANSIENT_THRESHOLD_DB = float(os.environ.get("SCAN_TRANSIENT_THRESHOLD", "15"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
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


# ─── DSP ─────────────────────────────────────────────────


def compute_power_spectrum(iq_bytes: bytes, fft_size: int, window: np.ndarray) -> np.ndarray:
    """Convert raw unsigned 8-bit IQ bytes to power spectrum in dBFS."""
    raw = np.frombuffer(iq_bytes, dtype=np.uint8).astype(np.float32)
    iq = (raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)
    iq /= 127.5

    iq[:fft_size] *= window
    spectrum = np.fft.fftshift(np.fft.fft(iq[:fft_size], n=fft_size))
    power = np.abs(spectrum) ** 2 / fft_size

    return 10.0 * np.log10(np.maximum(power, 1e-20))


def downsample_bins(
    power_db: np.ndarray, center_freq: int, sample_rate: int, output_bin_hz: int
) -> list[dict]:
    """Average FFT bins into wider output bins."""
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


def sweep(client: RTLTCPClient, freq_start: int, freq_end: int) -> list[dict]:
    """Perform a frequency sweep over the given range."""
    window = np.hanning(FFT_SIZE)
    capture_bytes = FFT_SIZE * 2

    all_bins = []
    center = freq_start + SAMPLE_RATE // 2

    while center < freq_end + SAMPLE_RATE // 2:
        client.set_frequency(center)
        time.sleep(0.005)
        client.discard(32768)

        power_sum = np.zeros(FFT_SIZE)
        for _ in range(NUM_AVERAGES):
            iq_data = client.read_samples(capture_bytes)
            power_sum += compute_power_spectrum(iq_data, FFT_SIZE, window)
        power_db = power_sum / NUM_AVERAGES

        bins = downsample_bins(power_db, center, SAMPLE_RATE, BIN_WIDTH)
        for b in bins:
            if freq_start <= b["freq_hz"] <= freq_end:
                all_bins.append(b)

        center += SAMPLE_RATE

    return all_bins


# ─── Peak detection ──────────────────────────────────────


def detect_peaks(bins: list[dict]) -> list[dict]:
    """
    Find spectral peaks — bins significantly above their neighbors.
    Like peak-picking in an audio spectrum analyzer.

    For each bin, compare against the average of PEAK_NEIGHBOR_BINS bins
    on each side. If it exceeds the neighborhood by PEAK_THRESHOLD_DB,
    it's a peak.
    """
    if len(bins) < PEAK_NEIGHBOR_BINS * 2 + 1:
        return []

    powers = np.array([b["power_dbfs"] for b in bins])
    peaks = []

    for i in range(PEAK_NEIGHBOR_BINS, len(bins) - PEAK_NEIGHBOR_BINS):
        left = powers[i - PEAK_NEIGHBOR_BINS : i]
        right = powers[i + 1 : i + 1 + PEAK_NEIGHBOR_BINS]
        neighbor_avg = float(np.mean(np.concatenate([left, right])))
        prominence = powers[i] - neighbor_avg

        if prominence >= PEAK_THRESHOLD_DB:
            peaks.append({
                "peak": True,
                "freq_hz": bins[i]["freq_hz"],
                "power_dbfs": bins[i]["power_dbfs"],
                "prominence_db": round(float(prominence), 1),
            })

    return peaks


# ─── Transient event detection ───────────────────────────

# Previous sweep power indexed by freq_hz
_prev_sweep: dict[int, float] = {}


def detect_transients(bins: list[dict]) -> list[dict]:
    """
    Detect signals that appeared or disappeared between sweeps.
    Like an edge detector on the spectrum — fires when something changes.

    Compares current power against the previous sweep at each frequency.
    A delta exceeding TRANSIENT_THRESHOLD_DB triggers an event.
    """
    global _prev_sweep
    events = []

    current = {b["freq_hz"]: b["power_dbfs"] for b in bins}

    if _prev_sweep:
        for freq_hz, power in current.items():
            prev = _prev_sweep.get(freq_hz)
            if prev is None:
                continue
            delta = power - prev
            if delta >= TRANSIENT_THRESHOLD_DB:
                events.append({
                    "event": True,
                    "freq_hz": freq_hz,
                    "event_type": "appeared",
                    "power_dbfs": power,
                    "prev_power": prev,
                    "delta_db": round(delta, 1),
                })
            elif delta <= -TRANSIENT_THRESHOLD_DB:
                events.append({
                    "event": True,
                    "freq_hz": freq_hz,
                    "event_type": "disappeared",
                    "power_dbfs": power,
                    "prev_power": prev,
                    "delta_db": round(abs(delta), 1),
                })

    _prev_sweep = current
    return events


# ─── Main loop with multi-preset scheduling ─────────────


def main():
    global running

    presets = [
        {"name": "full", "start": FREQ_START, "end": FREQ_END, "interval": FULL_INTERVAL},
        {"name": "airband", "start": AIRBAND_START, "end": AIRBAND_END, "interval": AIRBAND_INTERVAL},
    ]

    log.info(f"Spectrum scanner with {len(presets)} presets, gain: {GAIN_DB} dB")
    for p in presets:
        log.info(f"  {p['name']}: {p['start']/1e6:.1f}-{p['end']/1e6:.1f} MHz every {p['interval']}s")

    # Track last run time for each preset
    last_run = {p["name"]: 0.0 for p in presets}

    while running:
        # Pick the most overdue preset
        now = time.monotonic()
        best = None
        best_overdue = -1
        for p in presets:
            overdue = now - last_run[p["name"]] - p["interval"]
            if overdue > best_overdue:
                best_overdue = overdue
                best = p

        # If nothing is due yet, sleep 1s and check again
        if best_overdue < 0:
            time.sleep(1)
            continue

        preset = best

        try:
            client = RTLTCPClient(RTL_HOST, RTL_PORT)
            client.set_sample_rate(SAMPLE_RATE)
            client.set_gain(GAIN_DB)
            client.discard(65536)  # warmup

            sweep_id = f"{preset['name']}:{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}"
            t0 = time.monotonic()

            bins = sweep(client, preset["start"], preset["end"])
            elapsed = time.monotonic() - t0

            client.close()
            last_run[preset["name"]] = time.monotonic()

            # Output scan bins
            for b in bins:
                b["sweep_id"] = sweep_id
                print(json.dumps(b), flush=True)

            # Peak detection
            peaks = detect_peaks(bins)
            for p in peaks:
                p["sweep_id"] = sweep_id
                print(json.dumps(p), flush=True)

            # Transient event detection (only for same-range sweeps)
            if preset["name"] == "full":
                events = detect_transients(bins)
                for e in events:
                    e["sweep_id"] = sweep_id
                    print(json.dumps(e), flush=True)

            # Flush marker
            print(json.dumps({"flush": True}), flush=True)

            log.info(
                f"[{preset['name']}] {len(bins)} bins, {len(peaks)} peaks, "
                f"{len(events) if preset['name'] == 'full' else '-'} events in {elapsed:.1f}s"
            )

        except (ConnectionRefusedError, ConnectionError, socket.error, OSError) as e:
            log.warning(f"Connection error: {e} — retrying in 10s...")
            time.sleep(10)

    log.info("Scanner shutdown complete")


if __name__ == "__main__":
    main()
