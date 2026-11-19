"""Scope scraper-created tabs and popups to the query that opened them."""
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

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


def launch_context(chromium: BrowserType, browser: Browser) -> BrowserContext:
    # Keep session cookies across restarts. Chromium restores its own window;
    # Playwright's additional about:blank argument would add a tab on each launch.
    return chromium.launch_persistent_context(
        browser.profile_dir, channel=browser.channel, headless=browser.headless,
        locale="zh-CN", args=["--restore-last-session"], ignore_default_args=["about:blank"])
