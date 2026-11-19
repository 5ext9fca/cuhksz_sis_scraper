"""Read a student's term appointments and exam list in component order."""
import re
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from playwright.sync_api import Page, sync_playwright

from .catalog import _update
from .config import Browser, Profile
from .http_session import read_protected
from .login import ScheduleError
from .schedule import _open_schedule, save_courses
from .tabs import close_page, export_session, launch_context

_BASE = "https://sis.cuhk.edu.cn/psc/csprd/EMPLOYEE/HRMS/c/SA_LEARNER_SERVICES."
_WINDOWS = ("期次", "预定开始", "预定结束")
_EXAMS = ("类别", "描述", "考试类型", "考试日期", "考试时间", "教室")


def grid(form, key, headers):
    table = form.find("table", id=key + "$scroll$0")
    if table is None:
        return None
    columns = [th.get_text(strip=True) for th in table.select("th")]
    if not all(h in columns for h in headers):
        raise ScheduleError(f"{key} 表头已变化。")
    rows = []
    for tr in table.select("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells = tr.find_all("td", recursive=False)
        if not cells or (len(cells) == 1 and cells[0].has_attr("colspan")):
            continue
        if len(cells) != len(columns):
            raise ScheduleError(f"{key} 数据列已变化。")
        values = [cells[columns.index(h)].get_text(" ", strip=True) for h in headers]
        if any(values):
            rows.append(values)
    return rows


def term_label(term):
    if not re.fullmatch(r"\d{2}(10|20|50)", term):
        raise ScheduleError("请指定有效的四位 SIS 学期代码。")
    year = int(term[:2])
    season = {"10": "第一", "20": "第二", "50": "暑期"}[term[2:]]
    return f"{2000 + year}-{(year + 1) % 100:02} {season}学期"


def verify_term(form, label):
    if not any(s.split("|")[0].strip() == label and "|" in s for s in form.stripped_strings):
        raise ScheduleError(f"SIS 返回结果与 {label} 不符。")


def parse_appointments(form, label):
    verify_term(form, label)
    limits = grid(form, "SSR_DUMMY_RECVW", ("最多学分合计", "最少学分合计"))
    if limits is None or len(limits) > 1:
        raise ScheduleError("学期注册限制表缺失或结构已变化。")
    appointments = {}
    for name, key in (("cart", "STDNT_EDIT_A_VW"), ("enrollment", "STDNT_ENRL_A_VW")):
        rows = grid(form, key, _WINDOWS)
        if rows is None:
            appointments[name] = dict(status="unavailable", windows=[])
        else:
            windows = [dict(session=session, begins=begins, ends=ends) for session, begins, ends in rows]
            appointments[name] = dict(status="available" if windows else "empty", windows=windows)
    for i, key in enumerate(("max_units", "min_units")):
        value = limits[0][i] if limits else ""
        if value and not re.fullmatch(r"\d+(?:\.\d+)?", value):
            raise ScheduleError("学期学分限制格式已变化。")
        appointments[key] = float(value) if value else None
    return appointments


def read_exams(form, select_term, label):
    wizard = form.find(id="SSR_DUMMY_RECV1$scroll$0")
    if wizard:
        radios = wizard.select('input[type="radio"]')
        if not radios:
            raise ScheduleError("考试学期表缺少学期选项。")
        options = []
        for radio in radios:
            cells = list(radio.find_parent("tr").stripped_strings)
            if "CUHK(SZ)" not in cells or not any(re.fullmatch(r"20\d{2}-\d{2} .+学期", cell) for cell in cells):
                raise ScheduleError("考试学期表结构已变化。")
            if label in cells:
                options.append(radio)
        if len(options) > 1:
            raise ScheduleError("考试学期选项重复。")
        form = select_term(options[0]) if options else None
    status, entries = "unavailable", []
    if form is not None:
        verify_term(form, label)
        rows = grid(form, "SS_EXAMSCH1_VW", _EXAMS)
        if rows is None:
            if not re.search(r"(?:没有|无可用|不可用|尚未|未提供).{0,20}考试|考试.{0,20}(?:不可用|尚未公布)", form.get_text(" ", strip=True)):
                raise ScheduleError("考试安排表缺失。")
        else:
            status = "available" if rows else "empty"
            entries = [dict(zip(("class_label", "description", "exam_type", "date", "time", "room"), r)) for r in rows]
    return dict(status=status, entries=entries)


def read_term_info(open_form, select_term, term):
    label = term_label(term)

    def load(name, page):
        return open_form(f"{_BASE}{name}.GBL?Page={page}&Action=A&ACAD_CAREER=UG"
                         f"&ENRL_REQUEST_ID=&INSTITUTION=CUSZ1&STRM={term}")

    appointments = parse_appointments(load("SSR_SSENRL_APPT", "SSR_SSENRL_APPT"), label)
    exams = read_exams(load("SSR_SSENRL_EXAM_L", "SSR_SSENRL_TERM"), select_term, label)
    return dict(schema_version=1, term=term, term_label=label, appointments=appointments, exams=exams)


def fetch_term_info(page: Page, *, term: str) -> dict:
    def snapshot():
        return BeautifulSoup(page.content(), "html.parser")

    def open_form(url):
        page.goto(url)
        page.locator('form[name="win0"]').wait_for()
        return snapshot()

    def select(radio):
        page.locator(f'[id="{radio["id"]}"]').check()
        _update(page.main_frame, page.locator("#DERIVED_SSS_SCT_SSR_PB_GO").click, urlsplit(page.url).path)
        return snapshot()

    return read_term_info(open_form, select, term)


def export_term_info(output: str | Path, *, term: str, profile=None, browser_file=None, backend="http") -> dict:
    term_label(term)
    profile, browser = Profile.load(profile), Browser.load(browser_file)
    with sync_playwright() as pw:
        if backend == "http":
            def read(c):
                return read_term_info(c.open, lambda r: c.act("DERIVED_SSS_SCT_SSR_PB_GO", {r["name"]: r["value"]}), term)
            report = read_protected(pw, browser, profile, print, read)
        elif backend == "browser":
            with export_session(launch_context(pw.chromium, browser)) as context:
                page = _open_schedule(context, browser.headless, browser.login_timeout_ms, print, profile)
                try:
                    report = fetch_term_info(page, term=term)
                finally:
                    close_page(page)
        else:
            raise ScheduleError("backend 支持 http 或 browser。")
    save_courses(report, output)
    return report
