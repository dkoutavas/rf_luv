"""Synthetic sweep data for the emitter, TETRA-map and anomaly tests.

A small seeded generator. It builds `peaks` and `sweep_health` rows in memory
for two dongles over five days, plus the reference rows that name them
(classifications, known frequencies, the listening log, allocations, the
catalog). No ClickHouse, no dongle.

The rows look like what scanner.py writes. A peak is the output of the
scanner's peak detector: a 100 kHz bin more than 10 dB above its neighbors in
one sweep. So the generator does not model spectra. It decides, sweep by
sweep, which transmitters the detector would have caught, at what level.
Think of it as a drum machine: each transmitter is a track with a hit
probability per step, and the step grid is the sweep schedule.

What it models (times in UTC, and Athens is UTC+3 in October 2026):

- Day 2 is a session gap. Neither dongle sweeps at all. Collection is
  session-based, so a gap means "not listening", never "quiet band".
- The V3 sweeps around the clock on days 0, 1, 3 and 4. The V4 sweeps
  10:00-22:00 local, and from 08:00 local on day 4.
- The V3 starts at 7.7 dB gain, and 40 % of its full sweeps clip on FM. At
  12:00 on day 1 its gain drops to 3.7 dB and the clipping stops. Real
  carriers fall 1 dB per dB of gain. The 116.82 MHz intermodulation product
  falls 2.6 dB per dB and appears only in clipped sweeps.
- The V4 is water-damaged: 16.5 dB short at FM, 8 dB short at VHF, 3.5 dB
  short at UHF. It never clips.

Every frequency below is the label of a real scanner bin (a test checks it
against scanner.downsample_bins). The tile grid repeats every 2.048 MHz from
89.024 MHz for the full sweep and from 119.024 MHz for the airband sweep. The
bin that holds a tile's center (the LO, where the DC spike sits) is labeled
center + 26 kHz.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ATHENS = ZoneInfo("Europe/Athens")
START = datetime(2026, 10, 5, tzinfo=timezone.utc)
DAYS = 5
GAP_DAY = 2
V3 = "v3-01"
V4 = "v4-01"

FULL_INTERVAL_S = 280
AIRBAND_INTERVAL_S = 60
AIRBAND_OFFSET_S = 30          # keeps airband and full sweeps off the same instant

V3_GAIN_BEFORE = 7.7
V3_GAIN_AFTER = 3.7
GAIN_CHANGE_AT = START + timedelta(days=1, hours=12)
V3_CLIP_PROBABILITY = 0.4      # share of full sweeps that clip at 7.7 dB
V4_GAIN = 12.0

APPEARS_AT = START + timedelta(days=3, hours=8)
STOPS_AT = START + timedelta(days=3, hours=10)
LOUDER_FROM = START + timedelta(days=4)
LOUDER_DB = 10.0
# Day 4, 03:00-03:20 local: a marine burst at an hour this channel never uses.
ODD_HOUR_BURST = (START + timedelta(days=4), START + timedelta(days=4, minutes=20))

# ─── Bin frequencies (Hz) ───────────────────────────────────

FM_KOSMOS = 99_590_000          # Kosmos FM 99.6, full grid
FM_KOSMOS_SHOULDER = 99_690_000
INTERMOD = 116_822_000          # FM intermodulation inside the V3, full grid
APPROACH_AIRBAND = 118_550_000  # Athens Approach 118.575 on the airband grid
APPROACH_FULL = 118_622_000     # ... and on the full grid, 72 kHz away
NEW_ON_DAY3 = 140_550_000       # nothing allocated or catalogued here
APRS = 144_846_000              # 144.800 APRS, one bin from 144.775 voice
MARINE_CH16 = 156_834_000       # daytime only, then one burst at 03:00 local
COAST_STATION = 158_082_000     # Piraeus Radio 158.08, gets 10 dB louder on day 4
STOPS_ON_DAY3 = 165_026_000     # land mobile carrier that goes off air
TILE_SPUR = 299_994_000         # DC bin of tile 103
TETRA_SPURS = (381_914_000, 383_962_000, 386_010_000)  # DC bins, labeled tetra
TETRA_PS_CONTINUOUS = (390_506_000, 391_454_000)
TETRA_PS_BURSTY = 392_654_000
TETRA_OUTSIDE = 405_990_000     # tetra class, no TETRA allocation covers it
TETRA_CIVIL_CONTINUOUS = (424_822_000, 425_122_000)
TETRA_CIVIL_BURSTY = 426_270_000


@dataclass(frozen=True)
class Transmitter:
    """One track of the drum machine."""
    label: str
    bins: tuple[tuple[int, str, float], ...]   # (freq_hz, preset, dB relative to the main bin)
    level_dbfs: float                          # V3 level at 7.7 dB gain
    probability: object                        # callable(utc datetime) -> hit probability per sweep
    v4_offset_db: float | None = None          # None: the V4 does not see it
    gain_slope: float = 1.0                    # dB of level per dB of gain
    clipped_only: bool = False
    starts: datetime | None = None
    stops: datetime | None = None


def _local_hour(t: datetime) -> int:
    return t.astimezone(ATHENS).hour


def _flat(p: float):
    return lambda t: p


def _daytime(day_p: float, night_p: float, first: int = 8, last: int = 18):
    """Hit probability by local hour: busy from `first` to `last`, quiet otherwise."""
    return lambda t: day_p if first <= _local_hour(t) < last else night_p


def _marine_ch16(t: datetime) -> float:
    if ODD_HOUR_BURST[0] <= t < ODD_HOUR_BURST[1]:
        return 1.0
    return 0.5 if 8 <= _local_hour(t) < 18 else 0.0


TRANSMITTERS = (
    Transmitter("fm_kosmos", ((FM_KOSMOS, "full", 0.0), (FM_KOSMOS_SHOULDER, "full", -6.0)),
                -18.0, _flat(0.97), v4_offset_db=-16.5),
    Transmitter("approach", ((APPROACH_AIRBAND, "airband", 0.0), (APPROACH_FULL, "full", -1.0)),
                -40.0, _daytime(0.25, 0.05, 6, 23), v4_offset_db=-8.0),
    Transmitter("aprs", ((APRS, "full", 0.0),), -42.0, _flat(0.04), v4_offset_db=-8.0),
    Transmitter("marine_ch16", ((MARINE_CH16, "full", 0.0),), -38.0, _marine_ch16, v4_offset_db=-8.0),
    Transmitter("coast_station", ((COAST_STATION, "full", 0.0),), -41.0, _flat(0.6), v4_offset_db=-8.0),
    Transmitter("new_on_day3", ((NEW_ON_DAY3, "full", 0.0),), -44.0, _flat(0.4), starts=APPEARS_AT),
    Transmitter("stops_on_day3", ((STOPS_ON_DAY3, "full", 0.0),), -36.0, _flat(0.9), stops=STOPS_AT),
    Transmitter("intermod", ((INTERMOD, "full", 0.0),), -30.0, _flat(0.9),
                gain_slope=2.6, clipped_only=True),
    Transmitter("tile_spur", ((TILE_SPUR, "full", 0.0),), -50.0, _flat(0.5)),
    Transmitter("tetra_spur_1", ((TETRA_SPURS[0], "full", 0.0),), -52.0, _flat(0.6), v4_offset_db=-3.5),
    Transmitter("tetra_spur_2", ((TETRA_SPURS[1], "full", 0.0),), -52.0, _flat(0.6)),
    Transmitter("tetra_spur_3", ((TETRA_SPURS[2], "full", 0.0),), -52.0, _flat(0.6)),
    Transmitter("tetra_ps_1", ((TETRA_PS_CONTINUOUS[0], "full", 0.0),), -41.0, _flat(0.95), v4_offset_db=-3.5),
    Transmitter("tetra_ps_2", ((TETRA_PS_CONTINUOUS[1], "full", 0.0),), -38.0, _flat(0.96), v4_offset_db=-3.5),
    Transmitter("tetra_ps_bursty", ((TETRA_PS_BURSTY, "full", 0.0),), -45.0, _daytime(0.35, 0.05, 8, 20),
                v4_offset_db=-3.5),
    Transmitter("tetra_outside", ((TETRA_OUTSIDE, "full", 0.0),), -47.0, _flat(0.9)),
    Transmitter("tetra_civil_1", ((TETRA_CIVIL_CONTINUOUS[0], "full", 0.0),), -44.0, _flat(0.95),
                v4_offset_db=-3.5),
    Transmitter("tetra_civil_2", ((TETRA_CIVIL_CONTINUOUS[1], "full", 0.0),), -44.0, _flat(0.95),
                v4_offset_db=-3.5),
    Transmitter("tetra_civil_bursty", ((TETRA_CIVIL_BURSTY, "full", 0.0),), -48.0, _flat(0.2)),
)


# ─── Reference rows (copied from the real seeds and migrations) ───

ALLOCATIONS = [  # migrations 003 and 025, all 13 rows
    (87_500_000, 108_000_000, "broadcast_fm"),
    (108_000_000, 117_975_000, "aviation_navigation"),
    (118_000_000, 137_000_000, "aviation_voice"),
    (144_000_000, 146_000_000, "amateur_2m"),
    (146_000_000, 150_000_000, "gov_military_vhf_gr"),
    (150_000_000, 156_000_000, "land_mobile_business"),
    (156_000_000, 162_025_000, "marine_vhf"),
    (162_050_000, 174_000_000, "land_mobile_safety"),
    (174_000_000, 230_000_000, "dvb_t_gr"),
    (380_000_000, 400_000_000, "tetra_gr"),
    (420_000_000, 429_999_999, "tetra_gr"),
    (430_000_000, 440_000_000, "amateur_70cm"),
    (446_000_000, 446_200_000, "pmr446"),
]

SIGNAL_CATALOG = [  # migration 011, the rows that touch fixture frequencies
    (1, 87_500_000, 108_000_000, "broadcast_fm"),
    (2, 99_500_000, 99_700_000, "broadcast_fm"),
    (5, 112_000_000, 117_975_000, "aviation_nav_vor"),
    (10, 118_000_000, 137_000_000, "aviation_voice"),
    (16, 118_000_000, 137_000_000, "aviation_voice_atc_unverified"),
    (20, 144_000_000, 146_000_000, "amateur_2m"),
    (21, 144_795_000, 144_805_000, "amateur_2m_ssb_call"),
    (22, 144_770_000, 144_780_000, "amateur_2m_voice"),
    (50, 156_025_000, 162_025_000, "maritime_vhf"),
    (54, 156_787_500, 156_812_500, "maritime_vhf_ch16"),
    (56, 158_067_500, 158_092_500, "maritime_coast_duplex"),
    (70, 162_050_000, 174_000_000, "land_mobile_safety"),
    (100, 225_000_000, 400_000_000, "mil_uhf_aero"),
    (110, 380_000_000, 400_000_000, "tetra_gr"),
    (111, 410_000_000, 430_000_000, "tetra_civil"),
]

KNOWN_FREQUENCIES = [  # Athens seed after migration 005, plus 025
    (99_600_000, "Kosmos FM 99.6", "broadcast_fm"),
    (118_575_000, "Athens Approach", "am_airband_atc"),
    (144_775_000, "Greek 2m Ham", "nfm_voice_repeater"),
    (144_800_000, "APRS (IARU Region 1)", "aprs_packet"),
    (156_800_000, "Marine Ch16", "marine_vhf_channel"),
    (158_080_000, "Marine Coast Stn", "marine_vhf_channel"),
    (384_000_000, "Greek TETRA", "tetra"),
    (390_500_000, "TETRA BS ~390.5 (public safety)", "tetra"),
    (391_450_000, "TETRA BS ~391.45 (public safety)", "tetra"),
    (391_750_000, "TETRA BS ~391.75 (public safety)", "tetra"),
    (392_600_000, "TETRA BS ~392.6 (public safety)", "tetra"),
    (424_800_000, "TETRA BS 424.800 (civil)", "tetra"),
    (425_100_000, "TETRA BS 425.100 (civil)", "tetra"),
    (425_425_000, "TETRA BS 425.425 (civil)", "tetra"),
    (425_650_000, "TETRA BS 425.650 (civil)", "tetra"),
]

LISTENING_LOG = [  # migration 014, the four operator entries
    ("2026-04-01 18:00:00", 384.000, "confirmed", "tetra",
     "TETRA emergency services — blast beat pattern, speeds up during storm dispatch. "
     "Source: notes/signal-log.txt.", 384_000_000),
    ("2026-04-02 12:00:00", 446.210, "confirmed", "nfm_voice_repeater",
     "PMR446 walkie-talkie — ringing preamble + transmission. Source: notes/signal-log.txt.",
     446_210_000),
    ("2026-04-02 14:00:00", 144.775, "confirmed", "nfm_voice_repeater",
     "Greek ham radio 2m band — voice conversation. Source: notes/signal-log.txt.", 144_775_000),
    ("2026-04-02 16:00:00", 156.650, "confirmed", "marine_vhf_channel",
     "Marine Ch13 bridge-to-bridge — English voice, clean signal. Source: notes/signal-log.txt.",
     156_650_000),
]

SIGNAL_CLASSES = [  # migrations 003, 022 and 025
    ("dvbt_mux", "DVB-T multiplex"), ("am_airband_atis", "Airband ATIS (continuous)"),
    ("am_airband_atc", "Airband ATC (bursty)"), ("nfm_voice_repeater", "NFM voice repeater"),
    ("marine_vhf_channel", "Marine VHF channel"), ("ais", "AIS ship positions"),
    ("broadcast_fm", "Broadcast FM"), ("tetra", "TETRA (encrypted)"),
    ("unknown_continuous", "Unknown continuous"), ("unknown_bursty", "Unknown bursty"),
    ("acars_downlink", "ACARS aircraft downlink"), ("acars_uplink", "ACARS ground uplink"),
    ("aprs_packet", "APRS packet (AX.25, 1200 Bd AFSK)"),
]

# What the classifier has said about each fixture bin, per dongle. The tile
# spurs carry the same wrong `tetra` label as on the host. 144.846 sits within
# 150 kHz of the operator's 144.775 entry, so the classifier's operator
# override has made it nfm_voice_repeater at confidence 1.0. The day-3
# newcomer has no classification yet.
CLASSIFICATIONS = {
    V3: [
        (FM_KOSMOS, "broadcast_fm", 0.8), (FM_KOSMOS_SHOULDER, "broadcast_fm", 0.6),
        (INTERMOD, "unknown_continuous", 0.2),
        (APPROACH_AIRBAND, "am_airband_atc", 0.6), (APPROACH_FULL, "am_airband_atc", 0.6),
        (APRS, "nfm_voice_repeater", 1.0),
        (MARINE_CH16, "marine_vhf_channel", 0.8), (COAST_STATION, "marine_vhf_channel", 0.6),
        (STOPS_ON_DAY3, "unknown_continuous", 0.2), (TILE_SPUR, "unknown_continuous", 0.2),
        *[(f, "tetra", 0.6) for f in TETRA_SPURS],
        *[(f, "tetra", 0.8) for f in TETRA_PS_CONTINUOUS], (TETRA_PS_BURSTY, "tetra", 0.6),
        (TETRA_OUTSIDE, "tetra", 0.4),
        *[(f, "tetra", 0.8) for f in TETRA_CIVIL_CONTINUOUS],
    ],
    V4: [
        (FM_KOSMOS, "broadcast_fm", 0.8), (APPROACH_AIRBAND, "am_airband_atc", 0.4),
        (MARINE_CH16, "marine_vhf_channel", 0.6), (COAST_STATION, "marine_vhf_channel", 0.6),
        (TETRA_SPURS[0], "tetra", 0.6),
        *[(f, "tetra", 0.8) for f in TETRA_PS_CONTINUOUS],
        *[(f, "tetra", 0.8) for f in TETRA_CIVIL_CONTINUOUS],
    ],
}


@dataclass
class Synth:
    """Everything one fixture run produced. Timestamps are aware UTC datetimes."""
    peaks: list[dict] = field(default_factory=list)
    sweep_health: list[dict] = field(default_factory=list)
    signal_classifications: list[dict] = field(default_factory=list)
    known_frequencies: list[dict] = field(default_factory=list)
    listening_log: list[dict] = field(default_factory=list)
    allocations: list[dict] = field(default_factory=list)
    signal_catalog: list[dict] = field(default_factory=list)
    signal_classes: list[dict] = field(default_factory=list)
    end: datetime = START + timedelta(days=DAYS)


# ─── Generator ──────────────────────────────────────────────


def _session_hours(dongle: str, day: int) -> range:
    """UTC hours of the day in which this dongle sweeps."""
    if day == GAP_DAY:
        return range(0)
    if dongle == V3:
        return range(0, 24)
    return range(5 if day == 4 else 7, 19)   # 08:00 or 10:00 to 22:00 local


def _sweep_times(dongle: str, interval_s: int, offset_s: int) -> list[datetime]:
    times = []
    for day in range(DAYS):
        hours = _session_hours(dongle, day)
        day_start = START + timedelta(days=day)
        t = day_start + timedelta(seconds=offset_s)
        while t < day_start + timedelta(days=1):
            if t.hour in hours:
                times.append(t)
            t += timedelta(seconds=interval_s)
    return times


def _gain(dongle: str, t: datetime) -> float:
    if dongle == V4:
        return V4_GAIN
    return V3_GAIN_BEFORE if t < GAIN_CHANGE_AT else V3_GAIN_AFTER


def ch_time(t: datetime) -> str:
    """The text form ClickHouse uses for a DateTime64(3) in UTC."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def build(seed: int = 7) -> Synth:
    rng = random.Random(seed)
    out = Synth()

    for dongle in (V3, V4):
        sweeps = [(t, "full") for t in _sweep_times(dongle, FULL_INTERVAL_S, 0)]
        sweeps += [(t, "airband") for t in _sweep_times(dongle, AIRBAND_INTERVAL_S, AIRBAND_OFFSET_S)]
        sweeps.sort()
        for t, preset in sweeps:
            gain = _gain(dongle, t)
            clipped = (dongle == V3 and preset == "full" and gain == V3_GAIN_BEFORE
                       and rng.random() < V3_CLIP_PROBABILITY)
            sweep_id = f"{preset}:{ch_time(t)}"
            run_id = f"run_{dongle}_{t.strftime('%Y%m%d')}"
            out.sweep_health.append({
                "timestamp": t, "sweep_id": sweep_id, "preset": preset,
                "bin_count": 3820 if preset == "full" else 210,
                "max_power": -12.0 if preset == "full" else -35.0,
                "max_power_dvbt": -20.0, "sweep_duration_ms": 37000 if preset == "full" else 2400,
                "gain_db": gain, "clipped": clipped,
                "max_clip_fraction": 0.07 if clipped else 0.01,
                "worst_clip_freq_hz": 99_264_000 if clipped else 0,
                "clipped_captures": 12 if clipped else 0,
                "total_captures": 1528 if preset == "full" else 80,
                "run_id": run_id, "dongle_id": dongle,
            })
            for tx in TRANSMITTERS:
                _maybe_peak(out, rng, tx, dongle, t, preset, gain, clipped, sweep_id, run_id)

    for dongle, rows in CLASSIFICATIONS.items():
        for freq, class_id, confidence in rows:
            out.signal_classifications.append({
                "freq_hz": freq, "class_id": class_id, "confidence": confidence,
                "reasoning": "{}", "features_snapshot": "{}",
                "classified_at": out.end - timedelta(minutes=5), "dongle_id": dongle,
            })
    out.allocations = [{"freq_start_hz": a, "freq_end_hz": b, "service": s} for a, b, s in ALLOCATIONS]
    out.signal_catalog = [{"id": i, "freq_lo_hz": a, "freq_hi_hz": b, "service": s}
                          for i, a, b, s in SIGNAL_CATALOG]
    out.known_frequencies = [{"freq_hz": f, "name": n, "class_id": c} for f, n, c in KNOWN_FREQUENCIES]
    out.listening_log = [
        {"id": f"log-{i}", "timestamp": datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
         "freq_mhz": mhz, "heard": heard, "class_id": cls, "notes": notes, "confirmed_freq_hz": hz}
        for i, (ts, mhz, heard, cls, notes, hz) in enumerate(LISTENING_LOG)
    ]
    out.signal_classes = [{"class_id": c, "name": n} for c, n in SIGNAL_CLASSES]
    return out


def _maybe_peak(out: Synth, rng: random.Random, tx: Transmitter, dongle: str, t: datetime,
                preset: str, gain: float, clipped: bool, sweep_id: str, run_id: str) -> None:
    if dongle == V4 and tx.v4_offset_db is None:
        return
    if (tx.starts and t < tx.starts) or (tx.stops and t >= tx.stops):
        return
    if tx.clipped_only and not clipped:
        return
    if not any(p == preset for _, p, _ in tx.bins):
        return
    if rng.random() >= tx.probability(t):
        return
    level = tx.level_dbfs + tx.gain_slope * (gain - V3_GAIN_BEFORE)
    if dongle == V4:
        level += tx.v4_offset_db
    if tx.label == "coast_station" and t >= LOUDER_FROM:
        level += LOUDER_DB
    for freq, bin_preset, rel_db in tx.bins:
        if bin_preset != preset:
            continue
        out.peaks.append({
            "timestamp": t, "freq_hz": freq,
            "power_dbfs": round(level + rel_db + rng.gauss(0.0, 1.0), 1),
            "prominence_db": round(rng.uniform(11.0, 25.0), 1),
            "sweep_id": sweep_id, "run_id": run_id, "dongle_id": dongle,
        })


def as_ch_rows(rows: list[dict]) -> list[dict]:
    """Copies of `rows` with datetimes in ClickHouse's text form, ready for JSONEachRow."""
    return [{k: ch_time(v) if isinstance(v, datetime) else v for k, v in r.items()} for r in rows]
