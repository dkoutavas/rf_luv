"""rf.py: arguments, both refusals, the glue to the existing tools, and the output.

The live path needs a dongle, so the tools are replaced by a fake runner. The
refusal tests check that the runner is never called.
"""

import argparse
import json
import subprocess
from collections import defaultdict
from datetime import timedelta

import pytest

import anomalies
import emitters
import rf
from tests import emitter_world as world
from tests import synth_sweeps as synth

FIXTURE = synth.build()
NOW = world.at(5)


def _rows():
    """emitters_current as rf.EMITTERS_SQL returns it, at the end of the fixture."""
    out = []
    for r in world.run(FIXTURE, NOW):
        out.append({**r, "first_seen": world._epoch(r["first_seen"]), "last_seen": world._epoch(r["last_seen"])})
    return out


ROWS = _rows()


class FakeTools:
    """Stands in for iq_capture.py and blind_analyze.py."""

    VERDICT = {"modulation": "NFM", "confidence": 0.62, "baud_hz": None, "baud_confidence": "none",
               "occupied_bw_hz": 12_300.0, "carrier_offset_hz": 400.0, "snr_db": 18.0, "ofdm_flag": 0,
               "ofdm_tu_us": None, "ofdm_subcarrier_hz": None, "features": {},
               "reasoning": ["1. envelope flat", "2. frequency wanders: FM"]}

    def __init__(self, capture_exit=0):
        self.calls = []
        self.capture_exit = capture_exit

    def __call__(self, argv, env):
        self.calls.append((argv, env))
        if argv[0].endswith("iq_capture.py"):
            out = "" if self.capture_exit else "/var/lib/spectrum/iq_captures/x_144800_5s.cs8\n"
            return subprocess.CompletedProcess(argv, self.capture_exit, out)
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.VERDICT))


@pytest.fixture
def tools(monkeypatch, tmp_path):
    fake = FakeTools()
    monkeypatch.setattr(rf, "run_tool", fake)
    monkeypatch.setattr(rf, "load_emitter_rows", lambda: ROWS)
    monkeypatch.setattr(rf, "latest_gain", lambda dongle: 3.7)
    (tmp_path / "v3-01.env").write_text("SCAN_DONGLE_ID=v3-01\nRTL_TCP_PORT=1235\n")
    monkeypatch.setattr(rf, "ENV_DIR", str(tmp_path))
    monkeypatch.setattr(rf.iq_capture, "IQ_CAPTURE_DIR", str(tmp_path))   # the preflight finds a writable dir
    return fake


# ─── Arguments ──────────────────────────────────────────────


def test_parse_mhz():
    assert rf.parse_mhz("144.8") == 144_800_000
    assert rf.parse_mhz("425.425") == 425_425_000
    for bad in ("abc", "0", "-5", "2000"):
        with pytest.raises(argparse.ArgumentTypeError):
            rf.parse_mhz(bad)


def test_bad_arguments_exit_before_anything_runs(tools, capsys):
    for argv in (["autopsy"], ["autopsy", "fm"], ["now", "--minutes", "0"], []):
        with pytest.raises(SystemExit) as exc:
            rf.main(argv)
        assert exc.value.code == 2
    assert tools.calls == []


def test_autopsy_defaults_to_the_v3():
    args = rf.build_parser().parse_args(["autopsy", "144.8"])
    assert (args.dongle, args.mhz, args.gain, args.duration) == ("v3-01", 144_800_000, None, None)


# ─── Refusals ───────────────────────────────────────────────


def test_blocked_band_is_refused_before_any_io(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("must not run")
    monkeypatch.setattr(rf, "load_emitter_rows", boom)
    monkeypatch.setattr(rf, "run_tool", boom)
    for mhz, band in (("391.45", "TETRA"), ("935", "GSM/UMTS900")):
        assert rf.main(["autopsy", mhz]) == 2
        assert f"blocked band ({band})" in capsys.readouterr().out


def test_blocked_refusal_asks_iq_capture_across_the_window(monkeypatch):
    seen = []
    monkeypatch.setattr(rf.iq_capture, "freq_blocked", lambda f, src: seen.append((f, src)))
    assert rf.blocked_refusal(100_000_000, 2_048_000) is None
    assert seen[0] == (98_976_000, "operator:rf-autopsy") and seen[-1] == (101_024_000, "operator:rf-autopsy")
    assert all(b - a <= 100_000 for (a, _), (b, _) in zip(seen, seen[1:]))


@pytest.mark.parametrize("mhz", ["379.5", "400.9"])
def test_a_window_reaching_into_a_blocked_band_is_refused(tools, capsys, mhz):
    # The center is outside 380-400 MHz, the 2.048 MHz window is not.
    assert rf.main(["autopsy", mhz]) == 2
    assert "reaches into a blocked band (TETRA)" in capsys.readouterr().out
    assert tools.calls == []


@pytest.mark.parametrize("mhz, carrier", [
    ("425.0", synth.TETRA_CIVIL_CONTINUOUS[1]),     # civil TETRA, not in the blocklist
    ("424.0", synth.TETRA_CIVIL_CONTINUOUS[0]),     # 0.8 MHz away, still inside the 2.048 MHz window
    ("406.0", synth.TETRA_OUTSIDE),                 # TETRA outside every known band
])
def test_a_tetra_emitter_in_the_window_is_refused(tools, capsys, mhz, carrier):
    assert rf.main(["autopsy", mhz]) == 2
    out = capsys.readouterr().out
    assert f"at {carrier / 1e6:.3f} MHz" in out and "TETRA" in out
    assert tools.calls == []


def test_a_window_clear_of_tetra_is_not_refused():
    elist = [anomalies.Emitter.from_row(r) for r in ROWS]
    assert rf.tetra_refusal(422_500_000, 2_048_000, elist) is None
    assert rf.tetra_refusal(144_800_000, 2_048_000, elist) is None


def test_tetra_check_covers_every_dongle():
    v4_only = [anomalies.Emitter.from_row(r) for r in ROWS if r["dongle_id"] == "v4-01"]
    assert rf.tetra_refusal(425_000_000, 2_048_000, v4_only)


def test_unreadable_emitter_table_fails_closed(monkeypatch, capsys):
    def down():
        raise OSError("connection refused")
    monkeypatch.setattr(rf, "load_emitter_rows", down)
    monkeypatch.setattr(rf, "run_tool", lambda *a: pytest.fail("must not capture"))
    assert rf.main(["autopsy", "144.8"]) == 2
    assert "cannot be ruled out" in capsys.readouterr().out


def test_empty_emitter_table_fails_closed(monkeypatch, capsys):
    # Before the emitter job's first run nothing rules out the civil TETRA
    # carriers, which the blocklist does not cover.
    monkeypatch.setattr(rf, "load_emitter_rows", lambda: [])
    monkeypatch.setattr(rf, "run_tool", lambda *a: pytest.fail("must not capture"))
    assert rf.main(["autopsy", "425.1"]) == 2
    assert "is empty" in capsys.readouterr().out


# ─── The glue ───────────────────────────────────────────────


def test_rtl_tcp_port_from_the_env_file(tmp_path):
    (tmp_path / "v3-01.env").write_text("# comment\nRTL_TCP_HOST=127.0.0.1\nRTL_TCP_PORT=1235\n")
    assert rf.rtl_tcp_port("v3-01", str(tmp_path)) == 1235
    assert rf.rtl_tcp_port("v4-01", str(tmp_path)) is None


def test_missing_env_file_stops_before_capture(tools, capsys):
    assert rf.main(["autopsy", "144.8", "--dongle", "v9-99"]) == 1
    assert "No RTL_TCP_PORT" in capsys.readouterr().out
    assert tools.calls == []


def test_autopsy_runs_capture_then_analysis(tools, capsys):
    assert rf.main(["autopsy", "144.8", "--duration", "2"]) == 0
    (cap_argv, cap_env), (blind_argv, _) = tools.calls
    assert cap_argv[0].endswith("spectrum/iq_capture.py")
    assert cap_argv[1:] == ["--capture", "144800000", "--gain", "3.7", "--duration", "2"]
    assert (cap_env["RTL_TCP_PORT"], cap_env["SCAN_DONGLE_ID"]) == ("1235", "v3-01")
    assert blind_argv[0].endswith("spectrum/analysis/blind_analyze.py")
    assert blind_argv[1:] == ["--file", "/var/lib/spectrum/iq_captures/x_144800_5s.cs8", "--json"]
    out = capsys.readouterr().out
    assert "Greek ham radio 2m band [listening_log]" in out
    assert "within one bin : APRS (IARU Region 1) [known_frequencies]" in out
    assert "modulation     : NFM (confidence 0.62)" in out
    assert "2. frequency wanders: FM" in out


def test_autopsy_refuses_without_a_capture_directory(tools, monkeypatch, capsys):
    # iq_capture.py would take the dongle and then fail to open its file
    monkeypatch.setattr(rf.iq_capture, "IQ_CAPTURE_DIR", "/nonexistent/iq_captures")
    assert rf.main(["autopsy", "144.8"]) == 1
    assert tools.calls == []
    assert "ops/spectrum-iq-capture/install.sh" in capsys.readouterr().out


def test_failed_capture_skips_analysis(tools, capsys):
    tools.capture_exit = 1
    assert rf.main(["autopsy", "144.8"]) == 1
    assert len(tools.calls) == 1
    assert "iq_capture.py --capture failed" in capsys.readouterr().out


def test_card_without_an_emitter_and_with_an_artifact():
    card = rf.format_card(141_000_000, "v3-01", None, FakeTools.VERDICT, "/x.cs8")
    assert "none on record within one bin" in card
    spur = rf.nearest_emitter(synth.TILE_SPUR, "v3-01", ROWS)
    card = rf.format_card(synth.TILE_SPUR, "v3-01", spur, FakeTools.VERDICT, "/x.cs8")
    assert "artifact?      : yes, tile_grid" in card


# ─── rf.py now ──────────────────────────────────────────────


def _recent_peaks(minutes=10):
    groups = defaultdict(list)
    for p in FIXTURE.peaks:
        if p["dongle_id"] == "v3-01" and NOW - timedelta(minutes=minutes) <= p["timestamp"] < NOW:
            groups[p["freq_hz"]].append(p)
    return [{"dongle_id": "v3-01", "freq_hz": f, "last_at": int(max(x["timestamp"] for x in ps).timestamp()),
             "power_dbfs": max(x["power_dbfs"] for x in ps)} for f, ps in groups.items()]


def test_now_ranks_and_marks_artifacts_last():
    elist = [anomalies.Emitter.from_row(r) for r in ROWS]
    kinds = {"v3-01:158082": {"louder_than_usual"}}
    rows, unmatched = rf.on_air(elist, _recent_peaks(), kinds, world.ROLES, emitters.recent_split(NOW))
    assert unmatched == 0
    assert {r.emitter.dongle_id for r in rows} == {"v3-01"}
    artifacts = [i for i, r in enumerate(rows) if r.emitter.artifact_suspect]
    assert artifacts and artifacts == list(range(len(rows) - len(artifacts), len(rows)))
    fm = [i for i, r in enumerate(rows) if r.emitter.class_id == "broadcast_fm"]
    assert fm and fm[0] < artifacts[0] and fm[0] == len(rows) - len(artifacts) - 1
    text = rf.format_now(rows, unmatched, "v3-01", 10, int(NOW.timestamp()) - 30, int(NOW.timestamp()))
    assert "ARTIFACT SUSPECT (tile_grid): the receiver's own, not a transmitter" in text
    assert "TETRA civil, continuous, likely a control channel" in text
    assert "louder than usual" in text
    # A tile-grid spur with a tetra label is never shown as a TETRA carrier.
    spur_line = [line for line in text.splitlines() if line.lstrip().startswith("383.962")][0]
    assert "TETRA public_safety" not in spur_line and "ARTIFACT SUSPECT" in spur_line


def test_now_counts_peaks_without_an_emitter():
    elist = [anomalies.Emitter.from_row(r) for r in ROWS]
    stray = [{"dongle_id": "v3-01", "freq_hz": 250_000_000, "last_at": 0, "power_dbfs": -50.0}]
    rows, unmatched = rf.on_air(elist, stray, {}, world.ROLES, 0)
    assert (rows, unmatched) == ([], 1)


def test_now_says_not_listening_when_the_dongle_is_idle():
    now = int(NOW.timestamp())
    for last in (None, now - 11 * 60):
        assert "is not listening" in rf.format_now([], 0, "v3-01", 10, last, now)
