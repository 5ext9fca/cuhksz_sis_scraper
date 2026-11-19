"""Read course records from the Simplified Chinese CUHK(SZ) SIS schedule."""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic
from urllib.parse import urlsplit

from playwright.sync_api import (
    BrowserContext, Error as PlaywrightError, Frame, Page,
    TimeoutError as PlaywrightTimeout, sync_playwright,
)

from .config import Browser, Profile
from .tabs import QueryTabs, close_page, launch_context, query_popups
from .login import LoginRequired, ScheduleError, SchoolLogin

SIS_HOME = "https://sis.cuhk.edu.cn/"
PORTAL_URL = (
    "https://sis.cuhk.edu.cn/psp/csprd/EMPLOYEE/HRMS/s/"
    "WEBLIB_PTPP_SC.HOMEPAGE.FieldFormula.IScript_AppHP?pt_fname=HCCC_ENROLLMENT"
    "&FolderPath=PORTAL_ROOT_OBJECT.CO_EMPLOYEE_SELF_SERVICE.HCCC_ENROLLMENT&IsFolder=true"
)
SCHEDULE_URL = (
    "https://sis.cuhk.edu.cn/psc/csprd/EMPLOYEE/HRMS/c/"
    "SA_LEARNER_SERVICES.SSR_SSENRL_LIST.GBL"
)
_SCHEDULE = re.compile(
    r"^https://sis\.cuhk\.edu\.cn/psc/[^?#]+/SA_LEARNER_SERVICES\.SSR_SSENRL_LIST\.GBL(?:[?#]|$)",
    re.IGNORECASE,
)
_EMPTY = "您尚未注册此学期的课程。"
_CALLBACK_STALL_SECONDS = 15
_FILTERS = ("显示已注册课程", "显示已退选课程", "显示候补列表课程")
_SUMMARY = ["状态", "学分", "评分", "成绩"]
_MEETINGS = ["课程号码", "区段", "组件", "日期和时间", "教室", "讲师", "开始/结束日期"]

# Take one DOM snapshot per term. Keep empty cells and the distinction between
# section rows and continuation rows. innerText keeps SIS's displayed spelling.
_READ_GROUPS = r"""groups => groups.flatMap(group => {
    const titleRow = group.rows[0];
    if (!titleRow || titleRow.cells.length !== 1 || titleRow.cells[0].querySelector('tr'))
        return [];
    const text = cell => cell.innerText.trim().replace(/[ \t]*[\r\n]+[ \t]*/g, ' ');
    const grids = Array.from(group.querySelectorAll('table.PSLEVEL3GRIDNBO'))
        .filter(grid => grid.closest('table.PSGROUPBOXWBO') === group && grid.getClientRects().length);
    const title = text(titleRow.cells[0]);
    if (!grids.length && !/^[A-Z]+\s+\w+\s+-\s+/.test(title)) return [];
    return [{title, grids: grids.map(grid => Array.from(grid.rows,
        row => Array.from(row.cells, text)))}];
})"""


def _login_screen(page: Page) -> bool:
    location = urlsplit(page.url)
    if location.hostname and location.hostname != "sis.cuhk.edu.cn":
        return True
    if location.path.startswith("/sso/"):
        return True
    if page.get_by_role("link", name="简体中文", exact=True).is_visible() and (
        page.get_by_role("link", name="English", exact=True).is_visible()
    ):
        return True
    denied = re.compile(r"not authorized for this page|not authorized to access this component"
                        r"|无权访问|未经授权|会话已过期|session (?:has )?expired", re.I)
    return any(frame.get_by_text(denied).first.is_visible()
               or frame.locator('input[type="password"]:visible').count()
               for frame in page.frames)


def _ready_frame(page: Page) -> Frame | None:
    frame = page.frame(url=_SCHEDULE)
    if frame is None:
        return None
    if (frame.get_by_text("选择学期，然后单击继续。", exact=True).is_visible()
            and frame.get_by_role("radio", name="选择此行").first.is_visible()):
        return frame
    if (frame.get_by_role("link", name="更改学期", exact=True).is_visible()
            and frame.get_by_text(re.compile(r"^[^|]+\s*\|\s*[^|]+\s*\|\s*CUHK\(SZ\)$")).first.is_visible()):
        return frame
    return None


def _wait_for_schedule(page: Page, timeout_ms: int) -> Frame:
    deadline = monotonic() + timeout_ms / 1000
    while True:
        frame = _ready_frame(page)
        if frame:
            return frame
        if _login_screen(page):
            raise LoginRequired("当前会话无法读取课表，请在学校首页手动登录。")
        if monotonic() >= deadline:
            raise ScheduleError("请登录 SIS 并进入简体中文的「我的课程表」，然后重试。")
        page.wait_for_timeout(200)


def _term_list(frame: Frame) -> None:
    prompt = frame.get_by_text("选择学期，然后单击继续。", exact=True)
    change = frame.get_by_role("link", name="更改学期", exact=True)
    prompt.or_(change).first.wait_for()
    if not prompt.is_visible():
        change.click()
    prompt.wait_for()


def _terms(frame: Frame, career: str) -> list[tuple[str, str]]:
    terms = []
    for radio in frame.get_by_role("radio").all():
        cells = radio.locator("xpath=ancestor::tr[1]").locator(":scope > td").all_inner_texts()
        if len(cells) != 4:
            raise ScheduleError("学期选择表的列已变化。")
        _, term, level, institution = (cell.strip() for cell in cells)
        if level == career:
            terms.append((term, institution))
    if not terms or len({term for term, _ in terms}) != len(terms):
        raise ScheduleError(f"{career} 学期列表为空或学期名称重复。")
    return terms


def _course(group: dict) -> dict:
    title = group["title"]
    code, separator, name = title.partition(" - ")
    if not separator or not name or len(group["grids"]) != 2:
        raise ScheduleError(f"课程表结构有变化：{title}")
    summary, schedule = group["grids"]
    if len(summary) != 2 or summary[0][:4] != _SUMMARY or len(summary[1]) < 4:
        raise ScheduleError(f"{code} 的状态或成绩列有变化。")
    status, units, basis, grade = summary[1][:4]
    if not status or not units:
        raise ScheduleError(f"{code} 缺少课程状态或学分。")
    if len(schedule) < 2 or schedule[0][:7] != _MEETINGS:
        raise ScheduleError(f"{code} 的班别表有变化。")

    sections = []
    for row in schedule[1:]:
        # PeopleSoft may leave three empty cells or omit rowspan-covered cells.
        if len(row) >= 7:
            number, section, component = row[:3]
            if number:
                sections.append(dict(class_number=number, section=section,
                                     component=component, meetings=[]))
            elif section or component:
                raise ScheduleError(f"{code} 的班别缺少课程号码。")
            fields = row[3:7]
        elif len(row) in (4, 5):
            fields = row[:4]
        else:
            raise ScheduleError(f"{code} 的排课行列数异常。")
        if not sections:
            raise ScheduleError(f"{code} 的时段缺少所属班别。")
        sections[-1]["meetings"].append(dict(zip(
            ("days_and_times", "room", "instructor", "date_range"),
            (field or None for field in fields),
        )))
    value = float(units)
    return dict(course_code=code, title=name, status=status,
                units=int(value) if value.is_integer() else value,
                grading_basis=basis, grade=grade or None, sections=sections)


def _read_term(frame: Frame, term: str, career: str, institution: str) -> list[dict]:
    row = frame.get_by_role("row", name=f"选择此行 {term} {career} {institution}", exact=True)
    row.get_by_role("radio").check()
    frame.get_by_role("link", name="继续", exact=True).click()
    heading = re.compile(r"^" + r"\s*\|\s*".join(map(re.escape, (term, career, institution))) + r"$")
    frame.get_by_text(heading).first.wait_for()
    empty = frame.get_by_text(_EMPTY, exact=True)
    checkbox = frame.get_by_role("checkbox", name=_FILTERS[0], exact=True)
    collapsed = frame.get_by_role("link", name=re.compile(r"展开区段.*课表筛选选项"))
    empty.or_(checkbox).or_(collapsed).first.wait_for()
    if collapsed.is_visible():
        collapsed.click()
    if checkbox.is_visible():
        filters = [frame.get_by_role("checkbox", name=label, exact=True) for label in _FILTERS]
        if not all(box.is_checked() for box in filters):
            for box in filters:
                box.check()
            with frame.page.expect_response(
                lambda r: r.request.method == "POST" and bool(_SCHEDULE.search(r.url))
            ) as update:
                frame.get_by_role("link", name="筛选", exact=True).click()
            update.value.finished()
            frame.locator("#processing:visible").wait_for(state="hidden")
    if empty.is_visible():
        return []
    groups = frame.locator("table.PSGROUPBOXWBO:visible").evaluate_all(_READ_GROUPS)
    if not groups:
        raise ScheduleError(f"{term} 未找到课程或 SIS 的空学期提示。")
    return [_course(group) for group in groups]


def fetch_courses(page: Page, *, academic_career: str = "Undergraduate",
                  login_timeout_ms: int = 180_000) -> dict:
    """Read every term for one career from an open SIS Page (sync Playwright)."""
    frame = _wait_for_schedule(page, login_timeout_ms)
    try:
        with query_popups(page):
            _term_list(frame)
            terms = _terms(frame, academic_career)
        records = []
        for index, (term, institution) in enumerate(terms):
            with query_popups(page):
                if index:
                    _term_list(frame)
                courses = _read_term(frame, term, academic_career, institution)
            records.append({"term": term, "courses": courses})
    except (PlaywrightError, ScheduleError) as exc:
        if not page.is_closed() and _login_screen(page):
            raise LoginRequired("读取学期时 SIS 会话已失效，请手动登录后重新导出。") from exc
        raise
    return {"source": "CUHK(SZ) SIS · 我的课程表", "academic_career": academic_career,
            "terms": records}


def save_courses(report: dict, output: str | Path) -> None:
    """Atomically replace output after the complete report has been serialized."""
    path = Path(output)
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    f = NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    temporary = Path(f.name)
    try:
        with f:
            f.write(text)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _probe(context: BrowserContext, tabs: QueryTabs, timeout_ms: int, url: str = SCHEDULE_URL,
           ready: Callable[[Page], Frame | None] = _ready_frame) -> Page | None:
    # A fresh protected document is the session check. A restored tab, cookies,
    # a callback URL and even an HTTP 200 can all outlive the authenticated session.
    trial = tabs.open(context)
    verified = False
    try:
        deadline = monotonic() + timeout_ms / 1000
        trial.goto(url, wait_until="commit", timeout=timeout_ms)
        while monotonic() < deadline:
            if ready(trial):
                verified = True
                return trial
            if _login_screen(trial):
                break
            trial.wait_for_timeout(200)
    except PlaywrightTimeout:
        pass
    finally:
        if not verified and not trial.is_closed():
            trial.close()
    return None


def _open_component(context: BrowserContext, headless: bool, timeout_ms: int,
                    notify: Callable[[str], None], *, url: str, portal_url: str,
                    ready: Callable[[Page], Frame | None], menu: re.Pattern,
                    name: str, profile: Profile | None = None) -> Page:
    with QueryTabs() as tabs:
        page = _probe(context, tabs, min(8000, timeout_ms), url, ready)
        if page is None:
            page = _login_component(context, tabs, headless, timeout_ms, notify, url=url,
                                    portal_url=portal_url, ready=ready, menu=menu, name=name,
                                    profile=profile)
        return tabs.release(page)


def _login_component(context: BrowserContext, owned: QueryTabs, headless: bool, timeout_ms: int,
                     notify: Callable[[str], None], *, url: str, portal_url: str,
                     ready: Callable[[Page], Frame | None], menu: re.Pattern,
                     name: str, profile: Profile | None) -> Page:
    login = SchoolLogin(profile.username, profile.password) if profile and profile.username else None
    if headless and login is None:
        raise LoginRequired(f"{name}会话未通过校验。请在 profile.toml 配置账号密码，或在 browser.toml 设置 headless = false 后手动登录。")

    if login:
        notify(f"{name}会话已失效，正在使用配置文件登录学校认证页。")
    else:
        notify(f"{name}会话未通过校验。请在学校首页手动选择简体中文并登录，窗口会等待认证。")
    previous = set(context.pages)
    landing = owned.open(context)
    landing.goto(SIS_HOME, wait_until="commit")
    landing.bring_to_front()
    deadline = monotonic() + timeout_ms / 1000
    callbacks: dict[Page, float] = {}
    checked_portals: set[Page] = set()
    recovered = False
    while monotonic() < deadline:
        tabs = [tab for tab in context.pages if tab not in previous]
        if not tabs:
            raise LoginRequired("登录窗口已关闭，请重新运行导出。")
        for tab in reversed(tabs):
            try:
                if ready(tab):
                    return tab
                if login:
                    login.advance(tab)
                location = urlsplit(tab.url)
                if location.hostname != "sis.cuhk.edu.cn":
                    callbacks.pop(tab, None)
                    continue
                if location.path == "/sso/dologin.html":
                    since = callbacks.setdefault(tab, monotonic())
                    if not recovered and monotonic() - since >= _CALLBACK_STALL_SECONDS:
                        recovered = True
                        verified = _probe(context, owned, min(8000, timeout_ms), url, ready)
                        if verified:
                            return verified
                        notify(f"学校认证回调停留超过 15 秒，{name}仍不可读，正在重新打开学校首页。")
                        retry = owned.open(context)
                        retry.goto(SIS_HOME, wait_until="commit")
                        retry.bring_to_front()
                    continue
                callbacks.pop(tab, None)
                # Keep the user's authenticated tab when moving through the
                # portal. A new guest component tab can have a different view.
                if tab not in checked_portals and "/EMPLOYEE/HRMS/" in location.path:
                    home = location.path.endswith("/h/")
                    sign_out = tab.get_by_role("link", name=re.compile(r"^(登出|Sign Out)$", re.I))
                    if home or sign_out.first.is_visible():
                        checked_portals.add(tab)
                        notify(f"已进入 SIS 门户，正在打开「{name}」。")
                        if home:
                            tab.goto(portal_url, wait_until="domcontentloaded")
                        if not ready(tab):
                            for frame in tab.frames:
                                link = frame.get_by_text(menu).first
                                if link.is_visible():
                                    link.click()
                                    break
                            else:
                                notify(f"请在已登录的门户打开「{name}」。")
            except PlaywrightError:
                if not tab.is_closed():
                    raise
        # Keep processing Playwright events when SSO replaces its original tab.
        idle = next((tab for tab in context.pages if not tab.is_closed()), None)
        if idle is not None:
            try:
                idle.wait_for_timeout(300)
            except PlaywrightError:
                if not idle.is_closed():
                    raise
    raise LoginRequired(f"登录等待超时，SIS 仍未显示可读取的「{name}」。")


def _open_schedule(context: BrowserContext, headless: bool, timeout_ms: int,
                   notify: Callable[[str], None], profile: Profile | None = None) -> Page:
    return _open_component(context, headless, timeout_ms, notify, url=SCHEDULE_URL,
                           portal_url=PORTAL_URL, ready=_ready_frame,
                           menu=re.compile("^我的课程表$"), name="我的课程表", profile=profile)


def export_courses(output: str | Path, *, profile: Profile | str | Path | None = None,
                   browser_file: str | Path | None = None, academic_career: str = "Undergraduate",
                   on_status: Callable[[str], None] | None = None) -> dict:
    """Open SIS in the configured browser, read all terms, and save JSON."""
    profile = Profile.load(profile)
    browser = Browser.load(browser_file)
    notify = on_status if on_status is not None else lambda message: print(message, file=sys.stderr)
    with sync_playwright() as pw:
        context = launch_context(pw.chromium, browser)
        try:
            for attempt in range(2):
                page = _open_schedule(context, browser.headless, browser.login_timeout_ms, notify, profile)
                try:
                    report = fetch_courses(page, academic_career=academic_career,
                                           login_timeout_ms=browser.login_timeout_ms)
                    break
                except LoginRequired:
                    if attempt or (browser.headless and not profile.username):
                        raise
                    notify("导出途中会话失效，将在重新登录后从第一个学期开始。")
                finally:
                    close_page(page)
        finally:
            context.close()
    save_courses(report, output)
    return report
