"""Redacted component responses for the HTTP export entry points."""
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from cuhksz_sis_scraper import LoginRequired, Profile, ScheduleError, export_courses
from cuhksz_sis_scraper import http_session
from cuhksz_sis_scraper.config import Browser
from cuhksz_sis_scraper.directory import read_directory
from cuhksz_sis_scraper.http_catalog import choices, grid_pages, search
from cuhksz_sis_scraper.http_schedule import groups, read_schedule
from cuhksz_sis_scraper.schedule import _course, _SUMMARY, _MEETINGS
from cuhksz_sis_scraper.peoplesoft import Component, UncertainAction, document
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from cuhksz_sis_scraper.schedule import SCHEDULE_URL


def form(contents, state=1):
    return (f'<form name="win0" method="post" action="{SCHEDULE_URL}">'
            f'<input name="ICStateNum" value="{state}"><div id="win0divPAGECONTAINER">'
            f'{contents}</div></form>')


def response(html):
    result = Mock(status=200)
    result.body.return_value = html.encode()
    return result


class ComponentTests(unittest.TestCase):
    def test_partial_then_full_replacement_and_updated_hidden_fields(self):
        request = Mock()
        page = response(form('<div id="field"><a id="next$0">继续</a></div>'))
        request.get.return_value = page
        xml = response("""<PAGE><FIELD id="field"><![CDATA[<a id="next$7">继续</a>
            <input type="hidden" name="criteria" value="changed">]]></FIELD>
            <GENSCRIPT id="onloadScript">document.win0.ICStateNum.value = 4;</GENSCRIPT></PAGE>""")
        replacement = response(form('<a id="back">更改学期</a>', state=9))
        request.post.side_effect = [xml, replacement]
        component = Component(request)
        component.open(SCHEDULE_URL)
        component.act("next$0")
        with self.assertRaises(ScheduleError):
            component.act("next$0")
        component.act("next$7")
        self.assertEqual(request.post.call_args.kwargs["form"]["ICStateNum"], "4")
        self.assertEqual(request.post.call_args.kwargs["form"]["criteria"], "changed")
        self.assertIsNotNone(component.form.find(id="back"))
        for r in (page, xml, replacement):
            r.dispose.assert_called_once()

    def test_full_container_replacement_then_uncertain_post(self):
        request = Mock()
        request.get.return_value = response(form('<a id="first"></a>'))
        request.post.side_effect = [
            response("""<PAGE><FIELD id="win0divPAGECONTAINER"><![CDATA[<a id="second"></a>]]></FIELD>
                <GENSCRIPT id="onloadScript">document.win0.ICStateNum.value=2;</GENSCRIPT></PAGE>"""),
            PlaywrightTimeout("fixture timeout")]
        component = Component(request)
        component.open(SCHEDULE_URL)
        component.act("first")
        self.assertIsNone(component.form.find(id="first"))
        with self.assertRaises(UncertainAction):
            component.act("second")
        self.assertIsNone(component.form)
        with self.assertRaises(ScheduleError):
            component.act("second")
        self.assertEqual(request.post.call_count, 2)

    def test_unexpected_login_and_malformed_response_discard_form(self):
        for html, error in [('<input type="password">', LoginRequired),
                            ('<PAGE><FIELD id="x">?</FIELD></PAGE>', ScheduleError)]:
            request = Mock()
            request.get.return_value = response(form('<a id="next"></a>'))
            request.post.return_value = response(html)
            component = Component(request)
            component.open(SCHEDULE_URL)
            with self.assertRaises(error) as caught:
                component.act("next")
            self.assertIs(type(caught.exception), error)
            self.assertIsNone(component.form)

    def test_incomplete_grid_and_wrong_detail(self):
        component = Mock(form=document('<table id="grid"><td class="PSGRIDCOUNTER">1/2</td></table>'))
        with self.assertRaises(ScheduleError):
            grid_pages(component, "grid", lambda g: [dict(class_number="101")], "all", "next")
        group = '<span id="DERIVED_SSS_BCC_GROUP_BOX_1$span$0">BIO - Biology</span>'
        listing = group + ('<tr id="trCOURSE_LIST$0_row0"><td id="CRSE_NBR$span$0">1001</td>'
                           '<td><a id="CRSE_TITLE$0">Course</a></td></tr>')
        with self.assertRaisesRegex(ValueError, "Expected course detail"):
            read_directory(listing, lambda key: '<span id="DERIVED_CRSECAT_DESCR200">BIO 1002 - X</span>',
                           {"BIO": ["1001"]}, None, allow_missing=True)
        self.assertEqual(read_directory(listing, Mock(), {"BIO": ["1002"]}, None, allow_missing=True)[0], [])


class PageShapeTests(unittest.TestCase):
    def test_large_result_confirmation_and_empty_subject(self):
        component = Mock(form=document("""
            <select id="CLASS_SRCH_WRK2_STRM$35$"><option selected value="2620">春季</option></select>
            <select id="SSR_CLSRCH_WRK_SUBJECT$0"><option selected value="ACT">ACT</option></select>
            <select id="SSR_CLSRCH_WRK_SSR_EXACT_MATCH1$1" name="match"></select>
            <input id="SSR_CLSRCH_WRK_CATALOG_NBR$1" name="number">
            <input type="checkbox" id="SSR_CLSRCH_WRK_SSR_OPEN_ONLY$3" name="SSR_CLSRCH_WRK_SSR_OPEN_ONLY$3">
            <a id="CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH">搜索</a>"""))
        component.act.side_effect = [
            document('<div>搜索将返回 50 门以上的课程，是否继续？</div><a id="#ICSave">确定</a>'),
            document('<span id="DERIVED_CLSMSG_ERROR_TEXT">搜索未返回与指定标准匹配的结果。</span>')]
        self.assertEqual(search(component, "2620", "ACT", "春季", None), [])
        component.act.assert_called_with("#ICSave")
        changes = component.act.call_args_list[0].args[1]
        self.assertEqual(changes["SSR_CLSRCH_WRK_SSR_OPEN_ONLY$chk$3"], "N")

    def test_required_optional_and_unknown_previews(self):
        for label, minimum in [("必需", 1), ("可选", 0)]:
            with self.subTest(label=label):
                preview = document(f'<div>选择 指导教学 区段 ({label}):<div><table id="SSR_CLS_TBL_R1$scroll$0">'
                                   '<tr><td id="SSR_CLS_TBL_R1_RELATE_CLASS_NBR$0">201</td></tr>'
                                   '<tr><td class="PSGRIDCOUNTER">1/1</td></tr></table></div></div>')
                selection = choices(Mock(form=preview), {"201", "202"})
                self.assertEqual(selection["min_select"], minimum)
                self.assertEqual(selection["class_numbers"], ["201"])
        self.assertIsNone(choices(Mock(form=document("<div>课程详细信息</div>")), {"201"}))

    def test_empty_term_by_current_radio_and_term_heading(self):
        component = Mock(form=document('<table><tr><td><input type="radio" name="SSR_DUMMY_RECV1$sels$0" '
                                       'value="7"></td><td>春季</td><td>本科</td><td>CUHK(SZ)</td></tr></table>'
                                       '<a id="continue">继续</a>'))
        component.act.return_value = document("<div>春季 | 本科 | CUHK(SZ)</div><p>您尚未注册此学期的课程。</p>")
        report = read_schedule(component, "本科")
        self.assertEqual(report["terms"], [dict(term="春季", courses=[])])
        component.act.assert_called_once_with("continue", {"SSR_DUMMY_RECV1$sels$0": "7"})

    def test_schedule_preserves_empty_grade_continuations_and_components(self):
        def row(values):
            return "<tr>" + "".join(f"<td>{v}</td>" for v in values) + "</tr>"
        summary = row(_SUMMARY) + row(["已注册", "3", "评分", ""])
        meetings = (row(_MEETINGS) + row(["101", "L01", "讲课", "周一", "教室", "", "秋季"])
                    + row(["周二", "教室", "", "秋季"]) + row(["102", "T01", "指导教学", "", "", "", ""]))
        table = document('<table class="PSGROUPBOXWBO">' + row(["MAT 1001 - 示例"]) + "<tr><td>"
                         + "".join(f'<table class="PSLEVEL3GRIDNBO">{grid}</table>' for grid in (summary, meetings))
                         + "</td></tr></table>")
        course, = [_course(g) for g in groups(table)]
        self.assertIsNone(course["grade"])
        self.assertEqual([len(section["meetings"]) for section in course["sections"]], [2, 1])


class SessionTests(unittest.TestCase):
    def test_manual_handoff_rechecks_protected_page_and_closes_context(self):
        with TemporaryDirectory() as tmp:
            browser = Browser(Path(tmp), None, False, 100)
            pw, browser_context = Mock(), Mock()
            trial, verified = Mock(), Mock()
            trial.get.return_value = response('<input type="password">')
            verified.get.return_value = response(form('选择学期，然后单击继续。<input type="radio">'))
            safe_state = {"cookies": [], "origins": []}
            verified.storage_state.return_value = browser_context.storage_state.return_value = safe_state
            pw.request.new_context.side_effect = [trial, verified]
            with patch.object(http_session, "launch_context", return_value=browser_context), \
                 patch.object(http_session, "export_session") as session, \
                 patch.object(http_session, "_open_schedule") as opened, \
                 patch.object(http_session, "close_page") as close:
                session.return_value.__enter__.return_value = browser_context
                with http_session.protected(pw, browser, Profile(), Mock()) as component:
                    close.assert_called_once_with(opened.return_value)
                    session.return_value.__exit__.assert_called_once()
                    self.assertIs(component.request, verified)
                    verified.get.assert_called_once_with(SCHEDULE_URL, max_redirects=0)
            self.assertEqual(next(Path(tmp).rglob("session.json")).stat().st_mode & 0o777, 0o600)
            trial.dispose.assert_called_once()
            verified.dispose.assert_called_once()

    def test_headless_manual_challenge(self):
        with self.assertRaisesRegex(LoginRequired, "headless = false"):
            http_session.manual_state(Mock(), Browser(Path("."), None, True, 50), Profile(), Mock(), Path("."))

    def test_untrusted_password_action(self):
        html = ('<script>location.href="https://sts.cuhk.edu.cn/adfs/oauth2/authorize?'
                'redirect_uri=https://sis.cuhk.edu.cn/sso/dologin.html"</script>')
        page = ('<base href="https://example.org/"><form id="loginForm" action="/adfs/oauth2/authorize" '
                'method="post"><input id="passwordInput"></form>')
        with patch.object(http_session, "_exchange", side_effect=[(200, "", html), (200, "", page)]) as exchange:
            with self.assertRaises(ScheduleError):
                http_session.password_login(Mock(), Profile("user", "test"))
        self.assertEqual(exchange.call_count, 2)

    def test_expired_schedule_restarts_once_and_preserves_output(self):
        sessions = []
        @contextmanager
        def protected(*args):
            component = Mock()
            sessions.append(component)
            yield component

        with TemporaryDirectory() as tmp, patch.object(http_session, "protected", protected), \
             patch("cuhksz_sis_scraper.schedule.sync_playwright"), \
             patch("cuhksz_sis_scraper.http_schedule.read_schedule", side_effect=LoginRequired()) as read:
            output = Path(tmp) / "out.json"
            output.write_text("original")
            with self.assertRaises(LoginRequired):
                export_courses(output, profile=Profile(), on_status=lambda _: None)
            self.assertEqual([call.args[0] for call in read.call_args_list], sessions)
            self.assertEqual(len(sessions), 2)
            self.assertEqual(output.read_text(), "original")
