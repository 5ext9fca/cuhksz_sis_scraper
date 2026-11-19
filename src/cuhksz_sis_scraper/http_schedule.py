"""Read the personal schedule from current PeopleSoft forms."""
import re

from .login import ScheduleError
from .schedule import _course, _EMPTY, _FILTERS, SCHEMA_VERSION


def cells(row):
    return [" ".join(cell.stripped_strings) for cell in row.find_all(["td", "th"], recursive=False)]


def link(form, text):
    for anchor in form.select("a[id]"):
        if anchor.get_text(strip=True) == text:
            return anchor
    raise ScheduleError(f"SIS 缺少「{text}」控件。")


def term_rows(component, career):
    form = component.form
    if not form.select_one('input[type=radio][name^=SSR_DUMMY_RECV1]'):
        form = component.act(link(form, "更改学期")["id"])
    rows = {}
    for radio in form.select('input[type=radio][name^=SSR_DUMMY_RECV1]'):
        values = cells(radio.find_parent("tr"))
        if len(values) != 4:
            raise ScheduleError("学期选择表的列已变化。")
        _, term, level, institution = values
        if level == career:
            if term in rows:
                raise ScheduleError("学期名称重复。")
            rows[term] = institution, radio
    if not rows:
        raise ScheduleError(f"{career} 学期列表为空。")
    return rows


def groups(form):
    for table in form.select("table.PSGROUPBOXWBO"):
        rows = [row for row in table.select("tr") if row.find_parent("table") is table]
        if not rows or rows[0].find("tr"):
            continue
        title = cells(rows[0])
        if len(title) != 1:
            continue
        grids = [[cells(row) for row in grid.select("tr") if row.find_parent("table") is grid]
                 for grid in table.select("table.PSLEVEL3GRIDNBO")
                 if grid.find_parent("table", class_="PSGROUPBOXWBO") is table]
        if grids or re.match(r"^[A-Z]+\s+\w+\s+-\s+", title[0]):
            yield dict(title=title[0], grids=grids)


def read_schedule(component, career):
    terms = term_rows(component, career)
    records = []
    for index, term in enumerate(terms):
        rows = term_rows(component, career) if index else terms
        if term not in rows or rows[term][0] != terms[term][0]:
            raise ScheduleError("返回后学期列表已变化。")
        institution, radio = rows[term]
        form = component.act(link(component.form, "继续")["id"], {radio["name"]: radio["value"]})
        heading = re.compile(r"\s*\|\s*".join(map(re.escape, (term, career, institution))))
        if not any(heading.fullmatch(s) for s in form.stripped_strings):
            raise ScheduleError("课表结果与所选学期不符。")
        toggle = form.select_one('a[title*="展开区段"][title*="课表筛选选项"]')
        if toggle:
            form = component.act(toggle["id"])
        changes = {}
        for label in _FILTERS:
            node = next((el for el in form.select("label[for]") if el.get_text(strip=True) == label), None)
            box = form.find(id=node["for"]) if node else None
            if box is None:
                if _EMPTY in form.stripped_strings:
                    break
                raise ScheduleError(f"课表缺少「{label}」。")
            if not box.has_attr("checked"):
                changes[box["name"]] = box["value"]
                changes[box["name"] + "$chk"] = "Y"
        if changes:
            form = component.act(link(form, "筛选")["id"], changes)
        courses = [] if _EMPTY in form.stripped_strings else [_course(group) for group in groups(form)]
        if not courses and _EMPTY not in form.stripped_strings:
            raise ScheduleError(f"{term} 未找到课程或 SIS 的空学期提示。")
        records.append(dict(term=term, courses=courses))
    return dict(schema_version=SCHEMA_VERSION, source="CUHK(SZ) SIS · 我的课程表",
                academic_career=career, terms=records)
