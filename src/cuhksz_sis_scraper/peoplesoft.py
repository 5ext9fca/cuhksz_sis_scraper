"""Drive one current PeopleSoft component with a standalone request context."""
import re
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from .login import LoginRequired, ScheduleError


def form_values(form):
    values = {}
    for field in form.select("input[name], select[name], textarea[name]"):
        kind = field.get("type", "").lower()
        if field.has_attr("disabled") or kind in {"button", "submit", "reset", "image", "file"}:
            continue
        if kind in {"checkbox", "radio"} and not field.has_attr("checked"):
            continue
        if field.name == "select":
            option = field.select_one("option[selected]") or field.select_one("option")
            if option is None:
                continue
            value = option.get("value", option.get_text())
        elif field.name == "textarea":
            value = field.get_text()
        else:
            value = field.get("value", "on" if kind in {"checkbox", "radio"} else "")
        values[field["name"]] = value
    return values


def document(body):
    return BeautifulSoup(body, "html.parser")


def login_page(soup):
    return (soup.select_one('input[type=password], a[href="/sso/login.html"], '
                            'a[href="https://sis.cuhk.edu.cn/sso/login.html"]') is not None
            or re.search(r"not authorized (?:for this page|to access this component)|无权访问|未经授权"
                         r"|会话已过期|session (?:has )?expired", soup.get_text(" ", strip=True), re.I))


class UncertainAction(ScheduleError):
    """A POST may have completed. Discard the form and restart the read."""


class Component:
    def __init__(self, request):
        self.request = request
        self.form = None
        self.url = ""

    def _read(self, url, payload=None):
        try:
            response = (self.request.get(url, max_redirects=0) if payload is None else
                        self.request.post(url, form=payload, max_redirects=0))
        except PlaywrightTimeout:
            if payload is not None:
                raise UncertainAction("SIS 未确认组件操作，需重新打开组件。") from None
            raise ScheduleError("SIS 组件读取超时。") from None
        except PlaywrightError:
            raise ScheduleError("SIS HTTP 请求未完成，请从最近的断点重新运行。") from None
        try:
            if response.status in (401, 403):
                raise LoginRequired("SIS 会话需要认证。")
            if 300 <= response.status < 400:
                location = urlsplit(urljoin(url, response.headers.get("location", "")))
                if location.hostname == "sts.cuhk.edu.cn" or (
                        location.hostname == "sis.cuhk.edu.cn" and (
                            location.path.startswith("/sso/") or "cmd=login" in location.query)):
                    raise LoginRequired("SIS 会话需要认证。")
            if response.status != 200:
                raise ScheduleError(f"SIS 返回 HTTP {response.status}。")
            return response.body().decode("utf-8-sig")
        finally:
            response.dispose()

    def _form(self, html):
        soup = document(html)
        form = soup.select_one('form[name="win0"]')
        if login_page(soup) or (soup.title and soup.title.get_text(strip=True)
                                in {"Oracle PeopleSoft Sign-in", "Oracle PeopleSoft 登录"}):
            raise LoginRequired("SIS 会话需要认证。")
        if form is None or not form.select_one('input[name="ICStateNum"][value]'):
            raise ScheduleError("SIS 响应缺少活动表单或状态编号。")
        return form

    def open(self, url):
        self.form = None
        self.url = url
        html = self._read(url)
        bootstrap = re.search(r"self\.location\s*=\s*['\"]([^'\"]+)['\"]", html)
        if bootstrap and bootstrap[1] in {url + "?&", url.replace("/psc/", "/psp/") + "?&"}:
            self.url = url + "?&"
            html = self._read(self.url)
        self.form = self._form(html)
        return self.form

    def act(self, key, changes=None):
        form = self.form
        if form is None or form.find(id=key) is None:
            raise ScheduleError(f"SIS 当前表单缺少控件 {key}。")
        endpoint = urljoin(self.url, form.get("action", ""))
        if (urlsplit(endpoint)[:3] != urlsplit(self.url)[:3]
                or form.get("method", "").lower() != "post"):
            raise ScheduleError("SIS 组件表单的提交地址或方式已变化。")
        if self.url.endswith("?&") and not urlsplit(endpoint).query:
            endpoint += "?&"
        payload = form_values(form)
        for name, value in (changes or {}).items():
            if value is None:
                payload.pop(name, None)
            else:
                payload[name] = value
        payload.update(ICAction=key, ICAJAX="1", ICNAVTYPEDROPDOWN="0")
        self.form = None  # A lost POST response invalidates the previous state.
        body = self._read(endpoint, payload)
        if not re.match(r"\s*(?:<\?xml[^>]*>\s*)?<PAGE\b", body, re.I):
            self.form = self._form(body)
            return self.form
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise ScheduleError("SIS 返回的组件 XML 无效。") from exc
        state = re.search(r"ICStateNum\.value\s*=\s*(\d+)", root.findtext("GENSCRIPT[@id='onloadScript']", ""))
        if not state:
            raise ScheduleError("SIS XML 缺少下一次提交的状态编号。")
        for field in root.findall("FIELD"):
            target = form.find(id=field.get("id"))
            if target is None:
                raise ScheduleError(f"SIS 局部响应缺少对应字段 {field.get('id')}。")
            target.clear()
            target.extend(list(document(field.text or "").contents))
        form.find(attrs={"name": "ICStateNum"})["value"] = state[1]
        if login_page(form):
            raise LoginRequired("SIS 会话需要认证。")
        self.form = form
        return form
