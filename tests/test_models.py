import unittest
from dataclasses import asdict

from cuhksz_sis_scraper import (
    Course, CourseOffering, Lecture, ScheduleError, Section, Tutorial,
    TutorialSelection, parse_requirements,
)
from cuhksz_sis_scraper.models import validate_catalog
from cuhksz_sis_scraper.schedule import _course, _MEETINGS, _SUMMARY


class ModelTests(unittest.TestCase):
    def test_structured_and_partial_requirements(self):
        raw = ("1. Prerequisite: CSC1001 and (MAT1001 or MAT1002) "
               "2. Co-requisite: STA2001H; Exclusion: ACT2111")
        r = parse_requirements(raw)
        self.assertEqual((r.raw_text, r.parse_status), (raw, "parsed"))
        self.assertEqual(r.prerequisite, {"all_of": [{"course_code": "CSC 1001"}, {"any_of": [
            {"course_code": "MAT 1001"}, {"course_code": "MAT 1002"}]}]})
        self.assertEqual(r.corequisite, {"course_code": "STA 2001H"})
        self.assertEqual(r.exclusion, {"course_code": "ACT 2111"})
        partial = parse_requirements("先修：CSC1002 or instructor approval；同修：MAT1002")
        self.assertEqual(partial.parse_status, "partial")
        self.assertEqual(partial.prerequisite, {"text": "CSC1002 or instructor approval"})
        self.assertEqual(partial.corequisite, {"course_code": "MAT 1002"})
        self.assertEqual(parse_requirements("").parse_status, "empty")
        self.assertEqual(parse_requirements(None).parse_status, "unavailable")

    def test_offering_identity_and_tutorial_references(self):
        lecture = Lecture("101", "L01-LEC", "LEC", [],
                          tutorial_selection=TutorialSelection(1, 1, ["203"]))
        tutorial = Tutorial("203", "T03-TUT", "TUT", [])
        record = dict(course=asdict(Course("ACT 2111", "Accounting", parse_requirements(""))),
                      sections=[asdict(lecture), asdict(tutorial)])
        report = dict(schema_version=3, term="2620", subjects=["ACT"], offerings=[record])
        validate_catalog(report)
        offering = CourseOffering.from_record(record, term="2620")
        self.assertEqual(offering.sections, [lecture, tutorial])
        with self.assertRaisesRegex(ScheduleError, "重复"):
            validate_catalog({**report, "offerings": [record, record]})
        record["sections"].pop()
        with self.assertRaisesRegex(ScheduleError, "关联班号"):
            validate_catalog(report)

    def test_personal_schedule_uses_shared_section_types(self):
        rows = [_MEETINGS, ["101", "L01", "讲演", "周一", "101", "Lee", "春季"],
                ["201", "T01", "指导教学", "周二", "101", "Lee", "春季"],
                ["301", "LAB1", "实验室", "周三", "101", "Lee", "春季"]]
        course = _course(dict(title="CSC 1001 - CS", grids=[
            [_SUMMARY, ["已注册", "3", "已评分", ""]], rows]))
        sections = [Section.from_record(s) for s in course["sections"]]
        self.assertEqual([type(s) for s in sections], [Lecture, Tutorial, Section])
        self.assertEqual(sections[0].tutorial_selection.status, "unknown")
        self.assertEqual(sections[2].component, "实验室")

    def test_old_catalog_rejected(self):
        for report in [{"courses": []}, {"schema_version": 2, "courses": []},
                       {"schema_version": 3, "courses": [], "term": "2620", "subjects": []}]:
            with self.subTest(report=report), self.assertRaisesRegex(ScheduleError, "重新导出"):
                validate_catalog(report)
