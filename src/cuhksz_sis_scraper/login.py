"""Drive the CUHK(SZ) ADFS sign-in form with profile credentials."""
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError, Locator, Page


class ScheduleError(RuntimeError):
    """SIS did not present the expected data or controls."""


class LoginRequired(ScheduleError):
    """SIS requires the user to authenticate or renew the session."""


def _school_login(url: str) -> bool:
    location = urlsplit(url)
    return (location.scheme == "https" and location.netloc == "sts.cuhk.edu.cn"
            and location.path.rstrip("/") == "/adfs/oauth2/authorize")


class SchoolLogin:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.username_entered = False
        self.submitted = False
        self.language_selected: set[Page] = set()

    @staticmethod
    def _check_form(form: Locator) -> None:
        # Resolve relative actions in the document, including any HTML base URL.
        page_url, action, method = form.evaluate("f => [location.href, f.action, f.method]")
        if not (_school_login(page_url) and _school_login(action) and method.lower() == "post"):
            raise LoginRequired("学校登录表单的提交地址或方式已变化，请检查登录页。")

    def advance(self, page: Page) -> None:
        location = urlsplit(page.url)
        if location.scheme == "https" and location.netloc == "sis.cuhk.edu.cn":
            language = page.get_by_role("link", name="简体中文", exact=True)
            if page not in self.language_selected and language.is_visible():
                self.language_selected.add(page)
                language.click(no_wait_after=True)
            return
        if not _school_login(page.url):
            return
        error = page.locator("#errorText")
        if error.is_visible() and error.inner_text().strip():
            raise LoginRequired("学校登录页提示认证错误，请核对凭据文件后重试。")

        account = page.locator("form#loginFormPaginated")
        password = page.locator("form#loginForm")
        try:
            if not self.username_entered and account.locator("#userNameInput").is_visible():
                self._check_form(account)
                account.locator("#userNameInput").fill(self.username)
                self.username_entered = True
                account.locator("#nextButton").click()
            elif (self.username_entered and not self.submitted
                  and password.locator("#passwordInput").is_visible()):
                self._check_form(password)
                password.locator("#passwordInput").fill(self.password)
                self.submitted = True
                password.locator("#submitButton").click(no_wait_after=True)
        except PlaywrightError:
            # A Playwright action error can contain the argument passed to fill.
            raise LoginRequired("无法操作学校登录表单，请重新运行或使用有窗口的手动登录。") from None
