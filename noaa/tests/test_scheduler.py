"""NOAA scheduler de-duplication.

Run: python3 -m pytest noaa/tests
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scheduler  # noqa: E402


def test_already_scheduled_matches_satellite_and_start():
    known = [{"satellite": "NOAA 19", "pass_start": "2026-09-28 10:00:00"}]
    assert scheduler.already_scheduled(known, "2026-09-28 10:00:00", "NOAA 19")
    assert not scheduler.already_scheduled(known, "2026-09-28 10:00:00", "NOAA 15")
    assert not scheduler.already_scheduled(known, "2026-09-28 11:40:00", "NOAA 19")
    assert not scheduler.already_scheduled([], "2026-09-28 10:00:00", "NOAA 19")
