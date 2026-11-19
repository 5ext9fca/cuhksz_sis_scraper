import unittest

from bs4 import BeautifulSoup
from cuhksz_sis_scraper.directory import read_directory
from cuhksz_sis_scraper.peoplesoft import form_values


class HttpExperimentTests(unittest.TestCase):
    def test_form_and_state_transition(self):
        form = BeautifulSoup("""<form><input name="ICStateNum" value="1">
            <input disabled name="disabled"><input type="checkbox" name="off">
            <input type="checkbox" checked name="on"><select name="term">
            <option>old</option><option selected value="2620">new</option></select>
            <textarea name="note">text</textarea></form>""", "html.parser").form
        self.assertEqual(form_values(form), dict(ICStateNum="1", on="on", term="2620", note="text"))

    def test_multiple_details_resolve_links_again_after_return(self):
        group = '<span id="DERIVED_SSS_BCC_GROUP_BOX_1$147$$span$1">BIO - Biology</span>'
        def listing(offset):
            return group + ''.join(
                f'<tr id="trCOURSE_LIST$1_row{i}"><td><span id="CRSE_NBR$span${i}">{n}</span></td>'
                f'<td><a id="CRSE_TITLE${offset+i}">Course</a></td></tr>' for i,n in enumerate(["1001","1002"]))
        def detail(number):
            return (f'<span id="DERIVED_CRSECAT_DESCR200">BIO {number} - Course</span>'
                    '<span id="DERIVED_CRSECAT_DESCR254A$0">Prerequisite: MAT1001<br>or MAT1002</span>')
        steps = iter([("DERIVED_SSS_BCC_SSR_EXPAND_COLLAPS$1", listing(0)),
                      ("CRSE_TITLE$0", detail("1001")), ("DERIVED_SAA_CRS_RETURN_PB", listing(5)),
                      ("CRSE_TITLE$6", detail("1002")), ("DERIVED_SAA_CRS_RETURN_PB", group)])
        def action(key):
            expected, html = next(steps)
            self.assertEqual(key, expected)
            return html
        records, times = read_directory(group, action, {"BIO": []}, None)
        self.assertEqual([r["course_code"] for r in records], ["BIO 1001", "BIO 1002"])
        self.assertEqual(records[1]["enrollment_requirements"]["raw_text"], "Prerequisite: MAT1001\nor MAT1002")
        self.assertEqual(len(times), 5)
