"""Shared configuration for the spectrum pipeline.

Centralizes env-var loading and defaults so consumers don't have to
re-declare them. Only knobs referenced from multiple modules live here;
scanner-specific tunables (sweep schedule, antenna metadata, gain stepping)
stay in scanner.py.

Usage:

    from spectrum.config import config
    print(config.CH_HOST, config.PEAK_THRESHOLD_DB)

Tests override values via dataclasses.replace:

    from dataclasses import replace
    test_cfg = replace(config, CH_HOST="testhost")

ClickHouse defaults assume host-side deployment (systemd jobs, ad-hoc CLI).
docker-compose explicitly overrides CLICKHOUSE_HOST=clickhouse and
CLICKHOUSE_PORT=8123 for the in-container path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    CH_HOST: str = "localhost"
    CH_PORT: str = "8126"
    CH_DATABASE: str = "spectrum"
    CH_USER: str = "spectrum"
    CH_PASSWORD: str = "spectrum_local"

    SAMPLE_RATE: int = 2_048_000
    FFT_SIZE: int = 1024
    PEAK_THRESHOLD_DB: float = 10.0
    PEAK_NEIGHBOR_BINS: int = 5
    TRANSIENT_THRESHOLD_DB: float = 15.0

    DONGLE_ID: str = "v3-01"


def _load() -> Config:
    return Config(
        CH_HOST=os.environ.get("CLICKHOUSE_HOST", Config.CH_HOST),
        CH_PORT=os.environ.get("CLICKHOUSE_PORT", Config.CH_PORT),
        CH_DATABASE=os.environ.get("CLICKHOUSE_DB", Config.CH_DATABASE),
        CH_USER=os.environ.get("CLICKHOUSE_USER", Config.CH_USER),
        CH_PASSWORD=os.environ.get("CLICKHOUSE_PASSWORD", Config.CH_PASSWORD),
        SAMPLE_RATE=int(os.environ.get("SCAN_SAMPLE_RATE", Config.SAMPLE_RATE)),
        FFT_SIZE=int(os.environ.get("SCAN_FFT_SIZE", Config.FFT_SIZE)),
        PEAK_THRESHOLD_DB=float(os.environ.get("SCAN_PEAK_THRESHOLD", Config.PEAK_THRESHOLD_DB)),
        PEAK_NEIGHBOR_BINS=int(os.environ.get("SCAN_PEAK_NEIGHBORS", Config.PEAK_NEIGHBOR_BINS)),
        TRANSIENT_THRESHOLD_DB=float(os.environ.get("SCAN_TRANSIENT_THRESHOLD", Config.TRANSIENT_THRESHOLD_DB)),
        DONGLE_ID=os.environ.get("SCAN_DONGLE_ID", Config.DONGLE_ID),
    )


config = _load()
