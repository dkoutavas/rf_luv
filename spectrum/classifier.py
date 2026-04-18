#!/usr/bin/env python3
"""
rf_luv — Rule-based signal classifier (step 3 of 3)

For every bin with recent peak_features, classify it against
spectrum.signal_classes.evidence_rules. Operator confirmations in
listening_log (within 150 kHz tolerance) are hard overrides.
Produces one row per freq_hz in spectrum.signal_classifications with:
  class_id, confidence, reasoning (JSON score trace), features_snapshot.

Scheduler: systemd user timer on leap (ops/spectrum-classifier/), fires
30 s after the feature_extractor timer so fresh features are available.

Design choices:
  * stdlib only; matches scan_ingest / feature_extractor dependency posture.
  * All reference data loaded once per run (listening_log, allocations,
    known_frequencies, signal_classes) — tiny tables, negligible I/O.
  * Peak matching uses tolerance-based frequency comparison (≤150 kHz)
    because scanner bins don't land on canonical service frequencies —
    documented reality from step 2 validation.
  * Scoring weights follow the spec. When acceptance cases produce
    unexpected classifications, the reasoning column contains the full
    score trace so tuning is visible via SQL inspection.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
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

FREQ_MATCH_TOLERANCE_HZ = 150_000
DUTY_CONTINUOUS_THRESHOLD = 0.5    # duty_24h > this → "continuous"
DUTY_BURSTY_HIGH_MIN = 0.1         # duty_24h in [this, continuous) → "bursty_high"

BATCH_INSERT_LIMIT = 500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("classifier")


# ─── ClickHouse helpers ─────────────────────────────────────


def ch_call(query: str, data: str | None = None, timeout: int = 60) -> str:
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
    out = ch_call(sql + " FORMAT JSONEachRow")
    return [json.loads(line) for line in out.splitlines() if line]


# ─── Helpers ────────────────────────────────────────────────


def derive_duty_pattern(duty_24h: float) -> str:
    """Spec's derivation: >0.5 continuous, [0.1,0.5] bursty_high, <0.1 bursty_low."""
    if duty_24h > DUTY_CONTINUOUS_THRESHOLD:
        return "continuous"
    if duty_24h >= DUTY_BURSTY_HIGH_MIN:
        return "bursty_high"
    return "bursty_low"


def find_nearest(freq_hz: int, items: list[dict], key: str = "freq_hz") -> dict | None:
    """Return the item whose item[key] is closest to freq_hz within tolerance, else None."""
    best = None
    best_dist = FREQ_MATCH_TOLERANCE_HZ + 1
    for it in items:
        d = abs(int(it[key]) - freq_hz)
        if d <= FREQ_MATCH_TOLERANCE_HZ and d < best_dist:
            best_dist = d
            best = it
    return best


def find_allocation(freq_hz: int, allocations: list[dict]) -> dict | None:
    """Range-lookup: return the allocation whose [start, end) covers freq_hz."""
    for a in allocations:
        start = int(a["freq_start_hz"])
        end = int(a["freq_end_hz"])
        if start <= freq_hz < end:
            return a
    return None


# ─── Scoring ────────────────────────────────────────────────


def score_class(
    feat: dict,
    candidate_class_id: str,
    rules: dict,
    alloc: dict | None,
    kf_match: dict | None,
    derived_pattern: str,
) -> tuple[float, dict]:
    """Score a candidate signal_class against one peak's features.

    Weights (per spec):
      + bw_hz in range: +2
      + duty_pattern match: +2
      + duty_24h_range hit: +1
      + duty_24h_min hit: +1
      + burst_p50_s_range hit: +1
      + center_freq_hz_near match: +3
      + requires_allocation_in match: +2  (miss: −4, disqualifying)
      + known_frequencies class_id == candidate: +3
      + harmonic_of_hz set AND candidate not unknown_*: −3
    """
    score = 0.0
    trace: dict = {}

    # bandwidth
    if "bw_hz" in rules:
        lo, hi = rules["bw_hz"]
        bw = feat.get("bandwidth_hz")
        if bw is not None and lo <= bw <= hi:
            score += 2
            trace["bw_ok"] = [lo, bw, hi]
        else:
            trace["bw_miss"] = [lo, bw, hi]

    # duty pattern
    if "duty_pattern" in rules:
        if derived_pattern in rules["duty_pattern"]:
            score += 2
            trace["pattern_ok"] = derived_pattern
        else:
            trace["pattern_miss"] = [derived_pattern, rules["duty_pattern"]]

    # duty_24h range
    duty_24h = feat["duty_cycle_24h"]
    if "duty_24h_range" in rules:
        lo, hi = rules["duty_24h_range"]
        if lo <= duty_24h <= hi:
            score += 1
            trace["duty_range_ok"] = duty_24h
        else:
            trace["duty_range_miss"] = [lo, duty_24h, hi]

    # duty_24h min
    if "duty_24h_min" in rules:
        if duty_24h >= rules["duty_24h_min"]:
            score += 1
            trace["duty_min_ok"] = duty_24h
        else:
            trace["duty_min_miss"] = [rules["duty_24h_min"], duty_24h]

    # burst p50 range (only meaningful if NOT continuous)
    if "burst_p50_s_range" in rules:
        b50 = feat.get("burst_p50_s")
        lo, hi = rules["burst_p50_s_range"]
        if b50 is not None and lo <= b50 <= hi:
            score += 1
            trace["burst_ok"] = b50

    # center freq proximity (class-specific, e.g. AIS at 161.975/162.025)
    if "center_freq_hz_near" in rules:
        tol = rules.get("center_freq_tolerance_hz", FREQ_MATCH_TOLERANCE_HZ)
        if any(abs(feat["freq_hz"] - cf) <= tol for cf in rules["center_freq_hz_near"]):
            score += 3
            trace["center_freq_ok"] = True

    # allocation containment
    if "requires_allocation_in" in rules:
        required = rules["requires_allocation_in"]
        if alloc and alloc["service"] in required:
            score += 2
            trace["alloc_ok"] = alloc["service"]
        else:
            score -= 4
            trace["alloc_miss"] = alloc["service"] if alloc else None

    # known_frequencies prior (only scores for the matching class)
    if kf_match and kf_match.get("class_id") == candidate_class_id:
        score += 3
        trace["kf_match"] = kf_match.get("name", "")

    # harmonic demotion — real-transmitter classes lose points if this bin
    # is a 2x/3x/4x of a stronger base. unknown_* classes are spared.
    if feat.get("harmonic_of_hz") is not None:
        if not candidate_class_id.startswith("unknown_"):
            score -= 3
            trace["harmonic_penalty"] = feat["harmonic_of_hz"]

    return score, trace


def classify_peak(
    feat: dict,
    confirmations: list[dict],
    allocations: list[dict],
    known_freqs: list[dict],
    signal_classes: list[dict],
) -> tuple[str, float, dict]:
    """Return (class_id, confidence, reasoning_dict) for one peak_features row."""
    freq = int(feat["freq_hz"])

    # Step 1: operator-confirmation override
    for c in confirmations:
        if abs(int(c["confirmed_freq_hz"]) - freq) <= FREQ_MATCH_TOLERANCE_HZ:
            return c["class_id"], 1.0, {
                "override": "operator-confirmed",
                "listening_log_id": c["id"],
                "confirmed_freq_hz": c["confirmed_freq_hz"],
            }

    # Step 2: allocation lookup (one service per freq by spec — overlaps are bugs)
    alloc = find_allocation(freq, allocations)

    # Step 3: nearest known_frequencies entry within tolerance
    kf_match = find_nearest(freq, known_freqs)

    # Step 4: score every candidate class
    derived_pattern = derive_duty_pattern(feat["duty_cycle_24h"])
    scores: dict[str, float] = {}
    traces: dict[str, dict] = {}
    for sc in signal_classes:
        class_id = sc["class_id"]
        rules = sc.get("_rules", {})  # pre-parsed in main()
        score, trace = score_class(
            feat, class_id, rules, alloc, kf_match, derived_pattern
        )
        scores[class_id] = score
        traces[class_id] = trace

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    top_cls, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else float("-inf")
    top_trace = traces[top_cls]

    # Step 5: confidence tiers (per spec)
    if top_score >= 6 and top_score - second_score >= 2:
        confidence = 0.8
    elif top_score >= 4 and top_score - second_score >= 1:
        confidence = 0.6
    elif top_score >= 3:
        confidence = 0.4
    else:
        # Fall back to unknown bucket by derived pattern
        top_cls = (
            "unknown_continuous" if derived_pattern == "continuous" else "unknown_bursty"
        )
        top_score = scores.get(top_cls, top_score)
        top_trace = traces.get(top_cls, top_trace)
        confidence = 0.2

    reasoning = {
        "derived_pattern": derived_pattern,
        "alloc": alloc["service"] if alloc else None,
        "kf_match": {"name": kf_match["name"], "class_id": kf_match["class_id"]}
        if kf_match
        else None,
        "top": [top_cls, top_score],
        "second": [ranked[1][0] if len(ranked) > 1 else None, second_score],
        "trace": top_trace,
    }
    return top_cls, confidence, reasoning


# ─── Main ───────────────────────────────────────────────────


def main() -> None:
    t0 = datetime.now(timezone.utc)
    log.info(f"Classifier starting (ClickHouse at {CH_HOST}:{CH_PORT})")

    # Reference data — all tiny
    confirmations = ch_rows(
        "SELECT id, class_id, toUInt32(confirmed_freq_hz) AS confirmed_freq_hz "
        "FROM spectrum.listening_log "
        "WHERE class_id != '' AND confirmed_freq_hz != 0 "
        "ORDER BY timestamp DESC"
    )
    allocations = ch_rows("SELECT * FROM spectrum.allocations ORDER BY freq_start_hz")
    known_freqs = ch_rows(
        "SELECT toUInt32(freq_hz) AS freq_hz, name, class_id "
        "FROM spectrum.known_frequencies"
    )
    # signal_classes is a plain MergeTree (FINAL would illegal-final here).
    # ALTER TABLE ... UPDATE from migration 006 applies lazily on merge;
    # reads are eventually consistent, which is fine for a reference table.
    signal_classes = ch_rows(
        "SELECT class_id, evidence_rules FROM spectrum.signal_classes"
    )
    # Parse evidence_rules JSON once
    for sc in signal_classes:
        raw = sc.get("evidence_rules") or "{}"
        try:
            sc["_rules"] = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(f"signal_classes.{sc['class_id']} has invalid evidence_rules JSON")
            sc["_rules"] = {}
    log.info(
        f"Loaded {len(confirmations)} confirmations, "
        f"{len(allocations)} allocations, {len(known_freqs)} known freqs, "
        f"{len(signal_classes)} signal classes"
    )

    # Recent peak_features — use FINAL so we get one authoritative row per freq
    features = ch_rows(
        "SELECT freq_hz, bandwidth_hz, duty_cycle_1h, duty_cycle_24h, duty_cycle_7d, "
        "       burst_p50_s, burst_p95_s, harmonic_of_hz, "
        "       power_mean_dbfs, power_p95_dbfs, power_std_db, sweeps_observed_24h "
        "FROM spectrum.peak_features FINAL "
        "WHERE computed_at > now() - INTERVAL 1 HOUR"
    )
    if not features:
        log.info("No fresh peak_features in the last 1h, nothing to classify")
        return
    log.info(f"Classifying {len(features)} peaks")

    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    rows_out: list[dict] = []
    hist: dict[str, int] = {}
    for feat in features:
        cls, conf, reasoning = classify_peak(
            feat, confirmations, allocations, known_freqs, signal_classes
        )
        hist[cls] = hist.get(cls, 0) + 1
        rows_out.append({
            "freq_hz": int(feat["freq_hz"]),
            "class_id": cls,
            "confidence": round(float(conf), 3),
            "reasoning": json.dumps(reasoning, separators=(",", ":")),
            "features_snapshot": json.dumps(feat, separators=(",", ":")),
            "classified_at": now_str,
        })

    # Batch insert
    for i in range(0, len(rows_out), BATCH_INSERT_LIMIT):
        chunk = rows_out[i : i + BATCH_INSERT_LIMIT]
        payload = "\n".join(json.dumps(r) for r in chunk)
        ch_call(
            "INSERT INTO spectrum.signal_classifications FORMAT JSONEachRow",
            data=payload,
        )

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    hist_str = ", ".join(f"{k}={v}" for k, v in sorted(hist.items(), key=lambda x: -x[1]))
    log.info(f"Wrote {len(rows_out)} classifications in {elapsed:.1f}s | {hist_str}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Classifier failed")
        sys.exit(1)
