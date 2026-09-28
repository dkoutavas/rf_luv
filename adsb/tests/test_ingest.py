"""SBS BaseStation line -> adsb.positions row.

Run: python3 -m pytest adsb/tests
"""
import os
import sys

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
