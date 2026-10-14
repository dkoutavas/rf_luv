#!/usr/bin/env python3
"""
rf_luv: anomalies and the signal of the day (M4).

Asks five questions about every emitter (spectrum.emitters_current) over the
last 24 hours:

  new_emitter        First seen in the window.
  gone_quiet         No peak for GONE_QUIET_SWEPT_HOURS hours in which its
                     dongle swept its frequency.
  louder_than_usual  LOUDER_DB above its own usual level, on the same dongle
                     at the same gain, in clean (unclipped) sweeps only.
  broken_pattern     Active at an hour of day where its history says it
                     never is.
  new_tetra_carrier  A TETRA carrier that is new, or that sits outside every
                     known TETRA allocation and has no name from a person
                     or known_frequencies yet.

Artifact suspects never raise an anomaly. They are the receiver, not a
transmitter.

Every silence test counts swept time only. Collection is session-based: the
laptop sleeps and the dongles unplug together. A gap in the data means "not
listening". It never means "the band went quiet", like a muted channel on a
desk is not a silent instrument.

Then it picks one signal of the day with a written reason, by an interest
score that favors unnamed, new and rare emitters and pushes broadcast FM,
DVB-T and artifact suspects to the bottom. With --digest it also sends the
daily summary through ops/notify/notify.py, unless the dongles swept nothing
in the window.

Writes spectrum.anomalies and spectrum.signal_of_the_day (migration 028).
Scheduler: ops/spectrum-anomalies/, hourly, and the digest once a day.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import db
import emitters
from emitters import Activity, Coverage, floor_hour, local_hour

# ─── Config ─────────────────────────────────────────────────

WINDOW_HOURS = emitters.RECENT_HOURS

GONE_QUIET_SWEPT_HOURS = 6     # N: swept hours of silence before "gone quiet"
GONE_QUIET_MIN_EXPECTED = 5    # peaks its usual duty predicts in those hours
# Why the expected-peaks guard: a rare emitter (APRS shows up in about 4 %
# of sweeps) is silent for hours as a matter of course. Zero peaks only
# means something when the emitter's own rate predicts several. If the
# arrivals are random, the chance of zero when 5 are expected is e^-5,
# under 1 %.
BROKEN_MIN_SWEPT_HOURS = 2     # days with that hour of day swept, before "never" means anything
# Each emitter gets 24 tries a day, one per hour of day, so the 1 % bar is
# split across them (a Bonferroni correction). Otherwise a carrier that is
# rarely on at night would break its pattern by luck every few days.
BROKEN_MAX_CHANCE = 0.01 / 24

LOUDER_DB = 6.0                # N dB above its own baseline
LOUDER_SIGMAS = 3.0            # ...and clear of its usual spread
LOUDER_MIN_HISTORY = 10        # clean sweeps at that gain before the window
LOUDER_MIN_RECENT = 3          # clean sweeps at that gain in the window

RARE_DUTY = 0.05
BROADCAST_CLASSES = {"broadcast_fm", "dvbt_mux"}
BROADCAST_SERVICES = {"broadcast_fm", "dvb_t_gr"}
PERSON_NAMED = {"listening_log", "known_frequencies"}
UNIDENTIFIED = {"allocation", "signal_catalog", "unnamed"}

KIND_LABELS = {
    "new_emitter": "new emitter",
    "gone_quiet": "gone quiet",
    "louder_than_usual": "louder than usual",
    "broken_pattern": "broken pattern",
    "new_tetra_carrier": "new TETRA carrier",
}

REPO = Path(__file__).resolve().parent.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("anomalies")


# ─── Shapes ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Emitter:
    """One row of emitters_current, with times as unix seconds."""
    emitter_id: str
    dongle_id: str
    center_hz: int
    lo_hz: int
    hi_hz: int
    first_seen: int
    last_seen: int
    duty_overall: float
    name: str
    name_source: str
    class_id: str
    service: str
    artifact_suspect: bool
    artifact_reason: str
    tetra_network: str
    tetra_mode: str
    tetra_evidence: str

    @classmethod
    def from_row(cls, r: dict) -> "Emitter":
        return cls(r["emitter_id"], r["dongle_id"], int(r["center_hz"]), int(r["lo_hz"]),
                   int(r["hi_hz"]), int(r["first_seen"]), int(r["last_seen"]),
                   float(r["duty_overall"]), r["name"], r["name_source"], r["class_id"],
                   r["service"], bool(r["artifact_suspect"]), r["artifact_reason"],
                   r["tetra_network"], r["tetra_mode"], r["tetra_evidence"])

    @property
    def footprint(self) -> tuple[str, int, int]:
        return self.dongle_id, self.lo_hz - emitters.HALF_BIN_HZ, self.hi_hz + emitters.HALF_BIN_HZ


@dataclass(frozen=True)
class LevelStat:
    """Peak level of one emitter at one gain, clean sweeps only, one side of the window."""
    gain_db: float
    recent: bool
    n: int               # sweeps
    mean: float          # dBFS, the emitter's strongest bin per sweep
    std: float
    last_at: int


@dataclass(frozen=True)
class Role:
    is_primary: bool
    levels_trusted: bool


@dataclass(frozen=True)
class Anomaly:
    kind: str
    dongle_id: str
    emitter_id: str
    center_hz: int
    name: str
    event_at: int
    confidence: str      # normal | low
    detail: str


def _local(ts: int, fmt: str = "%a %H:%M") -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(emitters.LOCAL_TZ)).strftime(fmt)


def _anomaly(kind: str, e: Emitter, event_at: int, detail: str, confidence: str = "normal") -> Anomaly:
    return Anomaly(kind, e.dongle_id, e.emitter_id, e.center_hz, e.name, event_at, confidence, detail)


# ─── The five rules ─────────────────────────────────────────


def new_emitter(e: Emitter, act: Activity, window_start: int) -> Anomaly | None:
    if act.first_seen < window_start:
        return None
    return _anomaly("new_emitter", e, act.first_seen,
                    f"first seen {_local(act.first_seen)}, in {act.hits} sweeps since")


def gone_quiet(e: Emitter, act: Activity, hours: dict[int, tuple[int, int]], window_start: int,
               now: int) -> Anomaly | None:
    """Silent through GONE_QUIET_SWEPT_HOURS swept hours, the Nth of them inside the window.

    Only hours with covering sweeps count. Reported once: at the moment the
    Nth silent swept hour completes. Longer silence later does not report
    again.

    The expected number of peaks follows the emitter's own hour-of-day
    profile, so a channel that only works days is not "gone quiet" every
    night. Hours of day it was never swept at predict nothing.
    """
    first_hour, last_hour = floor_hour(act.first_seen), floor_hour(act.last_seen)
    silent = sorted(h for h, (s, _) in hours.items() if h > last_hour and s > 0)
    if len(silent) < GONE_QUIET_SWEPT_HOURS:
        return None
    nth = silent[:GONE_QUIET_SWEPT_HOURS]
    onset = min(nth[-1] + 3600, now)
    if onset < window_start:
        return None
    hod_sweeps = [0] * 24
    for h, (s, _) in hours.items():
        if first_hour <= h <= last_hour:
            hod_sweeps[local_hour(h)] += s
    hod_duty = [act.hod_hits[k] / hod_sweeps[k] if hod_sweeps[k] else 0.0 for k in range(24)]
    expected = sum(hod_duty[local_hour(h)] * hours[h][0] for h in nth)
    if expected < GONE_QUIET_MIN_EXPECTED:
        return None
    return _anomaly("gone_quiet", e, onset,
                    f"no peak in {len(silent)} swept hours since {_local(act.last_seen)}. "
                    f"Its usual rate at those hours of day predicts about {expected:.0f} "
                    f"peaks in the first {GONE_QUIET_SWEPT_HOURS}")


def poisson_tail(k: int, lam: float) -> float:
    """P(X >= k) for a Poisson count with mean lam: the chance of k or more peaks by luck."""
    if k <= 0:
        return 1.0
    term = math.exp(-lam)
    below = term
    for i in range(1, k):
        term *= lam / i
        below += term
    return max(0.0, 1.0 - below)


def broken_pattern(e: Emitter, act: Activity, hours: dict[int, tuple[int, int]],
                   window_start: int) -> Anomaly | None:
    """Peaks in the window at an hour of day where history has none, too many to be luck.

    History is everything from first_seen to the window start. An hour of
    day qualifies when the dongle swept it on at least BROKEN_MIN_SWEPT_HOURS
    days and the emitter never peaked there. Then the question is whether
    the new peaks are surprising. The emitter's rate at that hour and the
    hour on each side, plus one peak so the rate is never zero, predicts how
    many peaks luck alone would give. A night-dead channel that suddenly
    talks at 05:00 is far past that. A carrier that is rarely but sometimes
    on at night is not.

    Two days is not much history, but the host collects in sessions (48
    swept hours over 6 days so far), and the chance test carries the
    statistics.
    """
    first_hour = floor_hour(act.first_seen)
    hist_sweeps, hist_days, recent_sweeps = [0] * 24, [0] * 24, [0] * 24
    for h, (s, _) in hours.items():
        if s <= 0 or h < first_hour:
            continue
        hod = local_hour(h)
        if h >= window_start:
            recent_sweeps[hod] += s
        else:
            hist_sweeps[hod] += s
            hist_days[hod] += 1
    hist_hits = [a - r for a, r in zip(act.hod_hits, act.hod_hits_recent)]
    odd = []
    for hod in range(24):
        peaks = act.hod_hits_recent[hod]
        if not peaks or hist_hits[hod] or hist_days[hod] < BROKEN_MIN_SWEPT_HOURS:
            continue
        around = [(hod + d) % 24 for d in (-1, 0, 1)]
        rate = (sum(hist_hits[k] for k in around) + 1) / sum(hist_sweeps[k] for k in around)
        chance = poisson_tail(peaks, rate * recent_sweeps[hod])
        if chance < BROKEN_MAX_CHANCE:
            odd.append((hod, peaks, chance))
    if not odd:
        return None
    hod, peaks, chance = odd[0]
    event_at = min(act.hod_first_recent[h] for h, _, _ in odd)
    return _anomaly("broken_pattern", e, event_at,
                    f"active at {', '.join(f'{h:02d}:00' for h, _, _ in odd)} local. "
                    f"No peak at {hod:02d}:00 in {hist_days[hod]} swept days of history, and "
                    f"{peaks} peaks by luck had a {chance:.1e} chance")


def louder_than_usual(e: Emitter, levels: list[LevelStat], role: Role) -> Anomaly | None:
    """LOUDER_DB above its own level at the same gain, clean sweeps only.

    Levels are compared within one dongle and one gain setting, because the
    scanner's automatic gain control moves the gain between runs and every
    carrier's dBFS moves with it, as a fader moves every track in a bus.
    Clipped sweeps are left out before this point (levels_sql), because a
    clipping ADC squashes and smears levels. A dongle whose levels are not
    trusted (dongle_roles) still reports, marked low confidence.
    """
    best = None
    for hist in (s for s in levels if not s.recent and s.n >= LOUDER_MIN_HISTORY):
        for rec in (s for s in levels if s.recent and s.gain_db == hist.gain_db
                    and s.n >= LOUDER_MIN_RECENT):
            delta = rec.mean - hist.mean
            if delta >= max(LOUDER_DB, LOUDER_SIGMAS * hist.std) and (best is None or delta > best[0]):
                best = (delta, hist, rec)
    if best is None:
        return None
    delta, hist, rec = best
    detail = (f"{delta:+.1f} dB at gain {hist.gain_db:g} dB: {rec.mean:.1f} dBFS over {rec.n} clean "
              f"sweeps, usually {hist.mean:.1f} ± {hist.std:.1f} over {hist.n}")
    if not role.levels_trusted:
        detail += ". This dongle's levels are not trusted (dongle_roles)"
    return _anomaly("louder_than_usual", e, rec.last_at, detail,
                    "normal" if role.levels_trusted else "low")


def new_tetra_carrier(e: Emitter, act: Activity, window_start: int) -> Anomaly | None:
    """A TETRA carrier that is new, or outside every known allocation and nameless.

    The second case repeats each day the carrier is heard, until someone logs
    it in listening_log or known_frequencies.
    """
    if not e.tetra_network:
        return None
    outside = "allocation" not in e.tetra_evidence.split(",")
    mode = "continuous, likely a control channel" if e.tetra_mode == "continuous" else "bursty"
    if act.first_seen >= window_start:
        where = "outside every known TETRA allocation" if outside else f"in the {e.tetra_network} band"
        return _anomaly("new_tetra_carrier", e, act.first_seen, f"new carrier {where}, {mode}")
    if outside and e.name_source not in PERSON_NAMED and act.hits_recent:
        return _anomaly("new_tetra_carrier", e, act.last_seen,
                        f"outside every known TETRA allocation, {mode}, and not in listening_log "
                        f"or known_frequencies yet")
    return None


def detect(emitter_list: list[Emitter], activity: dict[int, Activity], coverage: Coverage,
           levels: dict[int, list[LevelStat]], roles: dict[str, Role], now: int) -> list[Anomaly]:
    """All five rules over every emitter. Indexes are 1-based, in emitter_list order."""
    window_start = emitters.recent_split(datetime.fromtimestamp(now, timezone.utc))
    out: list[Anomaly] = []
    for idx, e in enumerate(emitter_list, start=1):
        act = activity.get(idx)
        if e.artifact_suspect or act is None or not act.hits:
            continue
        hours = coverage.hours(e.dongle_id, e.center_hz)
        role = roles.get(e.dongle_id, Role(False, False))
        for found in (new_emitter(e, act, window_start),
                      gone_quiet(e, act, hours, window_start, now),
                      louder_than_usual(e, levels.get(idx, []), role),
                      broken_pattern(e, act, hours, window_start),
                      new_tetra_carrier(e, act, window_start)):
            if found:
                out.append(found)
    return out


# ─── Interest and the signal of the day ─────────────────────


def interest_score(e: Emitter, kinds: set[str], window_start: int) -> tuple[int, list[str]]:
    """A fixed point table, so the ranking is explainable line by line.

      +3 not identified: no name from a person, known_frequencies or the
         classifier, at most an allocation service
      +3 first seen in the window (the last 24 h)
      +2 rare: in fewer than 5 % of the sweeps that could see it
      +1 per other anomaly kind in the last 24 h
     -10 broadcast FM or DVB-T, by class or allocation
    -100 artifact suspect, always last
    """
    if e.artifact_suspect:
        return -100, [f"artifact suspect ({e.artifact_reason})"]
    score, reasons = 0, []
    if e.name_source in UNIDENTIFIED:
        score += 3
        reasons.append("unnamed" if e.name_source == "unnamed" else f"only an allocation name ({e.name})")
    if e.first_seen >= window_start:
        score += 3
        reasons.append(f"new, first seen {_local(e.first_seen)}")
    if e.duty_overall < RARE_DUTY:
        score += 2
        reasons.append(f"rare, in {e.duty_overall:.1%} of sweeps")
    for kind in sorted(kinds - {"new_emitter"}):
        score += 1
        reasons.append(KIND_LABELS[kind])
    if e.class_id in BROADCAST_CLASSES or e.service in BROADCAST_SERVICES:
        score -= 10
        reasons.append("broadcast")
    return score, reasons


@dataclass(frozen=True)
class Pick:
    emitter: Emitter
    score: int
    reason: str


def rank(candidates: list[Emitter], kinds: dict[str, set[str]], roles: dict[str, Role],
         window_start: int) -> list[tuple[int, list[str], Emitter]]:
    """Highest interest first. Ties go to the primary dongle, then the lower frequency."""
    scored = [(*interest_score(e, kinds.get(e.emitter_id, set()), window_start), e) for e in candidates]
    scored.sort(key=lambda t: (-t[0], not roles.get(t[2].dongle_id, Role(False, False)).is_primary,
                               t[2].center_hz, t[2].dongle_id, t[2].emitter_id))
    return scored


def signal_of_the_day(emitter_list: list[Emitter], anomalies: list[Anomaly], roles: dict[str, Role],
                      now: int) -> Pick | None:
    """The highest-ranked emitter heard in the window, deterministically, with its reason."""
    window_start = emitters.recent_split(datetime.fromtimestamp(now, timezone.utc))
    heard = [e for e in emitter_list if e.last_seen >= window_start]
    if not heard:
        return None
    kinds: dict[str, set[str]] = {}
    for a in anomalies:
        kinds.setdefault(a.emitter_id, set()).add(a.kind)
    score, reasons, e = rank(heard, kinds, roles, window_start)[0]
    why = ", ".join(reasons) if reasons else "nothing new, unnamed or rare was heard, so the top of the ranking"
    return Pick(e, score, f"{e.name} at {e.center_hz / 1e6:.3f} MHz on {e.dongle_id}: {why}")


# ─── The daily digest ───────────────────────────────────────


def digest(pick: Pick | None, anomalies: list[Anomaly], swept_hours: dict[str, int],
           day: str) -> tuple[str, str] | None:
    """(title, body) for notify.send, or None when the dongles swept nothing."""
    if not any(swept_hours.values()):
        return None
    title = f"rf_luv {day}: " + (f"{pick.emitter.name} {pick.emitter.center_hz / 1e6:.3f} MHz"
                                 if pick else "no signal of the day")
    normal = [a for a in anomalies if a.confidence == "normal"]
    low = [a for a in anomalies if a.confidence == "low"]
    lines = []
    if pick:
        lines.append(f"Signal of the day: {pick.reason}.")
    lines.append("Last 24 h: " + ", ".join(
        f"{label} {sum(a.kind == kind for a in normal)}" for kind, label in KIND_LABELS.items()) + ".")
    if low:
        lines.append("Low confidence (untrusted levels): " + ", ".join(
            f"{KIND_LABELS[k]} {sum(a.kind == k for a in low)}" for k in KIND_LABELS
            if any(a.kind == k for a in low)) + ".")
    lines.append("Swept: " + ", ".join(f"{d} {h} h" for d, h in sorted(swept_hours.items()) if h) + ".")
    return title, "\n".join(lines)


def swept_hours(coverage_rows: list[emitters.CoverageRow], window_start: int) -> dict[str, int]:
    """Clock hours in the window with at least one sweep, per dongle."""
    hours: dict[str, set[int]] = {}
    for r in coverage_rows:
        if r.hour >= window_start and r.sweeps > 0:
            hours.setdefault(r.dongle_id, set()).add(r.hour)
    return {d: len(h) for d, h in hours.items()}


# ─── SQL ────────────────────────────────────────────────────


EMITTERS_SQL = (
    "SELECT emitter_id, dongle_id, center_hz, lo_hz, hi_hz, "
    "toUnixTimestamp(first_seen) AS first_seen, toUnixTimestamp(last_seen) AS last_seen, "
    "duty_overall, name, name_source, class_id, service, artifact_suspect, artifact_reason, "
    "tetra_network, tetra_mode, tetra_evidence "
    "FROM spectrum.emitters_current ORDER BY dongle_id, center_hz"
)

ROLES_SQL = ("SELECT dongle_id, argMax(is_primary, updated_at) AS is_primary, "
             "argMax(levels_trusted, updated_at) AS levels_trusted "
             "FROM spectrum.dongle_roles GROUP BY dongle_id")


def levels_sql(footprints: list[tuple[str, int, int]], split_at: int) -> str:
    """Per emitter, per gain, per side of the window: clean sweeps only.

    Each sweep contributes the level of the emitter's strongest bin in it, so
    a wide emitter does not count one sweep twice. Gain is grouped in tenths
    of a dB, as sweep_health stores it.
    """
    dongles = ",".join(f"'{emitters._sql_str(d)}'" for d, _, _ in footprints)
    los = ",".join(str(lo) for _, lo, _ in footprints)
    his = ",".join(str(hi) for _, _, hi in footprints)
    return f"""
SELECT idx, gain_tenths, recent, count() AS n,
       round(avg(power), 2) AS mean, round(stddevPop(power), 2) AS std, max(ts) AS last_at
FROM (
    SELECT idx, sweep_id, any(gain_tenths) AS gain_tenths, any(recent) AS recent,
           max(power_dbfs) AS power, max(ts) AS ts
    FROM (
        SELECT arrayFirstIndex((d, lo, hi) -> d = p.dongle_id AND p.freq_hz >= lo AND p.freq_hz <= hi,
                               [{dongles}], [{los}], [{his}]) AS idx,
               p.sweep_id AS sweep_id,
               toInt32(round(h.gain_db * 10)) AS gain_tenths,
               toUInt8(toUnixTimestamp(p.timestamp) >= {split_at}) AS recent,
               p.power_dbfs AS power_dbfs,
               toUnixTimestamp(p.timestamp) AS ts
        FROM spectrum.peaks AS p
        INNER JOIN (SELECT dongle_id, sweep_id, gain_db, clipped FROM spectrum.sweep_health) AS h
            ON h.dongle_id = p.dongle_id AND h.sweep_id = p.sweep_id
        WHERE NOT h.clipped
    )
    WHERE idx > 0
    GROUP BY idx, sweep_id
)
GROUP BY idx, gain_tenths, recent
ORDER BY idx, gain_tenths, recent"""


def group_levels(rows: list[dict]) -> dict[int, list[LevelStat]]:
    out: dict[int, list[LevelStat]] = {}
    for r in rows:
        out.setdefault(int(r["idx"]), []).append(LevelStat(
            int(r["gain_tenths"]) / 10, bool(int(r["recent"])), int(r["n"]),
            float(r["mean"]), float(r["std"]), int(r["last_at"])))
    return out


def anomaly_rows(found: list[Anomaly], detected_at: datetime) -> list[dict]:
    tz = ZoneInfo(emitters.LOCAL_TZ)
    return [{
        "kind": a.kind, "dongle_id": a.dongle_id, "emitter_id": a.emitter_id,
        "event_day": datetime.fromtimestamp(a.event_at, tz).strftime("%Y-%m-%d"),
        "event_at": emitters.ch_time(a.event_at),
        "detected_at": detected_at.strftime("%Y-%m-%d %H:%M:%S"),
        "center_hz": a.center_hz, "name": a.name, "confidence": a.confidence, "detail": a.detail,
    } for a in found]


def pick_row(pick: Pick, day: str, picked_at: datetime) -> dict:
    e = pick.emitter
    return {"day": day, "picked_at": picked_at.strftime("%Y-%m-%d %H:%M:%S"),
            "dongle_id": e.dongle_id, "emitter_id": e.emitter_id, "center_hz": e.center_hz,
            "name": e.name, "name_source": e.name_source, "score": pick.score, "reason": pick.reason}


# ─── Run ────────────────────────────────────────────────────


def send_digest(title: str, body: str) -> bool:
    """Through ops/notify/notify.py: a desktop popup, or ntfy when a topic is set."""
    sys.path.insert(0, str(REPO / "ops" / "notify"))
    import notify
    return notify.send("INFO", title, body, ["satellite_antenna"])


def run(send: bool = False, dry_run: bool = False) -> list[Anomaly]:
    now_dt = datetime.now(timezone.utc)
    now = int(now_dt.timestamp())
    window_start = emitters.recent_split(now_dt)
    day = now_dt.astimezone(ZoneInfo(emitters.LOCAL_TZ)).strftime("%Y-%m-%d")

    emitter_list = [Emitter.from_row(r) for r in db.query_rows(EMITTERS_SQL)]
    roles = {r["dongle_id"]: Role(bool(r["is_primary"]), bool(r["levels_trusted"]))
             for r in db.query_rows(ROLES_SQL)}
    coverage_rows = [emitters.CoverageRow(r["dongle_id"], r["preset"], int(r["hour"]),
                                          int(r["sweeps"]), int(r["clipped_sweeps"]))
                     for r in db.query_rows(emitters.coverage_sql())]
    found: list[Anomaly] = []
    pick = None
    if emitter_list:
        fps = [e.footprint for e in emitter_list]
        activity = emitters.summarize_activity(db.query_rows(emitters.activity_sql(fps, window_start)))
        levels = group_levels(db.query_rows(levels_sql(fps, window_start)))
        found = detect(emitter_list, activity, Coverage(coverage_rows), levels, roles, now)
        # The cube's times are fresher than the emitter rows, which lag up to 15 minutes.
        fresh = [replace(e, first_seen=activity[i].first_seen, last_seen=activity[i].last_seen)
                 if i in activity else e for i, e in enumerate(emitter_list, start=1)]
        pick = signal_of_the_day(fresh, found, roles, now)

    for a in found:
        log.info("%s %s %.3f MHz %s [%s]: %s", a.kind, a.dongle_id, a.center_hz / 1e6, a.name,
                 a.confidence, a.detail)
    if pick:
        log.info("signal of the day: %s (score %d)", pick.reason, pick.score)
    message = digest(pick, found, swept_hours(coverage_rows, window_start), day) if send else None
    if dry_run:
        if message:
            print(message[0], message[1], sep="\n")
        return found
    db.insert("spectrum.anomalies", anomaly_rows(found, now_dt))
    if pick:
        db.insert("spectrum.signal_of_the_day", [pick_row(pick, day, now_dt)])
    if send:
        if message is None:
            log.info("The dongles swept nothing in the last %d h, no digest", WINDOW_HOURS)
        else:
            send_digest(*message)
            log.info("Digest sent: %s", message[0])
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description="Anomalies, the signal of the day and the daily digest.")
    ap.add_argument("--digest", action="store_true", help="also send the daily digest")
    ap.add_argument("--dry-run", action="store_true", help="log and print, write and send nothing")
    args = ap.parse_args()
    run(send=args.digest, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Anomaly job failed")
        sys.exit(1)
