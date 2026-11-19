"""Tab lifetimes during schedule reads, retries and persistent-browser restarts."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from sis_scrawler import LoginRequired, Profile, schedule
from sis_scrawler.config import Browser
from sis_scrawler.tabs import launch_context


class TabTests(unittest.TestCase):
    def test_repeated_exports_close_attempt_pages_before_session_restore(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "browser.toml"
            config.write_text('profile_dir="session"\nheadless=true\n' + (
                'channel="' + os.environ["SIS_TEST_CHANNEL"] + '"'
                if os.getenv("SIS_TEST_CHANNEL") else ""))
            attempts = []
            sizes = []

            def open_schedule(ctx, *args):
                if attempts:
                    self.assertTrue(attempts[-1].is_closed())
                sizes.append(len(ctx.pages))
                page = ctx.new_page()
                page.goto("data:text/plain,query")
                attempts.append(page)
                return page

            with patch.object(schedule, "_open_schedule", side_effect=open_schedule), \
                 patch.object(schedule, "fetch_courses", side_effect=[
                     LoginRequired("expired"), {"terms": []}, {"terms": []}]):
                for _ in range(2):
                    schedule.export_courses(root / "out.json", profile=Profile(username="test", password="test"),
                                            browser_file=config, on_status=lambda _: None)
            with sync_playwright() as pw:
                context = launch_context(pw.chromium, Browser.load(config))
                try:
                    self.assertEqual(sizes, [1, 1, 1])
                    self.assertEqual(len(context.pages), 1)
                    self.assertNotEqual(context.pages[0].url, "data:text/plain,query")
                finally:
                    context.close()

    def test_borrowed_schedule_closes_nested_popups_on_interrupt(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=os.getenv("SIS_TEST_CHANNEL"))
            context = browser.new_context()
            page = context.new_page()
            other = context.new_page()

            def read(frame, term, *args):
                self.assertEqual(context.pages, [page, other])
                with page.expect_popup() as first:
                    page.evaluate("window.open('about:blank'); void 0")
                with first.value.expect_popup():
                    first.value.evaluate("window.open('about:blank'); void 0")
                if term == "second":
                    raise KeyboardInterrupt
                return []

            with patch.object(schedule, "_wait_for_schedule", return_value=page.main_frame), \
                 patch.object(schedule, "_term_list"), \
                 patch.object(schedule, "_terms", return_value=[("first", ""), ("second", "")]), \
                 patch.object(schedule, "_read_term", side_effect=read), \
                 self.assertRaises(KeyboardInterrupt):
                schedule.fetch_courses(page)
            self.assertEqual(context.pages, [page, other])
            browser.close()
