"""SBS BaseStation line -> adsb.positions row.

Run: python3 -m pytest adsb/tests
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ingest  # noqa: E402

DATE = "2026/09/28,12:00:01.250,2026/09/28,12:00:01.300"


def sbs(msg_type, rest):
    # MSG,type,session,aircraft,hex,flight,dates...,callsign,alt,gs,track,lat,lon,vr,squawk,alert,emerg,spi,ground
    return f"MSG,{msg_type},1,1,4CA2D6,1,{DATE},{rest}"


def test_identification():
    row = ingest.parse_sbs_line(sbs(1, "AEE123  ,,,,,,,,,,,0"))
    assert row["hex_ident"] == "4CA2D6" and row["msg_type"] == 1
    assert row["callsign"] == "AEE123" and row["timestamp"] == "2026-09-28 12:00:01.250"


def test_airborne_position():
    row = ingest.parse_sbs_line(sbs(3, ",11000,,,37.93640,23.94450,,,0,0,0,0"))
    assert (row["altitude"], row["lat"], row["lon"], row["is_on_ground"]) == (11000, 37.9364, 23.9445, 0)
    assert "callsign" not in row


def test_velocity_and_ground_flag():
    row = ingest.parse_sbs_line(sbs(4, ",,452.5,87.3,,,-1216,,0,0,0,-1"))
    assert (row["ground_speed"], row["track"], row["vertical_rate"]) == (452.5, 87.3, -1216)
    assert row["is_on_ground"] == 1


def test_squawk():
    assert ingest.parse_sbs_line(sbs(6, ",,,,,,,7700,1,1,0,0"))["squawk"] == "7700"


def test_rejects():
    assert ingest.parse_sbs_line("") is None
    assert ingest.parse_sbs_line("MSG,3,1,1,4CA2D6") is None                   # too short
    assert ingest.parse_sbs_line("AIR,,1,1,4CA2D6,1," + DATE + ",,,,,,,,,,,,") is None  # not MSG
    assert ingest.parse_sbs_line(sbs(3, ",11000,,,,,,,,,,").replace("4CA2D6", "")) is None  # no hex


def test_quiet_gap_keeps_the_connection(monkeypatch):
    # With few aircraft, readsb can stay silent longer than the ingest's 1 s
    # read timeout. The ingest must keep reading the same connection: the old
    # makefile() reader broke after one timeout and reconnected, losing data.
    line = (sbs(3, ",11000,,,37.93640,23.94450,,,0,0,0,0") + "\r\n").encode()
    server = socket.create_server(("127.0.0.1", 0))
    accepts = []

    def fake_readsb():
        conn, _ = server.accept()
        accepts.append(conn)
        try:
            conn.sendall(line)
            time.sleep(1.5)                  # quiet gap longer than the timeout
            conn.sendall(line[:30])          # one line split across two reads
            time.sleep(0.1)
            conn.sendall(line[30:])
            time.sleep(0.2)
        except OSError:
            pass                             # the old code hung up during the gap
        ingest.running = False
        conn.close()

    inserted = []
    monkeypatch.setattr(ingest, "SBS_HOST", "127.0.0.1")
    monkeypatch.setattr(ingest, "SBS_PORT", server.getsockname()[1])
    monkeypatch.setattr(ingest, "wait_for_clickhouse", lambda: None)
    monkeypatch.setattr(ingest, "insert_batch", lambda rows: inserted.extend(rows) or len(rows))
    monkeypatch.setattr(ingest, "running", True)

    t = threading.Thread(target=fake_readsb, daemon=True)
    t.start()
    ingest.main()
    t.join(5)
    server.close()

    assert len(accepts) == 1
    assert [r["lat"] for r in inserted] == [37.9364, 37.9364]

