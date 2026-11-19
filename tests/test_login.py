"""Regression checks for expired sessions and the school's stalled SSO callback."""
import os
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from cuhksz_sis_scraper import LoginRequired, Profile
from cuhksz_sis_scraper import schedule


TERM_LIST = '<p>选择学期，然后单击继续。</p><input type="radio" aria-label="选择此行">'
DENIED = "<p>You are not authorized for this page.</p>"


class LoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel=os.getenv("SIS_TEST_CHANNEL"))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)

    def test_expired_session_with_a_restored_schedule(self):
        responses = iter([TERM_LIST, DENIED])
        self.context.route("**/*", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8", body=next(responses)
        ))
        cached = self.context.new_page()
        cached.goto(schedule.SCHEDULE_URL)
        self.assertIsNotNone(schedule._ready_frame(cached))
        with self.assertRaises(LoginRequired):
            schedule._open_schedule(self.context, True, 3000, self.fail)
        self.assertEqual(self.context.pages, [cached])

    def test_stalled_callback_recovers_once_via_manual_entry(self):
        home_visits = 0
        messages = []

        def respond(route):
            nonlocal home_visits
            url = route.request.url
            if url == schedule.SCHEDULE_URL:
                body = TERM_LIST if home_visits == 2 else DENIED
            elif url == schedule.SIS_HOME:
                home_visits += 1
                target = "/sso/dologin.html" if home_visits == 1 else schedule.SCHEDULE_URL
                body = f'<script>location.replace("{target}")</script>'
            elif url.endswith("/sso/dologin.html"):
                body = "<p>Loading...</p>"
            else:
                return route.abort()
            route.fulfill(content_type="text/html; charset=utf-8", body=body)

        self.context.route("**/*", respond)
        with patch.object(schedule, "_CALLBACK_STALL_SECONDS", 0.3):
            page = schedule._open_schedule(self.context, False, 8000, messages.append)
        self.assertIsNotNone(schedule._ready_frame(page))
        self.assertEqual(self.context.pages, [page])
        self.assertEqual(home_visits, 2)
        self.assertEqual(len(messages), 2)

    def test_new_portal_tab_after_login(self):
        def respond(route):
            url = route.request.url
            if url == schedule.SCHEDULE_URL and route.request.frame.parent_frame is None:
                body = DENIED
            elif url == schedule.SIS_HOME:
                body = """<script>
                    window.open('/psp/csprd/EMPLOYEE/HRMS/h/', '_blank');
                    window.close();
                </script>"""
            elif url == schedule.PORTAL_URL:
                body = f'<iframe src="{schedule.SCHEDULE_URL}"></iframe>'
            elif url == schedule.SCHEDULE_URL:
                body = TERM_LIST
            else:
                body = "<main>主页</main>"
            route.fulfill(content_type="text/html; charset=utf-8", body=body)

        self.context.route("**/*", respond)
        page = schedule._open_schedule(self.context, False, 8000, lambda _: None)
        self.assertIsNotNone(schedule._ready_frame(page))

    def test_valid_session_skips_login(self):
        self.context.route("**/*", lambda r: r.fulfill(
            content_type="text/html; charset=utf-8", body=TERM_LIST))
        page = schedule._open_schedule(self.context, True, 3000, self.fail, Profile(username="test", password="unused"))
        self.assertIsNotNone(schedule._ready_frame(page))

    def run_configured_login(self, *, rejected=False, action=None):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = Path(directory.name) / "profile.toml"
        config.write_text('username = "student@example.test"\npassword = "dummy-only"\n')
        sso = "https://sts.cuhk.edu.cn/adfs/oauth2/authorize"
        self.posts = []
        error = '<span id="errorText">账号或密码有误</span>'
        form = f"""
          <form method="post" id="loginFormPaginated">
          <input id="userNameInput"><button type="button" id="nextButton" onclick="
            userNameInputHolder.value=userNameInput.value;
            this.form.hidden=true; loginForm.hidden=false">下一步</button></form>
          <form hidden method="post" id="loginForm" action="{action or sso}">
          <input type="hidden" name="UserName" id="userNameInputHolder">
          <input type="password" name="Password" id="passwordInput">
          <button id="submitButton">登录</button></form>"""

        def respond(route):
            url = route.request.url
            if url == schedule.SIS_HOME:
                body = f'<a href="{sso}" target="_blank">简体中文</a>'
            elif url == sso and route.request.method == "POST":
                self.posts.append(route.request.post_data_json)
                body = (error + form if rejected else
                        f'<script>location.replace("{schedule.SCHEDULE_URL}")</script>')
            elif url == sso:
                body = form
            elif url == schedule.SCHEDULE_URL:
                body = TERM_LIST if self.posts and not rejected else DENIED
            else:
                self.fail("unexpected request destination")
            route.fulfill(content_type="text/html; charset=utf-8", body=body)

        self.context.route("**/*", respond)
        return schedule._open_schedule(self.context, True, 5000, lambda _: None, Profile.load(config))

    def test_configured_headless_login(self):
        page = self.run_configured_login()
        self.assertIsNotNone(schedule._ready_frame(page))
        self.assertEqual(self.context.pages, [page])
        self.assertEqual(self.posts, [{"UserName": "student@example.test", "Password": "dummy-only"}])

    def test_rejected_credentials_are_submitted_once(self):
        with self.assertRaisesRegex(LoginRequired, "认证错误"):
            self.run_configured_login(rejected=True)
        self.assertEqual(self.context.pages, [])
        self.assertEqual(len(self.posts), 1)

    def test_changed_form_action_blocks_submission(self):
        with self.assertRaisesRegex(LoginRequired, "提交地址"):
            self.run_configured_login(action="https://unexpected.example/login")
        self.assertEqual(self.context.pages, [])
        self.assertEqual(self.posts, [])
