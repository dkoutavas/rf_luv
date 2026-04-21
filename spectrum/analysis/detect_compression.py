#!/usr/bin/env python3
"""Compression-event detector (Phase 2 archaeology + Phase 3 live detection).

Scans full-band sweeps from spectrum.scans + spectrum.sweep_health and computes
a 3-part compression signature:

    sig_spur     — ≥N consecutive tiles where argMax bin sits at a near-constant
                   offset from tile center (stdev < SPUR_OFFSET_STDDEV_MAX_HZ).
                   This is the fingerprint of a wideband spur comb created by
                   LNA/ADC nonlinearity under a strong narrowband load.
    sig_baseline — median bin power, outside FM/DVB-T, > DEPRESSION_MIN_DB
                   below the same-hour hourly_baseline. This catches the
                   "everything else got quieter" signature of LNA compression.
    sig_clip     — worst_clip_freq_hz outside the usual FM/DAB zone (170-230)
                   AND clipped_captures > 0. Quick proxy using existing
                   sweep_health.

match_tier aggregates: 3 fires = 'high', 2 = 'medium', 1 = 'low', 0 = 'none'.
Only match_tier >= 'medium' is written to spectrum.compression_events by default.

The detector is honest about uncertainty: we have ONE unambiguously verified
compression event (2026-04-21 11:55:04.960 UTC at 304.19 MHz, +3.5 dBFS) and
no ground truth for pre-Phase-4 events. The sub-flags are reported independently;
there is no fabricated "compression_probability = 0.73".

Usage:
    python3 detect_compression.py --backfill                   # process all full sweeps
    python3 detect_compression.py --since '2026-04-18'          # process since timestamp
    python3 detect_compression.py --sweep 'full:2026-04-21 11:55:04.960'
    python3 detect_compression.py --dry-run --since '2026-04-20'  # no INSERT

Dependencies: numpy + stdlib. No clickhouse-driver — HTTP via urllib.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import numpy as np

# ─── Config ──────────────────────────────────────────────
CH_HOST = os.environ.get("CLICKHOUSE_HOST", "192.168.2.10")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8126")
CH_DB = os.environ.get("CLICKHOUSE_DB", "spectrum")
CH_USER = os.environ.get("CLICKHOUSE_USER", "spectrum")
CH_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "spectrum_local")
CH_URL = f"http://{CH_HOST}:{CH_PORT}"

# Scanner geometry (matches spectrum/scanner.py)
FREQ_START = 88_000_000
SAMPLE_RATE = 2_048_000
TILE_WIDTH_HZ = SAMPLE_RATE  # one tile per center-retune

# Signature thresholds
MIN_SPUR_BLOCK_TILES = 10         # need ≥10 tiles (allowing up to SPUR_BLOCK_OUTLIERS_MAX) for spur comb
SPUR_OFFSET_STDDEV_MAX_HZ = 30_000
SPUR_OFFSET_MIN_ABS_HZ = 100_000  # exclude DC-spike pattern (|offset| ~ 0-50 kHz is normal)
SPUR_MIN_MEDIAN_POWER_DBFS = -15.0  # real compression has peaks > -5 dBFS; normal spurs are at ~-30
SPUR_BLOCK_OUTLIERS_MAX = 2       # allow up to 2 consecutive outlier tiles before breaking block
DEPRESSION_MIN_DB = 5.0
# Emitter estimation
EMITTER_SEARCH_PADDING_TILES = 2      # search ±this many tiles around the spur block
EMITTER_MIN_POWER_ABOVE_MEDIAN_DB = 5.0  # emitter peak must be this far above spur-block median
EMITTER_MIN_ABS_POWER_DBFS = -10.0     # and at least this strong in absolute terms
EMITTER_OFFSET_MIN_DEVIATION_HZ = 100_000  # peak must deviate from dominant spur offset
# Clipping sub-signals (fix #3)
DVBT_CLIP_RANGE = (174_000_000, 230_000_000)   # actual Greek DVB-T Band III extent (174-230)
FM_CLIP_RANGE = (88_000_000, 108_000_000)      # FM broadcast band
MIN_CLIPPED_CAPTURES_FOR_SIG = 5              # normal baseline is 28-40; <5 is weak signal

# Exclude ranges (used by spur/baseline/emitter detectors)
FM_RANGE = (88_000_000, 108_000_000)
DVBT_RANGE = (174_000_000, 230_000_000)

DETECTOR_VERSION = "v2"  # v2: outlier-tolerant spur blocks, constrained emitter search, split clip sigs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("detect_compression")


# ─── ClickHouse I/O ──────────────────────────────────────
def ch_query(query: str) -> str:
    """Execute a ClickHouse query via HTTP; return raw text response."""
    params = f"user={CH_USER}&password={CH_PASSWORD}&database={CH_DB}"
    url = f"{CH_URL}/?{params}"
    req = Request(url, data=query.encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        return urlopen(req, timeout=60).read().decode().strip()
    except HTTPError as e:
        raise RuntimeError(f"ClickHouse error: {e.read().decode()[:500]}") from e


def ch_query_json(query: str) -> list[dict]:
    """Execute a query returning JSONEachRow; parse to list of dicts."""
    text = ch_query(query + " FORMAT JSONEachRow")
    if not text:
        return []
    return [json.loads(line) for line in text.split("\n") if line]


# ─── Tile math ───────────────────────────────────────────
def tile_idx_for(freq_hz: int) -> int:
    return (freq_hz - FREQ_START) // TILE_WIDTH_HZ


def tile_center_hz(tile_idx: int) -> int:
    return FREQ_START + tile_idx * TILE_WIDTH_HZ + TILE_WIDTH_HZ // 2


# ─── Sub-signature detectors ─────────────────────────────
@dataclass
class SpurInfo:
    sig: int  # 0/1
    block_tile_lo: int
    block_tile_hi: int
    block_count: int
    offset_mean_hz: int
    offset_stddev_khz: float
    block_median_power_dbfs: float = -100.0


def detect_spur_comb(per_tile_argmax_offset_hz: list[int], per_tile_peak_power_dbfs: list[float]) -> SpurInfo:
    """Find the longest consecutive run of tiles where the argmax offset is near-constant.

    A spur comb is the fingerprint of LNA/ADC nonlinearity: every tile in the
    compression zone shows its peak at the same offset from tile center, because
    the nonlinearity produces an intermod spur at a fixed LO-relative frequency.

    We sweep a window across the tile-offset array, finding the longest run
    where stddev(offset) < SPUR_OFFSET_STDDEV_MAX_HZ. Weak peaks (power below
    -30 dBFS) are skipped since argmax there is just noise.
    """
    n = len(per_tile_argmax_offset_hz)
    if n < MIN_SPUR_BLOCK_TILES:
        return SpurInfo(0, 0, 0, 0, 0, 0.0)

    # Mask out weak tiles: their argmax is dominated by noise, not structured
    POWER_FLOOR = -30.0
    active = [per_tile_peak_power_dbfs[i] > POWER_FLOOR for i in range(n)]

    best_lo = best_hi = 0
    best_count = 0
    best_offset = 0
    best_stddev = 0.0
    best_median_power = -100.0

    # Greedy sliding expansion with outlier tolerance.
    # The v1 detector terminated the block at the first offset-deviating tile. On
    # the 2026-04-21 11:55 event, tile 67 had an isolated +26 kHz offset which
    # split what should have been one 63-tile block (40-102) into two (40-66
    # and 68-102). Now we allow up to SPUR_BLOCK_OUTLIERS_MAX consecutive tiles
    # to deviate; the block resumes when offsets return to the dominant pattern.
    for start in range(n):
        if not active[start]:
            continue
        seed_offset = per_tile_argmax_offset_hz[start]
        offsets = [seed_offset]
        powers = [per_tile_peak_power_dbfs[start]]
        end = start
        outlier_streak = 0
        provisional_tail: list[tuple[int, int, float]] = []  # (idx, offset, power) held for rollback
        for j in range(start + 1, n):
            if not active[j]:
                break
            off_j = per_tile_argmax_offset_hz[j]
            pw_j = per_tile_peak_power_dbfs[j]
            # Check membership: offsets stays cohesive (stddev bound) when this
            # tile's offset is added to the ACCEPTED set (not outliers).
            candidate_offsets = offsets + [off_j]
            if float(np.std(candidate_offsets)) <= SPUR_OFFSET_STDDEV_MAX_HZ:
                # Tile fits: absorb any held provisional outliers into committed block
                for (_, po, pp) in provisional_tail:
                    # Don't add outlier offsets to the offsets list (they'd break stddev)
                    # but do count them in the block span.
                    powers.append(pp)
                provisional_tail = []
                outlier_streak = 0
                offsets.append(off_j)
                powers.append(pw_j)
                end = j
            else:
                outlier_streak += 1
                if outlier_streak > SPUR_BLOCK_OUTLIERS_MAX:
                    break  # too many consecutive outliers; block ends (provisional tail discarded)
                provisional_tail.append((j, off_j, pw_j))

        # Never include a trailing provisional tail — only outliers in the MIDDLE
        # (followed by a conforming tile) were absorbed via the branch above.
        count = (end - start + 1)
        if count >= MIN_SPUR_BLOCK_TILES and count > best_count:
            best_lo = start
            best_hi = end
            best_count = count
            best_offset = int(np.mean(offsets))
            best_stddev = float(np.std(offsets)) / 1000.0
            best_median_power = float(np.median(powers))

    # sig_spur fires only when all three conditions hold:
    #   (a) ≥MIN_SPUR_BLOCK_TILES consecutive tiles with near-constant offset
    #   (b) |offset| ≥ SPUR_OFFSET_MIN_ABS_HZ (not just the DC-spike pattern at +26 kHz)
    #   (c) median peak power in the block ≥ SPUR_MIN_MEDIAN_POWER_DBFS
    #       (real compression peaks are near-saturation; baseline spurs sit much lower)
    is_spur = (
        best_count >= MIN_SPUR_BLOCK_TILES
        and abs(best_offset) >= SPUR_OFFSET_MIN_ABS_HZ
        and best_median_power >= SPUR_MIN_MEDIAN_POWER_DBFS
    )

    return SpurInfo(
        sig=1 if is_spur else 0,
        block_tile_lo=best_lo,
        block_tile_hi=best_hi,
        block_count=best_count,
        offset_mean_hz=best_offset,
        offset_stddev_khz=best_stddev,
        block_median_power_dbfs=best_median_power,
    )


@dataclass
class BaselineInfo:
    sig: int
    depression_db: float
    bins_sampled: int


def detect_baseline_depression(
    sweep_bins: dict[int, float],
    baseline_bins: dict[int, float],
) -> BaselineInfo:
    """Check if known-strong carriers (FM + DVB-T) dropped vs their baseline.

    During LNA compression, a strong out-of-band emitter reduces effective
    wideband gain, so legitimate strong signals (FM broadcast 88-108, DVB-T
    174-230) show up ATTENUATED in the scan. Empty bins go the OTHER way —
    they elevate because the compression generates wideband intermod spurs.
    So the diagnostic is specifically the attenuation of normally-strong
    carriers.

    Metric: among bins whose hourly_baseline is "active" (> -35 dBFS, i.e.
    a known carrier bin), compute the median depression (baseline - current).
    Median > DEPRESSION_MIN_DB → sig_baseline fires.
    """
    STRONG_CARRIER_THRESHOLD = -35.0

    depressions: list[float] = []
    for freq_hz, cur_power in sweep_bins.items():
        base = baseline_bins.get(freq_hz)
        if base is None:
            continue
        if base < STRONG_CARRIER_THRESHOLD:
            continue  # empty bin — not a diagnostic for compression
        depressions.append(base - cur_power)

    if not depressions:
        return BaselineInfo(0, 0.0, 0)

    median = float(np.median(depressions))
    return BaselineInfo(
        sig=1 if median >= DEPRESSION_MIN_DB else 0,
        depression_db=round(median, 2),
        bins_sampled=len(depressions),
    )


@dataclass
class ClipInfo:
    sig: int          # UHF-compression clip (outside FM AND outside DVB-T, ≥ MIN_CLIPPED_CAPTURES)
    sig_fm: int       # FM-overload clip (in 88-108 MHz, ≥ MIN_CLIPPED_CAPTURES) — observability only
    worst_clip_freq_hz: int
    clipped_captures: int


def detect_clip(sweep_health: dict) -> ClipInfo:
    """Split the clipping signal into UHF-compression and FM-overload.

    sig      — worst_clip outside 88-108 (FM) AND outside 174-230 (DVB-T),
               clipped_captures ≥ 5. Indicates compression in a band where the
               dongle doesn't normally clip.
    sig_fm   — worst_clip in 88-108 MHz with ≥ 5 clipped captures. In Athens,
               FM clipping is normal baseline (Lycabettus/Hymettus TXs); this
               tells us the FM-overload subsystem is active but says nothing
               about wideband compression. Stored for observability, not
               aggregated into match_tier.

    Rationale (fix #3): v1 used `worst_clip outside 170-230 AND clipped > 0`
    which fired for any non-FM incidental clipping and for minor FM excursions
    into tile 99 MHz, and didn't distinguish these cases. The raised threshold
    of ≥5 captures matches the observed normal baseline (32-40 per sweep with
    persistent FM) — anything under 5 is a weak-signal blip, not compression.
    """
    wf = int(sweep_health.get("worst_clip_freq_hz") or 0)
    cc = int(sweep_health.get("clipped_captures") or 0)

    in_dvbt = DVBT_CLIP_RANGE[0] <= wf <= DVBT_CLIP_RANGE[1]
    in_fm = FM_CLIP_RANGE[0] <= wf <= FM_CLIP_RANGE[1]

    sig_uhf = 1 if (wf > 0 and not in_dvbt and not in_fm and cc >= MIN_CLIPPED_CAPTURES_FOR_SIG) else 0
    sig_fm_flag = 1 if (wf > 0 and in_fm and cc >= MIN_CLIPPED_CAPTURES_FOR_SIG) else 0

    return ClipInfo(
        sig=sig_uhf,
        sig_fm=sig_fm_flag,
        worst_clip_freq_hz=wf,
        clipped_captures=cc,
    )


# ─── Emitter estimation ──────────────────────────────────
def estimate_emitter_freq(
    tiles: dict[int, list[tuple[int, float]]],
    spur: SpurInfo,
    spur_median_power_dbfs: float,
) -> tuple[int | None, float | None]:
    """Constrained emitter-peak search within the compression zone.

    The real emitter must sit INSIDE the saturation zone by definition — either
    within the spur block or within EMITTER_SEARCH_PADDING_TILES of its edges.
    Searching the whole sweep (as v1 did) fabricated attributions by picking
    always-on repeaters far away from the compression signal.

    A peak qualifies as an emitter only if:
      (a) Inside [block_lo - padding, block_hi + padding]
      (b) Peak offset deviates from the dominant spur offset (not a spur itself)
      (c) Peak power > spur_block_median + EMITTER_MIN_POWER_ABOVE_MEDIAN_DB
      (d) Peak power ≥ EMITTER_MIN_ABS_POWER_DBFS (don't attribute weak events)
      (e) Not inside an FM or DVB-T broadcast band (those are legitimate carriers)

    If no tile qualifies, return (None, None). NULL attribution is the correct
    answer — we have detected compression but can't localize the emitter from
    power spectrum alone. Phase 4/5 IQ capture would resolve this.
    """
    if spur.sig == 0:
        return (None, None)

    dominant_offset = spur.offset_mean_hz
    power_threshold = spur_median_power_dbfs + EMITTER_MIN_POWER_ABOVE_MEDIAN_DB
    lo = spur.block_tile_lo - EMITTER_SEARCH_PADDING_TILES
    hi = spur.block_tile_hi + EMITTER_SEARCH_PADDING_TILES

    best_freq: int | None = None
    best_power: float | None = None

    for ti, bins in tiles.items():
        if ti < lo or ti > hi:
            continue
        if not bins:
            continue
        freq, power = max(bins, key=lambda x: x[1])
        if FM_RANGE[0] <= freq <= FM_RANGE[1]:
            continue
        if DVBT_RANGE[0] <= freq <= DVBT_RANGE[1]:
            continue
        tile_offset = freq - tile_center_hz(ti)
        if abs(tile_offset - dominant_offset) < EMITTER_OFFSET_MIN_DEVIATION_HZ:
            continue
        if power < power_threshold:
            continue
        if power < EMITTER_MIN_ABS_POWER_DBFS:
            continue
        if best_power is None or power > best_power:
            best_power = power
            best_freq = freq

    return (best_freq, best_power)


# ─── Main processing ─────────────────────────────────────
def fetch_full_sweep_list(since: str | None, sweep_id: str | None) -> list[dict]:
    where = "preset='full'"
    if sweep_id:
        where += f" AND sweep_id = '{sweep_id}'"
    elif since:
        where += f" AND timestamp >= toDateTime64('{since}', 3)"

    q = f"""
    SELECT sweep_id, timestamp, worst_clip_freq_hz, clipped_captures, max_clip_fraction
    FROM sweep_health
    WHERE {where}
    ORDER BY timestamp
    """
    return ch_query_json(q)


def fetch_sweep_bins(sweep_id: str) -> list[dict]:
    q = f"""
    SELECT freq_hz, power_dbfs
    FROM scans
    WHERE sweep_id = '{sweep_id}'
    ORDER BY freq_hz
    """
    return ch_query_json(q)


def fetch_hourly_baseline(hour_iso: str) -> dict[int, float]:
    """Fetch per-bin baseline from the PREVIOUS hour (not the sweep's own hour).

    Self-pollution: when a compression event occurs in a sweep at hour H, that
    sweep contributes to hourly_baseline[H]. With ~5 full sweeps per hour a
    single compression event shifts the H-hour baseline by ~20% of the
    compression depth, blunting the depression metric. Using H-1 avoids this.

    Fallback to same-hour if previous hour is empty (e.g. first hour of data).
    """
    # hourly_baseline is AggregatingMergeTree; must avgMerge the aggregate state
    q_prev = f"""
    SELECT freq_hz, avgMerge(avg_power) AS avg_p
    FROM hourly_baseline
    WHERE hour = toStartOfHour(toDateTime64('{hour_iso}', 3)) - INTERVAL 1 HOUR
    GROUP BY freq_hz
    """
    rows = ch_query_json(q_prev)
    if rows:
        return {int(r["freq_hz"]): float(r["avg_p"]) for r in rows}

    # Fallback: same-hour baseline
    q_same = f"""
    SELECT freq_hz, avgMerge(avg_power) AS avg_p
    FROM hourly_baseline
    WHERE hour = toStartOfHour(toDateTime64('{hour_iso}', 3))
    GROUP BY freq_hz
    """
    rows = ch_query_json(q_same)
    return {int(r["freq_hz"]): float(r["avg_p"]) for r in rows}


def group_bins_by_tile(rows: list[dict]) -> dict[int, list[tuple[int, float]]]:
    out: dict[int, list[tuple[int, float]]] = {}
    for r in rows:
        f = int(r["freq_hz"])
        p = float(r["power_dbfs"])
        ti = tile_idx_for(f)
        out.setdefault(ti, []).append((f, p))
    return out


def per_tile_argmax_offsets(tiles: dict[int, list[tuple[int, float]]]) -> tuple[list[int], list[float], list[int]]:
    """For each tile index present, return (argmax_offset_hz, peak_power_dbfs, tile_idx).

    Returned in tile-index order. Returns three parallel arrays.
    """
    sorted_idx = sorted(tiles.keys())
    offsets: list[int] = []
    powers: list[float] = []
    for ti in sorted_idx:
        bins = tiles[ti]
        peak_freq, peak_power = max(bins, key=lambda x: x[1])
        offset = peak_freq - tile_center_hz(ti)
        offsets.append(offset)
        powers.append(peak_power)
    return offsets, powers, sorted_idx


def tier_from_sigs(s_spur: int, s_base: int, s_clip: int) -> str:
    n = s_spur + s_base + s_clip
    return ["none", "low", "medium", "high"][n]


@dataclass
class SweepResult:
    sweep_id: str
    timestamp: str
    spur: SpurInfo
    baseline: BaselineInfo
    clip: ClipInfo
    emitter_freq_hz: int | None
    emitter_power_dbfs: float | None

    @property
    def tier(self) -> str:
        # match_tier uses only the 3 aggregated sigs (spur + baseline + clip).
        # sig_clip_fm is observability-only; do NOT aggregate it.
        return tier_from_sigs(self.spur.sig, self.baseline.sig, self.clip.sig)


def process_sweep(sh_row: dict) -> SweepResult:
    sweep_id = sh_row["sweep_id"]
    timestamp = sh_row["timestamp"]

    bins = fetch_sweep_bins(sweep_id)
    tiles = group_bins_by_tile(bins)
    offsets, powers, sorted_idx = per_tile_argmax_offsets(tiles)
    spur = detect_spur_comb(offsets, powers)
    # Map spur block from array index back to tile_idx
    if spur.sig:
        spur.block_tile_lo = sorted_idx[spur.block_tile_lo]
        spur.block_tile_hi = sorted_idx[spur.block_tile_hi]

    sweep_power_by_freq = {int(r["freq_hz"]): float(r["power_dbfs"]) for r in bins}
    baseline_power_by_freq = fetch_hourly_baseline(timestamp)
    baseline = detect_baseline_depression(sweep_power_by_freq, baseline_power_by_freq)

    clip = detect_clip(sh_row)

    emitter_freq, emitter_power = estimate_emitter_freq(tiles, spur, spur.block_median_power_dbfs)

    return SweepResult(sweep_id, timestamp, spur, baseline, clip, emitter_freq, emitter_power)


# ─── Output ──────────────────────────────────────────────
def insert_events(results: list[SweepResult], min_tier: str = "medium") -> int:
    tier_rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
    threshold = tier_rank[min_tier]
    keep = [r for r in results if tier_rank[r.tier] >= threshold]
    if not keep:
        return 0

    rows_json = "\n".join(
        json.dumps({
            "sweep_id": r.sweep_id,
            "timestamp": r.timestamp,
            # Nullable columns — json.dumps emits null for None, matching ClickHouse's
            # Nullable(UInt32)/Nullable(Float32). v2 deliberately refuses to fabricate
            # attribution when no peak qualifies inside the compression zone.
            "estimated_emitter_freq_hz": r.emitter_freq_hz,
            "estimated_emitter_power_dbfs": (None if r.emitter_power_dbfs is None
                                              else round(r.emitter_power_dbfs, 2)),
            "spur_offset_hz": int(r.spur.offset_mean_hz),
            "spur_block_tile_lo": int(r.spur.block_tile_lo),
            "spur_block_tile_hi": int(r.spur.block_tile_hi),
            "spur_block_tile_count": int(r.spur.block_count),
            "spur_offset_stddev_khz": round(float(r.spur.offset_stddev_khz), 2),
            "baseline_depression_db": float(r.baseline.depression_db),
            "baseline_bins_sampled": int(r.baseline.bins_sampled),
            "worst_clip_freq_hz": int(r.clip.worst_clip_freq_hz),
            "clipped_captures": int(r.clip.clipped_captures),
            "sig_spur": int(r.spur.sig),
            "sig_baseline": int(r.baseline.sig),
            "sig_clip": int(r.clip.sig),
            "sig_clip_fm": int(r.clip.sig_fm),
            "match_tier": r.tier,
            "detector_version": DETECTOR_VERSION,
        })
        for r in keep
    )

    params = f"user={CH_USER}&password={CH_PASSWORD}&database={CH_DB}"
    q = "INSERT INTO compression_events FORMAT JSONEachRow"
    url = f"{CH_URL}/?{params}&query={quote(q)}"
    req = Request(url, data=rows_json.encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    urlopen(req, timeout=60).read()
    return len(keep)


# ─── CLI ─────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--backfill", action="store_true", help="process all full sweeps")
    g.add_argument("--since", type=str, metavar="TS", help="process sweeps since this timestamp (UTC ISO)")
    g.add_argument("--sweep", type=str, metavar="SWEEP_ID", help="process a single sweep_id")
    ap.add_argument("--dry-run", action="store_true", help="compute + print, do not INSERT")
    ap.add_argument("--min-tier", choices=["low", "medium", "high"], default="medium",
                    help="minimum match_tier to persist (default: medium)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    sweeps = fetch_full_sweep_list(
        since=args.since, sweep_id=args.sweep,
    ) if not args.backfill else fetch_full_sweep_list(since=None, sweep_id=None)

    log.info(f"Processing {len(sweeps)} full sweep(s)")

    results: list[SweepResult] = []
    tier_counts = {"none": 0, "low": 0, "medium": 0, "high": 0}
    for i, sh in enumerate(sweeps):
        if (i + 1) % 50 == 0:
            log.info(f"  {i + 1}/{len(sweeps)}  (tiers so far: {tier_counts})")
        try:
            r = process_sweep(sh)
        except Exception as e:
            log.warning(f"  sweep {sh['sweep_id']} failed: {e}")
            continue
        tier_counts[r.tier] += 1
        results.append(r)

    log.info(f"Tier counts: {tier_counts}")

    # Print notable events
    for r in results:
        if r.tier in ("medium", "high"):
            if r.emitter_freq_hz is not None:
                em_str = f"emitter={r.emitter_freq_hz / 1e6:.3f} MHz @ {r.emitter_power_dbfs:.1f} dBFS"
            else:
                em_str = "emitter=UNKNOWN (no peak qualifies)"
            log.info(
                f"  {r.timestamp} [{r.tier}] "
                f"sigs=({r.spur.sig},{r.baseline.sig},{r.clip.sig},fm={r.clip.sig_fm}) "
                f"{em_str} "
                f"spur_block=[{r.spur.block_tile_lo}..{r.spur.block_tile_hi}] "
                f"n={r.spur.block_count} off={r.spur.offset_mean_hz/1000:.1f}kHz "
                f"median_pwr={r.spur.block_median_power_dbfs:.1f}dBFS "
                f"depression={r.baseline.depression_db:.1f}dB"
            )

    if args.dry_run:
        log.info("Dry-run: skipping INSERT.")
        return

    n_inserted = insert_events(results, min_tier=args.min_tier)
    log.info(f"Inserted {n_inserted} event(s) to compression_events (min_tier={args.min_tier})")


if __name__ == "__main__":
    main()
