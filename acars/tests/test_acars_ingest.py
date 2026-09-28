"""acarsdec JSON -> acars.messages row. The main vector is a real message
decoded on the V3 on 2026-09-27 (acarsdec 0b7ba27).

Run: python3 -m pytest acars/tests
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import acars_ingest as ai  # noqa: E402

REAL = json.loads('{"timestamp": 1790504233.222093, "station_id": "rf_luv-v3-01", '
                  '"channel": 0, "freq": 131.525, "level": -55.0, "noise": -65.5, "error": 1, '
                  '"mode": "2", "label": "_d", "block_id": "2", "ack": "T", "tail": "OH-LZP", '
                  '"flight": "AY1858", "msgno": "S34A", "text": "", "assstat": "skipped", '
                  '"app": {"name": "acarsdec", "ver": "0b7ba27"}}')


def test_real_message():
    row = ai.extract_fields(REAL)
    assert row["timestamp"] == "2026-09-27 10:17:13.222"   # UTC
    assert (row["freq_mhz"], row["channel"], row["level_db"], row["err_count"]) == (131.525, 0, -55.0, 1)
    assert (row["mode"], row["label"], row["block_id"], row["ack"]) == ("2", "_d", "2", "T")
    assert (row["tail"], row["flight"], row["msgno"]) == ("OH-LZP", "AY1858", "S34A")
    assert row["libacars_app"] == "acarsdec"          # from app.name when no libacars block
    assert row["station_id"] == "rf_luv-v3-01"
    assert row["dongle_id"] == ai.DONGLE_ID
    assert row["noise_db"] == -65.5                        # level_db - noise_db = 10.5 dB SNR


def test_no_payload_is_dropped():
    assert ai.extract_fields({"timestamp": 1.0, "freq": 131.525, "level": -40}) is None


def test_stringified_numbers_and_padding():
    row = ai.extract_fields({"freq": "131.725", "level": "bad", "tail": " .D-ABCD ", "flight": "LH123 "})
    assert row["freq_mhz"] == 131.725 and row["level_db"] == 0.0
    assert row["tail"] == ".D-ABCD" and row["flight"] == "LH123"


def test_libacars_block_names_the_app():
    row = ai.extract_fields({"label": "H1", "libacars": {"arinc622": {"msg_type": "adsc"}}})
    assert row["libacars_app"] == "arinc622"
    assert json.loads(row["libacars_json"]) == {"arinc622": {"msg_type": "adsc"}}
