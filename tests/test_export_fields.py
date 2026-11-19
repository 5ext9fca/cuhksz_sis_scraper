import unittest
from dataclasses import asdict

from bs4 import BeautifulSoup

from cuhksz_sis_scraper import Meeting, ScheduleError, parse_requirements
from cuhksz_sis_scraper.catalog import _section
from cuhksz_sis_scraper.directory import unit_fields
from cuhksz_sis_scraper.term_info import read_term_info


class ExportFieldTests(unittest.TestCase):
    def test_restrictions_with_exclusion_and_unrecognized_clause(self):
        raw = "For Y2-Y4 (SDS; SSE); CSE_IDA students. Exclusion: CSC1001"
        req = parse_requirements(raw)
        self.assertEqual(req.parse_status, "parsed")
        self.assertEqual(req.restrictions[0]["years"], ["Y2", "Y3", "Y4"])
        self.assertEqual(req.restrictions[0]["programs"], ["SDS", "SSE"])
        self.assertEqual(req.restrictions[1]["programs"], ["CSE_IDA"])
        self.assertEqual(req.exclusion, {"course_code": "CSC 1001"})
        req = parse_requirements("Prerequisite: CSC1001; Requires: consent of instructor")
        self.assertEqual(req.parse_status, "partial")
        self.assertEqual(req.restrictions[0]["raw_text"], "consent of instructor")

    def test_units_and_seats(self):
        self.assertEqual(unit_fields(" 1.50 "), dict(units=1.5, units_raw="1.50"))
        self.assertEqual(unit_fields("1–3"), dict(units=None, units_raw="1–3"))
        section = _section(dict(label="L01-LEC (123)", meetings=[], status="开放", session="正常", enrolled="0"))
        self.assertEqual(section["enrolled"], 0)
        self.assertIsNone(section["capacity"])

    def test_meeting_roundtrip_and_unknown_time(self):
        m = Meeting("星期二星期四 08:30 - 09:50", None, None, "2027/1/11 - 2027/5/14")
        self.assertEqual((m.weekdays, m.start_minutes, m.end_minutes), ([2, 4], 510, 590))
        self.assertEqual(Meeting.from_record(asdict(m)), m)
        tbd = Meeting("待定", None, None, m.date_range)
        self.assertEqual(tbd.start_date, "2027-01-11")
        self.assertEqual(tbd.weekdays, [])
        self.assertIsNone(tbd.start_minutes)

    def test_term_info_empty_unavailable_wrong_term_and_changed_grid(self):
        appt = """<span>2026-27 第二学期 | Undergraduate | CUHK(SZ)</span>
          <table id="SSR_DUMMY_RECVW$scroll$0"><tr><th>最多学分合计</th><th>最少学分合计</th></tr>
          <tr><td>18</td><td>0</td></tr></table>
          <table id="STDNT_ENRL_A_VW$scroll$0"><tr><th>期次</th><th>预定开始</th><th>预定结束</th></tr></table>"""
        exam = """<table id="SSR_DUMMY_RECV1$scroll$0"><tr><td><input type="radio"></td>
          <td>2025-26 第二学期</td><td>CUHK(SZ)</td></tr></table>"""

        def read(first, second):
            pages = iter((first, second))
            return read_term_info(lambda url: BeautifulSoup(next(pages), "html.parser"), None, "2620")

        report = read(appt, exam)
        self.assertEqual(report["appointments"]["enrollment"]["status"], "empty")
        self.assertEqual(report["appointments"]["min_units"], 0)
        self.assertEqual(report["exams"]["status"], "unavailable")
        for changed in [appt.replace("2026-27", "2025-26"), appt.replace("最多学分合计", "other")]:
            with self.assertRaises(ScheduleError):
                read(changed, exam)

    def test_exam_wizard_selected_term(self):
        from cuhksz_sis_scraper.term_info import read_exams
        label = "2025-26 第二学期"
        wizard = BeautifulSoup(f"""<table id="SSR_DUMMY_RECV1$scroll$0"><tr><td><input type="radio" value="1"></td>
          <td>{label}</td><td>CUHK(SZ)</td></tr></table>""", "html.parser")
        page = f"""<span>{label} | CUHK(SZ)</span><table id="SS_EXAMSCH1_VW$scroll$0"><tr>
          <th>类别</th><th>描述</th><th>考试类型</th><th>考试日期</th><th>考试时间</th><th>教室</th></tr>
          <tr><td>CSC 1001</td><td>示例</td><td>期末</td><td>5月</td><td>09:00</td><td>A</td></tr></table>"""

        def select(radio):
            self.assertEqual(radio["value"], "1")
            return BeautifulSoup(page, "html.parser")

        report = read_exams(wizard, select, label)
        self.assertEqual(report["status"], "available")
        self.assertEqual(report["entries"][0]["class_label"], "CSC 1001")
        with self.assertRaises(ScheduleError):
            read_exams(wizard, lambda _: BeautifulSoup(page.replace(label, "其他学期"), "html.parser"), label)
