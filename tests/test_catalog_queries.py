import io
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock

from bs4 import BeautifulSoup

from cuhksz_sis_scraper import catalog, list_subjects, list_terms, Profile, ScheduleError
from cuhksz_sis_scraper.cli import catalog_main
from cuhksz_sis_scraper.http_catalog import search


def form(html):
    return BeautifulSoup(html, "html.parser")


class QueryTests(unittest.TestCase):
    def test_single_course_fresh_read_and_checkpoint_isolation(self):
        report = {"offerings": []}
        with TemporaryDirectory() as tmp, patch.object(catalog, "_run_search_session", return_value=report) as run:
            output = Path(tmp) / "query.json"
            progress = catalog.CatalogProgress(output).path
            progress.write_text("existing progress")
            for _ in range(2):
                catalog.export_catalog(output, term="2620", course="mat 1011h", profile=Profile(major="无对应计划"))
            self.assertEqual(run.call_count, 2)
            self.assertEqual(progress.read_text(), "existing progress")
            previous = output.read_bytes()
            run.side_effect = ScheduleError("错误学期")
            with self.assertRaises(ScheduleError):
                catalog.export_catalog(output, term="2620", course="CSC1001")
            self.assertEqual(previous, output.read_bytes())

    def test_http_options_follow_term(self):
        c = MagicMock()
        c.form = form('<select id="CLASS_SRCH_WRK2_STRM" name="term"><option value=""></option>'
                      '<option selected value="2610">第一学期</option><option value="2620">第二学期</option></select>')
        def act(key, changes=None):
            if changes:
                self.assertEqual(changes, {"term": "2620"})
                c.form = form('<select id="CLASS_SRCH_WRK2_STRM"><option selected value="2620">第二学期</option></select>'
                              '<select id="SSR_CLSRCH_WRK_SUBJECT"><option value="ACT">会计</option></select>')
        c.act.side_effect = act
        with patch.object(catalog, "_run_search_session", side_effect=lambda *args: args[-2](None, c)):
            self.assertEqual(list_terms(), [("2610", "第一学期"), ("2620", "第二学期")])
            with self.assertRaisesRegex(ScheduleError, "未列出"):
                list_subjects("9999")
            self.assertEqual(list_subjects("2620"), [("ACT", "会计")])

    def test_exact_http_empty_and_mismatch(self):
        c = MagicMock()
        c.form = form('<select id="CLASS_SRCH_WRK2_STRM"><option selected value="2620">春</option></select>'
                      '<select id="SSR_CLSRCH_WRK_SUBJECT"><option selected value="MAT">数学</option></select>'
                      '<select name="operator" id="SSR_CLSRCH_WRK_SSR_EXACT_MATCH1"></select>'
                      '<input name="number" id="SSR_CLSRCH_WRK_CATALOG_NBR">'
                      '<input type="checkbox" name="open$1" id="SSR_CLSRCH_WRK_SSR_OPEN_ONLY">'
                      '<button id="CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH"></button>')
        c.act.return_value = form(f'<div id="DERIVED_CLSMSG_ERROR_TEXT">{catalog._EMPTY}</div>')
        self.assertEqual(search(c, "2620", "MAT", "春", None, "MAT 1011H"), [])
        self.assertEqual(c.act.call_args.args[1], {"operator": "E", "number": "1011H", "open$1": None, "open$chk$1": "N"})
        c.act.return_value = form('<span id="DERIVED_CLSRCH_SSS_PAGE_KEYDESCR">春</span>'
                                  '<span id="DERIVED_CLSRCH_DESCR200$0">MAT 1011 - 示例</span>')
        with self.assertRaisesRegex(ScheduleError, "精确查询"):
            search(c, "2620", "MAT", "春", None, "MAT 1011H")

    def test_cli_destination_counts_and_conflicts(self):
        section = dict(section="L01", class_number="123", status="关闭", enrolled=0, capacity=None)
        report = dict(term_label="春", offerings=[dict(sections=[section])])
        with patch.object(sys, "argv", ["sis-catalog", "--course", "csc1001", "--term", "2620"]), \
             patch.object(catalog, "export_catalog", return_value=report) as export, redirect_stdout(io.StringIO()) as out:
            catalog_main()
            self.assertEqual(export.call_args.args[0], Path("catalog-2620-CSC1001.json"))
            self.assertIn("0／—", out.getvalue())
        for flags in [["--course", "CSC1001", "--subject", "CSC"], ["--list-terms", "--term", "2620"]]:
            with patch.object(sys, "argv", ["sis-catalog", *flags]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                catalog_main()
