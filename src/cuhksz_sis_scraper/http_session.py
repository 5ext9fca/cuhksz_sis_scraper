"""Authenticate once and serialize use of a profile's protected HTTP session."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import re
import secrets
import string
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from filelock import FileLock
from playwright.sync_api import Error as PlaywrightError

from .login import LoginRequired, ScheduleError, _school_login
from .peoplesoft import Component, UncertainAction, document, form_values
from .schedule import SCHEDULE_URL, _open_schedule, save_courses
from .tabs import close_page, export_session, launch_context

_ORIGIN = "https://sis.cuhk.edu.cn"
_CALLBACK = _ORIGIN + "/sso/dologin.html"


def _exchange(request, url, fields=None):
    try:
        response = (request.get(url, max_redirects=0) if fields is None else
                    request.post(url, form=fields, max_redirects=0))
    except PlaywrightError:
        raise ScheduleError("学校认证请求未完成。") from None
    try:
        return response.status, response.headers.get("location", ""), response.body().decode("utf-8-sig")
    finally:
        response.dispose()


def finish_login(request, code):
    query = urlencode(dict(cmd="login", languageCd="ZHS", code=code))
    status, _, landing = _exchange(request, _ORIGIN + "/psp/csprd/?" + query,
                             dict(timezoneOffset="-480", ptmode="f", ptlangcd="ENG",
                                  ptinstalledlang="ENG,ZHT,ZHS", userid="CUSZ_SSO_LOGIN",
                                  pwd="".join(secrets.choice(string.ascii_uppercase) for _ in range(10)),
                                  ptlangsel="ENG"))
    if status not in (200, 302, 303):
        raise ScheduleError("学校认证回调失败。")
    start = re.search(r"self\.location\s*=\s*['\"]([^'\"]+)", landing)
    if start:
        portal = urlsplit(start[1])
        if (portal.scheme != "https" or portal.netloc != "sis.cuhk.edu.cn" or portal.path !=
                "/psc/csprd/EMPLOYEE/HRMS/s/WEBLIB_PTBR.ISCRIPT1.FieldFormula.IScript_StartPage"):
            raise ScheduleError("学校认证后的门户地址已变化。")
        _exchange(request, start[1])
        _exchange(request, _ORIGIN + "/psp/csprd/EMPLOYEE/HRMS/h/")


def password_login(request, profile):
    status, _, html = _exchange(request, _ORIGIN + "/sso/login.html")
    redirect = re.search(r'location\.href\s*=\s*"([^"]+)"', html)
    if status != 200 or not redirect:
        raise ScheduleError("学校认证入口已变化。")
    url = redirect[1]
    if not _school_login(url) or parse_qs(urlsplit(url).query).get("redirect_uri") != [_CALLBACK]:
        raise ScheduleError("学校认证入口地址不符。")
    fields = None
    submitted = False
    for _ in range(10):
        status, location, html = _exchange(request, url, fields)
        fields = None
        if status in (301, 302, 303, 307, 308):
            url = urljoin(url, location)
            target = urlsplit(url)
            if (target.scheme == "https" and target.netloc.lower() in {"sis.cuhk.edu.cn", "sis.cuhk.edu.cn:443"}
                    and target.path == "/sso/dologin.html"):
                codes = parse_qs(target.query).get("code", [])
                if len(codes) != 1:
                    raise LoginRequired("学校认证需要人工确认。")
                finish_login(request, codes[0])
                return
            if not _school_login(url):
                raise LoginRequired("学校认证需要人工确认。")
            continue
        if status != 200:
            raise ScheduleError(f"学校认证返回 HTTP {status}。")
        soup = document(html)
        form = soup.select_one("form#loginForm")
        error = soup.select_one("#errorText")
        if submitted and error and error.get_text(strip=True):
            raise ScheduleError("学校登录页提示认证错误，请核对 profile.toml 中的凭据。")
        if submitted or not form or not form.select_one("#passwordInput"):
            raise LoginRequired("学校认证需要人工确认。")
        base = soup.select_one("base[href]")
        action = urljoin(urljoin(url, base["href"]) if base else url, form.get("action", ""))
        if not (_school_login(url) and _school_login(action) and form.get("method", "").lower() == "post"):
            raise ScheduleError("学校登录表单的提交地址或方式已变化。")
        fields = form_values(form)
        username = profile.username
        if "@" not in username and "\\" not in username:
            domain = re.search(r'var contosoDomain\s*=\s*"([\w.-]+)"', html)
            if not domain:
                raise ScheduleError("学校登录页未提供账号域。")
            username = domain[1] + "\\" + username
        fields.update(UserName=username, Password=profile.password)
        submitted = True
        url = action
    raise LoginRequired("学校认证需要人工确认。")


def verify(component):
    form = component.open(SCHEDULE_URL)
    if not (form.select_one('input[type=radio]') and "选择学期，然后单击继续。" in form.get_text()
            or form.find("a", string=re.compile("更改学期"))):
        raise ScheduleError("无法验证 SIS 课表组件。")


def manual_state(pw, browser, profile, notify, directory):
    if browser.headless:
        raise LoginRequired("请在 browser.toml 设置 headless = false 后完成学校认证。")
    if profile.username:
        browser = replace(browser, profile_dir=directory / "browser")
    with export_session(launch_context(pw.chromium, browser)) as context:
        page = _open_schedule(context, False, browser.login_timeout_ms, notify)
        try:
            return sis_state(context)
        finally:
            close_page(page)


def sis_state(context):
    cookies = [c for c in context.storage_state()["cookies"]
               if c["domain"].lstrip(".").lower() in {"sis.cuhk.edu.cn", "cuhk.edu.cn"}]
    return {"cookies": cookies, "origins": []}


@contextmanager
def protected(pw, browser, profile, notify):
    identity = str(profile.source) + "\0" + profile.username
    directory = browser.profile_dir / "http" / hashlib.sha256(identity.encode()).hexdigest()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "session.json"
    with FileLock(str(path) + ".lock", mode=0o600):
        state = json.loads(path.read_text()) if path.exists() else {"cookies": [], "origins": []}
        if not any(c["name"] == "PS_DEVICEFEATURES" for c in state["cookies"]):
            state["cookies"].append(dict(name="PS_DEVICEFEATURES", value="width:1280 height:720 pixelratio:1 touch:0",
                                         domain="sis.cuhk.edu.cn", path="/", secure=True, httpOnly=False,
                                         expires=-1, sameSite="Lax"))
        request = pw.request.new_context(storage_state=state, extra_http_headers={"Accept-Language": "zh-CN"})
        try:
            component = Component(request)
            try:
                verify(component)
            except LoginRequired:
                try:
                    if not profile.username:
                        raise LoginRequired()
                    password_login(request, profile)
                    verify(component)
                except LoginRequired:
                    state = manual_state(pw, browser, profile, notify, directory)
                    request.dispose()
                    request = pw.request.new_context(storage_state=state, extra_http_headers={"Accept-Language": "zh-CN"})
                    component = Component(request)
                    verify(component)
            save_courses(sis_state(request), path)
            yield component
            save_courses(sis_state(request), path)
        finally:
            request.dispose()


def read_protected(pw, browser, profile, notify, read):
    for attempt in range(2):
        with protected(pw, browser, profile, notify) as component:
            try:
                return read(component)
            except (LoginRequired, UncertainAction):
                if attempt:
                    raise
                notify("SIS 会话或表单状态已失效，重新验证后从最近的断点继续。")
