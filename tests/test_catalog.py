"""Search-result pagination and completeness checks using a local SIS fixture."""
import os
import unittest

from playwright.sync_api import sync_playwright
from cuhksz_sis_scraper import Profile, ScheduleError, fetch_catalog
from cuhksz_sis_scraper.catalog import _DIRECTORY, _SEARCH, _link_tutorials, _sections


def grid(counter, rows, action=""):
    sections = []
    for number, status in rows:
        sections.append(f"""
            <a id="DERIVED_CLSRCH_SSR_CLASSNAME_LONG${number}">L01-LEC({number})</a>
            <span id="PSXLATITEM_XLATSHORTNAME${number}">正常</span>
            <div id="win0divDERIVED_CLSRCH_SSR_STATUS_LONG${number}"><img alt="{status}"></div>
            <table id="SSR_CLSRCH_MTG1$scroll${number}">
            <tr><th>星期和时间</th><th>教室</th><th>讲师</th><th>会议日期</th></tr>
            <tr><td>周一</td><td>101</td><td>Lee</td><td>9–12 月</td></tr></table>
        """)
    return (f'<div id="$ICField106$scroll$0"><span class="PSGRIDCOUNTER">{counter}</span>'
            + action + "".join(sections) + "</div>")


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel=os.getenv("SIS_TEST_CHANNEL"))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def page_with(self, *responses):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        documents = iter(responses)
        page.route(_SEARCH, lambda route: route.fulfill(content_type="text/html; charset=utf-8",
                                                        body=next(documents)))
        page.goto(_SEARCH)
        return page.main_frame

    def test_view_all_then_next_includes_closed_and_waitlisted(self):
        action = """<a href="#" id="$ICField106$%s$0"
            onclick="fetch(location.href,{method:'POST'}).then(r=>r.text()).then(
            s=>document.body.innerHTML=s);return false">更多</a>"""
        first = [(101, "开放")]
        second = first + [(102, "关闭")]
        frame = self.page_with(grid("1-1/3", first, action % "hviewall"),
                               grid("1-2/3", second, action % "hdown"),
                               grid("3/3", [(103, "等候列表")]))
        sections = _sections(frame, "0")
        self.assertEqual([s["status"] for s in sections], ["开放", "关闭", "等候列表"])
        self.assertEqual(sections[-1]["session"], "正常")
        self.assertEqual(sections[-1]["meetings"][0]["instructor"], "Lee")

    def test_lecture_tutorial_choices_are_scoped_and_paginated(self):
        def action(field, label):
            return (f'<a id="{field}" href="#" onclick="fetch(location.href,'
                    "{method:'POST'}).then(r=>r.text()).then(s=>document.body.innerHTML=s);"
                    f'return false">{label}</a>')

        numbers = [101, 102, 103, 201, 202, 203]
        listing = grid("1-6/6", [(n, "开放") for n in numbers],
                       action("CLASS_SRCH_WRK2_SSR_PB_SELECT$101", "选择课程")
                       + action("CLASS_SRCH_WRK2_SSR_PB_SELECT$102", "选择课程"))
        for n in numbers[3:]:
            listing = listing.replace(f"L01-LEC({n})", f"T{n - 200:02}-TUT({n})")

        def preview(count, options, more=False):
            rows = "".join(f'<tr><td id="SSR_CLS_TBL_R1_RELATE_CLASS_NBR${i}">{n}</td></tr>'
                           for i, n in enumerate(options))
            paging = action("SSR_CLS_TBL_R1$fviewall$0", "查看所有区段") if more else ""
            return ('<div>选择 指导教学 区段 (必需):<div><table id="SSR_CLS_TBL_R1$scroll$0">'
                    + rows + f'<tr><td class="PSGRIDCOUNTER">{count}</td><td>{paging}</td></tr>'
                    + '</table></div></div>' + action("DERIVED_CLS_DTL_CANCEL_PB", "取消"))

        frame = self.page_with(listing, preview("1/1", [203]), listing,
                               preview("1/2", [201], True), preview("1-2/2", [201, 202]), listing)
        sections = _sections(frame, "0")
        _link_tutorials(frame, "0", sections)
        self.assertEqual([s["tutorial_selection"] for s in sections[:3]], [
            dict(status="known", min_select=1, max_select=1, class_numbers=["203"]),
            dict(status="known", min_select=1, max_select=1, class_numbers=["201", "202"]), {"status": "unknown"}])
        self.assertNotIn("tutorial_selection", sections[3])

    def test_duplicate_section_or_short_page_fails(self):
        for rows in [[(101, "开放"), (101, "开放")], [(101, "关闭")]]:
            with self.subTest(rows=rows):
                with self.assertRaises(ScheduleError):
                    _sections(self.page_with(grid("1-2/2", rows)), "0")

    def test_search_uses_requested_subject_term_and_all_statuses(self):
        form = """
            <script>
            async function post(action) {
                if (action === "search") window.open("about:blank");
                const values = new URLSearchParams({action,
                    subject: document.querySelector('[id^=SSR_CLSRCH_WRK_SUBJECT]').value,
                    term: document.querySelector('[id^=CLASS_SRCH_WRK2_STRM]').value,
                    comparison: document.querySelector('[id^=SSR_CLSRCH_WRK_SSR_EXACT_MATCH1]').value,
                    number: document.querySelector('input[type=text]').value,
                    open: document.querySelector('input[type=checkbox]').checked});
                const res = await fetch(location.href, {method:'POST', body:values});
                if (action === 'search') document.body.innerHTML = await res.text();
            }
            </script>
            <select id="CLASS_SRCH_WRK2_STRM" onchange="post('term')">
              <option value="2520">旧学期</option><option value="2610">第一学期</option></select>
            <select id="SSR_CLSRCH_WRK_SUBJECT" onchange="post('subject')">
              <option value=""></option><option>ACT</option><option>CSC</option></select>
            <select id="SSR_CLSRCH_WRK_SSR_EXACT_MATCH1"><option>G</option></select>
            <input type="text" id="SSR_CLSRCH_WRK_CATALOG_NBR">
            <input type="checkbox" id="SSR_CLSRCH_WRK_SSR_OPEN_ONLY" checked>
            <button id="CLASS_SRCH_WRK2_SSR_PB_CLEAR" onclick="post('clear')">清除</button>
            <button id="CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH" onclick="post('search')">搜索</button>
        """
        result = ('<button id="CLASS_SRCH_WRK2_SSR_PB_NEW_SEARCH">新搜索</button>'
                  '<span id="DERIVED_CLSRCH_SSS_PAGE_KEYDESCR">CUHK(SZ) | 第一学期</span>'
                  '<span id="DERIVED_CLSRCH_DESCR200$0">ACT 2111 - Accounting</span>'
                  + grid("1/1", [(101, "关闭")]))
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        other = context.new_page()
        popups = []
        page.on("popup", lambda popup: popups.append(popup))
        searches = []

        def respond(route):
            body = form
            if route.request.method == "POST":
                data = route.request.post_data_json
                if data["action"] == "search":
                    searches.append(data)
                    body = result.replace("ACT 2111", data["subject"] + " 2111")
                    if data["subject"] == "CSC":
                        body = body.replace("(101)", "(102)")
                else:
                    body = ""
            route.fulfill(content_type="text/html; charset=utf-8", body=body)

        click = ("onclick=\"fetch(location.href,{method:'POST'}).then(r=>r.text()).then("
                 "s=>document.body.innerHTML=s);return false\"")
        listing = ('<h1>浏览课程目录</h1>'
                   '<span id="DERIVED_SSS_BCC_GROUP_BOX_1$147$$span$1">ACT - Accounting</span>'
                   '<table><tr id="trCOURSE_LIST$1_row1"><td>'
                   '<span id="CRSE_NBR$span$0">2111</span></td><td>'
                   f'<a href="#" id="CRSE_TITLE$0" {click}>Accounting</a></td></tr></table>')
        detail = ('<span id="DERIVED_CRSECAT_DESCR200">ACT 2111 - Accounting</span>'
                  '<div id="DERIVED_CRSECAT_DESCR254A$0">Prerequisite: A or B<br>'
                  'Co-requisite: C (or instructor approval)</div>'
                  f'<a href="#" id="DERIVED_SAA_CRS_RETURN_PB" {click}>返回 浏览课程目录</a>')
        entries = iter([listing, detail, listing, listing.replace("ACT -", "CSC -"),
                        detail.replace("ACT 2111", "CSC 2111"), listing.replace("ACT -", "CSC -")])
        page.route(_DIRECTORY, lambda route: route.fulfill(content_type="text/html; charset=utf-8",
                                                              body=next(entries)))
        page.route(_SEARCH, respond)
        page.goto(_SEARCH)
        report = fetch_catalog(page, term="2610", subjects=["act", "ACT", "CSC"], profile=Profile())
        self.assertEqual(searches, [dict(action="search", subject=subject, term="2610",
                                        comparison="G", number="0", open="false") for subject in ("ACT","CSC")])
        self.assertEqual(len(popups), 2)
        self.assertTrue(popups[0].is_closed())
        self.assertEqual(context.pages, [page, other])
        self.assertEqual(report["subjects"], ["ACT", "CSC"])
        self.assertEqual(page.url, _SEARCH)
        self.assertEqual(report["offerings"][0]["sections"][0]["class_number"], "101")
        self.assertEqual(report["offerings"][0]["course"]["enrollment_requirements"]["raw_text"],
                         "Prerequisite: A or B\nCo-requisite: C (or instructor approval)")
