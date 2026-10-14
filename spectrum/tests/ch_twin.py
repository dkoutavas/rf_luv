"""Python twins of the ClickHouse aggregations used by emitters.py and anomalies.py.

CI has no ClickHouse. These functions compute, from the synthetic fixture's
rows, what each SQL query returns, so the decision logic downstream can be
tested on realistic data. test_clickhouse_sql.py runs the real SQL against a
throwaway server and checks that it returns the same rows as these twins.

Times come back as unix seconds and hours of day in Europe/Athens, as in the SQL.
"""

from __future__ import annotations

from collections import defaultdict
from zoneinfo import ZoneInfo

import emitters

ATHENS = ZoneInfo(emitters.LOCAL_TZ)


def _ts(t) -> int:
    return int(t.timestamp())


def bin_stats(peaks: list[dict]) -> list[dict]:
    """emitters.bin_stats_sql()"""
    groups = defaultdict(list)
    for p in peaks:
        groups[(p["dongle_id"], p["freq_hz"])].append(p)
    out = []
    for (dongle, freq), rows in sorted(groups.items()):
        hits = len({r["sweep_id"] for r in rows})
        if hits < emitters.MIN_BIN_SWEEPS:
            continue
        out.append({
            "dongle_id": dongle, "freq_hz": freq, "hits": hits,
            "first_seen": min(_ts(r["timestamp"]) for r in rows),
            "last_seen": max(_ts(r["timestamp"]) for r in rows),
            "power_mean": round(sum(r["power_dbfs"] for r in rows) / len(rows), 2),
        })
    return out


def coverage(sweep_health: list[dict]) -> list[dict]:
    """emitters.coverage_sql()"""
    groups = defaultdict(lambda: [0, 0])
    for h in sweep_health:
        hour = emitters.floor_hour(_ts(h["timestamp"]))
        cell = groups[(h["dongle_id"], h["preset"], hour)]
        cell[0] += 1
        cell[1] += int(h["clipped"])
    return [{"dongle_id": d, "preset": p, "hour": hour, "sweeps": s, "clipped_sweeps": c}
            for (d, p, hour), (s, c) in sorted(groups.items())]


def _clipped_by_sweep(sweep_health: list[dict]) -> dict:
    return {(h["dongle_id"], h["sweep_id"]): int(h["clipped"]) for h in sweep_health}


def _index(p: dict, footprints: list[tuple[str, int, int]]) -> int:
    for i, (dongle, lo, hi) in enumerate(footprints, start=1):
        if dongle == p["dongle_id"] and lo <= p["freq_hz"] <= hi:
            return i
    return 0


def activity(peaks: list[dict], sweep_health: list[dict],
             footprints: list[tuple[str, int, int]], split_at: int) -> list[dict]:
    """emitters.activity_sql()"""
    clipped = _clipped_by_sweep(sweep_health)
    groups = defaultdict(list)
    for p in peaks:
        key = (p["dongle_id"], p["sweep_id"])
        idx = _index(p, footprints)
        if idx == 0 or key not in clipped:
            continue
        ts = _ts(p["timestamp"])
        cell = (idx, p["timestamp"].astimezone(ATHENS).hour, clipped[key], int(ts >= split_at))
        groups[cell].append(p)
    out = []
    for (idx, hod, clip, recent), rows in sorted(groups.items()):
        out.append({
            "idx": idx, "hod": hod, "clipped": clip, "recent": recent,
            "hits": len({r["sweep_id"] for r in rows}),
            "first_seen": min(_ts(r["timestamp"]) for r in rows),
            "last_seen": max(_ts(r["timestamp"]) for r in rows),
            "power_max": max(r["power_dbfs"] for r in rows),
            "power_sum": sum(r["power_dbfs"] for r in rows),
            "power_n": len(rows),
        })
    return out
