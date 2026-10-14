"""Run emitters.py's decision logic on the synthetic fixture, with no ClickHouse.

The SQL steps are replaced by their Python twins (ch_twin.py). Everything
else, from clustering to naming, is the real code.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import emitters
from tests import ch_twin
from tests import synth_sweeps as synth


def references(fixture: synth.Synth) -> emitters.References:
    """What emitters.load_references() would read from the fixture's tables."""
    log_rows = [{"freq_hz": r["confirmed_freq_hz"] or round(r["freq_mhz"] * 1e6),
                 "ts": int(r["timestamp"].timestamp()), "class_id": r["class_id"],
                 "label": emitters.operator_label(r["heard"], r["notes"], r["class_id"])}
                for r in fixture.listening_log]
    classifications: dict[str, list[dict]] = {}
    for c in fixture.signal_classifications:
        classifications.setdefault(c["dongle_id"], []).append(
            {"freq_hz": c["freq_hz"], "class_id": c["class_id"], "confidence": c["confidence"]})
    ranges = [(a["freq_start_hz"], a["freq_end_hz"] - 1, a["service"], "allocations")
              for a in fixture.allocations]
    ranges += [(c["freq_lo_hz"], c["freq_hi_hz"], c["service"], "signal_catalog")
               for c in fixture.signal_catalog]
    return emitters.References(
        listening_log=log_rows,
        known_frequencies=list(fixture.known_frequencies),
        classifications=classifications,
        class_names={c["class_id"]: c["name"] for c in fixture.signal_classes},
        ranges=ranges,
    )


def cut(fixture: synth.Synth, now: datetime) -> synth.Synth:
    """The fixture as the database would hold it at `now`: nothing from the future."""
    return synth.Synth(
        peaks=[p for p in fixture.peaks if p["timestamp"] < now],
        sweep_health=[h for h in fixture.sweep_health if h["timestamp"] < now],
        signal_classifications=fixture.signal_classifications,
        known_frequencies=fixture.known_frequencies,
        listening_log=fixture.listening_log,
        allocations=fixture.allocations,
        signal_catalog=fixture.signal_catalog,
        signal_classes=fixture.signal_classes,
        end=now,
    )


def run(fixture: synth.Synth, now: datetime | None = None,
        previous: dict[str, list[emitters.Previous]] | None = None) -> list[dict]:
    """One emitters.run(), with `now` defaulting to the end of the fixture."""
    now = now or fixture.end
    data = cut(fixture, now)
    split_at = emitters.recent_split(now)
    bins = [emitters.BinStat(**r) for r in ch_twin.bin_stats(data.peaks)]
    planned = emitters.plan(bins, previous or {})
    cube = ch_twin.activity(data.peaks, data.sweep_health, emitters.footprints(planned), split_at)
    coverage = emitters.Coverage([emitters.CoverageRow(**r) for r in ch_twin.coverage(data.sweep_health)])
    return emitters.build_rows(planned, emitters.summarize_activity(cube), coverage,
                               references(data), split_at, now)


def as_previous(rows: list[dict]) -> dict[str, list[emitters.Previous]]:
    out: dict[str, list[emitters.Previous]] = {}
    for r in rows:
        out.setdefault(r["dongle_id"], []).append(
            emitters.Previous(r["emitter_id"], r["lo_hz"], r["hi_hz"], r["sweeps_hit"]))
    return out


def find(rows: list[dict], dongle: str, freq_hz: int) -> dict:
    """The emitter on `dongle` whose bins include freq_hz."""
    hits = [r for r in rows if r["dongle_id"] == dongle and r["lo_hz"] <= freq_hz <= r["hi_hz"]]
    assert len(hits) == 1, (dongle, freq_hz, hits)
    return hits[0]


def at(day: int, hour: int = 0) -> datetime:
    return synth.START + timedelta(days=day, hours=hour)

