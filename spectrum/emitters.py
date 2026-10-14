#!/usr/bin/env python3
"""
rf_luv: emitters, the transmitters behind the peaks (M2).

The scanner's peak detector fires on one 100 kHz bin in one sweep. An emitter
is the layer above: one transmitter as one dongle sees it, a cluster of
adjacent bins that peaks across many sweeps. This job rebuilds the emitter list
from the whole peaks history and writes it to spectrum.emitters. Per-bin
classifications stay as they are. Emitters sit on top of them.

Audio analogy: the peak detector is an onset detector running on a
spectrogram. This job is the partial tracker of a sinusoidal-modeling vocoder.
It links detections at the same frequency over time into one partial, keeps
that partial's identity from frame to frame, and gives it a name.

One run:
  1. ClickHouse aggregates peaks per (dongle, bin): sweeps hit, first and last seen.
  2. Python chains the bins that peaked in at least MIN_BIN_SWEEPS sweeps into
     clusters, and each cluster inherits the ID of the previous run's emitter
     it overlaps most.
  3. ClickHouse maps every peak onto its cluster and aggregates the hits by
     hour of day, clipped or not, recent or not. It also counts the sweeps per
     dongle, preset and hour (the denominators).
  4. Python turns that into duty cycles, flags artifact suspects, picks a
     name and decides whether the emitter belongs on the TETRA map.
  5. One INSERT writes the run. The view emitters_current shows the latest run.

Heavy work stays in ClickHouse: no query here reads `scans`, and Python only
ever sees aggregated rows, a few thousand at most.

Scheduler: systemd user timer (ops/spectrum-emitters/), every 15 minutes.
stdlib only, like classifier.py and feature_extractor.py.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import db
import sweep_grid

# ─── Config ─────────────────────────────────────────────────

LOCAL_TZ = "Europe/Athens"         # hour-of-day patterns follow local time
MIN_BIN_SWEEPS = 3                  # a bin must peak in this many sweeps to seed an emitter
ADJACENT_HZ = 150_000               # bins this close chain into one cluster
BIN_HZ = sweep_grid.BIN_WIDTH_HZ
HALF_BIN_HZ = BIN_HZ // 2           # a bin covers its label +/- half a bin
MATCH_HZ = BIN_HZ                   # names match within one bin, the scanner's resolution
RECENT_HOURS = 24

# TETRA map (see tetra_evidence). The bands match the TETRA rows of
# allocations (tetra_gr) and signal_catalog (tetra_gr, tetra_civil).
TETRA_NETWORKS = (("public_safety", 380_000_000, 400_000_000),
                  ("civil", 410_000_000, 430_000_000))
CONTINUOUS_DUTY = 0.5               # on in most sweeps: a carrier that never keys down

# Artifact tests (see artifact_reasons)
KNOWN_INTERMOD_HZ = (116_820_000,)  # migration 025: FM intermod inside the V3, not a navaid
CLIP_RATE_RATIO = 0.1               # peaks at most this often unclipped as clipped
MIN_CLIPPED_HITS = 3
MIN_UNCLIPPED_SWEEPS = 10           # below this the dongle had too few clean looks to judge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("emitters")


# ─── Small helpers ──────────────────────────────────────────


def floor_hour(ts: int) -> int:
    return ts - ts % 3600


def local_hour(ts: int) -> int:
    return datetime.fromtimestamp(ts, ZoneInfo(LOCAL_TZ)).hour


def ch_time(ts: int | float) -> str:
    """ClickHouse DateTime64(3) text for a unix time. The project's server runs in UTC."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def distance_to_span(freq_hz: int, lo_hz: int, hi_hz: int) -> int:
    """How far freq_hz lies outside [lo_hz, hi_hz]. Zero inside."""
    if freq_hz < lo_hz:
        return lo_hz - freq_hz
    if freq_hz > hi_hz:
        return freq_hz - hi_hz
    return 0


def within_one_bin(freq_hz: int, lo_hz: int, hi_hz: int) -> bool:
    """True if freq_hz is within one bin of the emitter's bins.

    100 kHz bins cannot place a carrier closer than that, so any reference
    frequency within one bin is a candidate name. Claiming more would be
    precision the scanner does not have.
    """
    return distance_to_span(freq_hz, lo_hz, hi_hz) <= MATCH_HZ


# ─── Clustering ─────────────────────────────────────────────


@dataclass(frozen=True)
class BinStat:
    """One scanner bin's peak history on one dongle."""
    dongle_id: str
    freq_hz: int
    hits: int            # sweeps in which this bin peaked
    first_seen: int      # unix seconds
    last_seen: int
    power_mean: float


@dataclass
class Cluster:
    dongle_id: str
    members: list[BinStat]

    @property
    def lo_hz(self) -> int:
        return self.members[0].freq_hz

    @property
    def hi_hz(self) -> int:
        return self.members[-1].freq_hz

    @property
    def footprint(self) -> tuple[int, int]:
        """The frequencies the emitter's bins cover. A peak inside belongs to it."""
        return self.lo_hz - HALF_BIN_HZ, self.hi_hz + HALF_BIN_HZ

    @property
    def anchor(self) -> BinStat:
        """The bin that peaks most often, then the stronger one, then the lower one."""
        return min(self.members, key=lambda b: (-b.hits, -b.power_mean, b.freq_hz))


def cluster_bins(bins: list[BinStat]) -> list[Cluster]:
    """Chain persistent bins into clusters, per dongle, low to high.

    A bin joins the cluster below it when the two labels are at most
    ADJACENT_HZ apart. That links the full sweep's bins with the airband
    preset's (a few tens of kHz apart) and the 48 kHz slip at tile edges. One
    empty bin between two signals (200 kHz) keeps them apart.
    """
    out: list[Cluster] = []
    persistent = sorted((b for b in bins if b.hits >= MIN_BIN_SWEEPS),
                        key=lambda b: (b.dongle_id, b.freq_hz))
    for b in persistent:
        last = out[-1] if out else None
        if last and last.dongle_id == b.dongle_id and b.freq_hz - last.hi_hz <= ADJACENT_HZ:
            last.members.append(b)
        else:
            out.append(Cluster(b.dongle_id, [b]))
    return out


# ─── Stable IDs ─────────────────────────────────────────────


@dataclass(frozen=True)
class Previous:
    """An emitter from the previous run, as emitters_current shows it."""
    emitter_id: str
    lo_hz: int
    hi_hz: int
    sweeps_hit: int


def assign_ids(dongle_id: str, clusters: list[Cluster], previous: list[Previous]) -> list[str]:
    """Give each cluster the ID of the previous emitter it overlaps most.

    Why tracking and not a frequency hash: an ID derived from the frequency
    alone changes as soon as the cluster grows by a bin, loses one, or its
    center shifts across a rounding edge. Tomorrow's run must give the same
    transmitter the same ID. So each run carries IDs forward by footprint
    overlap, like a partial tracker links a peak in this frame to the nearest
    partial of the last frame.

    A previous emitter and a cluster link when their bins overlap or sit
    within ADJACENT_HZ, the distance that chains bins into one cluster. So a
    one-bin shift keeps the ID. Greedy, largest overlap first (a gap counts
    as negative overlap), then the busier previous emitter, then the ID text,
    so the result does not depend on input order. When two previous
    emitters merge into one cluster, the cluster keeps the ID of the larger
    overlap. When one splits into two, the larger overlap keeps the ID and the
    other half gets a new one.

    A new ID is "<dongle>:<anchor bin in kHz>". The number names the bin where
    the emitter was born. It is a label, not a measurement, and it does not
    follow the emitter if the emitter drifts.
    """
    pairs = []
    for ci, c in enumerate(clusters):
        for p in previous:
            overlap = min(c.hi_hz, p.hi_hz) - max(c.lo_hz, p.lo_hz)
            if overlap >= -ADJACENT_HZ:
                pairs.append((-overlap, -p.sweeps_hit, p.emitter_id, ci))

    ids: list[str | None] = [None] * len(clusters)
    taken: set[str] = set()
    for _, _, emitter_id, ci in sorted(pairs):
        if ids[ci] is None and emitter_id not in taken:
            ids[ci] = emitter_id
            taken.add(emitter_id)

    used = taken | {p.emitter_id for p in previous}
    for ci, c in enumerate(clusters):
        if ids[ci] is not None:
            continue
        base = f"{dongle_id}:{c.anchor.freq_hz // 1000}"
        new_id, n = base, 2
        while new_id in used:
            new_id, n = f"{base}-{n}", n + 1
        ids[ci] = new_id
        used.add(new_id)
    return ids  # type: ignore[return-value]


# ─── Sweep coverage (the denominators) ──────────────────────


@dataclass(frozen=True)
class CoverageRow:
    """Sweeps one dongle made of one preset in one clock hour."""
    dongle_id: str
    preset: str
    hour: int            # unix seconds, start of the hour
    sweeps: int
    clipped_sweeps: int


class Coverage:
    """Sweeps per clock hour that could have seen a given frequency.

    A sweep "covers" a frequency when its preset's range includes it. Only
    covering sweeps count, so a session gap, when the dongle was unplugged,
    adds nothing to a numerator or a denominator. A gap means "not
    listening". It never means "quiet".
    """

    def __init__(self, rows: list[CoverageRow]):
        self._rows = rows
        self._cache: dict[tuple[str, tuple[str, ...]], dict[int, tuple[int, int]]] = {}

    def hours(self, dongle_id: str, freq_hz: int) -> dict[int, tuple[int, int]]:
        """{hour: (sweeps, clipped sweeps)} for the presets that cover freq_hz."""
        presets = tuple(sweep_grid.presets_covering(freq_hz))
        key = (dongle_id, presets)
        if key not in self._cache:
            out: dict[int, tuple[int, int]] = {}
            for r in self._rows:
                if r.dongle_id == dongle_id and r.preset in presets and r.sweeps > 0:
                    sweeps, clipped = out.get(r.hour, (0, 0))
                    out[r.hour] = (sweeps + r.sweeps, clipped + r.clipped_sweeps)
            self._cache[key] = out
        return self._cache[key]


# ─── Activity (the numerators) ──────────────────────────────


@dataclass
class Activity:
    """Peaks that fell inside one emitter's footprint, summed over the cube."""
    hits: int = 0                 # sweeps with a peak in the footprint
    hits_recent: int = 0
    hits_clipped: int = 0
    first_seen: int | None = None
    last_seen: int | None = None
    power_max: float = float("-inf")
    power_sum: float = 0.0
    power_n: int = 0
    hod_hits: list[int] = field(default_factory=lambda: [0] * 24)
    hod_hits_recent: list[int] = field(default_factory=lambda: [0] * 24)
    hod_first_recent: list[int | None] = field(default_factory=lambda: [None] * 24)


def summarize_activity(cube_rows: list[dict]) -> dict[int, Activity]:
    """Fold the activity cube (see activity_sql) into one Activity per emitter index.

    The cube's cells partition the sweeps: a sweep has one timestamp, so one
    hour of day, one clipped flag and one side of the recent split. So hit
    counts add up across cells without counting a sweep twice.
    """
    out: dict[int, Activity] = {}
    for r in cube_rows:
        a = out.setdefault(int(r["idx"]), Activity())
        hits = int(r["hits"])
        hod = int(r["hod"])
        a.hits += hits
        a.hod_hits[hod] += hits
        if int(r["recent"]):
            a.hits_recent += hits
            a.hod_hits_recent[hod] += hits
            first_recent = a.hod_first_recent[hod]
            a.hod_first_recent[hod] = (int(r["first_seen"]) if first_recent is None
                                       else min(first_recent, int(r["first_seen"])))
        if int(r["clipped"]):
            a.hits_clipped += hits
        first, last = int(r["first_seen"]), int(r["last_seen"])
        a.first_seen = first if a.first_seen is None else min(a.first_seen, first)
        a.last_seen = last if a.last_seen is None else max(a.last_seen, last)
        a.power_max = max(a.power_max, float(r["power_max"]))
        a.power_sum += float(r["power_sum"])
        a.power_n += int(r["power_n"])
    return out


@dataclass(frozen=True)
class Numbers:
    duty_overall: float          # hits / covering sweeps since first seen
    duty_24h: float | None       # same over the recent window, None if not swept
    hours_observed: int          # clock hours with covering sweeps since first seen
    sweeps_clipped: int          # covering sweeps from first to last seen, clipped
    sweeps_unclipped: int        # ... and not clipped
    diurnal_sweeps: list[int]    # covering sweeps since first seen, by local hour of day


def duty_numbers(act: Activity, hours: dict[int, tuple[int, int]], split_at: int) -> Numbers:
    """Duty cycles over swept time only.

    duty = sweeps in which the emitter peaked / sweeps that could have seen it.
    Hours with no covering sweep are left out of both. split_at is
    hour-aligned, so the recent window's hits and sweeps cover the same
    hours exactly.
    """
    first_hour = floor_hour(act.first_seen)
    last_hour = floor_hour(act.last_seen)
    swept = {h: sc for h, sc in hours.items() if h >= first_hour}
    total = sum(s for s, _ in swept.values())
    recent = sum(s for h, (s, _) in swept.items() if h >= split_at)
    window = [(s, c) for h, (s, c) in swept.items() if h <= last_hour]
    clipped = sum(c for _, c in window)
    diurnal = [0] * 24
    for h, (s, _) in swept.items():
        diurnal[local_hour(h)] += s
    return Numbers(
        duty_overall=act.hits / total if total else 0.0,
        duty_24h=act.hits_recent / recent if recent else None,
        hours_observed=len(swept),
        sweeps_clipped=clipped,
        sweeps_unclipped=sum(s for s, _ in window) - clipped,
        diurnal_sweeps=diurnal,
    )


# ─── Artifact suspects ──────────────────────────────────────


def artifact_reasons(member_freqs: list[int], lo_hz: int, hi_hz: int, hits_clipped: int,
                     hits_unclipped: int, sweeps_clipped: int, sweeps_unclipped: int) -> list[str]:
    """Why this emitter is probably the receiver and not a transmitter.

    tile_grid     Every bin is a tile's LO (DC) bin. The DC spike of the
                  RTL-SDR shows up there in every tile, 2.048 MHz apart.
    clipped_only  It peaks in clipped sweeps at least ten times as often as in
                  clean ones. Clipping makes distortion products, the way a
                  clipped guitar DI grows harmonics that the amp never played.
                  Compared as rates, because on a V3 without an FM bandstop
                  many sweeps clip, and real carriers show up in those too.
    intermod      Within one bin of a known intermodulation product.

    The documented compression spurs at -174 and +526 kHz from the tile
    center are not tested by offset. The +526 kHz bin of the 424.896 MHz
    tile holds the civil TETRA carrier at 425.425 MHz, so a static offset
    test would hide a real carrier. Those spurs only appear during
    compression, which clipped_only covers.
    """
    reasons = []
    if member_freqs and all(sweep_grid.is_dc_bin(f) for f in member_freqs):
        reasons.append("tile_grid")
    if (hits_clipped >= MIN_CLIPPED_HITS and sweeps_clipped > 0
            and sweeps_unclipped >= MIN_UNCLIPPED_SWEEPS):
        rate_clipped = hits_clipped / sweeps_clipped
        rate_unclipped = hits_unclipped / sweeps_unclipped
        if rate_unclipped <= CLIP_RATE_RATIO * rate_clipped:
            reasons.append("clipped_only")
    if any(within_one_bin(f, lo_hz, hi_hz) for f in KNOWN_INTERMOD_HZ):
        reasons.append("intermod")
    return reasons


# ─── Names ──────────────────────────────────────────────────


@dataclass(frozen=True)
class References:
    """The tables that name things. All small, loaded once per run."""
    listening_log: list[dict]                    # freq_hz, label, class_id, ts
    known_frequencies: list[dict]                # freq_hz, name, class_id
    classifications: dict[str, list[dict]]       # dongle -> freq_hz, class_id, confidence
    class_names: dict[str, str]                  # class_id -> readable name
    ranges: list[tuple[int, int, str, str]]      # (lo, hi, service, table), allocations first


@dataclass(frozen=True)
class Naming:
    name: str
    source: str          # listening_log | known_frequencies | classifier | allocation | signal_catalog | unnamed
    note: str            # other names within one bin, when the bins cannot tell them apart
    class_id: str
    service: str         # narrowest allocation or catalog service at the center


def operator_label(heard: str, notes: str, class_id: str) -> str:
    """A short name from one listening_log row.

    The logging form requires `heard`, a description of what was heard.
    Migration 014's backfill used heard='confirmed' as a status and put the
    description in `notes`, so fall back to the first clause of the notes.
    """
    heard = (heard or "").strip()
    if heard and heard.lower() != "confirmed":
        return heard
    notes = (notes or "").strip()
    if notes:
        return notes.split(" — ")[0].split(". ")[0].strip()
    return class_id or "operator entry"


def service_at(freq_hz: int, ranges: list[tuple[int, int, str, str]]) -> tuple[str, str]:
    """(service, table) of the narrowest range that contains freq_hz, or ('', '')."""
    best = None
    for order, (lo, hi, service, table) in enumerate(ranges):
        if lo <= freq_hz <= hi:
            key = (hi - lo, order)
            if best is None or key < best[0]:
                best = (key, service, table)
    return (best[1], best[2]) if best else ("", "")


def name_emitter(dongle_id: str, lo_hz: int, hi_hz: int, center_hz: int,
                 member_hits: dict[int, int], refs: References) -> Naming:
    """Pick a name by precedence, highest first.

    1. An operator entry in listening_log. Operator entries always win: a
       person listened. (acars_feedback.py writes CRC-checked decodes there
       too, which the classifier already treats the same way.)
    2. known_frequencies.
    3. The class from signal_classifications. unknown_* classes are not an
       identification, so they fall through.
    4. The service from allocations or signal_catalog, the narrowest range
       that contains the center.
    5. "unnamed".

    When one bin holds two named things, as 144.775 MHz voice and 144.800 MHz
    APRS do, the higher source wins and the others go into the note. The
    scanner cannot tell them apart, and the note says so.
    """
    ops = sorted((r for r in refs.listening_log if within_one_bin(r["freq_hz"], lo_hz, hi_hz)),
                 key=lambda r: (distance_to_span(r["freq_hz"], lo_hz, hi_hz), -r["ts"], r["freq_hz"]))
    kfs = sorted((r for r in refs.known_frequencies if within_one_bin(r["freq_hz"], lo_hz, hi_hz)),
                 key=lambda r: (distance_to_span(r["freq_hz"], lo_hz, hi_hz), r["freq_hz"]))
    classified = sorted(
        (c for c in refs.classifications.get(dongle_id, [])
         if lo_hz - HALF_BIN_HZ <= c["freq_hz"] <= hi_hz + HALF_BIN_HZ),
        key=lambda c: (-c["confidence"], -member_hits.get(c["freq_hz"], 0), c["freq_hz"]))
    identifying = [c for c in classified if not c["class_id"].startswith("unknown_")]
    service, table = service_at(center_hz, refs.ranges)

    labelled = ([(r["label"], "listening_log", r["class_id"]) for r in ops]
                + [(r["name"], "known_frequencies", r["class_id"]) for r in kfs])
    if labelled:
        name, source, _ = labelled[0]
    elif identifying:
        cid = identifying[0]["class_id"]
        name, source = refs.class_names.get(cid, cid), "classifier"
    elif service:
        name, source = service, ("allocation" if table == "allocations" else "signal_catalog")
    else:
        name, source = "unnamed", "unnamed"

    others: list[str] = []
    for other, other_source, _ in labelled[1:]:
        text = f"{other} [{other_source}]"
        if other.lower() != name.lower() and text not in others:
            others.append(text)
    note = ("also within one bin: " + " | ".join(others[:3])) if others else ""

    if classified:
        class_id = classified[0]["class_id"]
    else:
        class_id = labelled[0][2] if labelled else ""
    return Naming(name, source, note, class_id, service)


# ─── TETRA map ──────────────────────────────────────────────


def tetra_evidence(class_id: str, center_hz: int, lo_hz: int, hi_hz: int,
                   refs: References) -> list[str]:
    """Why this emitter counts as a TETRA carrier. Empty if it does not.

    class       the classifier says tetra
    allocation  the center lies in a TETRA range of allocations or signal_catalog
    named       a listening_log or known_frequencies entry within one bin says tetra

    This is occupancy mapping from sweep data only: where a carrier sits,
    how often it is on, at what hours. TETRA is encrypted. Nothing here, and
    nothing that reads this, captures, demodulates or decodes it.
    """
    evidence = []
    if class_id == "tetra":
        evidence.append("class")
    if any(lo <= center_hz <= hi for lo, hi, service, _ in refs.ranges if service.startswith("tetra")):
        evidence.append("allocation")
    named = [r for r in refs.listening_log + refs.known_frequencies
             if r["class_id"] == "tetra" and within_one_bin(r["freq_hz"], lo_hz, hi_hz)]
    if named:
        evidence.append("named")
    return evidence


def tetra_network(center_hz: int) -> str:
    """public_safety (380-400 MHz), civil (410-430 MHz) or other.

    "other" is a finding: a TETRA-looking carrier outside both known bands.
    """
    for network, lo, hi in TETRA_NETWORKS:
        if lo <= center_hz < hi:
            return network
    return "other"


def carrier_mode(duty: float) -> str:
    """continuous or bursty, from the share of sweeps the carrier is on.

    A TETRA base station keeps its main carrier, which holds the control
    channel, on all the time. Extra carriers key up with traffic. So a
    continuous carrier is most likely a control channel. Most likely only:
    the scanner sees power, not what the carrier holds.
    """
    return "continuous" if duty >= CONTINUOUS_DUTY else "bursty"


# ─── SQL (ClickHouse does the aggregation) ──────────────────


def bin_stats_sql() -> str:
    return (
        "SELECT dongle_id, freq_hz, uniqExact(sweep_id) AS hits, "
        "toUnixTimestamp(min(timestamp)) AS first_seen, "
        "toUnixTimestamp(max(timestamp)) AS last_seen, "
        "round(avg(power_dbfs), 2) AS power_mean "
        "FROM spectrum.peaks "
        "GROUP BY dongle_id, freq_hz "
        f"HAVING hits >= {MIN_BIN_SWEEPS} "
        "ORDER BY dongle_id, freq_hz"
    )


def coverage_sql() -> str:
    return (
        "SELECT dongle_id, preset, toUnixTimestamp(toStartOfHour(timestamp)) AS hour, "
        "count() AS sweeps, countIf(clipped) AS clipped_sweeps "
        "FROM spectrum.sweep_health "
        "GROUP BY dongle_id, preset, hour "
        "ORDER BY dongle_id, preset, hour"
    )


def _sql_str(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def activity_sql(footprints: list[tuple[str, int, int]], split_at: int) -> str:
    """Hits per emitter, by local hour of day, clipped flag and recent flag.

    footprints: (dongle, lo Hz, hi Hz) per emitter, in index order (1-based in
    the result, as arrayFirstIndex counts). Every peak is mapped onto the first
    footprint that contains it. A sweep counts once per emitter even when
    several of the emitter's bins peaked in it: hits is uniqExact(sweep_id).

    Joined to sweep_health on (dongle, sweep_id) for the clipped flag. The
    two dongles share sweep_id strings, so the dongle is part of the key.
    """
    dongles = ",".join(f"'{_sql_str(d)}'" for d, _, _ in footprints)
    los = ",".join(str(lo) for _, lo, _ in footprints)
    his = ",".join(str(hi) for _, _, hi in footprints)
    return f"""
SELECT idx, hod, clipped, recent,
       uniqExact(sweep_id) AS hits,
       toUnixTimestamp(min(timestamp)) AS first_seen,
       toUnixTimestamp(max(timestamp)) AS last_seen,
       max(power_dbfs) AS power_max,
       sum(power_dbfs) AS power_sum,
       count() AS power_n
FROM (
    SELECT arrayFirstIndex((d, lo, hi) -> d = p.dongle_id AND p.freq_hz >= lo AND p.freq_hz <= hi,
                           [{dongles}], [{los}], [{his}]) AS idx,
           toHour(p.timestamp, '{LOCAL_TZ}') AS hod,
           toUInt8(h.clipped) AS clipped,
           toUInt8(toUnixTimestamp(p.timestamp) >= {split_at}) AS recent,
           p.sweep_id AS sweep_id, p.timestamp AS timestamp, p.power_dbfs AS power_dbfs
    FROM spectrum.peaks AS p
    INNER JOIN (SELECT dongle_id, sweep_id, clipped FROM spectrum.sweep_health) AS h
        ON h.dongle_id = p.dongle_id AND h.sweep_id = p.sweep_id
)
WHERE idx > 0
GROUP BY idx, hod, clipped, recent
ORDER BY idx, hod, clipped, recent"""


# ─── Building the rows ──────────────────────────────────────


@dataclass(frozen=True)
class Planned:
    emitter_id: str
    cluster: Cluster


def plan(bins: list[BinStat], previous: dict[str, list[Previous]]) -> list[Planned]:
    """Cluster every dongle's bins and carry the IDs forward."""
    clusters = cluster_bins(bins)
    out: list[Planned] = []
    for dongle_id in sorted({c.dongle_id for c in clusters}):
        mine = [c for c in clusters if c.dongle_id == dongle_id]
        ids = assign_ids(dongle_id, mine, previous.get(dongle_id, []))
        out += [Planned(i, c) for i, c in zip(ids, mine)]
    return out


def footprints(planned: list[Planned]) -> list[tuple[str, int, int]]:
    return [(p.cluster.dongle_id, *p.cluster.footprint) for p in planned]


def build_rows(planned: list[Planned], activity: dict[int, Activity], coverage: Coverage,
               refs: References, split_at: int, run_at: datetime) -> list[dict]:
    """One spectrum.emitters row per planned cluster that has activity."""
    rows = []
    for idx, p in enumerate(planned, start=1):
        act = activity.get(idx)
        if act is None or act.hits == 0:
            continue
        c = p.cluster
        center = (c.lo_hz + c.hi_hz) // 2
        nums = duty_numbers(act, coverage.hours(c.dongle_id, center), split_at)
        naming = name_emitter(c.dongle_id, c.lo_hz, c.hi_hz, center,
                              {b.freq_hz: b.hits for b in c.members}, refs)
        reasons = artifact_reasons([b.freq_hz for b in c.members], c.lo_hz, c.hi_hz,
                                   act.hits_clipped, act.hits - act.hits_clipped,
                                   nums.sweeps_clipped, nums.sweeps_unclipped)
        evidence = tetra_evidence(naming.class_id, center, c.lo_hz, c.hi_hz, refs)
        rows.append({
            "emitter_id": p.emitter_id,
            "dongle_id": c.dongle_id,
            "center_hz": center,
            "lo_hz": c.lo_hz,
            "hi_hz": c.hi_hz,
            # Bin resolution: an upper bound, not a measured occupied bandwidth.
            "bandwidth_hz": c.hi_hz - c.lo_hz + BIN_HZ,
            "first_seen": ch_time(act.first_seen),
            "last_seen": ch_time(act.last_seen),
            "sweeps_hit": act.hits,
            "duty_overall": round(nums.duty_overall, 4),
            "duty_24h": None if nums.duty_24h is None else round(nums.duty_24h, 4),
            "hours_observed": nums.hours_observed,
            "power_max_dbfs": round(act.power_max, 1),
            "power_mean_dbfs": round(act.power_sum / act.power_n, 1),
            "class_id": naming.class_id,
            "service": naming.service,
            "name": naming.name,
            "name_source": naming.source,
            "name_note": naming.note,
            "artifact_suspect": bool(reasons),
            "artifact_reason": ",".join(reasons),
            "tetra_network": tetra_network(center) if evidence else "",
            "tetra_mode": carrier_mode(nums.duty_overall) if evidence else "",
            "tetra_evidence": ",".join(evidence),
            "diurnal_hits": act.hod_hits,
            "diurnal_sweeps": nums.diurnal_sweeps,
            "run_at": run_at.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return rows


# ─── ClickHouse I/O ─────────────────────────────────────────


def load_references() -> References:
    log_rows = []
    for r in db.query_rows(
        "SELECT toUnixTimestamp(timestamp) AS ts, freq_mhz, heard, class_id, notes, "
        "confirmed_freq_hz FROM spectrum.listening_log"
    ):
        hz = int(r["confirmed_freq_hz"]) or round(float(r["freq_mhz"]) * 1e6)
        log_rows.append({"freq_hz": hz, "ts": int(r["ts"]), "class_id": r["class_id"],
                         "label": operator_label(r["heard"], r["notes"], r["class_id"])})
    known = [{"freq_hz": int(r["freq_hz"]), "name": r["name"], "class_id": r["class_id"]}
             for r in db.query_rows("SELECT freq_hz, name, class_id FROM spectrum.known_frequencies")]
    classifications: dict[str, list[dict]] = {}
    for r in db.query_rows("SELECT dongle_id, freq_hz, class_id, confidence "
                           "FROM spectrum.signal_classifications FINAL"):
        classifications.setdefault(r["dongle_id"], []).append(
            {"freq_hz": int(r["freq_hz"]), "class_id": r["class_id"], "confidence": float(r["confidence"])})
    class_names = {r["class_id"]: r["name"]
                   for r in db.query_rows("SELECT class_id, name FROM spectrum.signal_classes")}
    ranges = [(int(r["freq_start_hz"]), int(r["freq_end_hz"]) - 1, r["service"], "allocations")
              for r in db.query_rows("SELECT freq_start_hz, freq_end_hz, service FROM spectrum.allocations")]
    ranges += [(int(r["freq_lo_hz"]), int(r["freq_hi_hz"]), r["service"], "signal_catalog")
               for r in db.query_rows("SELECT freq_lo_hz, freq_hi_hz, service FROM spectrum.signal_catalog")]
    return References(log_rows, known, classifications, class_names, ranges)


def load_previous() -> dict[str, list[Previous]]:
    out: dict[str, list[Previous]] = {}
    for r in db.query_rows("SELECT dongle_id, emitter_id, lo_hz, hi_hz, sweeps_hit "
                           "FROM spectrum.emitters_current"):
        out.setdefault(r["dongle_id"], []).append(
            Previous(r["emitter_id"], int(r["lo_hz"]), int(r["hi_hz"]), int(r["sweeps_hit"])))
    return out


def load_coverage() -> Coverage:
    return Coverage([CoverageRow(r["dongle_id"], r["preset"], int(r["hour"]), int(r["sweeps"]),
                                 int(r["clipped_sweeps"])) for r in db.query_rows(coverage_sql())])


def recent_split(now: datetime) -> int:
    """Start of the recent window: the current hour and the 23 before it."""
    return floor_hour(int(now.timestamp())) - (RECENT_HOURS - 1) * 3600


def run(dry_run: bool = False) -> int:
    now = datetime.now(timezone.utc)
    split_at = recent_split(now)
    bins = [BinStat(r["dongle_id"], int(r["freq_hz"]), int(r["hits"]), int(r["first_seen"]),
                    int(r["last_seen"]), float(r["power_mean"]))
            for r in db.query_rows(bin_stats_sql())]
    if not bins:
        log.info("No bin peaked in %d sweeps yet, nothing to cluster", MIN_BIN_SWEEPS)
        return 0
    planned = plan(bins, load_previous())
    activity = summarize_activity(db.query_rows(activity_sql(footprints(planned), split_at)))
    rows = build_rows(planned, activity, load_coverage(), load_references(), split_at, now)

    for dongle_id in sorted({r["dongle_id"] for r in rows}):
        mine = [r for r in rows if r["dongle_id"] == dongle_id]
        sources: dict[str, int] = {}
        for r in mine:
            sources[r["name_source"]] = sources.get(r["name_source"], 0) + 1
        log.info("%s: %d emitters, %d artifact suspects, names: %s", dongle_id, len(mine),
                 sum(r["artifact_suspect"] for r in mine),
                 ", ".join(f"{k}={v}" for k, v in sorted(sources.items())))
    if dry_run:
        for r in rows:
            flag = f"  ARTIFACT? {r['artifact_reason']}" if r["artifact_suspect"] else ""
            print(f"{r['dongle_id']}  {r['center_hz'] / 1e6:9.3f} MHz  duty {r['duty_overall']:5.2f}  "
                  f"{r['emitter_id']:<18} {r['name']} [{r['name_source']}]{flag}")
        return len(rows)
    db.insert("spectrum.emitters", rows)
    log.info("Wrote %d emitters (run_at %s)", len(rows), now.strftime("%Y-%m-%d %H:%M:%S"))
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Cluster persistent peaks into named emitters.")
    ap.add_argument("--dry-run", action="store_true", help="print the emitters, write nothing")
    args = ap.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Emitter job failed")
        sys.exit(1)
