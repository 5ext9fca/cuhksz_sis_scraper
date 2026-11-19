"""Read the guest SIS course directory over HTTP or compare it with Chrome."""
import argparse
from collections import defaultdict
from contextlib import contextmanager
import json
from pathlib import Path
import re
from statistics import median

from playwright.sync_api import sync_playwright

from cuhksz_sis_scraper.catalog import _DIRECTORY, _DIRECTORY_PATH, _update
from cuhksz_sis_scraper.peoplesoft import Component
from cuhksz_sis_scraper.directory import read_directory
from cuhksz_sis_scraper.schedule import save_courses


@contextmanager
def directory(pw, mode):
    if mode == "http":
        request = pw.request.new_context(extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
        try:
            component = Component(request)
            component.open(_DIRECTORY)
            yield str(component.form), lambda key: str(component.act(key))
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
