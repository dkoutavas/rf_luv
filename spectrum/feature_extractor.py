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

import bisect
import logging
import sys
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev

import db

# ─── Config ─────────────────────────────────────────────────

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


def bandwidth_from_neighbors(
    freq_hz: int,
    latest_freqs_sorted: list[int],
    latest_powers: list[float],
) -> int:
    """Estimate FWHM-like bandwidth from ±1..±5 neighbor bins of the peak.

    Uses index-based neighbors in the sorted full-sweep bin list. Peaks may
    come from the airband preset (different grid), so look up the CLOSEST bin
    in the full-sweep grid and walk ±5 positions from there. If no 3 dB drop
    within ±5 bins on either side, return BANDWIDTH_SENTINEL — DVB-T muxes
    and other wideband signals will hit this, which is itself classifier signal.
    """
    if not latest_freqs_sorted:
        return BANDWIDTH_SENTINEL
    idx = bisect.bisect_left(latest_freqs_sorted, freq_hz)
    # Snap to nearest actual bin if peak's exact freq isn't in full-sweep grid
    if idx == len(latest_freqs_sorted):
        idx -= 1
    elif idx > 0 and abs(latest_freqs_sorted[idx] - freq_hz) > abs(
        latest_freqs_sorted[idx - 1] - freq_hz
    ):
        idx -= 1
    p_center = latest_powers[idx]

    left_n: int | None = None
    right_n: int | None = None
    for n in range(1, BANDWIDTH_MAX_NBR + 1):
        li = idx - n
        ri = idx + n
        if left_n is None and li >= 0:
            if (p_center - latest_powers[li]) >= BANDWIDTH_DROP_DB:
                left_n = n
        if right_n is None and ri < len(latest_freqs_sorted):
            if (p_center - latest_powers[ri]) >= BANDWIDTH_DROP_DB:
                right_n = n
        if left_n is not None and right_n is not None:
            break
    if left_n is None or right_n is None:
        return BANDWIDTH_SENTINEL
    return (left_n + right_n + 1) * BIN_WIDTH_HZ


def allocation_service_for(freq_hz: int, allocations: list[dict]) -> str | None:
    """Return the allocation service that covers freq_hz, or None if outside all."""
    for a in allocations:
        if int(a["freq_start_hz"]) <= freq_hz < int(a["freq_end_hz"]):
            return a["service"]
    return None


def detect_harmonic(
    freq_hz: int,
    peak_powers: dict[int, float],
    allocations: list[dict],
) -> int | None:
    """Return base frequency if freq_hz is a 2×/3×/4× of a stronger peak AND both
    sit in the same regulatory allocation.

    Why the allocation check: step 3 audit found DVB-T at 182.2 MHz flagged as
    2× of FM at 91.1 MHz — coincidental integer ratio between independent
    transmitters in separate allocations, not an actual harmonic relationship.
    Flagging cross-allocation ratios caused real DVB-T bins to be cap-capped
    at 0.4 confidence in the classifier. Same-allocation harmonics (e.g. a
    spur at 2× a strong airband carrier within the airband band) remain
    detected.

    Skip entirely if either side has no allocation — we don't flag
    unallocated frequencies as harmonics of anything.
    """
    p_self = peak_powers.get(freq_hz)
    if p_self is None:
        return None
    peak_service = allocation_service_for(freq_hz, allocations)
    if peak_service is None:
        return None
    for n in (2, 3, 4):
        base = freq_hz // n
        tol_hz = int(base * HARMONIC_TOLERANCE)
        for cand, p in peak_powers.items():
            if abs(cand - base) > tol_hz:
                continue
            if (p - p_self) < HARMONIC_POWER_REL_DB:
                continue
            base_service = allocation_service_for(cand, allocations)
            if base_service != peak_service:
                continue
            return cand
    return None


# ─── Main pipeline ──────────────────────────────────────────


def build_feature_row(
    freq_hz: int,
    series: list[tuple[datetime, float]],
    latest_freqs_sorted: list[int],
    latest_powers: list[float],
    peak_powers: dict[int, float],
    allocations: list[dict],
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
    interval = sweep_interval_s(freq_hz)
    bursts = run_length_bursts_s(actives_24h, interval)
    # Filter single-sweep "bursts": a continuous signal that briefly dips below
    # threshold for one sample creates a 1-sample run with duration == interval.
    # Real bursts span multiple samples. Observed fragility on 136.254 ATIS —
    # confidence flipped 0.6↔0.8 depending on whether 3+ single-sample pseudo-
    # bursts accumulated, which enabled am_airband_atc's burst_p50_s bonus.
    multi_sweep_bursts = [b for b in bursts if b > interval]
    if len(multi_sweep_bursts) >= MIN_BURSTS_STATS:
        b_p50: float | None = percentile(multi_sweep_bursts, 50)
        b_p95: float | None = percentile(multi_sweep_bursts, 95)
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

    bw = bandwidth_from_neighbors(freq_hz, latest_freqs_sorted, latest_powers)
    harm = detect_harmonic(freq_hz, peak_powers, allocations)

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
        db.insert("spectrum.peak_features", rows[i : i + BATCH_INSERT_LIMIT])


def discover_active_dongles() -> list[str]:
    """Dongles that have written scans in the last hour. Used to drive the
    per-dongle loop. If none, the scanner isn't running and there's nothing
    to extract."""
    rows = db.query_rows(
        "SELECT DISTINCT dongle_id FROM spectrum.scans "
        "WHERE timestamp > now() - INTERVAL 1 HOUR "
        "ORDER BY dongle_id"
    )
    return [r["dongle_id"] for r in rows]


def process_dongle(dongle_id: str, allocations: list[dict]) -> int:
    """Run one full extractor pass scoped to a single dongle. Returns the
    number of feature rows written.

    sweep_id is shared across dongles (commit 91d3860 kept the format
    unchanged for Grafana compatibility), so the suspect-sweeps subquery
    against compression_events is also scoped per-dongle — a V4 compression
    event must not exclude the V3 sweep that happens to share its sweep_id.
    """
    log.info(f"== {dongle_id}: scanning ==")
    suspect_sweeps_subquery = (
        f"(SELECT sweep_id FROM spectrum.compression_events FINAL "
        f" WHERE match_tier IN ('medium', 'high') "
        f"   AND dongle_id = '{dongle_id}')"
    )

    # 1. Active bins = distinct freqs with any peak in last 24h (from non-suspect sweeps)
    active_bins = [
        r["freq_hz"]
        for r in db.query_rows(
            f"SELECT DISTINCT freq_hz FROM spectrum.peaks "
            f"WHERE timestamp > now() - INTERVAL 24 HOUR "
            f"  AND dongle_id = '{dongle_id}' "
            f"  AND sweep_id NOT IN {suspect_sweeps_subquery} "
            f"ORDER BY freq_hz"
        )
    ]
    if not active_bins:
        log.info(f"  {dongle_id}: no active bins in last 24h, skipping")
        return 0
    log.info(f"  {dongle_id}: {len(active_bins)} active bins in last 24h")

    bins_csv = ",".join(str(b) for b in active_bins)

    # 2. Pull 14d scan history for all active bins in one query (excluding suspect sweeps)
    scans = db.query_rows(
        f"SELECT freq_hz, timestamp, power_dbfs "
        f"FROM spectrum.scans "
        f"WHERE freq_hz IN ({bins_csv}) "
        f"  AND timestamp > now() - INTERVAL 14 DAY "
        f"  AND dongle_id = '{dongle_id}' "
        f"  AND sweep_id NOT IN {suspect_sweeps_subquery} "
        f"ORDER BY freq_hz, timestamp"
    )
    log.info(f"  {dongle_id}: {len(scans)} scan rows loaded")

    by_bin: dict[int, list[tuple[datetime, float]]] = {}
    for r in scans:
        by_bin.setdefault(r["freq_hz"], []).append(
            (parse_ch_datetime(r["timestamp"]), float(r["power_dbfs"]))
        )

    # 3. Latest NON-SUSPECT full-sweep power snapshot for bandwidth estimation.
    latest = db.query_rows(
        f"SELECT freq_hz, power_dbfs FROM spectrum.scans "
        f"WHERE dongle_id = '{dongle_id}' "
        f"  AND sweep_id = (SELECT sweep_id FROM spectrum.sweep_health "
        f"                   WHERE preset = 'full' "
        f"                     AND dongle_id = '{dongle_id}' "
        f"                     AND sweep_id NOT IN {suspect_sweeps_subquery} "
        f"                   ORDER BY timestamp DESC LIMIT 1) "
        f"ORDER BY freq_hz"
    )
    latest_freqs_sorted = [r["freq_hz"] for r in latest]
    latest_powers = [float(r["power_dbfs"]) for r in latest]
    log.info(f"  {dongle_id}: {len(latest_freqs_sorted)} bins in latest full sweep")

    # 4. Max peak power per bin over 24h for harmonic detection (excluding suspect sweeps)
    peaks = db.query_rows(
        f"SELECT freq_hz, max(power_dbfs) AS power_dbfs FROM spectrum.peaks "
        f"WHERE timestamp > now() - INTERVAL 24 HOUR "
        f"  AND dongle_id = '{dongle_id}' "
        f"  AND sweep_id NOT IN {suspect_sweeps_subquery} "
        f"GROUP BY freq_hz"
    )
    peak_powers = {r["freq_hz"]: float(r["power_dbfs"]) for r in peaks}

    # 5. Build + insert feature rows tagged with dongle_id
    now = datetime.now(timezone.utc)
    rows_out: list[dict] = []
    for freq in active_bins:
        row = build_feature_row(
            freq,
            by_bin.get(freq, []),
            latest_freqs_sorted,
            latest_powers,
            peak_powers,
            allocations,
            now,
        )
        if row is not None:
            row["dongle_id"] = dongle_id
            rows_out.append(row)

    if not rows_out:
        log.warning(f"  {dongle_id}: no feature rows produced (active bins had no recent scans?)")
        return 0
    insert_features(rows_out)

    bws = [r["bandwidth_hz"] for r in rows_out]
    wide = sum(1 for b in bws if b == BANDWIDTH_SENTINEL)
    duty_24h_vals = [r["duty_cycle_24h"] for r in rows_out]
    log.info(
        f"  {dongle_id}: wrote {len(rows_out)} rows | "
        f"bw: median={percentile(bws, 50):.0f} Hz, wider-than-measurable={wide} | "
        f"duty_24h: median={percentile(duty_24h_vals, 50):.3f}, "
        f"max={max(duty_24h_vals):.3f}"
    )
    return len(rows_out)


def main() -> None:
    t0 = datetime.now(timezone.utc)
    log.info(f"Feature extractor starting (ClickHouse at {CH_HOST}:{CH_PORT})")

    dongles = discover_active_dongles()
    if not dongles:
        log.info("No dongles have written scans in the last hour, nothing to do")
        return
    log.info(f"Active dongles: {dongles}")

    # Allocations are global (regulatory bands don't depend on which radio sees them),
    # so fetch once and pass into the per-dongle loop.
    allocations = db.query_rows(
        "SELECT freq_start_hz, freq_end_hz, service FROM spectrum.allocations"
    )

    total = 0
    for dongle_id in dongles:
        try:
            total += process_dongle(dongle_id, allocations)
        except Exception:
            log.exception(f"Dongle {dongle_id} failed; continuing with remaining dongles")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    log.info(f"Feature extractor done: {total} rows across {len(dongles)} dongle(s) in {elapsed:.1f}s")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Feature extractor failed")
        sys.exit(1)
