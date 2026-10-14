"""The real SQL, on a throwaway ClickHouse, against the Python twins.

Opt-in. CI has no ClickHouse, so every test here skips unless
RF_LUV_SQL_TEST_URL points at a disposable server's admin user:

    docker run -d --rm --name ch-sql-test -p 127.0.0.1:18123:8123 \\
        -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 -e CLICKHOUSE_PASSWORD=throwaway \\
        clickhouse/clickhouse-server:26.8.12.53-alpine
    RF_LUV_SQL_TEST_URL=http://default:throwaway@127.0.0.1:18123 \\
        python3 -m pytest spectrum/tests/test_clickhouse_sql.py
    docker rm -f ch-sql-test

The module builds the spectrum schema the way ch-bootstrap does (init.sql,
the Athens seed, every migration as user spectrum), loads the synthetic
fixture, and checks that each query returns what its twin computes. It
refuses a server whose spectrum database it did not create, so it cannot
touch the live one.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import pytest

import anomalies
import db
import emitters
import migrate
from tests import ch_twin
from tests import emitter_world as world
from tests import synth_sweeps as synth

URL = os.environ.get("RF_LUV_SQL_TEST_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="RF_LUV_SQL_TEST_URL not set (needs a throwaway ClickHouse)")

SPECTRUM = Path(__file__).resolve().parent.parent
MARKER = "spectrum.rf_luv_sql_test_marker"
FIXTURE = synth.build()
SPLIT_AT = int(synth.START.timestamp()) + 4 * 86400 + 3600   # day 4, 01:00 UTC


def _admin(sql: str) -> str:
    u = urlparse(URL)
    req = Request(f"{u.scheme}://{u.hostname}:{u.port}/?user={quote(u.username or 'default')}"
                  f"&password={quote(u.password or '')}", data=sql.encode())
    with urlopen(req, timeout=60) as r:
        return r.read().decode()


@pytest.fixture(scope="module", autouse=True)
def server():
    u = urlparse(URL)
    has_db = _admin("SELECT count() FROM system.databases WHERE name = 'spectrum'").strip() == "1"
    if has_db:
        mine = _admin(f"EXISTS TABLE {MARKER}").strip() == "1"
        if not mine:
            pytest.fail("this server already has a spectrum database that this test did not create")
        _admin("DROP DATABASE spectrum SYNC")
    _admin("CREATE DATABASE spectrum")
    _admin("CREATE USER IF NOT EXISTS spectrum IDENTIFIED BY 'spectrum_local'")
    _admin("GRANT ALL ON spectrum.* TO spectrum")
    _admin(f"CREATE TABLE {MARKER} (x UInt8) ENGINE = Memory")

    original = db.config
    db.config = replace(original, CH_HOST=u.hostname, CH_PORT=str(u.port), CH_DATABASE="spectrum",
                        CH_USER="spectrum", CH_PASSWORD="spectrum_local")
    try:
        for f in (SPECTRUM / "clickhouse" / "init.sql",
                  SPECTRUM / "clickhouse" / "seeds" / "known_frequencies_athens.sql"):
            for stmt in migrate.split_sql_statements(f.read_text()):
                db.query(stmt)
        migrate.run_migrations()
        for table, rows in (("peaks", FIXTURE.peaks), ("sweep_health", FIXTURE.sweep_health),
                            ("signal_classifications", FIXTURE.signal_classifications)):
            db.insert(f"spectrum.{table}", synth.as_ch_rows(rows))
        yield
    finally:
        db.config = original


def _close(a: dict, b: dict) -> bool:
    if a.keys() != b.keys():
        return False
    for k in a:
        if isinstance(a[k], float) or isinstance(b[k], float):
            if abs(float(a[k]) - float(b[k])) > 0.05:
                return False
        elif str(a[k]) != str(b[k]):
            return False
    return True


def _same(sql_rows: list[dict], twin_rows: list[dict]):
    assert len(sql_rows) == len(twin_rows)
    for s, t in zip(sql_rows, twin_rows):
        assert _close(s, t), (s, t)


def test_bin_stats_sql_matches_twin():
    _same(db.query_rows(emitters.bin_stats_sql()), ch_twin.bin_stats(FIXTURE.peaks))


def test_coverage_sql_matches_twin():
    _same(db.query_rows(emitters.coverage_sql()), ch_twin.coverage(FIXTURE.sweep_health))


def test_activity_sql_matches_twin():
    bins = [emitters.BinStat(**r) for r in ch_twin.bin_stats(FIXTURE.peaks)]
    fps = emitters.footprints(emitters.plan(bins, {}))
    _same(db.query_rows(emitters.activity_sql(fps, SPLIT_AT)),
          ch_twin.activity(FIXTURE.peaks, FIXTURE.sweep_health, fps, SPLIT_AT))


def test_emitter_job_writes_and_the_views_read():
    first = emitters.run()
    assert first > 20
    ids = {r["emitter_id"] for r in db.query_rows("SELECT emitter_id FROM spectrum.emitters_current")}
    assert emitters.run() == first
    again = {r["emitter_id"] for r in db.query_rows("SELECT emitter_id FROM spectrum.emitters_current")}
    assert again == ids
    merged = db.query_rows(
        "SELECT dongle_id, also_dongle_id, also_emitter_id FROM spectrum.emitters_merged "
        f"WHERE lo_hz <= {synth.TETRA_PS_CONTINUOUS[1]} AND hi_hz >= {synth.TETRA_PS_CONTINUOUS[1]}")
    assert merged == [{"dongle_id": "v3-01", "also_dongle_id": "v4-01",
                       "also_emitter_id": f"v4-01:{synth.TETRA_PS_CONTINUOUS[1] // 1000}"}]
    solo = db.query_rows("SELECT dongle_id, from_primary, also_emitter_id FROM spectrum.emitters_merged "
                         f"WHERE lo_hz <= {synth.NEW_ON_DAY3} AND hi_hz >= {synth.NEW_ON_DAY3}")
    assert solo == [{"dongle_id": "v3-01", "from_primary": True, "also_emitter_id": ""}]


def test_tetra_views_count_primary_carriers_and_skip_spurs():
    emitters.run()
    nets = {r["network"]: r for r in db.query_rows(
        "SELECT network, carriers, likely_control_channels, bursty, outside_known_allocation, "
        "also_on_secondary, secondary_only, length(diurnal_duty) AS hours FROM spectrum.tetra_networks")}
    assert {k: (v["carriers"], v["likely_control_channels"], v["bursty"]) for k, v in nets.items()} == {
        "public_safety": (3, 2, 1), "civil": (3, 2, 1), "other": (1, 1, 0)}
    assert nets["other"]["outside_known_allocation"] == 1
    assert nets["public_safety"]["outside_known_allocation"] == 0
    assert nets["public_safety"]["also_on_secondary"] == 3
    assert nets["civil"]["also_on_secondary"] == 2
    assert all(v["secondary_only"] == 0 and v["hours"] == 24 for v in nets.values())

    centers = {int(r["center_hz"]) for r in db.query_rows("SELECT center_hz FROM spectrum.tetra_carriers")}
    assert not centers & set(synth.TETRA_SPURS)
    modes = {r["mode"] for r in db.query_rows("SELECT mode FROM spectrum.tetra_carriers")}
    assert modes == {"continuous (likely control channel)", "bursty"}

    hourly = db.query_rows("SELECT network, hour_of_day, carriers, duty FROM spectrum.tetra_network_hourly "
                           "ORDER BY network, hour_of_day")
    assert len(hourly) == 3 * 24
    # Hours the secondary never swept stay NULL in its own carriers' patterns.
    v4 = db.query_rows("SELECT diurnal_duty FROM spectrum.tetra_carriers "
                       f"WHERE dongle_id = 'v4-01' AND center_hz = {synth.TETRA_PS_CONTINUOUS[1]}")
    assert v4[0]["diurnal_duty"][3] is None


def test_levels_sql_matches_twin():
    elist = world.emitter_view(world.run(FIXTURE))
    fps = [e.footprint for e in elist]
    _same(db.query_rows(anomalies.levels_sql(fps, SPLIT_AT)),
          ch_twin.levels(FIXTURE.peaks, FIXTURE.sweep_health, fps, SPLIT_AT))


def test_detection_on_sql_output_matches_the_twin_world(monkeypatch):
    now = world.at(5)
    expected, expected_pick, elist, _ = world.detect(FIXTURE, now)
    fps = [e.footprint for e in elist]
    window_start = emitters.recent_split(now)
    activity = emitters.summarize_activity(db.query_rows(emitters.activity_sql(fps, window_start)))
    levels = anomalies.group_levels(db.query_rows(anomalies.levels_sql(fps, window_start)))
    coverage = emitters.Coverage([emitters.CoverageRow(**r) for r in db.query_rows(emitters.coverage_sql())])
    roles = {r["dongle_id"]: anomalies.Role(bool(r["is_primary"]), bool(r["levels_trusted"]))
             for r in db.query_rows(anomalies.ROLES_SQL)}
    assert roles == world.ROLES
    ts = int(now.timestamp())
    found = anomalies.detect(elist, activity, coverage, levels, roles, ts)
    assert found == expected
    assert anomalies.signal_of_the_day(elist, found, roles, ts) == expected_pick
    db.insert("spectrum.anomalies", anomalies.anomaly_rows(found, now))
    db.insert("spectrum.signal_of_the_day", [anomalies.pick_row(expected_pick, "2026-10-09", now)])
    assert db.query_scalar("SELECT count() FROM spectrum.anomalies FINAL") == len(found)


def test_the_job_sends_no_digest_when_nothing_was_swept(monkeypatch):
    # Wall-clock now is days after the fixture: the window holds no sweeps.
    sent = []
    monkeypatch.setattr(anomalies, "send_digest", lambda *a: sent.append(a))
    emitters.run()
    before = db.query_scalar("SELECT count() FROM spectrum.anomalies")
    assert anomalies.run(send=True) == []
    assert sent == []
    assert db.query_scalar("SELECT count() FROM spectrum.anomalies") == before
