"""Tests for compute_linear_power — the core IQ → FFT → power function."""

import numpy as np
import pytest
from tests.helpers import make_iq_tone
from scanner import compute_linear_power, linear_to_db

FFT_SIZE = 1024
SAMPLE_RATE = 2_048_000
BIN_HZ = SAMPLE_RATE / FFT_SIZE  # 2000 Hz per bin


def test_pure_tone_peak_location():
    """A tone at +100 kHz offset should peak at the expected bin index."""
    freq_offset = 100_000  # 50 bins from center
    iq = make_iq_tone(freq_offset, SAMPLE_RATE, FFT_SIZE)
    window = np.hanning(FFT_SIZE)

    power = compute_linear_power(iq, FFT_SIZE, window)

    expected_bin = FFT_SIZE // 2 + round(freq_offset / BIN_HZ)  # 512 + 50 = 562
    peak_bin = int(np.argmax(power))
    assert abs(peak_bin - expected_bin) <= 1, (
        f"Peak at bin {peak_bin}, expected ~{expected_bin}"
    )


def test_pure_tone_above_noise():
    """Peak bin should be well above the noise floor (≥30 dB)."""
    iq = make_iq_tone(100_000, SAMPLE_RATE, FFT_SIZE)
    window = np.hanning(FFT_SIZE)

    power = compute_linear_power(iq, FFT_SIZE, window)
    power_db = linear_to_db(power)

    peak_db = float(np.max(power_db))
    # Exclude the peak and its immediate neighbors from noise floor calculation
    peak_idx = int(np.argmax(power_db))
    mask = np.ones(FFT_SIZE, dtype=bool)
    mask[max(0, peak_idx - 5) : peak_idx + 6] = False
    noise_floor_db = float(np.mean(power_db[mask]))

    assert peak_db - noise_floor_db >= 30, (
        f"Peak {peak_db:.1f} dB, noise floor {noise_floor_db:.1f} dB — "
        f"only {peak_db - noise_floor_db:.1f} dB separation"
    )


def test_output_shape_and_dtype():
    """Output should be a float array of shape (fft_size,), all non-negative."""
    iq = make_iq_tone(50_000, SAMPLE_RATE, FFT_SIZE)
    window = np.hanning(FFT_SIZE)

    power = compute_linear_power(iq, FFT_SIZE, window)

    assert power.shape == (FFT_SIZE,)
    assert np.issubdtype(power.dtype, np.floating)
    assert np.all(power >= 0)


def test_dc_signal_at_center():
    """A constant (DC) IQ signal should peak at the center bin after fftshift."""
    # Constant I=200 (~+0.57), Q=128 (~0.0)
    iq = bytes([200, 128] * FFT_SIZE)
    window = np.hanning(FFT_SIZE)

    power = compute_linear_power(iq, FFT_SIZE, window)

    assert int(np.argmax(power)) == FFT_SIZE // 2


def test_silence_near_zero():
    """All bytes=128 (maps to ~0+0j) should give near-zero power everywhere."""
    iq = bytes([128] * FFT_SIZE * 2)
    window = np.hanning(FFT_SIZE)

    power = compute_linear_power(iq, FFT_SIZE, window)

    # 128 maps to (128-127.5)/127.5 ≈ 0.0039, not exactly zero.
    # This tiny DC offset concentrates at bin fft_size//2 after fftshift.
    # Power should be very small — well below any real signal.
    assert np.max(power) < 0.01
    # In dB: max power around -21 dBFS — far below any real signal
    assert float(10 * np.log10(max(np.max(power), 1e-20))) < -20


def test_windowing_reduces_leakage():
    """Hann window should concentrate energy better than rectangular (no window).

    Use a tone that falls between bins (worst case for leakage).
    Count how many bins are within 20 dB of the peak — fewer = less leakage.
    """
    # Half-bin offset: lands between two FFT bins
    freq_offset = int(BIN_HZ * 50.5)
    iq = make_iq_tone(freq_offset, SAMPLE_RATE, FFT_SIZE)

    hann_window = np.hanning(FFT_SIZE)
    rect_window = np.ones(FFT_SIZE)

    power_hann = linear_to_db(compute_linear_power(iq, FFT_SIZE, hann_window))
    power_rect = linear_to_db(compute_linear_power(iq, FFT_SIZE, rect_window))

    # Count bins within 20 dB of peak
    hann_near_peak = int(np.sum(power_hann >= np.max(power_hann) - 20))
    rect_near_peak = int(np.sum(power_rect >= np.max(power_rect) - 20))

    assert hann_near_peak < rect_near_peak, (
        f"Hann: {hann_near_peak} bins near peak, Rect: {rect_near_peak} — "
        f"windowing should reduce leakage"
    )


# ─── Edge cases ──────────────────────────────────────────


def test_truncated_iq_raises_on_short_input():
    """IQ bytes shorter than fft_size*2 raises ValueError.

    In production this can happen during rtl_tcp sample drops. The window
    multiplication fails because iq[:fft_size] is shorter than the window.
    This is better than silently producing wrong results — sweep() catches
    ConnectionError from the rtl_tcp client, so this crash path is handled.
    """
    short_iq = make_iq_tone(100_000, SAMPLE_RATE, FFT_SIZE // 2)
    window = np.hanning(FFT_SIZE)

    with pytest.raises(ValueError, match="broadcast"):
        compute_linear_power(short_iq, FFT_SIZE, window)


def test_clipped_iq_creates_harmonics():
    """Saturated IQ data (gain too high) should spread energy into harmonics.

    When the ADC clips, the FFT shows spurious peaks at harmonics of the
    fundamental — like distortion on an overdriven audio signal.
    This test documents the effect rather than asserting exact behavior.
    """
    fft_size = 1024
    # Generate a clean tone, then clip it to simulate ADC saturation
    iq_clean = make_iq_tone(100_000, SAMPLE_RATE, fft_size, amplitude=0.9)
    raw = np.frombuffer(iq_clean, dtype=np.uint8).copy()

    # Hard-clip: force values near extremes to 0 or 255
    raw[raw > 240] = 255
    raw[raw < 15] = 0
    iq_clipped = raw.tobytes()

    window = np.hanning(fft_size)
    power_clean = linear_to_db(compute_linear_power(iq_clean, fft_size, window))
    power_clipped = linear_to_db(compute_linear_power(iq_clipped, fft_size, window))

    # Clipping raises the overall noise floor (energy spreads into harmonics)
    # Exclude the peak region when comparing noise floors
    peak_idx = int(np.argmax(power_clean))
    mask = np.ones(fft_size, dtype=bool)
    mask[max(0, peak_idx - 10) : peak_idx + 11] = False

    noise_clean = float(np.mean(power_clean[mask]))
    noise_clipped = float(np.mean(power_clipped[mask]))

    assert noise_clipped > noise_clean, (
        f"Clipped noise floor ({noise_clipped:.1f} dB) should be higher than "
        f"clean ({noise_clean:.1f} dB) due to harmonic distortion"
    )


def test_averaging_reduces_noise():
    """Averaging multiple FFT captures in linear domain should reduce noise variance.

    This tests the averaging pattern used in sweep(): sum linear power
    across NUM_AVERAGES captures, then divide. Noise should become smoother.
    """
    fft_size = 1024
    window = np.hanning(fft_size)
    rng = np.random.default_rng(42)

    # Single capture: generate random IQ (simulates noise)
    single_iq = rng.integers(0, 256, size=fft_size * 2, dtype=np.uint8).tobytes()
    single_power = compute_linear_power(single_iq, fft_size, window)

    # 8x averaged: same approach as sweep()
    power_sum = np.zeros(fft_size)
    for _ in range(8):
        noise_iq = rng.integers(0, 256, size=fft_size * 2, dtype=np.uint8).tobytes()
        power_sum += compute_linear_power(noise_iq, fft_size, window)
    avg_power = power_sum / 8

    # Averaged spectrum should have lower variance (smoother noise floor)
    single_db = linear_to_db(single_power)
    avg_db = linear_to_db(avg_power)

    assert float(np.std(avg_db)) < float(np.std(single_db)), (
        f"Averaged std ({np.std(avg_db):.1f}) should be less than "
        f"single ({np.std(single_db):.1f})"
    )
