"""Test that sweep() measures each hop on its own frequency.

The regression this guards: after set_frequency, samples from the OLD
frequency keep arriving for 80-136 ms (measured on the V3, 2026-09-26),
because the dongle hands data over in 256 KiB USB blocks and rtl_tcp queues
more. sweep() used to discard only 32 KiB (8 ms) per hop, so each hop's
spectrum came from a frequency many hops earlier and loud FM was smeared
20-30 MHz up into airband.
"""

import numpy as np

import scanner

# Worst latency measured on the V3: 544 KiB of old-frequency samples
LATENCY_BYTES = 544 * 1024


class LaggingClient:
    """In-memory stand-in for RTLTCPClient. A retune takes effect only after
    LATENCY_BYTES more bytes have been read, like the real USB pipeline.
    Below 100 MHz the "antenna" carries a loud random signal, above it
    near-silence (mid-scale samples)."""

    def __init__(self):
        self.pos = 0
        self.freq = 99_000_000
        self.pending = None      # (switch_at_byte, new_freq)
        self.rng = np.random.default_rng(0)

    def set_frequency(self, freq_hz):
        self.pending = (self.pos + LATENCY_BYTES, freq_hz)

    def read_samples(self, n):
        if self.pending and self.pos >= self.pending[0]:
            self.freq = self.pending[1]
            self.pending = None
        self.pos += n
        if self.freq < 100_000_000:
            return self.rng.integers(0, 256, n, dtype=np.uint8).tobytes()
        return bytes([127, 128]) * (n // 2)

    def discard(self, n):
        # Read in USB-block-sized steps so a retune can land mid-discard
        while n > 0:
            step = min(n, 262144)
            self.read_samples(step)
            n -= step


def test_each_hop_is_measured_on_its_own_frequency():
    # Two hops: 99.024 MHz (loud) then 101.072 MHz (quiet)
    bins, _ = scanner.sweep(LaggingClient(), 98_000_000, 102_000_000)
    loud = [b["power_dbfs"] for b in bins if b["freq_hz"] < 100_000_000]
    quiet = [b["power_dbfs"] for b in bins if b["freq_hz"] > 100_100_000]
    assert loud and quiet
    # The quiet hop must not carry the loud hop's energy. The old 32 KiB
    # discard read hop 2 from hop 1's frequency and failed here.
    assert max(quiet) < min(loud) - 20, (max(quiet), min(loud))


class StopAfterClient(LaggingClient):
    """Delivers a stop (as the SIGTERM handler would) after `hops` retunes."""

    def __init__(self, hops):
        super().__init__()
        self.hops = hops
        self.retunes = 0

    def set_frequency(self, freq_hz):
        super().set_frequency(freq_hz)
        self.retunes += 1
        if self.retunes == self.hops:
            scanner.running = False


def test_stop_mid_sweep_drops_the_partial_sweep():
    client = StopAfterClient(hops=2)
    try:
        bins, clipping = scanner.sweep(client, 88_000_000, 470_000_000)
    finally:
        scanner.running = True
    # A full 88-470 MHz sweep is ~187 hops; the stop must end it after the
    # current hop and return nothing to store.
    assert (bins, clipping) == (None, None)
    assert client.retunes == 2
