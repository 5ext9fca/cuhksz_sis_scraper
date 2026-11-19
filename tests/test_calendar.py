"""Registry refresh, isolated from live network and system time."""
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from cuhksz_sis_scraper import ScheduleError
from cuhksz_sis_scraper.calendar import current_term, update_calendar, _INDEX


def calendar_page():
    rows = [
        ("First Term", "6 September 2027 - 17 December 2027"),
        ("Course examinations", "18 December 2027 - 24 December 2027"),
        ("Second Term", "10 January 2028 - 12 May 2028"),
        ("Course examinations", "14 May 2028 - 21 May 2028"),
        ("Summer Session", "1 June 2028 - 15 July 2028"),
        ("Course examinations", "17 July 2028 - 19 July 2028"),
    ]
    table = "".join(f"<tr><td>{label}</td><td>{dates}</td></tr>" for label, dates in rows)
    return f"<title>Academic Calendar 2027-28</title><h3>Teaching Terms</h3><table>{table}</table>".encode()


class CalendarTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "calendar.json"
        self.source = "https://registry.cuhk.edu.cn/en/page/next"
        pages = {_INDEX: b'<a href="/en/page/next">Academic Calendar 2027-28 (Tentative)</a>',
                 self.source: calendar_page()}
        self.pages = pages
        mocked = patch("cuhksz_sis_scraper.calendar.download", side_effect=pages.__getitem__)
        self.download = mocked.start()
        self.addCleanup(mocked.stop)

    def test_discovers_future_year_and_checks_breaks_and_summer(self):
        for day, expected in [("2027-12-24", "2710"), ("2027-12-25", None),
                              ("2028-01-09", None), ("2028-01-10", "2720"),
                              ("2028-05-21", "2720"), ("2028-05-22", None),
                              ("2028-05-31", None), ("2028-06-01", "2750"),
                              ("2028-07-19", "2750"), ("2028-07-20", None),
                              ("2030-10-01", None)]:
            with self.subTest(day=day):
                if expected:
                    self.assertEqual(current_term(date.fromisoformat(day), cache=self.path), expected)
                else:
                    with self.assertRaisesRegex(ScheduleError, "--term"):
                        current_term(date.fromisoformat(day), cache=self.path)
        self.assertEqual(self.download.call_count, 2)
        cached = json.loads(self.path.read_text())
        self.assertEqual(cached["years"]["2027"]["source"], self.source)
        self.assertEqual(cached["years"]["2027"]["updated_at"], cached["updated_at"])

    def test_refresh_picks_up_revision_and_rejects_partial_year(self):
        cached = update_calendar(self.path)
        cached["updated_at"] = "2020-01-01T00:00:00+00:00"
        self.path.write_text(json.dumps(cached))
        self.pages[self.source] = calendar_page().replace(b"19 July", b"20 July")
        self.assertEqual(current_term(date(2028, 7, 20), cache=self.path), "2750")
        previous = self.path.read_bytes()
        self.pages[self.source] = calendar_page().replace(b"Summer Session", b"Unknown Session")
        with self.assertRaises(ScheduleError):
            update_calendar(self.path)
        self.assertEqual(self.path.read_bytes(), previous)
