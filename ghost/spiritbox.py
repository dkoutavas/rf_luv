#!/usr/bin/env python3
"""
Spirit-box replica — the core of the ghost/ pipeline.

Rebuilds a commercial spirit box (SB7/SB11) from first principles on the SDR
station: sweep the FM broadcast band with no squelch and no lock, dwelling a
fixed ~150 ms per channel, and concatenate the raw demodulated audio into one
WAV. Every "word" in that WAV is a broadcast fragment, and this tool writes down
exactly which station and frequency each fragment came from (via an RDS pre-pass)
so the output is a labelled receipt, not a mystery.

Runs natively on the host (like spectrum/iq_capture.py), holding the ghost dongle
(the bare V3 on rtl_tcp :1235) for the session through the coordinator. Reuses:
  - RTLTCPClient        (spectrum/scanner.py) — the rtl_tcp client
  - dongle_lock         (spectrum/coordinator.py) — the flock dongle lock
  - RDSDemodulator      (rds/rds_decoder.py) — PS/PI/RadioText per station
  - db.insert/query     (spectrum/db.py) — ClickHouse over HTTP
  - dsp.wfm_demod etc.  (ghost/dsp.py) — the demod chain and sweep planner

Everything here is reception or self-generated audio in the flat. No transmit,
no message-payload decode; RDS is public broadcast metadata only.

Usage:
    python3 spiritbox.py --mode forward --dwell-ms 150 --duration 60
    python3 spiritbox.py --mode random --steps 200 --fx
    python3 spiritbox.py --file capture.cs8 --dry-run    # offline, no hardware
"""

import os
import sys
import json
import time
import wave
import argparse
import logging
import datetime as dt

import numpy as np

# ── local imports: reuse the spectrum + rds code in-tree ──────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in (_HERE, os.path.join(_REPO, "spectrum"), os.path.join(_REPO, "rds")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Point the shared ClickHouse client at the ghost database before importing it.
os.environ.setdefault("CLICKHOUSE_DB", os.environ.get("GHOST_CH_DB", "ghost"))
os.environ.setdefault("CLICKHOUSE_USER", os.environ.get("GHOST_CH_USER", "ghost"))
os.environ.setdefault("CLICKHOUSE_PASSWORD", os.environ.get("GHOST_CH_PASSWORD", "ghost_local"))

import dsp  # noqa: E402  (ghost/dsp.py)

try:
    from scanner import RTLTCPClient  # type: ignore  # noqa: E402
except Exception:  # pragma: no cover - only when scanner deps are absent
    RTLTCPClient = None

try:
    from coordinator import dongle_lock  # type: ignore  # noqa: E402
except Exception:  # pragma: no cover
    dongle_lock = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("spiritbox")

# ── config (env-overridable) ──────────────────────────────────────────────────
RTL_TCP_HOST = os.environ.get("RTL_TCP_HOST", "127.0.0.1")
RTL_TCP_PORT = int(os.environ.get("RTL_TCP_PORT", "1235"))   # V3, the ghost dongle
DONGLE_ID = os.environ.get("GHOST_DONGLE_ID", "v3-01")
GAIN_DB = float(os.environ.get("GHOST_GAIN", "12"))   # 12 validated for Athens FM (gain 20 clips); override per site
FM_START = int(os.environ.get("GHOST_FM_START", "87500000"))
FM_END = int(os.environ.get("GHOST_FM_END", "108000000"))
WAV_DIR = os.environ.get("GHOST_WAV_DIR", "/data/rf_luv/ghost/recordings")

FS_RDS = 228000                 # RDSDemodulator's required rate
RDS_DWELL_S = float(os.environ.get("GHOST_RDS_DWELL_S", "2.0"))
RDS_TOP_N = int(os.environ.get("GHOST_RDS_TOP_N", "12"))
WARMUP_BYTES = 8192             # discard after a retune to let the PLL settle
SETTLE_S = 0.005

# ── ClickHouse shims (wrap db.* so the self-test can stub them) ────────────────
def _ch_insert(table: str, rows: list) -> None:
    import db  # imported lazily so --file/--dry-run need no CH reachable
    db.insert(table, rows)


# ── rtl_tcp helpers ───────────────────────────────────────────────────────────
def _default_client_factory():
    if RTLTCPClient is None:
        raise RuntimeError("RTLTCPClient unavailable (numpy/scanner import failed)")
    return RTLTCPClient(RTL_TCP_HOST, RTL_TCP_PORT)


def _read_iq(client, n_complex: int):
    """Read n_complex IQ samples; return (complex64 array, raw u8 bytes)."""
    raw = client.read_samples(n_complex * 2)
    return dsp.cu8_to_complex(raw), raw


def _tune(client, freq_hz: int):
    client.set_frequency(int(freq_hz))
    time.sleep(SETTLE_S)
    client.discard(WARMUP_BYTES)


# ── module 2: RDS station pre-pass ────────────────────────────────────────────
def build_station_table(client, session_id: str, step_hz: int,
                        top_n: int = RDS_TOP_N, dwell_s: float = RDS_DWELL_S) -> dict:
    """Find strong FM carriers and decode their RDS PS/PI/RadioText.

    Returns {freq_hz: {'pi','ps','rt','rssi'}}. Also writes ghost.stations rows.
    Strong-carrier discovery is by RSSI on the channel grid (a real station's
    200 kHz occupies several grid points; the RDS decode confirms which are real).
    """
    from rds_decoder import decode_iq  # local, numpy-only

    # 1. rank the channel grid by a short RSSI probe at the RDS rate.
    client.set_sample_rate(FS_RDS)
    grid = list(range(FM_START, FM_END + 1, step_hz))
    probe_n = int(FS_RDS * 0.05)
    ranked = []
    for f in grid:
        _tune(client, f)
        iq, _ = _read_iq(client, probe_n)
        ranked.append((dsp.rssi_dbfs(iq), f))
    ranked.sort(reverse=True)
    candidates = [f for _, f in ranked[:top_n]]

    # 2. decode RDS on the strongest candidates.
    table, rows = {}, []
    cap_n = int(FS_RDS * dwell_s)
    for f in sorted(candidates):
        _tune(client, f)
        iq, _ = _read_iq(client, cap_n)
        rssi = dsp.rssi_dbfs(iq)
        groups = decode_iq(iq, fs=FS_RDS)
        pi = next((g["pi"] for g in groups if g.get("pi")), 0)
        ps = next((g["ps"] for g in reversed(groups) if g.get("ps")), "")
        rt = next((g["radiotext"] for g in reversed(groups) if g.get("radiotext")), "")
        pty = next((g["pty"] for g in reversed(groups) if g.get("pty")), 0)
        if pi or ps:
            table[f] = {"pi": pi, "ps": ps, "rt": rt, "rssi": rssi}
            rows.append({
                "freq_hz": int(f), "pi_code": int(pi), "ps": ps, "radiotext": rt,
                "pty": int(pty), "rssi_dbfs": round(rssi, 1),
                "session_id": session_id,
                "last_seen": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            })
            log.info("RDS %0.1f MHz -> PI=%04X PS=%r", f / 1e6, pi, ps)
    return table, rows


def _label_for(freq_hz: int, stations: dict, tol_hz: int = 100000):
    """Nearest RDS-identified station within tol_hz, or ('', 0, '')."""
    best, bestd = None, tol_hz + 1
    for f, info in stations.items():
        d = abs(f - freq_hz)
        if d < bestd:
            best, bestd = info, d
    if best is None:
        return "", 0, ""
    return best.get("ps", ""), best.get("pi", 0), best.get("rt", "")


# ── the sweep ─────────────────────────────────────────────────────────────────
def run_sweep(client, session_id: str, mode: str, dwell_ms: int, step_hz: int,
              n_steps: int, stations: dict, seed=None):
    """Sweep the FM band; return (audio float array @48k, list of step dicts)."""
    client.set_sample_rate(dsp.FS_CAPTURE)
    plan = dsp.plan_sweep(FM_START, FM_END, step_hz, mode, seed=seed)
    if n_steps > 0:
        # repeat the plan to reach the requested step count (a spirit box loops)
        plan = (plan * ((n_steps // len(plan)) + 1))[:n_steps]
    cap_n = int(dsp.FS_CAPTURE * dwell_ms / 1000.0)

    audio_parts, steps = [], []
    t_cursor = 0.0
    for idx, f in enumerate(plan):
        _tune(client, f)
        iq, raw = _read_iq(client, cap_n)
        seg = dsp.wfm_demod(iq)
        dur = seg.size / dsp.FS_AUDIO
        ps, pi, rt = _label_for(f, stations)
        steps.append({
            "step_idx": idx, "t_start": round(t_cursor, 4),
            "t_end": round(t_cursor + dur, 4), "freq_hz": int(f),
            "dwell_ms": int(dwell_ms), "rssi_db": round(dsp.rssi_dbfs(iq), 1),
            "clip_fraction": round(dsp.clip_fraction_u8(raw), 4),
            "rds_pi": int(pi), "rds_ps": ps, "rds_rt": rt,
        })
        audio_parts.append(seg)
        t_cursor += dur
    audio = np.concatenate(audio_parts) if audio_parts else np.zeros(0)
    return audio, steps


# ── output ────────────────────────────────────────────────────────────────────
def write_wav(path: str, audio: np.ndarray, fs: int = dsp.FS_AUDIO):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(dsp.to_int16(audio).tobytes())


def write_sidecar(path: str, session_id: str, mode: str, steps: list, fx: dict):
    payload = {
        "session_id": session_id, "sweep_mode": mode, "sample_rate_hz": dsp.FS_AUDIO,
        "fx": fx, "steps": steps,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def spins_rows(session_id: str, mode: str, steps: list, wav_path: str):
    rows = []
    for s in steps:
        ps = s["rds_ps"]
        rows.append({
            "timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "session_id": session_id, "sweep_mode": mode, "step_idx": s["step_idx"],
            "freq_hz": s["freq_hz"], "dwell_ms": s["dwell_ms"],
            "rssi_dbfs": s["rssi_db"], "clip_fraction": s["clip_fraction"],
            "rds_pi": s["rds_pi"], "rds_ps": ps, "rds_rt": s["rds_rt"],
            "station_name": ps, "wav_path": wav_path, "dongle_id": DONGLE_ID,
        })
    return rows


# ── modes ─────────────────────────────────────────────────────────────────────
def run_live(args, client_factory=None):
    client_factory = client_factory or _default_client_factory
    session_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    wav_path = os.path.join(WAV_DIR, f"{session_id}.wav")
    json_path = os.path.join(WAV_DIR, f"{session_id}.json")
    step_hz = args.step_khz * 1000

    def _session(client):
        client.set_gain(GAIN_DB)
        stations, station_rows = ({}, [])
        if not args.no_rds:
            log.info("RDS pre-pass: ranking + decoding up to %d FM carriers", RDS_TOP_N)
            stations, station_rows = build_station_table(client, session_id, step_hz)
        audio, steps = run_sweep(client, session_id, args.mode, args.dwell_ms,
                                 step_hz, args.steps, stations, seed=args.seed)
        fx = {}
        if args.fx:
            fx = {"slapback_ms": 90.0, "feedback": 0.25, "reverb_decay": 0.3}
            audio = dsp.slapback(audio, dsp.FS_AUDIO, fx["slapback_ms"], fx["feedback"])
            audio = dsp.reverb(audio, dsp.FS_AUDIO, fx["reverb_decay"])
        write_wav(wav_path, audio)
        write_sidecar(json_path, session_id, args.mode, steps, fx)
        log.info("wrote %s (%d steps, %.1fs audio) + sidecar", wav_path, len(steps),
                 audio.size / dsp.FS_AUDIO)
        if not args.dry_run:
            if station_rows:
                _ch_insert("stations", station_rows)
            _ch_insert("spins", spins_rows(session_id, args.mode, steps, wav_path))
            log.info("wrote %d spins + %d stations to ClickHouse", len(steps), len(station_rows))
        else:
            log.info("--dry-run: skipped ClickHouse writes")
        return wav_path

    # Hold the dongle for the whole session (two-dongle host: no contention, but
    # correct for the watchdog and any future consumer).
    if dongle_lock is not None and not args.no_lock:
        with dongle_lock(DONGLE_ID, mode="wait"):
            client = client_factory()
            try:
                return _session(client)
            finally:
                client.close()
    else:
        client = client_factory()
        try:
            return _session(client)
        finally:
            client.close()


def run_file(args):
    """Offline: demod a .cs8 capture straight to a WAV. No hardware, no DB."""
    rate = args.rate
    sidecar = os.path.splitext(args.file)[0] + ".json"
    if rate is None and os.path.exists(sidecar):
        try:
            rate = int(json.load(open(sidecar)).get("sample_rate_hz", dsp.FS_CAPTURE))
        except Exception:
            rate = dsp.FS_CAPTURE
    rate = rate or dsp.FS_CAPTURE
    raw = np.fromfile(args.file, dtype=np.int8)
    iq = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 127.5
    audio = dsp.wfm_demod(iq.astype(np.complex64), fs_in=rate)
    out = args.out or (os.path.splitext(args.file)[0] + "_spiritbox.wav")
    write_wav(out, audio)
    log.info("offline: %s -> %s (%.1fs @ %d Hz)", args.file, out,
             audio.size / dsp.FS_AUDIO, rate)
    return out


def build_parser():
    p = argparse.ArgumentParser(description="Spirit-box replica (FM sweep -> labelled WAV)")
    p.add_argument("--mode", choices=["forward", "reverse", "random"], default="forward")
    p.add_argument("--dwell-ms", type=int, default=150)
    p.add_argument("--step-khz", type=int, default=100, help="channel grid spacing")
    p.add_argument("--steps", type=int, default=0, help="fixed step count (0 = one pass)")
    p.add_argument("--seed", type=int, default=None, help="random-sweep seed")
    p.add_argument("--fx", action="store_true", help="apply slapback+reverb post-fx")
    p.add_argument("--no-rds", action="store_true", help="skip the RDS station pre-pass")
    p.add_argument("--no-lock", action="store_true", help="skip the coordinator dongle lock")
    p.add_argument("--dry-run", action="store_true", help="write WAV+sidecar but no ClickHouse")
    p.add_argument("--file", help="offline: demod a .cs8 capture, no hardware")
    p.add_argument("--rate", type=int, default=None, help="sample rate for --file (else sidecar)")
    p.add_argument("--out", help="output WAV path for --file")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.file:
        run_file(args)
    else:
        run_live(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
