#!/usr/bin/env python3
"""
rf_luv — Per-peak feature extractor (step 2 of 3 for the classifier)

For every frequency that produced a peak in the last 24 h, compute a per-bin
feature row (bandwidth, duty cycles at 1 h/24 h/7 d, burst statistics, diurnal
+ weekday patterns, harmonic relationship, active-sweep power statistics) and
upsert it into spectrum.peak_features.

Idempotent — uses ReplacingMergeTree(computed_at) to collapse repeat runs.
Designed to run on leap as a user-systemd timer at ~5 min cadence.
stdlib only; matches scan_ingest.py dependency posture.

DSP/audio analogy for reviewers: this is an envelope follower + spectrogram
rolloff meter applied over long time windows — the features the classifier
(step 3) will use to separate "bursty voice" from "continuous broadcast" and
to fingerprint signal identity beyond raw center frequency.
"""

# Defer type-hint evaluation so modern generic/union syntax (dict[...], X | None)
# is inert at runtime — script runs on any Python 3.7+ without importing typing.
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

# ─── Config ─────────────────────────────────────────────────

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8126")
CH_DB = os.environ.get("CLICKHOUSE_DB", "spectrum")
CH_USER = os.environ.get("CLICKHOUSE_USER", "spectrum")
CH_PASS = os.environ.get("CLICKHOUSE_PASSWORD", "spectrum_local")
CH_URL = f"http://{CH_HOST}:{CH_PORT}/"

BIN_WIDTH_HZ = 100_000
DUTY_THRESHOLD_DB = 6.0            # power > baseline + this = "active"
BANDWIDTH_DROP_DB = 3.0             # per spec — FWHM-style 3 dB point
BANDWIDTH_MAX_NBR = 5               # ±5 bins per spec
BANDWIDTH_SENTINEL = 999_000_000    # "wider than measurable" — itself useful signal
MIN_BURSTS_STATS = 3                # emit NULL if fewer bursts than this in 24 h
HARMONIC_TOLERANCE = 0.005          # 0.5%
HARMONIC_POWER_REL_DB = 6.0         # base must be ≥ this stronger than harmonic
FULL_SWEEP_INTERVAL_S = 300         # sweeps every 5 min outside airband
AIRBAND_SWEEP_INTERVAL_S = 60       # sweeps every 60 s inside airband
AIRBAND_LO_HZ = 118_000_000
AIRBAND_HI_HZ = 137_000_000
BATCH_INSERT_LIMIT = 500            # peak_features inserts batched per call

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("features")


# ─── ClickHouse helpers ─────────────────────────────────────


def ch_call(query: str, data: str | None = None, timeout: int = 120) -> str:
    """Invoke ClickHouse HTTP API. Without data: query in POST body.
    With data: query in URL, data in POST body (INSERT pattern)."""
    params = f"user={CH_USER}&password={CH_PASS}&database={CH_DB}"
    if data is not None:
        url = f"{CH_URL}?{params}&query={quote(query)}"
        body = data.encode()
    else:
        url = f"{CH_URL}?{params}"
        body = query.encode()
    req = Request(url, data=body)
    req.add_header("Content-Type", "text/plain")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()
    except HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        log.error(f"ClickHouse HTTP {e.code}: {err_body[:300]}")
        raise


def ch_rows(sql: str) -> list[dict]:
    """Run a SELECT and parse JSONEachRow lines into dicts."""
    out = ch_call(sql + " FORMAT JSONEachRow")
    return [json.loads(line) for line in out.splitlines() if line]


# ─── Pure helpers ───────────────────────────────────────────


def sweep_interval_s(freq_hz: int) -> int:
    """How often this bin is sampled, by preset."""
    if AIRBAND_LO_HZ <= freq_hz <= AIRBAND_HI_HZ:
        return AIRBAND_SWEEP_INTERVAL_S
    return FULL_SWEEP_INTERVAL_S


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile. p in [0, 100]."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def parse_ch_datetime(raw: str) -> datetime:
    """ClickHouse emits 'YYYY-MM-DD HH:MM:SS[.fff]' in UTC."""
    if "." in raw:
        base, frac = raw.rsplit(".", 1)
        dt = datetime.strptime(base, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(microsecond=int(frac.ljust(6, "0")[:6]))
    else:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)


def run_length_bursts_s(actives: list[bool], interval_s: int) -> list[float]:
    """Run-length encode a boolean activity series into burst durations in seconds.

    Quantization caveat: a 5 s voice burst on a bin sampled every 5 min will be
    reported as a ~300 s burst. Documented in the spec as accepted — the classifier
    (step 3) should not over-rely on burst duration for non-airband bins.
    """
    bursts = []
    run = 0
    for a in actives:
        if a:
            run += 1
        else:
            if run > 0:
                bursts.append(run * interval_s)
                run = 0
    if run > 0:
        bursts.append(run * interval_s)
    return bursts


def bandwidth_from_neighbors(freq_hz: int, latest: dict[int, float]) -> int:
    """Estimate FWHM-like bandwidth from ±1..±5 neighbor bins of the peak.

    Return BANDWIDTH_SENTINEL if no 3 dB drop found on either side within ±5 bins —
    DVB-T muxes and other wideband signals will hit this, which is itself useful.
    """
    p_center = latest.get(freq_hz)
    if p_center is None:
        return BANDWIDTH_SENTINEL
    left_n = right_n = None
    for n in range(1, BANDWIDTH_MAX_NBR + 1):
        if left_n is None:
            lp = latest.get(freq_hz - n * BIN_WIDTH_HZ)
            if lp is not None and (p_center - lp) >= BANDWIDTH_DROP_DB:
                left_n = n
        if right_n is None:
            rp = latest.get(freq_hz + n * BIN_WIDTH_HZ)
            if rp is not None and (p_center - rp) >= BANDWIDTH_DROP_DB:
                right_n = n
        if left_n is not None and right_n is not None:
            break
    if left_n is None or right_n is None:
        return BANDWIDTH_SENTINEL
    return (left_n + right_n + 1) * BIN_WIDTH_HZ


def detect_harmonic(freq_hz: int, peak_powers: dict[int, float]) -> int | None:
    """Return base frequency if freq_hz is a 2×/3×/4× of a stronger peak."""
    p_self = peak_powers.get(freq_hz)
    if p_self is None:
        return None
    for n in (2, 3, 4):
        base = freq_hz // n
        tol_hz = int(base * HARMONIC_TOLERANCE)
        for cand, p in peak_powers.items():
            if abs(cand - base) <= tol_hz and (p - p_self) >= HARMONIC_POWER_REL_DB:
                return cand
    return None


# ─── Main pipeline ──────────────────────────────────────────


def build_feature_row(
    freq_hz: int,
    series: list[tuple[datetime, float]],
    latest_power: dict[int, float],
    peak_powers: dict[int, float],
    now: datetime,
) -> dict | None:
    """Compute one peak_features row for a single bin."""
    if not series:
        return None

    h1 = now - timedelta(hours=1)
    d1 = now - timedelta(days=1)
    d7 = now - timedelta(days=7)

    series_1h = [(t, p) for t, p in series if t >= h1]
    series_24h = [(t, p) for t, p in series if t >= d1]
    series_7d = [(t, p) for t, p in series if t >= d7]
    series_14d = series

    if not series_24h:
        return None

    powers_24h = [p for _, p in series_24h]
    baseline = percentile(powers_24h, 10)
    thresh = baseline + DUTY_THRESHOLD_DB

    def duty(window: list[tuple[datetime, float]]) -> float:
        return sum(1 for _, p in window if p > thresh) / len(window) if window else 0.0

    duty_1h = duty(series_1h)
    duty_24h = duty(series_24h)
    duty_7d = duty(series_7d)

    active_24h = [p for _, p in series_24h if p > thresh]
    if active_24h:
        p_mean = mean(active_24h)
        p_p95 = percentile(active_24h, 95)
        p_std = pstdev(active_24h) if len(active_24h) > 1 else 0.0
    else:
        p_mean = baseline
        p_p95 = baseline
        p_std = 0.0

    actives_24h = [p > thresh for _, p in series_24h]
    bursts = run_length_bursts_s(actives_24h, sweep_interval_s(freq_hz))
    if len(bursts) >= MIN_BURSTS_STATS:
        b_p50: float | None = percentile(bursts, 50)
        b_p95: float | None = percentile(bursts, 95)
    else:
        b_p50 = None
        b_p95 = None

    # Diurnal pattern (7d, by UTC hour-of-day)
    diurnal = [0] * 24
    diurnal_tot = [0] * 24
    for t, p in series_7d:
        h = t.hour
        diurnal_tot[h] += 1
        if p > thresh:
            diurnal[h] += 1
    diurnal_pattern = [
        diurnal[i] / diurnal_tot[i] if diurnal_tot[i] > 0 else 0.0 for i in range(24)
    ]

    # Weekday pattern (14d, by UTC weekday Mon=0..Sun=6)
    weekday = [0] * 7
    weekday_tot = [0] * 7
    for t, p in series_14d:
        w = t.weekday()
        weekday_tot[w] += 1
        if p > thresh:
            weekday[w] += 1
    weekday_pattern = [
        weekday[i] / weekday_tot[i] if weekday_tot[i] > 0 else 0.0 for i in range(7)
    ]

    bw = bandwidth_from_neighbors(freq_hz, latest_power)
    harm = detect_harmonic(freq_hz, peak_powers)

    return {
        "freq_hz": freq_hz,
        "bandwidth_hz": bw,
        "duty_cycle_1h": round(duty_1h, 4),
        "duty_cycle_24h": round(duty_24h, 4),
        "duty_cycle_7d": round(duty_7d, 4),
        "burst_p50_s": round(b_p50, 2) if b_p50 is not None else None,
        "burst_p95_s": round(b_p95, 2) if b_p95 is not None else None,
        "diurnal_pattern": [round(x, 4) for x in diurnal_pattern],
        "weekday_pattern": [round(x, 4) for x in weekday_pattern],
        "harmonic_of_hz": harm,
        "power_mean_dbfs": round(p_mean, 2),
        "power_p95_dbfs": round(p_p95, 2),
        "power_std_db": round(p_std, 2),
        "sweeps_observed_24h": len(series_24h),
        "computed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def insert_features(rows: list[dict]) -> None:
    """Batch-insert peak_features rows via JSONEachRow."""
    for i in range(0, len(rows), BATCH_INSERT_LIMIT):
        chunk = rows[i : i + BATCH_INSERT_LIMIT]
        payload = "\n".join(json.dumps(r) for r in chunk)
        ch_call("INSERT INTO spectrum.peak_features FORMAT JSONEachRow", data=payload)


def main() -> None:
    t0 = datetime.now(timezone.utc)
    log.info(f"Feature extractor starting (ClickHouse at {CH_HOST}:{CH_PORT})")

    # 1. Active bins = distinct freqs with any peak in last 24h
    active_bins = [
        r["freq_hz"]
        for r in ch_rows(
            "SELECT DISTINCT freq_hz FROM spectrum.peaks "
            "WHERE timestamp > now() - INTERVAL 24 HOUR ORDER BY freq_hz"
        )
    ]
    if not active_bins:
        log.info("No active bins in last 24h, nothing to do")
        return
    log.info(f"{len(active_bins)} active bins in last 24h")

    bins_csv = ",".join(str(b) for b in active_bins)

    # 2. Pull 14d scan history for all active bins in one query
    log.info("Fetching 14d scan history...")
    scans = ch_rows(
        f"SELECT freq_hz, toString(timestamp) AS timestamp, power_dbfs "
        f"FROM spectrum.scans "
        f"WHERE freq_hz IN ({bins_csv}) "
        f"  AND timestamp > now() - INTERVAL 14 DAY "
        f"ORDER BY freq_hz, timestamp"
    )
    log.info(f"  {len(scans)} scan rows loaded")

    by_bin: dict[int, list[tuple[datetime, float]]] = {}
    for r in scans:
        by_bin.setdefault(r["freq_hz"], []).append(
            (parse_ch_datetime(r["timestamp"]), float(r["power_dbfs"]))
        )

    # 3. Latest full-sweep power snapshot for bandwidth estimation
    log.info("Fetching latest full-sweep snapshot for bandwidth...")
    latest = ch_rows(
        "SELECT freq_hz, power_dbfs FROM spectrum.scans "
        "WHERE sweep_id = (SELECT sweep_id FROM spectrum.sweep_health "
        "                   WHERE preset = 'full' ORDER BY timestamp DESC LIMIT 1)"
    )
    latest_power = {r["freq_hz"]: float(r["power_dbfs"]) for r in latest}
    log.info(f"  {len(latest_power)} bins in latest full sweep")

    # 4. Max peak power per bin over 24h for harmonic detection
    log.info("Fetching peak-power snapshot for harmonic detection...")
    peaks = ch_rows(
        "SELECT freq_hz, max(power_dbfs) AS power_dbfs FROM spectrum.peaks "
        "WHERE timestamp > now() - INTERVAL 24 HOUR GROUP BY freq_hz"
    )
    peak_powers = {r["freq_hz"]: float(r["power_dbfs"]) for r in peaks}

    # 5. Build feature rows
    now = datetime.now(timezone.utc)
    rows_out = []
    for freq in active_bins:
        row = build_feature_row(
            freq, by_bin.get(freq, []), latest_power, peak_powers, now
        )
        if row is not None:
            rows_out.append(row)

    # 6. Insert
    if not rows_out:
        log.warning("No feature rows produced (active bins had no recent scans?)")
        return
    insert_features(rows_out)

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    # Quick glance stats so journald logs are useful at a glance
    bws = [r["bandwidth_hz"] for r in rows_out]
    wide = sum(1 for b in bws if b == BANDWIDTH_SENTINEL)
    duty_24h_vals = [r["duty_cycle_24h"] for r in rows_out]
    log.info(
        f"Wrote {len(rows_out)} rows in {elapsed:.1f}s | "
        f"bw: median={percentile(bws, 50):.0f} Hz, wider-than-measurable={wide} | "
        f"duty_24h: median={percentile(duty_24h_vals, 50):.3f}, "
        f"max={max(duty_24h_vals):.3f}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Feature extractor failed")
        sys.exit(1)
