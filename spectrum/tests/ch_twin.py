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
        cell = groups[(h["dongle_id"], h["preset"], hour, int(round(h["gain_db"] * 10)))]
        cell[0] += 1
        cell[1] += int(h["clipped"])
    return [{"dongle_id": d, "preset": p, "hour": hour, "gain_tenths": g, "sweeps": s, "clipped_sweeps": c}
            for (d, p, hour, g), (s, c) in sorted(groups.items())]


def _sweep_meta(sweep_health: list[dict]) -> dict:
    return {(h["dongle_id"], h["sweep_id"]): (int(round(h["gain_db"] * 10)), int(h["clipped"]))
            for h in sweep_health}


def _index(p: dict, footprints: list[tuple[str, int, int]]) -> int:
    for i, (dongle, lo, hi) in enumerate(footprints, start=1):
        if dongle == p["dongle_id"] and lo <= p["freq_hz"] <= hi:
            return i
    return 0


def activity(peaks: list[dict], sweep_health: list[dict],
             footprints: list[tuple[str, int, int]], split_at: int) -> list[dict]:
    """emitters.activity_sql()"""
    meta = _sweep_meta(sweep_health)
    groups = defaultdict(list)
    for p in peaks:
        key = (p["dongle_id"], p["sweep_id"])
        idx = _index(p, footprints)
        if idx == 0 or key not in meta:
            continue
        ts = _ts(p["timestamp"])
        gain, clip = meta[key]
        cell = (idx, p["timestamp"].astimezone(ATHENS).hour, gain, clip, int(ts >= split_at))
        groups[cell].append(p)
    out = []
    for (idx, hod, gain, clip, recent), rows in sorted(groups.items()):
        out.append({
            "idx": idx, "hod": hod, "gain_tenths": gain, "clipped": clip, "recent": recent,
            "hits": len({r["sweep_id"] for r in rows}),
            "first_seen": min(_ts(r["timestamp"]) for r in rows),
            "last_seen": max(_ts(r["timestamp"]) for r in rows),
            "power_max": max(r["power_dbfs"] for r in rows),
            "power_sum": sum(r["power_dbfs"] for r in rows),
            "power_n": len(rows),
        })
    return out


def levels(peaks: list[dict], sweep_health: list[dict],
           footprints: list[tuple[str, int, int]], split_at: int) -> list[dict]:
    """anomalies.levels_sql(): clean sweeps only, the strongest bin per sweep."""
    meta = {(h["dongle_id"], h["sweep_id"]): h for h in sweep_health}
    per_sweep = {}
    for p in peaks:
        h = meta.get((p["dongle_id"], p["sweep_id"]))
        idx = _index(p, footprints)
        if h is None or h["clipped"] or idx == 0:
            continue
        ts = _ts(p["timestamp"])
        cell = per_sweep.setdefault((idx, p["sweep_id"]), {
            "gain_tenths": int(round(h["gain_db"] * 10)), "recent": int(ts >= split_at),
            "power": p["power_dbfs"], "ts": ts})
        cell["power"] = max(cell["power"], p["power_dbfs"])
        cell["ts"] = max(cell["ts"], ts)
    groups = defaultdict(list)
    for (idx, _), cell in per_sweep.items():
        groups[(idx, cell["gain_tenths"], cell["recent"])].append(cell)
    out = []
    for (idx, gain, recent), cells in sorted(groups.items()):
        powers = [c["power"] for c in cells]
        mean = sum(powers) / len(powers)
        out.append({
            "idx": idx, "gain_tenths": gain, "recent": recent, "n": len(cells),
            "mean": round(mean, 2),
            "std": round((sum((x - mean) ** 2 for x in powers) / len(powers)) ** 0.5, 2),
            "last_at": max(c["ts"] for c in cells),
        })
    return out


def occupancy(peaks: list[dict], sweep_health: list[dict]) -> list[dict]:
    """The occupancy_hourly view (migration 029), when every sweep wrote all of its preset's bins."""
    import sweep_grid
    bands = {p: sorted({f // 1_000_000 for f in sweep_grid.bin_labels(p)}) for p in sweep_grid.PRESETS}
    swept = defaultdict(int)
    for h in sweep_health:
        hour = emitters.floor_hour(_ts(h["timestamp"]))
        for band in bands[h["preset"]]:
            swept[(h["dongle_id"], band, hour)] += 1
    active = defaultdict(set)
    for p in peaks:
        active[(p["dongle_id"], p["freq_hz"] // 1_000_000, emitters.floor_hour(_ts(p["timestamp"])))].add(p["sweep_id"])
    return [{"dongle_id": d, "band_mhz": b, "hour": hour, "swept_sweeps": s,
             "active_sweeps": len(active[(d, b, hour)]), "active_pct": round(100 * len(active[(d, b, hour)]) / s, 1)}
            for (d, b, hour), s in sorted(swept.items())]
