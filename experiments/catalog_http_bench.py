"""Compare guest SIS catalog actions over HTTP and headless Chrome."""
import argparse
from html.parser import HTMLParser
import json
import re
from statistics import median
from time import perf_counter
import xml.etree.ElementTree as ET

from cuhksz_sis_scraper.catalog import _DIRECTORY, _DIRECTORY_PATH, _update
from playwright.sync_api import sync_playwright


class Fields(HTMLParser):
    """Collect text under SIS field IDs, including nested formatting."""
    void = set("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.fields = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        field = dict(attrs).get("id")
        if field:
            self.fields[field] = []
        if tag not in self.void:
            self.stack.append((tag, field))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        for _, field in self.stack:
            if field and field.startswith(("CRSE_", "DERIVED_")):
                self.fields[field].append(data)

    def values(self, prefix):
        return {k: " ".join("".join(v).split()) for k, v in self.fields.items()
                if k.startswith(prefix)}


def run(browser, mode):
    with browser.new_context(locale="en-US") as context:
        page = context.new_page()
        page.goto(_DIRECTORY)
        letter = "DERIVED_SSS_BCC_SSR_ALPHANUM_B"
        page.locator("#" + letter).wait_for()
        form = dict(page.locator("form[name=win0]").evaluate("f => [...new FormData(f)]"))
        observations = []
        timings = []

        def action(key):
            start = perf_counter()
            if mode == "browser":
                _update(page.main_frame, lambda: page.locator(f'[id="{key}"]').click(),
                        _DIRECTORY_PATH)
                html = page.locator("#win0divPSPAGECONTAINER").inner_html()
            else:
                form.update(ICAction=key, ICAJAX="1", ICNAVTYPEDROPDOWN="0")
                response = context.request.post(_DIRECTORY, form=form, max_redirects=0)
                try:
                    if response.status != 200:
                        raise RuntimeError(f"SIS returned HTTP {response.status}")
                    root = ET.fromstring(response.body())
                finally:
                    response.dispose()
                html = root.findtext("FIELD[@id='win0divPSPAGECONTAINER']")
                if html is None:
                    html = root.findtext("FIELD[@id='win0divPAGECONTAINER']")
                state = re.search(r"ICStateNum\.value\s*=\s*(\d+)", root.findtext(
                    "GENSCRIPT[@id='onloadScript']", ""))
                if not html or not state:
                    raise ValueError("Missing SIS component or next form state")
                form["ICStateNum"] = state[1]
            result = Fields(html)
            timings.append(perf_counter() - start)
            return result

        subjects = action(letter).values("DERIVED_SSS_BCC_GROUP_BOX_1")
        group = next(k.rsplit("$", 1)[1] for k, v in subjects.items() if v.startswith("BIO -"))
        observations.append(subjects)
        rows = action("DERIVED_SSS_BCC_SSR_EXPAND_COLLAPS$" + group)
        titles = rows.values("CRSE_TITLE$")
        link = next(k for k, v in titles.items() if re.fullmatch(r"CRSE_TITLE\$\d+", k) and v)
        number = rows.values("CRSE_NBR$span$")["CRSE_NBR$span$" + link.rsplit("$", 1)[1]]
        observations.append((titles, number))
        detail = action(link)
        heading = detail.values("DERIVED_CRSECAT_DESCR200")["DERIVED_CRSECAT_DESCR200"]
        if heading.partition(" - ")[0].split() != ["BIO", number]:
            raise ValueError(f"Unexpected detail heading {heading!r}")
        observations.append((heading, detail.values("DERIVED_CRSECAT_DESCR254A$")))
        returned = action("DERIVED_SAA_CRS_RETURN_PB").values("DERIVED_SSS_BCC_GROUP_BOX_1")
        if returned != subjects:
            raise ValueError("Return did not restore the subject list")
        return observations, timings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=5)
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("--pairs must be positive")
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        results = {"browser": [], "http": []}
        reference = None
        for i in range(args.pairs):
            for mode in (["browser", "http"] if i % 2 == 0 else ["http", "browser"]):
                data, times = run(browser, mode)
                if reference is not None and data != reference:
                    raise ValueError("Results differ across runs")
                reference = data
                results[mode].append(times)
        totals = {k: median(map(sum, samples)) for k, samples in results.items()}
        print(json.dumps(dict(course=reference[2][0], samples_s=results, median_s=totals,
                              speedup=totals["browser"] / totals["http"]), indent=2))


if __name__ == "__main__":
    main()
