"""Search term offerings and read lecture previews over PeopleSoft HTTP."""
from dataclasses import asdict
import re

from .catalog import (_collect, _DIRECTORY, _EMPTY, _ERROR, _HEADINGS, _LARGE, _merge_page, _page_range,
                      normalize_course, _SEARCH, _section, _SUBJECT, _SUBMIT, _TERM)
from .directory import read_directory
from .login import ScheduleError
from .models import Course, TutorialSelection
from .peoplesoft import Component
from .requirements import parse_requirements


def required(node, selector):
    field = node.select_one(selector)
    if field is None:
        raise ScheduleError(f"SIS 缺少 {selector}。")
    return field


def select(component, selector, value):
    control = required(component.form, selector)
    option = control.find("option", value=value)
    if option is None:
        raise ScheduleError(f"SIS 未列出 {value}。")
    if not option.has_attr("selected"):
        component.act(control["id"], {control["name"]: value})
        if not required(component.form, selector).find("option", selected=True, value=value):
            raise ScheduleError(f"SIS 未选中 {value}。")
    return option.get_text(strip=True)


def grid_pages(component, grid_id, read, all_id, next_id):
    records = {}
    total = None
    last_read = 0
    while True:
        grid = required(component.form, f'[id="{grid_id}"]')
        first, last, expected = _page_range(required(grid, ".PSGRIDCOUNTER").get_text(strip=True))
        rows = read(grid)
        if last <= last_read:
            raise ScheduleError("教学班分页没有前进。")
        _merge_page(records, rows, first, last, expected, total)
        if last == expected:
            return list(records.values())
        action = grid.find("a", id=all_id) if not last_read else None
        if not action:
            action = required(grid, f'a[id="{next_id}"]')
        component.act(action["id"])
        total, last_read = expected, last


def section_rows(grid):
    rows = []
    for anchor in grid.select('a[id^="DERIVED_CLSRCH_SSR_CLASSNAME_LONG$"]'):
        suffix = anchor["id"][anchor["id"].rindex("$"):]
        table = required(grid, f'[id="SSR_CLSRCH_MTG1$scroll{suffix}"]')
        meetings = [[cell.get_text(" ", strip=True) or None for cell in tr.find_all(["td", "th"], recursive=False)]
                    for tr in table.select("tr")[1:]]
        counts = {}
        for key, prefix in (("enrolled", "TOT"), ("capacity", "CAP")):
            node = grid.find(id=f"SSR_CLS_DTL_WRK_ENRL_{prefix}{suffix}")
            counts[key] = node.get_text(strip=True) if node else None
        rows.append(_section(dict(**counts, label=anchor.get_text(strip=True), meetings=meetings,
                                  session=required(grid, f'[id="PSXLATITEM_XLATSHORTNAME{suffix}"]').get_text(strip=True),
                                  status=required(grid, f'[id="win0divDERIVED_CLSRCH_SSR_STATUS_LONG{suffix}"] img').get("alt"))))
    return rows


def choices(component, tutorials):
    for grid in component.form.select('table[id^="SSR_CLS_TBL_R"][id*="$scroll$"]'):
        match = re.search(r"选择\s*指导教学\s*区段\s*[（(](必需|可选)[）)]",
                          grid.parent.parent.get_text(" ", strip=True))
        if match:
            prefix, group = grid["id"].split("$scroll$")
            rows = grid_pages(component, grid["id"], lambda g: [
                dict(class_number=x.get_text(strip=True)) for x in g.select(f'[id^="{prefix}_RELATE_CLASS_NBR$"]')],
                f"{prefix}$fviewall${group}", f"{prefix}$fdown${group}")
            numbers = [r["class_number"] for r in rows]
            if not tutorials.issuperset(numbers):
                raise ScheduleError("关联页指导教学班号与搜索结果不符。")
            return asdict(TutorialSelection(int(match[1] == "必需"), 1, numbers))
    return None


def group_id(component, code):
    for title in component.form.select(_HEADINGS):
        if title.get_text(strip=True).partition(" - ")[0] == code:
            return title["id"].rsplit("$", 1)[1]
    raise ScheduleError(f"搜索结果缺少 {code}。")


def read_sections(component, code):
    group = group_id(component, code)
    sections = grid_pages(component, f"$ICField106$scroll${group}", section_rows,
                          f"$ICField106$hviewall${group}", f"$ICField106$hdown${group}")
    tutorials = {s["class_number"] for s in sections if s["kind"] == "tutorial"}
    for index, section in enumerate(sections, 1):
        if section["kind"] != "lecture":
            continue
        if not tutorials:
            section["tutorial_selection"] = asdict(TutorialSelection(0, 0, []))
            continue
        number = section["class_number"]
        seen = set()
        while True:
            group = group_id(component, code)
            grid = required(component.form, f'[id="$ICField106$scroll${group}"]')
            anchor = next((a for a in grid.select('a[id^="DERIVED_CLSRCH_SSR_CLASSNAME_LONG$"]')
                           if re.search(r"\(" + re.escape(number) + r"\)\s*$", a.get_text(strip=True))), None)
            if anchor:
                break
            first, last, _ = _page_range(required(grid, ".PSGRIDCOUNTER").get_text(strip=True))
            if first in seen or first <= index <= last:
                raise ScheduleError(f"无法定位教学班 {number}。")
            seen.add(first)
            direction = "hup" if index < first else "hdown"
            component.act(f"$ICField106${direction}${group}")
        suffix = anchor["id"].rsplit("$", 1)[1]
        preview = grid.find("a", id=f"CLASS_SRCH_WRK2_SSR_PB_SELECT${suffix}")
        if preview:
            component.act(preview["id"])
            selection = choices(component, tutorials)
            if selection is not None:
                section["tutorial_selection"] = selection
            component.act("DERIVED_CLS_DTL_CANCEL_PB")
    return sections


def search(component, term, subject, label, plan, course=None):
    select(component, _TERM, term)
    select(component, _SUBJECT, subject)
    changes = {}
    criteria = {"SSR_CLSRCH_WRK_SSR_EXACT_MATCH1": "E" if course else "G",
                "SSR_CLSRCH_WRK_CATALOG_NBR": course.split()[1] if course else "0"}
    for prefix, value in criteria.items():
        control = required(component.form, f'[id^="{prefix}"][name]')
        changes[control["name"]] = value
    checkbox = required(component.form, 'input[type=checkbox][id^="SSR_CLSRCH_WRK_SSR_OPEN_ONLY"]')
    changes[checkbox["name"]] = None
    changes[checkbox["name"].replace("$", "$chk$", 1)] = "N"
    form = component.act(required(component.form, _SUBMIT)["id"], changes)
    if _LARGE.search(form.get_text(" ", strip=True)):
        form = component.act("#ICSave")
    error = form.select_one(_ERROR)
    if error and error.get_text(strip=True):
        if error.get_text(strip=True) == _EMPTY:
            return []
        raise ScheduleError(f"{subject} 搜索失败。")
    heading = required(form, "#DERIVED_CLSRCH_SSS_PAGE_KEYDESCR").get_text(strip=True)
    if heading.split("|")[-1].strip() != label:
        raise ScheduleError("搜索结果学期不符。")
    titles = [title.get_text(strip=True) for title in form.select(_HEADINGS)]
    if not titles:
        raise ScheduleError("搜索结果缺少课程分组。")
    batch = []
    for title in titles:
        code, sep, name = title.partition(" - ")
        if not sep or not name or code.split()[:1] != [subject]:
            raise ScheduleError("搜索结果课程与学科不符。")
        if course and normalize_course(code) != course:
            raise ScheduleError(f"精确查询 {course} 返回了 {code}。")
        if not plan or plan.matches(code):
            batch.append(dict(course=asdict(Course(code, name, parse_requirements(None))),
                              sections=read_sections(component, code)))
    return batch


def read_catalog(component, guest, term, subjects, notify, plan, progress, course=None):
    def open_search():
        component.open(_SEARCH)
        component.act("CLASS_SRCH_WRK2_SSR_PB_CLEAR")
        return select(component, _TERM, term)

    label = open_search()
    available = [o["value"] for o in required(component.form, _SUBJECT).select("option[value]") if o["value"]]
    first = True

    def read_subject(subject):
        nonlocal first
        if not first:
            open_search()
        first = False
        batch = search(component, term, subject, label, plan, course)
        if batch:
            directory = Component(guest)
            form = directory.open(_DIRECTORY)
            numbers = [item["course"]["course_code"].split()[1] for item in batch]
            records, _ = read_directory(form, directory.act,
                                        {subject: numbers}, None, allow_missing=True)
            by_code = {r["course_code"]: r for r in records}
            for offering in batch:
                record = offering["course"]
                if record["course_code"] in by_code:
                    detail = by_code[record["course_code"]]
                    record.update({k: detail[k] for k in ("units", "units_raw", "enrollment_requirements")})
        return batch

    return _collect(term, subjects, notify, plan, progress, label, available, read_subject)
