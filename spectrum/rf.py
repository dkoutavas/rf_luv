#!/usr/bin/env python3
"""
rf_luv: two commands to look closer.

  rf.py now [--dongle v3-01] [--minutes 10]
      What is on air right now. Maps the peaks of the last few minutes onto
      the emitters and ranks them by the same interest score as the signal
      of the day. Artifact suspects are marked and sorted last.

  rf.py autopsy <MHz> [--dongle v3-01] [--duration S] [--gain DB]
      Captures raw IQ there with iq_capture.py --capture, runs
      analysis/blind_analyze.py on the file, and prints an identity card:
      the emitter's name and history next to the blind analyzer's verdict.

autopsy is glue. It shells out to the existing tools and re-implements
neither capture nor analysis. It refuses before it touches the dongle when:

  * iq_capture.freq_blocked() blocks the frequency. That is the one legal
    blocklist (TETRA 380-400 MHz and three cellular bands). This file does
    not keep a copy.
  * the capture window, 2.048 MHz wide around the frequency, would include
    a TETRA emitter from spectrum.emitters, on any dongle, wherever it is.
    That covers the civil carriers at 424-426 MHz, which the blocklist does
    not, and any TETRA carrier found outside the known bands.
  * the emitter table cannot be read, because then it cannot rule TETRA out.

TETRA is encrypted. Listening is legal, decoding it is not, and capturing it
to decode is out of scope. Nothing here tries.

autopsy defaults to the V3, the primary dongle, and reads its rtl_tcp port
from /etc/rtl-scanner/<serial>.env, as ops/spectrum-monitor/freshness-probe.py
does. The capture briefly takes the dongle from its scanner through the
coordinator lock.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anomalies
import db
import emitters
import iq_capture
from anomalies import Emitter

SPECTRUM = Path(__file__).resolve().parent
DEFAULT_DONGLE = "v3-01"                     # the primary dongle (dongle_roles)
ENV_DIR = "/etc/rtl-scanner"
RTL_MIN_HZ, RTL_MAX_HZ = 24_000_000, 1_766_000_000   # the R820T and R828D tuning range
AUTOPSY_SOURCE = "operator:rf-autopsy"

EMITTERS_SQL = (
    "SELECT emitter_id, dongle_id, center_hz, lo_hz, hi_hz, "
    "toUnixTimestamp(first_seen) AS first_seen, toUnixTimestamp(last_seen) AS last_seen, "
    "duty_overall, sweeps_hit, hours_observed, name, name_source, name_note, class_id, service, "
    "artifact_suspect, artifact_reason, tetra_network, tetra_mode, tetra_evidence "
    "FROM spectrum.emitters_current ORDER BY dongle_id, center_hz"
)


def _local(ts: int, fmt: str = "%a %H:%M") -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(emitters.LOCAL_TZ)).strftime(fmt)


def _sql_str(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ─── Arguments ──────────────────────────────────────────────


def parse_mhz(text: str) -> int:
    """'144.8' -> 144800000 Hz, inside the dongle's tuning range."""
    try:
        mhz = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a frequency in MHz: {text!r}")
    hz = round(mhz * 1e6)
    if not RTL_MIN_HZ <= hz <= RTL_MAX_HZ:
        raise argparse.ArgumentTypeError(
            f"{mhz:g} MHz is outside the dongle's range ({RTL_MIN_HZ / 1e6:g}-{RTL_MAX_HZ / 1e6:g} MHz)")
    return hz


def positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive whole number")
    return value


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rf.py", description="Look closer at the spectrum.")
    sub = ap.add_subparsers(dest="command", required=True)
    now = sub.add_parser("now", help="what is on air right now")
    now.add_argument("--dongle", default=DEFAULT_DONGLE)
    now.add_argument("--minutes", type=positive_int, default=10,
                     help="look back this far (default 10, about two full sweeps)")
    autopsy = sub.add_parser("autopsy", help="capture IQ at a frequency and identify it")
    autopsy.add_argument("mhz", type=parse_mhz, help="center frequency in MHz, e.g. 144.8")
    autopsy.add_argument("--dongle", default=DEFAULT_DONGLE)
    autopsy.add_argument("--duration", type=float, help="seconds of IQ (iq_capture's default if unset)")
    autopsy.add_argument("--gain", type=float,
                         help="tuner gain in dB (default: the gain of the dongle's latest sweep)")
    return ap


# ─── rf.py now ──────────────────────────────────────────────


@dataclass(frozen=True)
class OnAir:
    emitter: Emitter
    score: int
    reasons: list[str]
    last_at: int
    power_dbfs: float


def on_air(emitter_list: list[Emitter], recent_peaks: list[dict], kinds: dict[str, set[str]],
           roles: dict[str, anomalies.Role], window_start: int) -> tuple[list[OnAir], int]:
    """Emitters with a peak in the recent rows, most interesting first, and the
    number of peaked bins that belong to no emitter yet (fewer than 3 sweeps)."""
    hits: dict[str, list[dict]] = {}
    unmatched = 0
    for p in recent_peaks:
        owner = next((e for e in emitter_list
                      if e.footprint[0] == p["dongle_id"] and e.footprint[1] <= p["freq_hz"] <= e.footprint[2]),
                     None)
        if owner is None:
            unmatched += 1
        else:
            hits.setdefault(owner.emitter_id, []).append(p)
    heard = [e for e in emitter_list if e.emitter_id in hits]
    rows = []
    for score, reasons, e in anomalies.rank(heard, kinds, roles, window_start):
        mine = hits[e.emitter_id]
        rows.append(OnAir(e, score, reasons, max(int(p["last_at"]) for p in mine),
                          max(float(p["power_dbfs"]) for p in mine)))
    return rows, unmatched


def _why(r: OnAir) -> str:
    """The reasons, plus a TETRA tag. An artifact suspect gets only its mark:
    whatever label its bins carry, it is not presented as a transmitter."""
    e = r.emitter
    if e.artifact_suspect:
        return f"ARTIFACT SUSPECT ({e.artifact_reason}): the receiver's own, not a transmitter"
    why = ", ".join(r.reasons) or "-"
    if e.tetra_network:
        mode = "continuous, likely a control channel" if e.tetra_mode == "continuous" else "bursty"
        why += f"  [TETRA {e.tetra_network}, {mode}]"
    return why


def format_now(rows: list[OnAir], unmatched: int, dongle: str, minutes: int,
               last_sweep_at: int | None, now: int) -> str:
    if last_sweep_at is None or last_sweep_at < now - minutes * 60:
        return (f"{dongle} has not swept in the last {minutes} minutes. It is not listening, so this "
                f"station cannot say what is on air.")
    lines = [f"On air on {dongle}, last {minutes} min (latest sweep {_local(last_sweep_at, '%H:%M:%S')} "
             f"local). Most interesting first.", ""]
    if not rows:
        lines.append("No known emitter peaked.")
    else:
        lines.append(f"{'MHz':>9}  {'name':<34} {'source':<17} {'duty':>5}  {'last':>5}  {'dBFS':>6}  why")
        for r in rows:
            e = r.emitter
            lines.append(f"{e.center_hz / 1e6:9.3f}  {e.name[:34]:<34} {e.name_source:<17} "
                         f"{e.duty_overall:5.0%}  {_local(r.last_at, '%H:%M'):>5}  {r.power_dbfs:6.1f}  "
                         f"{_why(r)}")
    if unmatched:
        lines += ["", f"{unmatched} peaked bins belong to no emitter yet (an emitter needs "
                      f"{emitters.MIN_BIN_SWEEPS} sweeps)."]
    return "\n".join(lines)


def cmd_now(args) -> int:
    rows = load_emitter_rows()
    emitter_list = [Emitter.from_row(r) for r in rows]
    dongle = _sql_str(args.dongle)
    recent = db.query_rows(
        "SELECT dongle_id, freq_hz, toUnixTimestamp(max(timestamp)) AS last_at, "
        "max(power_dbfs) AS power_dbfs FROM spectrum.peaks "
        f"WHERE dongle_id = '{dongle}' AND timestamp > now() - INTERVAL {args.minutes} MINUTE "
        "GROUP BY dongle_id, freq_hz")
    last_sweep = db.query_scalar(
        f"SELECT toUnixTimestamp(max(timestamp)) FROM spectrum.sweep_health WHERE dongle_id = '{dongle}'")
    kinds: dict[str, set[str]] = {}
    for r in db.query_rows(
            "SELECT emitter_id, groupUniqArray(kind) AS kinds FROM spectrum.anomalies FINAL "
            f"WHERE dongle_id = '{dongle}' AND event_at > now() - INTERVAL 24 HOUR GROUP BY emitter_id"):
        kinds[r["emitter_id"]] = set(r["kinds"])
    roles = {r["dongle_id"]: anomalies.Role(bool(r["is_primary"]), bool(r["levels_trusted"]))
             for r in db.query_rows(anomalies.ROLES_SQL)}
    now = datetime.now(timezone.utc)
    found, unmatched = on_air(emitter_list, recent, kinds, roles, emitters.recent_split(now))
    last = int(last_sweep) if last_sweep else None   # max() of nothing reads as 0
    print(format_now(found, unmatched, args.dongle, args.minutes, last, int(now.timestamp())))
    return 0


# ─── rf.py autopsy ──────────────────────────────────────────


def blocked_refusal(freq_hz: int) -> str | None:
    """iq_capture's own blocklist, asked the way an operator capture would be."""
    band = iq_capture.freq_blocked(freq_hz, AUTOPSY_SOURCE)
    if band:
        return (f"Refused: {freq_hz / 1e6:.3f} MHz is in a blocked band ({band}). Capturing it is out "
                f"of scope. Encrypted traffic is never decoded here.")
    return None


def tetra_refusal(freq_hz: int, span_hz: int, emitter_list: list[Emitter]) -> str | None:
    """Refuse when the capture window would include any TETRA emitter, on any dongle.

    Artifact suspects count too. A tile-grid spur with a tetra label is not a
    carrier, but it sits among real ones, and refusing costs nothing.
    """
    lo, hi = freq_hz - span_hz // 2, freq_hz + span_hz // 2
    inside = [e for e in emitter_list if e.tetra_network and e.footprint[1] <= hi and e.footprint[2] >= lo]
    if not inside:
        return None
    e = min(inside, key=lambda x: (abs(x.center_hz - freq_hz), x.dongle_id))
    return (f"Refused: the {span_hz / 1e6:.3f} MHz capture window {lo / 1e6:.3f}-{hi / 1e6:.3f} MHz "
            f"would include TETRA emitter {e.emitter_id} '{e.name}' at {e.center_hz / 1e6:.3f} MHz "
            f"({e.tetra_network}). TETRA is encrypted and stays occupancy-only.")


def rtl_tcp_port(serial: str, env_dir: str = ENV_DIR) -> int | None:
    """RTL_TCP_PORT from the dongle's scanner env file, as freshness-probe.py reads it."""
    try:
        with open(os.path.join(env_dir, f"{serial}.env")) as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key == "RTL_TCP_PORT":
                    return int(value)
    except (OSError, ValueError):
        pass
    return None


def run_tool(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run one of the existing tools with this Python. Its log lines go to the terminal."""
    return subprocess.run([sys.executable, *argv], env=env, stdout=subprocess.PIPE, text=True)


def capture_and_analyze(freq_hz: int, dongle: str, port: int, gain: float | None,
                        duration: float | None) -> tuple[str, dict]:
    """iq_capture.py --capture, then blind_analyze.py --json on the file it wrote."""
    env = {**os.environ, "RTL_TCP_PORT": str(port), "SCAN_DONGLE_ID": dongle}
    argv = [str(SPECTRUM / "iq_capture.py"), "--capture", str(freq_hz)]
    if gain is not None:
        argv += ["--gain", f"{gain:g}"]
    if duration is not None:
        argv += ["--duration", f"{duration:g}"]
    cap = run_tool(argv, env)
    lines = cap.stdout.strip().splitlines()
    if cap.returncode != 0 or not lines:
        raise RuntimeError(f"iq_capture.py --capture failed (exit {cap.returncode})")
    path = lines[-1]
    blind = run_tool([str(SPECTRUM / "analysis" / "blind_analyze.py"), "--file", path, "--json"], env)
    if blind.returncode != 0:
        raise RuntimeError(f"blind_analyze.py failed on {path} (exit {blind.returncode})")
    return path, json.loads(blind.stdout)


def nearest_emitter(freq_hz: int, dongle: str, rows: list[dict]) -> dict | None:
    """The dongle's emitter within one bin of freq_hz, the closest one."""
    near = [r for r in rows if r["dongle_id"] == dongle
            and emitters.within_one_bin(freq_hz, int(r["lo_hz"]), int(r["hi_hz"]))]
    if not near:
        return None
    return min(near, key=lambda r: emitters.distance_to_span(freq_hz, int(r["lo_hz"]), int(r["hi_hz"])))


def format_card(freq_hz: int, dongle: str, emitter: dict | None, verdict: dict, path: str) -> str:
    """The identity card: what the station knows about this frequency, then what the IQ says."""
    lines = ["", f"  ┌─ autopsy {freq_hz / 1e6:.3f} MHz on {dongle} " + "─" * 30]
    if emitter is None:
        lines.append("  │ emitter        : none on record within one bin (100 kHz)")
    else:
        e = emitter
        lines.append(f"  │ emitter        : {e['emitter_id']}, {e['name']} [{e['name_source']}]")
        if e.get("name_note"):
            lines.append(f"  │ same bin       : {e['name_note'].removeprefix('also within one bin: ')}")
        lines.append(f"  │ class          : {e['class_id'] or '-'}")
        lines.append(f"  │ history        : first seen {_local(int(e['first_seen']))}, last "
                     f"{_local(int(e['last_seen']))}, in {float(e['duty_overall']):.1%} of sweeps "
                     f"({int(e['sweeps_hit'])} sweeps over {int(e['hours_observed'])} swept hours)")
        lines.append("  │ artifact?      : "
                     + (f"yes, {e['artifact_reason']}" if e["artifact_suspect"] else "no"))
    baud = verdict.get("baud_hz")
    lines += [
        "  │ ── blind verdict",
        f"  │ modulation     : {verdict['modulation']} (confidence {verdict['confidence']:.2f})",
        f"  │ occupied bw    : {verdict['occupied_bw_hz'] / 1e3:,.1f} kHz",
        f"  │ carrier offset : {verdict['carrier_offset_hz'] / 1e3:+.2f} kHz",
        "  │ symbol rate    : " + ("n/a" if baud is None else f"{baud:,.0f} Bd ({verdict['baud_confidence']})"),
        f"  │ OFDM           : {'yes' if verdict.get('ofdm_flag') else 'no'}",
        f"  │ SNR            : {verdict['snr_db']:.1f} dB",
        f"  │ capture        : {path}",
        "  └" + "─" * 54,
        "  reasoning:",
    ]
    lines += [f"    {r}" for r in verdict.get("reasoning", [])]
    return "\n".join(lines)


def load_emitter_rows() -> list[dict]:
    return db.query_rows(EMITTERS_SQL)


def latest_gain(dongle: str) -> float | None:
    """The gain the scanner's automatic gain control settled on in the dongle's latest sweep."""
    rows = db.query_rows("SELECT gain_db FROM spectrum.sweep_health "
                         f"WHERE dongle_id = '{_sql_str(dongle)}' ORDER BY timestamp DESC LIMIT 1")
    return float(rows[0]["gain_db"]) if rows else None


def cmd_autopsy(args) -> int:
    freq = args.mhz
    refusal = blocked_refusal(freq)
    if refusal:
        print(refusal)
        return 2
    try:
        rows = load_emitter_rows()
    except Exception as exc:  # any failure at all means TETRA cannot be ruled out
        print(f"Refused: cannot read spectrum.emitters_current ({exc}), so a TETRA carrier in the "
              f"capture window cannot be ruled out.")
        return 2
    refusal = tetra_refusal(freq, iq_capture.DEFAULT_SAMPLE_RATE, [Emitter.from_row(r) for r in rows])
    if refusal:
        print(refusal)
        return 2
    port = rtl_tcp_port(args.dongle, ENV_DIR)
    if port is None:
        print(f"No RTL_TCP_PORT in {ENV_DIR}/{args.dongle}.env, so the dongle cannot be reached.")
        return 1
    gain = args.gain if args.gain is not None else latest_gain(args.dongle)
    try:
        path, verdict = capture_and_analyze(freq, args.dongle, port, gain, args.duration)
    except RuntimeError as exc:
        print(str(exc))
        return 1
    print(format_card(freq, args.dongle, nearest_emitter(freq, args.dongle, rows), verdict, path))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return cmd_now(args) if args.command == "now" else cmd_autopsy(args)


if __name__ == "__main__":
    # Importing iq_capture imports scanner.py, which traps SIGINT and SIGTERM
    # for its sweep loop. This is an interactive command, so Ctrl-C must stop
    # it. Only here, so that importing rf (as the tests do) changes nothing.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    raise SystemExit(main())
