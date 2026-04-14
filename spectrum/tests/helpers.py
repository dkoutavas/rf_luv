"""Shared fixtures for spectrum scanner tests."""

import numpy as np
import pytest


def make_iq_tone(freq_offset_hz: float, sample_rate: int, fft_size: int, amplitude: float = 0.9) -> bytes:
    """Generate 8-bit IQ bytes for a pure tone at freq_offset_hz from center.

    To land exactly on an FFT bin, use freq_offset_hz = k * sample_rate / fft_size
    where k is an integer. For fft_size=1024, sample_rate=2048000: one bin = 2000 Hz.
    """
    n = np.arange(fft_size)
    phase = 2 * np.pi * freq_offset_hz * n / sample_rate
    i_float = amplitude * np.cos(phase)
    q_float = amplitude * np.sin(phase)

    # Convert [-1, +1] float to [0, 255] unsigned 8-bit (RTL-SDR format)
    i_bytes = np.clip(np.round(i_float * 127.5 + 127.5), 0, 255).astype(np.uint8)
    q_bytes = np.clip(np.round(q_float * 127.5 + 127.5), 0, 255).astype(np.uint8)

    # Interleave: [I0, Q0, I1, Q1, ...]
    iq = np.empty(fft_size * 2, dtype=np.uint8)
    iq[0::2] = i_bytes
    iq[1::2] = q_bytes
    return iq.tobytes()


def make_bins(freqs_and_powers: list[tuple[int, float]]) -> list[dict]:
    """Build a list of bin dicts from (freq_hz, power_dbfs) tuples."""
    return [{"freq_hz": f, "power_dbfs": p} for f, p in freqs_and_powers]
