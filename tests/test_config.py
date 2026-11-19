"""Configuration file boundaries and catalog/CLI selection."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sis_scrawler import Profile, ScheduleError, export_courses
from sis_scrawler.cli import catalog_main
from sis_scrawler.config import Browser
from sis_scrawler.schemes import profile_plan


class ConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(tmp.name)

    def test_defaults_and_browser_relative_paths(self):
        self.assertEqual(Profile.load(), Profile())
        self.assertEqual(Browser.load().login_timeout_ms, 180_000)
        Path("cfg").mkdir()
        path = Path("cfg/browser.toml")
        path.write_text('channel="chrome"\nheadless=true\nprofile_dir="session"\nlogin_timeout=7')
        browser = Browser.load(path)
        self.assertEqual(browser, Browser(Path("cfg/session").resolve(), "chrome", True, 7000))
        with self.assertRaises(ScheduleError):
            Profile.load("missing.toml")

    def test_query_term_and_misplaced_options_rejected(self):
        for loader, contents in [(Browser.load, 'term="2620"'), (Profile.load, 'term="2620"'),
                                 (Profile.load, 'headless=true'), (Browser.load, 'password="secret"'),
                                 (Profile.load, 'username="user"'), (Profile.load, 'cohort=true'),
                                 (Profile.load, 'password="secret')]:
            with self.subTest(contents=contents):
                Path("custom.toml").write_text(contents)
                with self.assertRaises(ScheduleError) as error:
                    loader("custom.toml")
                self.assertNotIn("secret", str(error.exception))

    def test_profile_cohort_is_used_for_web_edition(self):
        Path("profile.toml").write_text('major="甲"\ncohort=2025\nstream="CE"')
        with patch("sis_scrawler.schemes.load_study_plan") as load:
            profile_plan(Profile.load())
            load.assert_called_once_with("甲", source=None, cohort=2025, stream="CE")
            profile_plan(Profile.load(), major="乙", cohort=2024)
            load.assert_called_with("乙", source=None, cohort=2024, stream=None)

    def test_catalog_cli_with_local_scheme_profile_and_explicit_term(self):
        Path("profile.toml").write_text('major="甲"\ncohort=2025')
        Path("browser.toml").write_text("invalid browser config is never used offline")
        Path("plans.json").write_text('{"甲":{"必修":{"CSC1001":"A"}}}')
        Path("catalog.json").write_text(json.dumps({
            "term": "2620", "term_label": "第二学期", "subjects": ["CSC"],
            "courses": [{"course_code": "CSC 1001"}, {"course_code": "CSC 1002"}]}))
        argv = ["sis-catalog", "--scheme", "plans.json", "--from-catalog", "catalog.json",
                "--term", "2620"]
        with patch("sys.argv", argv):
            catalog_main()
        report = json.loads(Path("catalog-major.json").read_text())
        self.assertEqual(report["term"], "2620")
        self.assertEqual([c["course_code"] for c in report["courses"]], ["CSC 1001"])

    def test_export_api_uses_browser_and_login_defaults(self):
        Path("profile.toml").write_text('username="student"\npassword="dummy-pass"')
        Path("browser.toml").write_text('channel="chrome"\nheadless=true\nlogin_timeout=9')
        with patch("sis_scrawler.schedule.sync_playwright") as playwright, \
             patch("sis_scrawler.schedule._open_schedule") as opened, \
             patch("sis_scrawler.schedule.fetch_courses", return_value={"terms": []}):
            export_courses("schedule.json")
        launcher = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
        self.assertTrue(launcher.call_args.kwargs["headless"])
        self.assertEqual(launcher.call_args.kwargs["channel"], "chrome")
        self.assertEqual(opened.call_args.args[1:3], (True, 9000))
        self.assertEqual(opened.call_args.args[-1].username, "student")
        self.assertNotIn("dummy-pass", repr(opened.call_args.args[-1]))
