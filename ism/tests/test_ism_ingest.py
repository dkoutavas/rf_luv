"""rtl_433 JSON -> ism.events row.

Run: python3 -m pytest ism/tests
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ism_ingest as ii  # noqa: E402


def test_weather_sensor():
    data = {"time": "2026-09-28 12:00:00", "model": "Acurite-Tower", "id": 1234,
            "channel": "A", "battery_ok": 1, "temperature_C": 21.5, "humidity": 48,
            "mic": "CHECKSUM"}
    row = ii.extract_fields(data)
    assert row["timestamp"] == "2026-09-28 12:00:00.000"
    assert (row["model"], row["device_id"], row["channel"]) == ("Acurite-Tower", "1234", "A")
    assert (row["temperature_c"], row["humidity"], row["battery_ok"]) == (21.5, 48.0, 1.0)
    assert json.loads(row["raw_json"])["mic"] == "CHECKSUM"   # unmapped fields kept


def test_no_model_is_dropped():
    assert ii.extract_fields({"time": "2026-09-28 12:00:00", "id": 1}) is None


def test_non_numeric_value_is_skipped():
    row = ii.extract_fields({"model": "X", "temperature_C": "n/a", "humidity": "55"})
    assert "temperature_c" not in row and row["humidity"] == 55.0
