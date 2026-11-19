"""Read the guest SIS course directory over HTTP or compare it with Chrome."""
import argparse
from collections import defaultdict
from dataclasses import asdict
from contextlib import contextmanager
import json
from pathlib import Path
import re
from statistics import median
from time import perf_counter
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from cuhksz_sis_scraper.catalog import _DIRECTORY, _DIRECTORY_PATH, _update
from cuhksz_sis_scraper.requirements import parse_requirements
from cuhksz_sis_scraper.schedule import save_courses


def form_values(form):
    values = {}
    for field in form.select("input[name], select[name], textarea[name]"):
        kind = field.get("type", "").lower()
        if field.has_attr("disabled") or kind in {"button", "submit", "reset", "image", "file"}:
            continue
        if kind in {"checkbox", "radio"} and not field.has_attr("checked"):
            continue
        if field.name == "select":
            option = field.select_one("option[selected]") or field.select_one("option")
            if option is None:
                continue
            value = option.get("value", option.get_text())
        elif field.name == "textarea":
            value = field.get_text()
        else:
            value = field.get("value", "on" if kind in {"checkbox", "radio"} else "")
        values[field["name"]] = value
    return values


def decode_update(body):
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError("Expected SIS directory XML; start a fresh guest session") from exc
    for name in ("win0divPSPAGECONTAINER", "win0divPAGECONTAINER"):
        html = root.findtext(f"FIELD[@id='{name}']")
        if html:
            break
    script = root.findtext("GENSCRIPT[@id='onloadScript']", "")
    state = re.search(r"ICStateNum\.value\s*=\s*(\d+)", script)
    if not html or not state:
        raise ValueError("SIS response lacks the directory page or next form state")
    return html, state[1]


def response_body(response):
    try:
        if response.status != 200:
            raise RuntimeError(f"SIS returned HTTP {response.status}")
        return response.body()
    finally:
        response.dispose()


@contextmanager
def directory(pw, mode):
    if mode == "http":
        request = pw.request.new_context(extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
        try:
            endpoint = _DIRECTORY
            body = response_body(request.get(endpoint, max_redirects=0))
            html = body.decode("utf-8-sig")
            # The public landing page sets the guest cookie and navigates once
            # via inline JS. Follow only the known same-component bootstrap URL.
            if re.search(r"self\.location\s*=\s*['\"]" + re.escape(_DIRECTORY + "?&") + "['\"]", html):
                endpoint = _DIRECTORY + "?&"
                html = response_body(request.get(endpoint, max_redirects=0)).decode("utf-8-sig")
            form = BeautifulSoup(html, "html.parser").select_one('form[name="win0"]')
            if form is None:
                raise ValueError("Guest course directory form not found")
            values = form_values(form)
            if not values.get("ICStateNum"):
                raise ValueError("Guest course directory has no initial form state")

            def action(key):
                payload = dict(values, ICAction=key, ICAJAX="1", ICNAVTYPEDROPDOWN="0")
                body = response_body(request.post(endpoint, form=payload, max_redirects=0))
                html, values["ICStateNum"] = decode_update(body)
                return html

            yield html, action
        finally:
            request.dispose()
    else:
        browser = pw.chromium.launch(channel="chrome")
        try:
            with browser.new_context(locale="en-US") as context:
                page = context.new_page()
                page.goto(_DIRECTORY)
                container = page.locator("#win0divPSPAGECONTAINER")

                def click(key):
                    _update(page.main_frame, lambda: page.locator(f'[id="{key}"]').click(),
                            _DIRECTORY_PATH)
                    return container.inner_html()

                yield container.inner_html(), click
        finally:
            browser.close()


def subjects_in(soup):
    return {el.get_text(strip=True).split(" - ", 1)[0]: el["id"].rsplit("$", 1)[1]
            for el in soup.select('span[id^="DERIVED_SSS_BCC_GROUP_BOX_1"][id*="$span$"]')}


def courses_in(soup, group):
    courses = {}
    for row in soup.select(f'tr[id^="trCOURSE_LIST${group}_row"]'):
        number = row.select_one('[id^="CRSE_NBR$span$"]')
        title = row.select_one('a[id^="CRSE_TITLE$"]')
        if not number or not title:
            raise ValueError("Incomplete SIS course row")
        code = number.get_text(strip=True)
        if code in courses:
            raise ValueError(f"Duplicate SIS catalog number {code}")
        courses[code] = title["id"]
    return courses


def field_text(soup, prefix):
    parts = []
    for field in soup.select(f'[id^="{prefix}$"]'):
        for br in field.select("br"):
            br.replace_with("\n")
        text = field.get_text().strip()
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts)


def read_directory(initial_html, action, targets, limit):
    soup = BeautifulSoup(initial_html, "html.parser")
    timings = []

    def navigate(key):
        start = perf_counter()
        html = action(key)
        timings.append(perf_counter() - start)
        return BeautifulSoup(html, "html.parser")

    records = []
    for subject, requested in targets.items():
        groups = subjects_in(soup)
        if subject not in groups:
            letter = "DERIVED_SSS_BCC_SSR_ALPHANUM_" + subject[0]
            if soup.find("a", id=letter):
                soup = navigate(letter)
            groups = subjects_in(soup)
        if subject not in groups:
            raise ValueError(f"Subject missing from the directory: {subject}")

        def listing(current):
            group = subjects_in(current).get(subject)
            if group is None:
                raise ValueError(f"Directory no longer shows {subject}")
            rows = courses_in(current, group)
            if not rows:
                current = navigate("DERIVED_SSS_BCC_SSR_EXPAND_COLLAPS$" + group)
                rows = courses_in(current, group)
            if not rows:
                raise ValueError(f"Empty directory listing for {subject}")
            return current, rows

        soup, rows = listing(soup)
        numbers = requested or list(rows)[:limit]
        for i, number in enumerate(numbers):
            if i:
                soup, rows = listing(soup)
            if number not in rows:
                raise ValueError(f"Course missing from the directory: {subject} {number}")
            detail = navigate(rows[number])
            heading = detail.select_one("#DERIVED_CRSECAT_DESCR200")
            code, separator, title = (heading.get_text(strip=True) if heading else "").partition(" - ")
            expected = f"{subject} {number}"
            if " ".join(code.split()) != expected or not separator or not title:
                raise ValueError(f"Expected course detail for {expected}")
            requirements = field_text(detail, "DERIVED_CRSECAT_DESCR254A")
            records.append(dict(course_code=expected, title=title,
                                description=field_text(detail, "SSR_CRSE_OFF_VW_DESCRLONG"),
                                enrollment_requirements=asdict(parse_requirements(requirements))))
            soup = navigate("DERIVED_SAA_CRS_RETURN_PB")
            if subject not in subjects_in(soup):
                raise ValueError(f"Return did not restore {subject}")
    return records, timings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["http", "browser", "compare"], default="compare")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--subject", action="append", help="repeatable subject, default BIO")
    selection.add_argument("--course", action="append", help="exact course, e.g. 'ACT 3011', repeatable")
    parser.add_argument("--limit", type=int, help="courses per subject, default 1")
    parser.add_argument("--all", action="store_true", help="read every course in the selected subjects")
    parser.add_argument("--pairs", type=int, help="compare repetitions, default 5")
    parser.add_argument("-o", "--output", type=Path, help="write course records to JSON")
    args = parser.parse_args()
    if args.pairs is not None and (args.mode != "compare" or args.pairs < 1):
        parser.error("--pairs requires compare mode and a positive count")
    if args.limit is not None and (args.limit < 1 or args.all):
        parser.error("--limit must be positive and cannot be combined with --all")
    if args.course and (args.limit is not None or args.all):
        parser.error("use --limit or --all only with subject selection")
    targets = defaultdict(list)
    for code in args.course or []:
        match = re.fullmatch(r"([A-Z]+)\s*(\d{4}[A-Z]*)", code.strip().upper())
        if not match:
            parser.error(f"invalid course code: {code}")
        if match[2] not in targets[match[1]]:
            targets[match[1]].append(match[2])
    if not targets:
        for subject in args.subject or ["BIO"]:
            subject = subject.strip().upper()
            if not re.fullmatch("[A-Z]+", subject):
                parser.error(f"invalid subject: {subject}")
            targets[subject] = []
    samples = defaultdict(list)
    reference = None
    with sync_playwright() as pw:
        pairs = (args.pairs or 5) if args.mode == "compare" else 1
        modes = ["browser", "http"] if args.mode == "compare" else [args.mode]
        for i in range(pairs):
            for mode in reversed(modes) if i % 2 else modes:
                with directory(pw, mode) as (html, action):
                    data, times = read_directory(html, action, targets, None if args.all else args.limit or 1)
                if reference is not None and data != reference:
                    raise ValueError("Directory records differ across runs")
                reference = data
                samples[mode].append(times)
    if args.output:
        save_courses(dict(source=_DIRECTORY, courses=reference), args.output)
    totals = {mode: median(map(sum, runs)) for mode, runs in samples.items()}
    result = dict(courses=[c["course_code"] for c in reference], samples_s=samples, median_s=totals)
    if args.mode == "compare":
        result["speedup"] = totals["browser"] / totals["http"]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
