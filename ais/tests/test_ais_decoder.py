"""AIVDM decoder tests. Expected values were cross-checked against pyais 3.2.3
(an independent decoder) on 2026-09-28; pyais is not a project dependency.

Run: python3 -m pytest ais/tests
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ais_decoder as ais  # noqa: E402


def decode(*sentences):
    asm = ais.NMEAAssembler()
    out = None
    for s in sentences:
        out = ais.decode_nmea(s, asm) or out
    return out


def test_type1_position_report():
    m = decode("!AIVDM,1,1,,B,15M67FC000G?ufbE`FepT@3n00Sa,0*5C")
    assert m == {"mmsi": 366053209, "msg_type": 1, "nav_status": 3, "speed": 0.0,
                 "lon": -122.341618, "lat": 37.802118, "course": 219.3, "heading": 1}


def test_type1_heading_not_available_is_omitted():
    # heading 511 means "not available" and must not reach the database
    m = decode("!AIVDM,1,1,,A,13HOI:0P0000VOHLCnHQKwvL05Ip,0*23")
    assert m["mmsi"] == 227006760 and m["lat"] == 49.475577 and m["lon"] == 0.13138
    assert "heading" not in m


def test_type5_static_data_across_two_sentences():
    first = "!AIVDM,2,1,1,A,55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp8,0*1C"
    second = "!AIVDM,2,2,1,A,88888888880,2*25"
    assert decode(first) is None, "first part alone must wait for the second"
    m = decode(first, second)
    assert m == {"mmsi": 351759000, "msg_type": 5, "imo": 9134270, "callsign": "3FOF8",
                 "ship_name": "EVER DIADEM", "ship_type": 70, "dim_bow": 225,
                 "dim_stern": 70, "dim_port": 1, "dim_starboard": 31,
                 "destination": "NEW YORK"}


def test_type18_class_b_position():
    m = decode("!AIVDM,1,1,,A,B6CdCm0t3`tba35f@V9faHi7kP06,0*58")
    assert m == {"mmsi": 423302100, "msg_type": 18, "speed": 1.4, "lon": 53.010997,
                 "lat": 40.005283, "course": 177.0, "heading": 177}


def test_type24_parts_a_and_b():
    a = decode("!AIVDM,1,1,,A,H42O55i18tMET00000000000000,2*6D")
    b = decode("!AIVDM,1,1,,A,H42O55lti4hhhilD3nink000?050,0*40")
    assert a == {"mmsi": 271041815, "msg_type": 24, "ship_name": "PROGUY"}
    assert b == {"mmsi": 271041815, "msg_type": 24, "ship_type": 60, "callsign": "TC6163",
                 "dim_stern": 15, "dim_starboard": 5}


def test_type4_base_station():
    # A base station broadcasts its fixed position.
    m = decode("!AIVDM,1,1,,A,403OviQuMGCqWrRO9>E6fE700@GO,0*4D")
    assert m == {"mmsi": 3669702, "msg_type": 4, "lon": -76.352362, "lat": 36.883767}


def test_type21_aid_to_navigation():
    # A navigation aid (buoy/lighthouse); its name goes into ship_name.
    m = decode("!AIVDM,1,1,,B,E>k`b9J610V60@2ab@0b@@@@@@@0h;Ow?WdMh088i<h1<@P00,4*0F")
    assert m == {"mmsi": 993667621, "msg_type": 21, "ship_name": "LBALL@ EST AT",
                 "lon": 21.050025, "lat": 54.596663,
                 "dim_stern": 65, "dim_port": 6, "dim_starboard": 9}


def test_type27_long_range():
    # Coarse long-range position: 1/10-minute lat/lon, whole-unit speed/course.
    m = decode("!AIVDM,1,1,,B,KC5E2b@U19PFdLbMuc5=ROv62<7m,0*16")
    assert m == {"mmsi": 206914217, "msg_type": 27, "nav_status": 2,
                 "lon": 137.023333, "lat": 4.84, "speed": 57.0, "course": 167.0}


def test_garbage_and_unsupported_return_none():
    assert decode("not an nmea sentence") is None
    assert decode("!AIVDM,1,1,,A,,0*00") is None
    # type 9 (SAR aircraft) is still not decoded
    assert decode("!AIVDM,1,1,,A,900048wwTcw29TR1jHDGSC7`0`1b,0*4A") is None
