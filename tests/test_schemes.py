"""Regression checks for real study-scheme notation and cohort selection."""
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import subprocess

from cuhksz_sis_scraper import Course, parse_requirements, ScheduleError, StudyPlan, filter_catalog, load_study_plan
from cuhksz_sis_scraper.schemes import _attachment, _pdf_plan


class SchemeTests(TestCase):
    def test_stream_abbreviations_and_permitted_alternatives(self):
        text = """Programme Title
Electrical and Computer Engineering
Major Programme Requirement
Computer Engineering Stream
    MAT1001, 1002, CSC1005,       22
    1006, STA2001 or 2001H, CHM2118E, MSE3011/6002
Electronic Engineering Stream
    ECE2001, 2050
Explanatory Note:
[a] Students can choose to take either CSC1003 and CSC1004 or CSC1005 and CSC1006
    to fulfill the graduation requirement.
[b] ECE2050 and ECO2011 are included in the calculation of Major GPA.
(Study Scheme - Chinese)
CSC9998
Recommended Course Pattern
CSC9999
"""
        completed = subprocess.CompletedProcess([], 0, stdout=text.encode())
        with patch("cuhksz_sis_scraper.schemes.subprocess.run", return_value=completed):
            plan = _pdf_plan(b"%PDF-test", "test.pdf", "", None, "Computer Engineering")
            with self.assertRaises(ScheduleError):
                _pdf_plan(b"%PDF-test", "test.pdf", "", None, "unknown")
        self.assertEqual(plan.codes, set("MAT1001 MAT1002 CSC1003 CSC1004 CSC1005 CSC1006 "
                                         "STA2001 STA2001H CHM2118E MSE3011 MSE6002".split()))

    def test_registry_selects_multi_cohort_or_open_ended_edition(self):
        rows = [("new", "2025至26年度及以后"), ("old", "2022至23及2023至24年度")]
        html = "".join(f'<div class="list-content"><a href="/{key}.pdf"></a>'
                       f'<div class="list-title">适用于{year}入学学生</div></div>'
                       for key, year in rows)
        with patch("cuhksz_sis_scraper.schemes._download", return_value=html.encode()):
            for year, expected in [(2023, "old"), (2027, "new"), (None, "new")]:
                self.assertEqual(_attachment("https://example.test/page", year)[1],
                                 f"https://example.test/{expected}.pdf")
            with self.assertRaises(ScheduleError):
                _attachment("https://example.test/page", 2024)

    def test_nested_json_and_catalog_subject_intersection(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "plans.json"
            path.write_text('{"甲":{"核心":{"CSC1005":"A"},"选修":{"专业组":{"MAT1011H":"B"}}},'
                            '"乙":{"其他":{"ECO2011":"C"}}}', encoding="utf-8")
            plan = load_study_plan("甲", source=path)
        self.assertEqual(plan.codes, {"CSC1005", "MAT1011H"})
        courses = [dict(course=asdict(Course(code, "示例", parse_requirements(""))), sections=[])
                   for code in ["CSC 1005", "CSC 1006", "MAT 1011H", "MAT 1011", "ECO 2011"]]
        report = {"schema_version": 3, "term": "2610", "subjects": ["CSC", "MAT", "ECO"], "offerings": courses}
        self.assertEqual(filter_catalog(report, plan)["offerings"],
                         [plan.annotate(courses[0]), plan.annotate(courses[2])])
        self.assertEqual(plan.evidence["MAT1011H"], [{"requirement": "选修",
                                                     "path": ["甲", "选修", "专业组", "MAT1011H"]}])
        self.assertEqual(filter_catalog(report, plan, ["MAT"])["offerings"], [plan.annotate(courses[2])])
        self.assertEqual(filter_catalog(report, plan, ["ECO"])["subjects"], [])
        self.assertEqual(len(report["offerings"]), 5)

    def test_evidence_tracks_shared_courses_abbreviations_and_note_across_pages(self):
        text = """Programme Title
Engineering
Major Programme Requirement
Computer Engineering Stream
1. School Package:
MAT1001,
\f1002
2. Required Courses:
ECE2050
3. Elective Courses:
PHY2001
Electronic Engineering Stream
1. School Package:
MAT1001
2. Required Courses:
PHY2001
Explanatory Note:
[a] Students can choose to take either MAT1001 or
\fMAT1011 to fulfill the graduation requirement."""
        completed = subprocess.CompletedProcess([], 0, stdout=text.encode())
        with patch("cuhksz_sis_scraper.schemes.subprocess.run", return_value=completed):
            plan = _pdf_plan(b"%PDF-test", "test.pdf", "", None, None)
            ce = _pdf_plan(b"%PDF-test", "test.pdf", "", None, "Computer Engineering")
        self.assertEqual(plan.evidence["PHY2001"], [
            {"requirement": "选修", "page": 2, "stream": "Computer Engineering"},
            {"requirement": "必修", "page": 2, "stream": "Electronic Engineering"}])
        self.assertEqual(ce.evidence["MAT1002"][0]["page"], 2)
        self.assertEqual(ce.evidence["MAT1011"], [
            {"requirement": "等效选项", "page": 3, "stream": "Computer Engineering"}])
        self.assertEqual(set(ce.metadata()["course_evidence"]), ce.codes)
