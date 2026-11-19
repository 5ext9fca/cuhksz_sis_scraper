"""Read verified guest catalog entries by their exact subject and number."""
from dataclasses import asdict
import re
from time import perf_counter

from bs4 import BeautifulSoup, NavigableString, Tag

from .requirements import parse_requirements


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
        parts_of_field = [
            str(node) if isinstance(node, NavigableString) else "\0"
            for node in field.descendants if isinstance(node, NavigableString) or node.name == "br"
        ]
        text = "\n".join(re.sub(r"[ \t\r\n]+", " ", line).strip()
                         for line in "".join(parts_of_field).split("\0")).strip()
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts)


def read_directory(initial_html, action, targets, limit, *, allow_missing=False):
    soup = initial_html if isinstance(initial_html, Tag) else BeautifulSoup(initial_html, "html.parser")
    timings = []

    def navigate(key):
        start = perf_counter()
        html = action(key)
        timings.append(perf_counter() - start)
        return html if isinstance(html, Tag) else BeautifulSoup(html, "html.parser")

    records = []
    for subject, requested in targets.items():
        groups = subjects_in(soup)
        if subject not in groups:
            letter = "DERIVED_SSS_BCC_SSR_ALPHANUM_" + subject[0]
            if soup.find("a", id=letter):
                soup = navigate(letter)
            groups = subjects_in(soup)
        if subject not in groups:
            if allow_missing:
                continue
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
                if allow_missing:
                    continue
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
