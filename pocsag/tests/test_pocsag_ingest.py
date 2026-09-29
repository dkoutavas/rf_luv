"""multimon-ng POCSAG line -> pocsag.messages row.

Run: python3 -m pytest pocsag/tests
All inputs are synthetic; no captured pager content is used.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pocsag_ingest as ingest  # noqa: E402


def test_alpha_page():
    row = ingest.parse_line(
        "POCSAG1200: Address: 1234567  Function: 3  Alpha:   ON CALL TEAM B",
        freq_hz=169600000, dongle_id="v3-01")
    assert row["protocol"] == "POCSAG1200"
    assert row["capcode"] == 1234567
    assert row["function"] == 3
    assert row["msg_type"] == "alpha"
    assert row["message"] == "ON CALL TEAM B"
    assert row["freq_hz"] == 169600000 and row["dongle_id"] == "v3-01"


def test_numeric_page():
    row = ingest.parse_line("POCSAG512: Address:  123456  Function: 0  Numeric: 12345")
    assert row["protocol"] == "POCSAG512"
    assert row["capcode"] == 123456
    assert row["msg_type"] == "numeric"
    assert row["message"] == "12345"


def test_tone_only_page():
    # No content section: a tone-only page.
    row = ingest.parse_line("POCSAG1200: Address: 1234567  Function: 0")
    assert row["capcode"] == 1234567
    assert row["msg_type"] == "tone"
    assert row["message"] == ""


def test_empty_alpha():
    row = ingest.parse_line("POCSAG2400: Address: 7654321  Function: 1  Alpha:")
    assert row["protocol"] == "POCSAG2400"
    assert row["msg_type"] == "alpha"
    assert row["message"] == ""


def test_non_pocsag_line_is_ignored():
    assert ingest.parse_line("Enabled demodulators: POCSAG512 POCSAG1200") is None
    assert ingest.parse_line("") is None
    assert ingest.parse_line("some garbage") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
