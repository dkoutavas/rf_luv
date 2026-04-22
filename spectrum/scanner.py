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

# Default works in Docker on both Linux (20.10+ with extra_hosts) and WSL/Docker Desktop
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

# Adaptive gain: auto-reduce when ADC clips, floor at GAIN_MIN
GAIN_MIN = float(os.environ.get("SCAN_GAIN_MIN", "0"))
GAIN_STEP_DB = float(os.environ.get("SCAN_GAIN_STEP", "2"))

# DVB-T exclusion range for sweep health (strong local transmitters).
# Default covers VHF Band III (174-230 MHz) used for DVB-T in Athens.
DVBT_EXCLUDE_START = int(os.environ.get("SCAN_DVBT_EXCLUDE_START", "174000000"))
DVBT_EXCLUDE_END = int(os.environ.get("SCAN_DVBT_EXCLUDE_END", "230000000"))

# Antenna / run tracking (logged with each run for A/B comparisons)
ANTENNA_POSITION = os.environ.get("SCAN_ANTENNA_POSITION", "unknown")
ANTENNA_ARMS_CM = float(os.environ.get("SCAN_ANTENNA_ARMS_CM", "0"))
ANTENNA_ORIENTATION = int(os.environ.get("SCAN_ANTENNA_ORIENTATION", "0"))
ANTENNA_HEIGHT_M = float(os.environ.get("SCAN_ANTENNA_HEIGHT_M", "0"))
SCAN_NOTES = os.environ.get("SCAN_NOTES", "")

# Dongle identity — tags every output line so downstream queries can
# slice by source dongle. See spectrum/docs/dongle_identity.md.
# Default matches the V3 serial so single-dongle deployments continue
# to work without extra config; log a warning when we hit the default
# so misconfigured instances are visible.
DONGLE_ID = os.environ.get("SCAN_DONGLE_ID", "v3-01")

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


def compute_linear_power(iq_bytes: bytes, fft_size: int, window: np.ndarray) -> np.ndarray:
    """Convert raw unsigned 8-bit IQ bytes to LINEAR power spectrum.

    Returns linear power (not dB) so multiple captures can be averaged
    correctly in the linear domain before converting to dB.
    Averaging in dB domain (log scale) underestimates bursty signals
    due to Jensen's inequality — same issue as RMS vs average in audio.
    """
    raw = np.frombuffer(iq_bytes, dtype=np.uint8).astype(np.float32)
    iq = (raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)
    iq /= 127.5

    iq[:fft_size] *= window
    spectrum = np.fft.fftshift(np.fft.fft(iq[:fft_size], n=fft_size))
    return np.abs(spectrum) ** 2 / fft_size


def linear_to_db(power_linear: np.ndarray) -> np.ndarray:
    """Convert linear power array to dBFS."""
    return 10.0 * np.log10(np.maximum(power_linear, 1e-20))


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
        avg_db = float(10.0 * np.log10(np.maximum(np.mean(10.0 ** (chunk / 10.0)), 1e-20)))
        bin_center = int(freq_start + (i + bins_per_output // 2) * fft_bin_hz)
        results.append({"freq_hz": bin_center, "power_dbfs": round(avg_db, 1)})

    return results


def detect_clipping(iq_bytes: bytes, threshold: float = 0.05) -> dict:
    """Check raw IQ bytes for ADC saturation (clipping at 0 or 255).

    The RTL-SDR's 8-bit ADC clips when gain is too high or strong signals
    overload the frontend. Clipped data corrupts FFT results — it creates
    phantom harmonics across the spectrum, like hard clipping in audio.

    Args:
        iq_bytes: Raw interleaved IQ bytes from RTL-SDR.
        threshold: Fraction of clipped samples above which data is considered
                   unreliable (default 5%).

    Returns:
        Dict with clipping stats: clipped (bool), clip_fraction, n_clipped, n_samples.
    """
    if not iq_bytes:
        return {"clipped": False, "clip_fraction": 0.0, "n_clipped": 0, "n_samples": 0}
    raw = np.frombuffer(iq_bytes, dtype=np.uint8)
    n_clipped = int(np.sum((raw == 0) | (raw == 255)))
    clip_fraction = n_clipped / len(raw)
    return {
        "clipped": clip_fraction > threshold,
        "clip_fraction": round(clip_fraction, 4),
        "n_clipped": n_clipped,
        "n_samples": len(raw),
    }


# ─── Adaptive gain ──────────────────────────────────────


def adapt_gain(effective_gain: float, clipped: bool, gain_min: float, gain_step: float) -> float:
    """Compute next gain after a sweep. Reduces by gain_step if clipping detected.

    Pure function — no side effects, no logging. The caller handles logging
    and tracking the returned value.

    Args:
        effective_gain: Current gain in dB.
        clipped: Whether the last sweep detected ADC clipping.
        gain_min: Floor gain in dB (won't reduce below this).
        gain_step: How many dB to reduce per clipping event.

    Returns:
        New gain value in dB.
    """
    if clipped:
        return max(gain_min, effective_gain - gain_step)
    return effective_gain


# ─── Sweep ───────────────────────────────────────────────


def sweep(client: RTLTCPClient, freq_start: int, freq_end: int) -> tuple[list[dict], dict]:
    """Perform a frequency sweep over the given range.

    Returns:
        (bins, clipping) where bins is the list of frequency/power dicts
        and clipping summarizes ADC saturation across the sweep.
    """
    window = np.hanning(FFT_SIZE)
    capture_bytes = FFT_SIZE * 2

    all_bins = []
    worst_clip = 0.0
    worst_clip_freq = 0
    total_captures = 0
    clipped_captures = 0
    center = freq_start + SAMPLE_RATE // 2

    while center < freq_end + SAMPLE_RATE // 2:
        client.set_frequency(center)
        time.sleep(0.005)
        client.discard(32768)

        power_sum = np.zeros(FFT_SIZE)
        for _ in range(NUM_AVERAGES):
            iq_data = client.read_samples(capture_bytes)
            clip = detect_clipping(iq_data)
            total_captures += 1
            if clip["clipped"]:
                clipped_captures += 1
            if clip["clip_fraction"] > worst_clip:
                worst_clip = clip["clip_fraction"]
                worst_clip_freq = center
            power_sum += compute_linear_power(iq_data, FFT_SIZE, window)
        power_db = linear_to_db(power_sum / NUM_AVERAGES)

        bins = downsample_bins(power_db, center, SAMPLE_RATE, BIN_WIDTH)
        for b in bins:
            if freq_start <= b["freq_hz"] <= freq_end:
                all_bins.append(b)

        center += SAMPLE_RATE

    clipping = {
        "max_clip_fraction": round(worst_clip, 4),
        "worst_clip_freq_hz": worst_clip_freq,
        "clipped_captures": clipped_captures,
        "total_captures": total_captures,
        "clipped": worst_clip > 0.05,
    }
    return all_bins, clipping


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

    effective_gain = GAIN_DB

    if "SCAN_DONGLE_ID" not in os.environ:
        log.warning(f"SCAN_DONGLE_ID not set, defaulting to '{DONGLE_ID}' — set it explicitly in per-instance env file")
    log.info(f"Spectrum scanner dongle={DONGLE_ID} with {len(presets)} presets, gain: {GAIN_DB} dB (min: {GAIN_MIN} dB, step: {GAIN_STEP_DB} dB)")
    for p in presets:
        log.info(f"  {p['name']}: {p['start']/1e6:.1f}-{p['end']/1e6:.1f} MHz every {p['interval']}s")

    # Track last run time for each preset
    last_run = {p["name"]: 0.0 for p in presets}

    # Generate run ID and emit run_start for configuration tracking.
    # dongle_id is embedded in run_id so two scanners starting in the same
    # second cannot collide on scan_runs' PK.
    run_id = f"run_{DONGLE_ID}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    log.info(f"Run {run_id}: gain={effective_gain}, antenna={ANTENNA_POSITION}, arms={ANTENNA_ARMS_CM}cm")
    print(json.dumps({
        "run_start": True,
        "run_id": run_id,
        "dongle_id": DONGLE_ID,
        "started_at": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
        "gain_db": effective_gain,
        "antenna_position": ANTENNA_POSITION,
        "antenna_arms_cm": ANTENNA_ARMS_CM,
        "antenna_orientation_deg": ANTENNA_ORIENTATION,
        "antenna_height_m": ANTENNA_HEIGHT_M,
        "notes": SCAN_NOTES,
    }), flush=True)
    first_full_done = False

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
            client.set_gain(effective_gain)
            # Warmup: tune to sweep start frequency and discard enough
            # for PLL to settle after large frequency jump (e.g. 470→118 MHz).
            # 131072 bytes = 64K IQ samples = ~32ms at 2.048 MS/s.
            client.set_frequency(preset["start"] + SAMPLE_RATE // 2)
            time.sleep(0.010)
            client.discard(131072)

            sweep_ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            sweep_id = f"{preset['name']}:{sweep_ts}"
            t0 = time.monotonic()

            bins, clipping = sweep(client, preset["start"], preset["end"])
            elapsed = time.monotonic() - t0

            client.close()
            last_run[preset["name"]] = time.monotonic()

            # Output scan bins
            for b in bins:
                b["sweep_id"] = sweep_id
                b["timestamp"] = sweep_ts
                b["run_id"] = run_id
                b["dongle_id"] = DONGLE_ID
                print(json.dumps(b), flush=True)

            # Peak detection
            peaks = detect_peaks(bins)
            for p in peaks:
                p["sweep_id"] = sweep_id
                p["timestamp"] = sweep_ts
                p["run_id"] = run_id
                p["dongle_id"] = DONGLE_ID
                print(json.dumps(p), flush=True)

            # Transient event detection (only for same-range sweeps)
            if preset["name"] == "full":
                events = detect_transients(bins)
                for e in events:
                    e["sweep_id"] = sweep_id
                    e["timestamp"] = sweep_ts
                    e["run_id"] = run_id
                    e["dongle_id"] = DONGLE_ID
                    print(json.dumps(e), flush=True)

            # Sweep health (for clipping detection)
            # Exclude DVB-T range — clipping there is a known accepted
            # condition due to strong local transmitters (configurable via env).
            non_dvbt = [b["power_dbfs"] for b in bins
                        if not (DVBT_EXCLUDE_START <= b["freq_hz"] <= DVBT_EXCLUDE_END)]
            max_power = max(non_dvbt, default=-100.0)
            max_power_dvbt = max(
                (b["power_dbfs"] for b in bins if DVBT_EXCLUDE_START <= b["freq_hz"] <= DVBT_EXCLUDE_END),
                default=-100.0)
            print(json.dumps({
                "health": True,
                "sweep_id": sweep_id,
                "timestamp": sweep_ts,
                "run_id": run_id,
                "dongle_id": DONGLE_ID,
                "preset": preset["name"],
                "bin_count": len(bins),
                "max_power": round(max_power, 1),
                "max_power_dvbt": round(max_power_dvbt, 1),
                "sweep_duration_ms": int(elapsed * 1000),
                "gain_db": effective_gain,
                "clipped": clipping["clipped"],
                "max_clip_fraction": clipping["max_clip_fraction"],
                "worst_clip_freq_hz": clipping["worst_clip_freq_hz"],
                "clipped_captures": clipping["clipped_captures"],
                "total_captures": clipping["total_captures"],
            }), flush=True)

            new_gain = adapt_gain(effective_gain, clipping["clipped"], GAIN_MIN, GAIN_STEP_DB)
            if new_gain < effective_gain:
                log.warning(
                    f"[{preset['name']}] ADC CLIPPING: "
                    f"{clipping['max_clip_fraction']*100:.1f}% near "
                    f"{clipping['worst_clip_freq_hz']/1e6:.1f} MHz — "
                    f"reducing gain {effective_gain} → {new_gain} dB"
                )
                effective_gain = new_gain
            elif clipping["clipped"]:
                log.warning(
                    f"[{preset['name']}] ADC CLIPPING: "
                    f"{clipping['max_clip_fraction']*100:.1f}% near "
                    f"{clipping['worst_clip_freq_hz']/1e6:.1f} MHz — "
                    f"already at minimum gain ({GAIN_MIN} dB)"
                )

            # After first full sweep, report measured noise floor and peak
            if preset["name"] == "full" and not first_full_done:
                first_full_done = True
                uhf_powers = [b["power_dbfs"] for b in bins if 400000000 <= b["freq_hz"] <= 470000000]
                nf = float(np.percentile(uhf_powers, 10)) if uhf_powers else -100.0
                peak_bin = max(bins, key=lambda b: b["power_dbfs"])
                print(json.dumps({
                    "run_update": True,
                    "run_id": run_id,
                    "dongle_id": DONGLE_ID,
                    "noise_floor_dbfs": round(nf, 1),
                    "peak_signal_dbfs": round(peak_bin["power_dbfs"], 1),
                    "peak_signal_freq_hz": peak_bin["freq_hz"],
                }), flush=True)
                log.info(f"Run {run_id}: noise_floor={nf:.1f}, peak={peak_bin['power_dbfs']:.1f} @ {peak_bin['freq_hz']/1e6:.2f} MHz")

            # Flush marker
            print(json.dumps({"flush": True}), flush=True)

            clip_pct = f"{clipping['max_clip_fraction']*100:.1f}%"
            log.info(
                f"[{preset['name']}] {len(bins)} bins, {len(peaks)} peaks, "
                f"{len(events) if preset['name'] == 'full' else '-'} events, "
                f"clip={clip_pct} in {elapsed:.1f}s"
            )

        except (ConnectionRefusedError, ConnectionError, socket.error, OSError) as e:
            log.warning(f"Connection error: {e} — retrying in 10s...")
            time.sleep(10)

    # Emit run_end so ingest can close the scan_runs entry
    print(json.dumps({
        "run_end": True,
        "run_id": run_id,
        "dongle_id": DONGLE_ID,
        "ended_at": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
    }), flush=True)
    log.info("Scanner shutdown complete")


if __name__ == "__main__":
    main()
