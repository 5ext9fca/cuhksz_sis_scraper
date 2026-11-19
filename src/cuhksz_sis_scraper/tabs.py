"""Scope scraper-created tabs and popups to the query that opened them."""
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import BrowserContext, BrowserType, Error as PlaywrightError, Page

from .config import Browser


def close_page(page: Page) -> None:
    if not page.is_closed():
        try:
            page.close()
        except PlaywrightError:
            if not page.is_closed():
                raise


class QueryTabs(ExitStack):
    def __init__(self):
        super().__init__()
        self._owned: set[Page] = set()

    def watch(self, page: Page) -> None:
        """Track popup descendants while leaving the supplied page with its caller."""
        page.on("popup", self.own)
        self.callback(page.remove_listener, "popup", self.own)

    def own(self, page: Page) -> None:
        if page not in self._owned:
            self._owned.add(page)
            self.callback(self._close_owned, page)
            self.watch(page)

    def open(self, context: BrowserContext) -> Page:
        page = context.new_page()
        self.own(page)
        return page

    def release(self, page: Page) -> Page:
        """Transfer the usable component to the caller, cleaning up the login tabs."""
        self._owned.discard(page)
        return page

    def _close_owned(self, page: Page) -> None:
        if page in self._owned:
            close_page(page)


@contextmanager
def query_popups(page: Page) -> Iterator[None]:
    with QueryTabs() as tabs:
        tabs.watch(page)
        yield


def _restored_query(url: str) -> bool:
    location = urlsplit(url)
    if location.hostname != "sis.cuhk.edu.cn":
        return False
    component = location.path.rsplit("/", 1)[-1]
    if component in {"SA_LEARNER_SERVICES.CLASS_SEARCH.GBL",
                     "SA_LEARNER_SERVICES.SSR_SSENRL_LIST.GBL",
                     "ESTABLISH_COURSES.SSS_BROWSE_CATLG.GBL"}:
        return True
    return (component == "WEBLIB_PTPP_SC.HOMEPAGE.FieldFormula.IScript_AppHP"
            and parse_qs(location.query).get("pt_fname") in (
                ["HCCC_ENROLLMENT"], ["HCSR_COURSE_CATALOG"]))


@contextmanager
def export_session(context: BrowserContext) -> Iterator[BrowserContext]:
    """Clean a scraper-launched profile before Chromium saves its next session."""
    original = context.pages
    try:
        yield context
    finally:
        try:
            # Restored pages have no opener or ownership callbacks in this driver.
            # Keep unrelated restored tabs and at most one neutral startup tab.
            neutral = {"about:blank", "chrome://newtab/", "chrome://new-tab-page/", "edge://newtab/"}
            keep = {page for page in original if not page.is_closed()
                    and page.url not in neutral and not _restored_query(page.url)}
            if not keep:
                blank = next((page for page in original if not page.is_closed() and page.url in neutral), None)
                # Closing Chromium's last tab can terminate the persistent context.
                keep.add(blank or context.new_page())
            for page in context.pages:
                if page not in keep:
                    close_page(page)
        finally:
            context.close()


def launch_context(chromium: BrowserType, browser: Browser) -> BrowserContext:
    # Keep session cookies across restarts. Chromium restores its own window;
    # Playwright's additional about:blank argument would add a tab on each launch.
    return chromium.launch_persistent_context(
        browser.profile_dir, channel=browser.channel, headless=browser.headless,
        locale="zh-CN", args=["--restore-last-session"], ignore_default_args=["about:blank"])
