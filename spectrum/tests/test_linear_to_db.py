"""Tests for linear_to_db — power to dBFS conversion."""

import numpy as np
from scanner import linear_to_db


def test_known_values():
    """Standard power-to-dB conversions."""
    inp = np.array([1.0, 0.1, 0.01, 0.001])
    expected = np.array([0.0, -10.0, -20.0, -30.0])
    np.testing.assert_allclose(linear_to_db(inp), expected, atol=1e-10)


def test_floor_clamp():
    """Zero and negative values should clamp to 10*log10(1e-20) = -200."""
    inp = np.array([0.0, -1.0, 1e-30])
    result = linear_to_db(inp)
    np.testing.assert_allclose(result, [-200.0, -200.0, -200.0], atol=1e-10)


def test_large_values():
    """No upper clamp — large powers convert normally."""
    inp = np.array([1000.0])
    expected = np.array([30.0])
    np.testing.assert_allclose(linear_to_db(inp), expected, atol=1e-10)


def test_shape_preserved():
    """Output shape should match input."""
    inp = np.random.rand(100)
    result = linear_to_db(inp)
    assert result.shape == inp.shape
