#!/usr/bin/env python3
"""
rf_luv — Classifier health monitor

Writes one row per run to spectrum.classifier_health capturing:
  * Precision sentinels (catches fd5016e regression — Float32 wobble)
  * Harmonic filter sentinels (catches 2b12a70 regression — cross-alloc FPs)
  * Burst filter sentinels (catches 43744b6 regression — single-sweep bursts)
  * Distribution shape (unknowns ratio, mean confidence, class histogram)
  * Known-good acceptance alignment (8 reference bins from step-3 acceptance)
  * Pipeline liveness (seconds since last sweep / features / classification)

No alerting here — visibility only. Grafana reads this table.
Scheduler: systemd user timer at *:0/5:45 (45 s after classifier's *:0/5:30).
stdlib only; matches classifier.py / feature_extractor.py posture.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone

import db

# ─── Config ─────────────────────────────────────────────────

FREQ_MATCH_TOLERANCE_HZ = 150_000
ATIS_CLASS_ID = "am_airband_atis"  # for the dedicated atis_confidence_current metric

# Reference bins for the known-good check come from spectrum.known_frequencies
# (rows whose class_id is a canonical signal_classes value). Each row carries
# its own min_confidence (migration 010). Hardcoded list retired 2026-04-18
# after the sanity check found the ATIS target (136_254_000) was pointing at
# the wrong bin — the canonical 136_125_000 resolves through ±150 kHz
# tolerance to the 136.034 MHz bin that actually carries ATIS here.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("classifier_health")


# ─── Metric computation ─────────────────────────────────────


def latest_run_stats(latest_classified_at: str) -> dict:
    """Aggregate stats over the classifier's latest batch."""
    rows = db.query_rows(
        f"SELECT count() AS total, "
        f"  uniqExact(confidence) AS distinct_confs, "
        f"  countIf(abs(confidence - round(confidence, 1)) > 0.001) AS precision_tails, "
        f"  avg(confidence) AS mean_conf, "
        f"  countIf(class_id LIKE 'unknown_%' OR confidence < 0.5) AS unknowns_count "
        f"FROM spectrum.signal_classifications FINAL "
        f"WHERE classified_at = toDateTime('{latest_classified_at}')"
    )
    return rows[0] if rows else {}


def class_distribution(latest_classified_at: str) -> dict[str, int]:
    rows = db.query_rows(
        f"SELECT class_id, count() AS n "
        f"FROM spectrum.signal_classifications FINAL "
        f"WHERE classified_at = toDateTime('{latest_classified_at}') "
        f"GROUP BY class_id ORDER BY n DESC"
    )
    return {r["class_id"]: int(r["n"]) for r in rows}


def harmonic_flag_counts() -> tuple[int, int]:
    """(total flagged in last 1h, cross-allocation flagged in last 1h)."""
    total = int(db.query_scalar(
        "SELECT count() FROM spectrum.peak_features FINAL "
        "WHERE harmonic_of_hz IS NOT NULL AND computed_at > now() - INTERVAL 1 HOUR"
    ) or 0)
    # CROSS JOIN with WHERE — ClickHouse's JOIN analyzer rejects BETWEEN in ON,
    # but the pre-analyzer comma-join form works.
    xalloc = int(db.query_scalar(
        "SELECT count() FROM "
        "(SELECT freq_hz, harmonic_of_hz FROM spectrum.peak_features FINAL "
        " WHERE harmonic_of_hz IS NOT NULL AND computed_at > now() - INTERVAL 1 HOUR) pf, "
        "spectrum.allocations AS a1, spectrum.allocations AS a2 "
        "WHERE pf.freq_hz BETWEEN a1.freq_start_hz AND a1.freq_end_hz "
        "  AND pf.harmonic_of_hz BETWEEN a2.freq_start_hz AND a2.freq_end_hz "
        "  AND a1.service != a2.service"
    ) or 0)
    return total, xalloc


def continuous_with_bursts_count() -> int:
    return int(db.query_scalar(
        "SELECT count() FROM spectrum.peak_features FINAL "
        "WHERE duty_cycle_24h > 0.85 AND burst_p50_s IS NOT NULL"
    ) or 0)


def liveness_seconds() -> tuple[float, float, float]:
    """Age in seconds of latest row in each of (classifications, features, scans)."""
    sec_classif = float(db.query_scalar(
        "SELECT dateDiff('second', max(classified_at), now()) "
        "FROM spectrum.signal_classifications FINAL"
    ) or 0)
    sec_features = float(db.query_scalar(
        "SELECT dateDiff('second', max(computed_at), now()) "
        "FROM spectrum.peak_features FINAL"
    ) or 0)
    sec_scans = float(db.query_scalar(
        "SELECT dateDiff('second', max(timestamp), now()) FROM spectrum.scans"
    ) or 0)
    return sec_classif, sec_features, sec_scans


def best_classification_in_tolerance(
    target_freq_hz: int, expected_class: str
) -> dict | None:
    """Return the best-matching classification within ±150 kHz of target_freq_hz.

    Ordering asks the right question for a known-good check: does the system
    see the expected signal class anywhere in tolerance? Falls back to the
    nearest-bin semantics only if no bin in tolerance matches the expected
    class, so failing_json still records the class actually being seen.

    ORDER BY:
      1. expected-class match first (DESC on boolean so TRUE=1 wins)
      2. highest confidence among matches
      3. nearest bin among ties

    Earlier "nearest only" semantics flapped on ATIS because the
    geometrically-closest bin (136.154) occasionally caught adjacent ATC
    activity, even though 136.034 always carried ATIS cleanly. The new
    ordering reports the ATIS classification from whichever in-tolerance
    bin actually represents it.
    """
    lo = target_freq_hz - FREQ_MATCH_TOLERANCE_HZ
    hi = target_freq_hz + FREQ_MATCH_TOLERANCE_HZ
    # Escape single quotes defensively — class_ids are schema-controlled
    # ('am_airband_atis', 'dvbt_mux', …) but keep the substitution safe.
    safe_class = expected_class.replace("'", "''")
    rows = db.query_rows(
        f"SELECT freq_hz, class_id, confidence "
        f"FROM spectrum.signal_classifications FINAL "
        f"WHERE freq_hz BETWEEN {lo} AND {hi} "
        f"ORDER BY (class_id = '{safe_class}') DESC, "
        f"         confidence DESC, "
        f"         abs(toInt64(freq_hz) - toInt64({target_freq_hz})) ASC "
        f"LIMIT 1"
    )
    return rows[0] if rows else None


def load_reference_bins() -> list[dict]:
    """Pull reference bins from spectrum.known_frequencies, restricted to rows
    whose class_id maps to a canonical signal_classes entry (skip legacy
    danglers like 'satcom'/'ism'/'broadcast' that don't match any class).

    Excludes unknown_* fallback classes — they're not real-signal targets."""
    return db.query_rows(
        "SELECT freq_hz, name, class_id, min_confidence "
        "FROM spectrum.known_frequencies "
        "WHERE class_id IN ("
        "  SELECT class_id FROM spectrum.signal_classes "
        "  WHERE class_id NOT LIKE 'unknown_%'"
        ") "
        "ORDER BY freq_hz"
    )


def known_good_assessment() -> tuple[int, list[dict], float | None, int]:
    """For every canonical-class row in known_frequencies, resolve the nearest
    classification within ±150 kHz and compare to the row's min_confidence.

    Returns (passing, failing, atis_conf, total). ATIS confidence is tracked
    from the nearest-bin match on the am_airband_atis row specifically —
    preserves the atis_confidence_current metric's shape for Grafana.

    No-data bins count as passing per spec (so 'sweeper hasn't run yet'
    doesn't look like a regression)."""
    refs = load_reference_bins()
    passing = 0
    failing: list[dict] = []
    atis_conf: float | None = None
    for r in refs:
        target_freq = int(r["freq_hz"])
        expected_class = r["class_id"]
        min_conf = float(r["min_confidence"])
        name = r["name"]
        match = best_classification_in_tolerance(target_freq, expected_class)
        if match is None:
            passing += 1
            failing.append({"name": name, "status": "no_data"})
            continue
        cls = match["class_id"]
        conf = float(match["confidence"])
        if expected_class == ATIS_CLASS_ID and atis_conf is None:
            atis_conf = conf
        if cls == expected_class and conf >= min_conf:
            passing += 1
        else:
            failing.append({
                "name": name,
                "freq_mhz": round(target_freq / 1e6, 3),
                "class_id": cls,
                "confidence": round(conf, 2),
                "expected_class": expected_class,
                "min_conf": min_conf,
            })
    return passing, failing, atis_conf, len(refs)


# ─── Main ───────────────────────────────────────────────────


def main() -> None:
    t0 = time.monotonic()
    log.info(f"classifier_health starting (ClickHouse at {CH_HOST}:{CH_PORT})")

    latest_classified_at = db.query_scalar(
        "SELECT toString(max(classified_at)) FROM spectrum.signal_classifications FINAL"
    )
    if not latest_classified_at or latest_classified_at == "1970-01-01 00:00:00":
        log.error("No classifications present — classifier hasn't run yet")
        sys.exit(1)

    stats = latest_run_stats(latest_classified_at)
    class_dist = class_distribution(latest_classified_at)
    h_total, h_xalloc = harmonic_flag_counts()
    cwb = continuous_with_bursts_count()
    sec_classif, sec_features, sec_scans = liveness_seconds()
    passing, failing, atis_conf, known_good_total = known_good_assessment()

    total = int(stats.get("total", 0))
    unknowns_ratio = (
        int(stats.get("unknowns_count", 0)) / total if total > 0 else 0.0
    )

    row = {
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "total_classifications": total,
        "classifier_runtime_seconds": None,  # see module docstring / migration 009
        "confidence_distinct_values": int(stats.get("distinct_confs", 0)),
        "confidence_precision_tail_count": int(stats.get("precision_tails", 0)),
        "harmonic_flags_total": h_total,
        "harmonic_flags_cross_allocation": h_xalloc,
        "atis_confidence_current": float(atis_conf) if atis_conf is not None else 0.0,
        "continuous_signals_with_bursts": cwb,
        "class_distribution_json": json.dumps(class_dist, separators=(",", ":")),
        "unknowns_ratio": round(unknowns_ratio, 4),
        "confidence_mean": round(float(stats.get("mean_conf", 0.0)), 4),
        "known_good_passing": passing,
        "known_good_total": known_good_total,
        "known_good_failing_json": json.dumps(failing, separators=(",", ":")),
        "seconds_since_last_classification": sec_classif,
        "seconds_since_last_peak_features": sec_features,
        "seconds_since_last_sweep": sec_scans,
    }

    db.insert("spectrum.classifier_health", [row])

    elapsed = time.monotonic() - t0
    log.info(
        f"Wrote 1 health row in {elapsed:.2f}s | "
        f"passing={passing}/{known_good_total} | "
        f"unknowns_ratio={unknowns_ratio:.3f} | "
        f"sentinels: tails={row['confidence_precision_tail_count']}, "
        f"xalloc={h_xalloc}, cwb={cwb}, "
        f"distinct_conf={row['confidence_distinct_values']}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("classifier_health failed")
        sys.exit(1)
