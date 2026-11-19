"""Search the CUHK(SZ) SIS class offerings by term and subject."""
from collections.abc import Callable, Sequence
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from filelock import FileLock, Timeout as LockTimeout
from playwright.sync_api import Error as PlaywrightError, Frame, Locator, Page, sync_playwright

from .tabs import close_page, launch_context, query_popups
from .config import Browser, Profile
from .calendar import current_term
from .checkpoint import CatalogProgress
from .schemes import StudyPlan, profile_plan
from .schedule import LoginRequired, ScheduleError, _login_screen, _open_component, save_courses

_PATH = "/psc/csprd/EMPLOYEE/HRMS/c/SA_LEARNER_SERVICES.CLASS_SEARCH.GBL"
_SEARCH = "https://sis.cuhk.edu.cn" + _PATH
_TERM = 'select[id^="CLASS_SRCH_WRK2_STRM"]'
_SUBJECT = 'select[id^="SSR_CLSRCH_WRK_SUBJECT"]'
_SUBMIT = "#CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH"
_NEW = "#CLASS_SRCH_WRK2_SSR_PB_NEW_SEARCH"
_ERROR = "#DERIVED_CLSMSG_ERROR_TEXT"
_HEADINGS = 'span[id^="DERIVED_CLSRCH_DESCR200$"]'
_LARGE = re.compile(r"搜索将返回\s*\d+\s*门以上的课程，是否继续[？?]")
_EMPTY = "搜索未返回与指定标准匹配的结果。"

# Each heading owns a scroll area. SIS renumbers the nested fields when a
# section group expands, so resolve the section suffixes inside the current DOM.
_READ_SECTIONS = """grid => Array.from(
    grid.querySelectorAll('a[id^="DERIVED_CLSRCH_SSR_CLASSNAME_LONG$"]'), link => {
        const suffix = link.id.slice(link.id.lastIndexOf('$'));
        const field = prefix => grid.querySelector('[id="' + prefix + suffix + '"]');
        const meetings = field('SSR_CLSRCH_MTG1$scroll');
        return {
            label: link.innerText.trim(),
            session: field('PSXLATITEM_XLATSHORTNAME')?.innerText.trim(),
            status: field('win0divDERIVED_CLSRCH_SSR_STATUS_LONG')?.querySelector('img')?.alt,
            meetings: meetings && Array.from(meetings.rows).slice(1).map(row =>
                Array.from(row.cells, cell => cell.innerText.trim() || null))
        };
    })"""


def _frame(page: Page) -> Frame | None:
    for frame in page.frames:
        url = urlsplit(frame.url)
        if url.hostname == "sis.cuhk.edu.cn" and url.path == _PATH:
            if (frame.locator(_SUBMIT).filter(has_text="搜索").is_visible()
                    or frame.locator(_NEW).filter(has_text="新搜索").is_visible()):
                return frame
    return None


def _update(frame: Frame, action: Callable[[], object]) -> None:
    # PeopleSoft replaces controls asynchronously after a POST, even when the
    # next form has the same labels or the same empty-results message.
    with frame.page.expect_response(lambda response: response.request.method == "POST"
                                    and urlsplit(response.url).path == _PATH) as event:
        action()
    response = event.value
    response.finished()
    if not response.ok:
        raise ScheduleError(f"SIS 请求失败 (HTTP {response.status})。")
    frame.locator("#processing:visible").wait_for(state="hidden")


def _select(frame: Frame, control: Locator, value: str) -> None:
    if control.input_value() != value:
        _update(frame, lambda: control.select_option(value=value))
        if control.input_value() != value:
            raise ScheduleError(f"SIS 未选中 {value}。")


def _section(row: dict) -> dict:
    label = re.fullmatch(r"(.+?)\s*\((\d+)\)", row["label"])
    if not label or not row.get("status") or not row.get("session"):
        raise ScheduleError(f"教学班字段不完整：{row['label']}")
    meetings = row.get("meetings")
    if meetings is None or any(len(values) != 4 for values in meetings):
        raise ScheduleError(f"教学班 {label[2]} 的排课列有变化。")
    keys = ("days_and_times", "room", "instructor", "date_range")
    return dict(class_number=label[2], section=label[1].strip(), session=row["session"],
                status=row["status"], meetings=[dict(zip(keys, values)) for values in meetings])


def _sections(frame: Frame, group: str) -> list[dict]:
    grid = frame.locator(f'[id="$ICField106$scroll${group}"]')
    counter = grid.locator(".PSGRIDCOUNTER").first
    records = {}
    total = None
    use_view_all = True
    while True:
        count = counter.inner_text().strip()
        match = re.fullmatch(r"(\d+)(?:\s*[-–]\s*(\d+))?\s*(?:/|of)\s*(\d+)", count)
        if not match:
            raise ScheduleError(f"无法识别教学班分页数量 {count!r}。")
        first, last, expected = int(match[1]), int(match[2] or match[1]), int(match[3])
        rows = grid.evaluate(_READ_SECTIONS)
        if ((total is not None and total != expected) or not 1 <= first <= last <= expected
                or first > len(records) + 1 or len(rows) != last - first + 1):
            raise ScheduleError(f"教学班分页不完整 ({count})。")
        total = expected
        for row in rows:
            section = _section(row)
            number = section["class_number"]
            if number in records and records[number] != section:
                raise ScheduleError(f"教学班 {number} 的分页数据冲突。")
            records[number] = section
        if len(records) != last:
            raise ScheduleError(f"教学班分页含重复或遗漏记录 ({count})。")
        if last == expected:
            return list(records.values())
        view_all = grid.locator(f'a[id="$ICField106$hviewall${group}"]')
        next_page = grid.locator(f'a[id="$ICField106$hdown${group}"]')
        action = view_all if use_view_all and view_all.is_visible() else next_page
        use_view_all = False
        if not action.is_visible():
            raise ScheduleError(f"SIS 缺少后续教学班分页 ({count})。")
        _update(frame, action.click)
        counter.filter(has_text=re.compile("^" + re.escape(count) + "$")).wait_for(state="hidden")


def _search(frame: Frame, term_label: str, subject: str, plan: StudyPlan | None = None) -> list[dict]:
    _select(frame, frame.locator(_SUBJECT), subject)
    frame.locator('select[id^="SSR_CLSRCH_WRK_SSR_EXACT_MATCH1"]').select_option("G")
    frame.locator('input[id^="SSR_CLSRCH_WRK_CATALOG_NBR"]').fill("0")
    frame.locator('input[type=checkbox][id^="SSR_CLSRCH_WRK_SSR_OPEN_ONLY"]').uncheck()
    _update(frame, frame.locator(_SUBMIT).click)
    confirm = frame.get_by_text(_LARGE)
    error = frame.locator(_ERROR)
    result = frame.locator(_NEW)
    confirm.or_(error).or_(result).first.wait_for()
    if confirm.is_visible():
        _update(frame, frame.locator('[id="#ICSave"]').click)
        error.or_(result).first.wait_for()
    if error.is_visible():
        message = error.inner_text()
        if message.strip() == _EMPTY:
            return []
        raise ScheduleError(f"{subject} 搜索失败：{message}")
    heading = frame.locator("#DERIVED_CLSRCH_SSS_PAGE_KEYDESCR").inner_text()
    if heading.split("|")[-1].strip() != term_label:
        raise ScheduleError(f"{subject} 搜索结果学期有误：{heading}")
    titles = frame.locator(_HEADINGS).evaluate_all("els => els.map(e => [e.id, e.innerText])")
    if not titles:
        raise ScheduleError(f"{subject} 缺少课程分组。")
    courses = []
    for field_id, title in titles:
        code, sep, name = title.strip().partition(" - ")
        if not sep or not name or code.split()[:1] != [subject]:
            raise ScheduleError(f"{subject} 搜索结果有异常课程：{title}")
        if plan is not None and not plan.matches(code):
            continue
        sections = _sections(frame, field_id.rsplit("$", 1)[1])
        courses.append(dict(course_code=code, title=name, sections=sections))
    return courses


def fetch_catalog(page: Page, *, term: str | None = None, subjects: Sequence[str] | None = None,
                  on_status: Callable[[str], None] | None = None, plan: StudyPlan | None = None,
                  profile: Profile | str | Path | None = None) -> dict:
    """Read term offerings from an authenticated Simplified Chinese class-search Page."""
    plan = plan or profile_plan(Profile.load(profile))
    return _fetch_catalog(page, term or current_term(), subjects, on_status, plan, None)


def _fetch_catalog(page: Page, term: str, subjects: Sequence[str] | None,
                   on_status: Callable[[str], None] | None, plan: StudyPlan | None,
                   progress: CatalogProgress | None) -> dict:
    frame = _frame(page)
    if frame is None:
        if _login_screen(page):
            raise LoginRequired("请登录 SIS 并打开简体中文「课程搜索」。")
        raise ScheduleError("请打开简体中文「课程搜索」。")
    try:
        with query_popups(page):
            # Reset any criteria left by the user before running the first query.
            if frame.locator(_NEW).is_visible():
                _update(frame, frame.locator(_NEW).click)
            _update(frame, frame.locator("#CLASS_SRCH_WRK2_SSR_PB_CLEAR").click)
            term_select = frame.locator(_TERM)
            options = term_select.locator("option").evaluate_all("els => els.map(e => e.value)")
            if term not in options:
                raise ScheduleError(f"SIS 未列出学期 {term}。")
            _select(frame, term_select, term)
            term_label = term_select.locator("option:checked").inner_text()
            available = frame.locator(_SUBJECT + " option").evaluate_all(
                "els => els.map(e => e.value).filter(Boolean)")
        if isinstance(subjects, str):
            subjects = [subjects]
        selected = available if subjects is None else list(dict.fromkeys(
            s.strip().upper() for s in subjects))
        unknown = set(selected) - set(available)
        if unknown or not selected:
            raise ScheduleError(f"学科为空或 SIS 无此学科：{sorted(unknown)}")
        if plan is not None:
            prefixes = plan.subjects
            selected = [subject for subject in selected if subject in prefixes]
        if progress is not None:
            progress.restore(dict(term=term, subjects=sorted(selected),
                                  requested=None if subjects is None else sorted({
                                      s.strip().upper() for s in subjects}),
                                  plan=plan.metadata() if plan else None))
        courses = []
        for subject in selected:
            if progress is not None and subject in progress.batches:
                courses.extend(progress.batches[subject])
                if on_status:
                    on_status(f"{subject}  已从断点恢复")
                continue
            with query_popups(page):
                if frame.locator(_NEW).is_visible():
                    _update(frame, frame.locator(_NEW).click)
                _select(frame, term_select, term)
                batch = _search(frame, term_label, subject, plan)
            numbers = [s["class_number"] for c in batch for s in c["sections"]]
            if len(numbers) != len(set(numbers)):
                raise ScheduleError(f"{subject} 搜索结果含重复班号。")
            if plan:
                batch = [plan.annotate(c) for c in batch]
            if progress is not None:
                progress.commit(subject, batch)
            courses.extend(batch)
            if on_status:
                on_status(f"{subject}  {len(batch)} 门课程，{sum(len(c['sections']) for c in batch)} 个教学班")
    except (PlaywrightError, ScheduleError) as exc:
        if not page.is_closed() and _login_screen(page):
            raise LoginRequired("课程搜索会话已失效，请在学校页面重新登录。") from exc
        raise
    report = dict(source="CUHK(SZ) SIS · 课程搜索", term=term, term_label=term_label,
                  subjects=selected, courses=courses)
    if plan is not None:
        report["study_plan"] = plan.metadata()
    return report


def export_catalog(output: str | Path, *, profile: Profile | str | Path | None = None,
                   term: str | None = None, subjects: Sequence[str] | None = None,
                   browser_file: str | Path | None = None,
                   plan: StudyPlan | None = None, restart: bool = False) -> dict:
    """Read all requested subjects and atomically save their offerings."""
    term = term or current_term()
    profile = Profile.load(profile)
    plan = plan or profile_plan(profile)
    browser = Browser.load(browser_file)

    def notify(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    output = Path(output).expanduser().resolve()
    progress = CatalogProgress(output)
    lock = FileLock(str(progress.path) + ".lock")
    try:
        lock.acquire(timeout=0)
    except LockTimeout as exc:
        raise ScheduleError(f"另一个进程正在导出 {output}。") from exc
    try:
        if restart:
            progress.path.unlink(missing_ok=True)
        with sync_playwright() as pw:
            context = launch_context(pw.chromium, browser)
            try:
                for attempt in range(2):
                    page = _open_component(context, browser.headless, browser.login_timeout_ms, notify,
                                           url=_SEARCH, portal_url=_SEARCH.replace("/psc/", "/psp/"),
                                           ready=_frame, menu=re.compile("^课程搜索$"), name="课程搜索",
                                           profile=profile)
                    try:
                        report = _fetch_catalog(page, term, subjects, notify, plan, progress)
                        break
                    except LoginRequired:
                        if attempt or (browser.headless and not profile.username):
                            raise
                        notify("课程搜索会话已失效，重新登录后将从断点继续。")
                    finally:
                        close_page(page)
            finally:
                context.close()
        save_courses(report, output)
        progress.path.unlink(missing_ok=True)
        return report
    finally:
        lock.release()
