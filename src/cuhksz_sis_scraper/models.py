"""Shared course and teaching-section models. A catalog contains one term's offerings."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .login import ScheduleError
from .meeting import Meeting
from .requirements import EnrollmentRequirements

SCHEMA_VERSION = 3


@dataclass
class Section:
    class_number: str
    section: str
    component: str
    meetings: list[Meeting]
    session: str | None = None
    status: str | None = None
    enrolled: int | None = None
    capacity: int | None = None
    kind: str = field(default="other", init=False)

    @classmethod
    def from_sis(cls, *, component: str, **fields) -> Section:
        section_type = {"LEC": Lecture, "讲演": Lecture, "TUT": Tutorial,
                        "指导教学": Tutorial}.get(component, Section)
        return section_type(component=component, **fields)

    @classmethod
    def from_record(cls, record: dict) -> Section:
        fields = dict(record)
        for key in ("enrolled", "capacity"):
            count = fields[key]
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError(f"无效的教学班人数 {key}。")
        kind = fields.pop("kind")
        section_type = {"lecture": Lecture, "tutorial": Tutorial, "other": Section}[kind]
        fields["meetings"] = [Meeting.from_record(m) for m in fields["meetings"]]
        if section_type is Lecture:
            selection = fields["tutorial_selection"]
            if selection == {"status": "unknown"}:
                fields["tutorial_selection"] = UnknownSelection()
            else:
                fields["tutorial_selection"] = TutorialSelection(**selection)
        return section_type(**fields)


@dataclass
class UnknownSelection:
    status: Literal["unknown"] = field(default="unknown", init=False)


@dataclass
class TutorialSelection:
    min_select: int
    max_select: int
    class_numbers: list[str]
    status: Literal["known"] = "known"

    def __post_init__(self):
        if (self.status != "known" or not 0 <= self.min_select <= self.max_select <= 1
                or self.max_select != int(bool(self.class_numbers))
                or len(set(self.class_numbers)) != len(self.class_numbers)):
            raise ValueError("无效的 tutorial 选择范围或班号列表。")


@dataclass
class Lecture(Section):
    kind: str = field(default="lecture", init=False)
    tutorial_selection: UnknownSelection | TutorialSelection = field(default_factory=UnknownSelection)


@dataclass
class Tutorial(Section):
    kind: str = field(default="tutorial", init=False)


@dataclass
class Course:
    course_code: str
    title: str
    enrollment_requirements: EnrollmentRequirements
    units: float | None = None
    units_raw: str | None = None


@dataclass
class CourseOffering:
    term: str
    course: Course
    sections: list[Section]
    plan_matches: list[dict] | None = None

    @classmethod
    def from_record(cls, record: dict, *, term: str) -> CourseOffering:
        fields = dict(record)
        data = dict(fields.pop("course"))
        if not {"units", "units_raw"} <= data.keys():
            raise ValueError("缺少课程学分字段。")
        requirements = data["enrollment_requirements"]
        if not isinstance(requirements["restrictions"], list):
            raise ValueError("无效的注册限制。")
        data["enrollment_requirements"] = EnrollmentRequirements(**requirements)
        sections = [Section.from_record(s) for s in fields.pop("sections")]
        return cls(term, Course(**data), sections, **fields)

    def __post_init__(self):
        numbers = {s.class_number for s in self.sections}
        if len(numbers) != len(self.sections):
            raise ValueError("开课记录含重复班号。")
        tutorials = {s.class_number for s in self.sections if isinstance(s, Tutorial)}
        for section in self.sections:
            if isinstance(section, Lecture) and isinstance(section.tutorial_selection, TutorialSelection):
                if not tutorials.issuperset(section.tutorial_selection.class_numbers):
                    raise ValueError("关联班号必须属于同一次开课的 tutorial。")


def validate_catalog(report: dict) -> None:
    if not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION:
        raise ScheduleError(f"需要版本 {SCHEMA_VERSION} 的开课目录，请重新导出。")
    try:
        if ("courses" in report or not isinstance(report["offerings"], list)
                or not isinstance(report["subjects"], list) or not isinstance(report["term"], str)):
            raise ValueError("学期、学科或开课列表无效。")
        seen = set()
        codes = set()
        for entry in report["offerings"]:
            offering = CourseOffering.from_record(entry, term=report["term"])
            numbers = {s.class_number for s in offering.sections}
            code = offering.course.course_code
            if numbers & seen or code in codes:
                raise ValueError("学期内课号或班号重复。")
            seen.update(numbers)
            codes.add(code)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ScheduleError(f"开课目录结构无效，请重新导出：{exc}") from exc
