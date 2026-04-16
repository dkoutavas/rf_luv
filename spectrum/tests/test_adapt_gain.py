"""Tests for adapt_gain — adaptive gain step-down on ADC clipping."""

import pytest
from scanner import adapt_gain


def test_no_clipping_gain_unchanged():
    """Clean sweep should not change gain."""
    assert adapt_gain(12.0, clipped=False, gain_min=0.0, gain_step=2.0) == 12.0


def test_clipping_reduces_by_step():
    """Clipping should reduce gain by exactly one step."""
    assert adapt_gain(12.0, clipped=True, gain_min=0.0, gain_step=2.0) == 10.0


def test_clipping_respects_floor():
    """Gain should never drop below gain_min."""
    assert adapt_gain(1.0, clipped=True, gain_min=0.0, gain_step=2.0) == 0.0


def test_clipping_at_floor_stays_at_floor():
    """Already at minimum — clipping can't reduce further."""
    assert adapt_gain(0.0, clipped=True, gain_min=0.0, gain_step=2.0) == 0.0


def test_custom_floor():
    """Non-zero gain_min is respected."""
    assert adapt_gain(5.0, clipped=True, gain_min=4.0, gain_step=2.0) == 4.0


def test_step_larger_than_remaining_headroom():
    """Step that would overshoot the floor is clamped."""
    assert adapt_gain(3.0, clipped=True, gain_min=2.0, gain_step=5.0) == 2.0


def test_successive_reductions():
    """Simulate multiple clipping sweeps stepping gain down."""
    gain = 12.0
    for expected in [10.0, 8.0, 6.0, 4.0, 2.0, 0.0, 0.0]:
        gain = adapt_gain(gain, clipped=True, gain_min=0.0, gain_step=2.0)
        assert gain == expected


def test_mixed_clipping_and_clean():
    """Gain only drops on clipping sweeps, holds steady on clean ones."""
    gain = 12.0
    # Clip → drop
    gain = adapt_gain(gain, clipped=True, gain_min=0.0, gain_step=2.0)
    assert gain == 10.0
    # Clean → hold
    gain = adapt_gain(gain, clipped=False, gain_min=0.0, gain_step=2.0)
    assert gain == 10.0
    # Clean → hold
    gain = adapt_gain(gain, clipped=False, gain_min=0.0, gain_step=2.0)
    assert gain == 10.0
    # Clip → drop
    gain = adapt_gain(gain, clipped=True, gain_min=0.0, gain_step=2.0)
    assert gain == 8.0


def test_fractional_step():
    """Non-integer step sizes work correctly."""
    assert adapt_gain(10.0, clipped=True, gain_min=0.0, gain_step=1.5) == 8.5


def test_fractional_floor():
    """Non-integer gain_min is respected."""
    assert adapt_gain(2.0, clipped=True, gain_min=1.5, gain_step=2.0) == 1.5
