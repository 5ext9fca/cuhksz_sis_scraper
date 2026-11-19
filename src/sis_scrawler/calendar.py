"""Resolve SIS terms from the Registry's teaching and examination dates."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from filelock import FileLock

from .schedule import ScheduleError, save_courses
from .web import download

_INDEX = "https://registry.cuhk.edu.cn/en"
CACHE = Path(".sis-calendar.json")
_MAX_AGE = timedelta(days=7)
_TITLE = re.compile(r"Academic Calendar\s+(20\d{2})\s*[-–/]\s*(\d{2}|20\d{2})(?!\d)", re.I)
_TERMS = {"first term": "10", "second term": "20", "summer session": "50"}
_MONTHS = "January February March April May June July August September October November December".split()

# Last known published dates, usable offline before the first refresh.
_BUILTIN = {
    "2510": ("2025-09-01", "2025-12-21"), "2520": ("2026-01-05", "2026-05-16"),
    "2550": ("2026-06-01", "2026-07-22"), "2610": ("2026-09-07", "2026-12-27"),
    "2620": ("2027-01-11", "2027-05-22"), "2650": ("2027-05-31", "2027-07-21"),
}


def _range(text: str) -> tuple[date, date]:
    parts = re.findall(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})\b", text)
    if len(parts) != 2:
        raise ScheduleError(f"无法识别校历日期：{text}")
    try:
        start, end = [date(int(y), _MONTHS.index(m.capitalize()) + 1, int(d)) for d, m, y in parts]
    except ValueError as exc:
        raise ScheduleError(f"校历日期无效：{text}") from exc
    if end < start:
        raise ScheduleError(f"校历日期顺序有误：{text}")
    return start, end


def _calendar_year(html: bytes, year: int) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(string=re.compile(r"^\s*Teaching Terms\s*$", re.I))
    table = heading.find_next("table") if heading else None
    title = _TITLE.search(soup.title.get_text() if soup.title else "")
    if not table or not title or int(title[1]) != year or int(title[2]) % 100 != (year + 1) % 100:
        raise ScheduleError(f"{year} 年校历缺少相应年份的 Teaching Terms 表。")
    periods = {}
    current = None
    for row in table.select("tr"):
        cells = [" ".join(c.stripped_strings) for c in row.find_all(["td", "th"], recursive=False)]
        if len(cells) < 2:
            continue
        label = cells[0].lower()
        if label in _TERMS:
            if current is not None:
                raise ScheduleError(f"{year} 年校历缺少考试日期。")
            current = _TERMS[label]
            start, teaching_end = _range(cells[1])
        elif label == "course examinations" and current is not None:
            exam_start, end = _range(cells[1])
            code = f"{year % 100:02}{current}"
            if (code in periods or start.year != year + int(current != "10")
                    or teaching_end > exam_start or end.year > year + 1):
                raise ScheduleError(f"{code} 教学期与考试期日期不一致。")
            periods[code] = {"start": start.isoformat(), "end": end.isoformat()}
            current = None
    if list(periods) != [f"{year % 100:02}{suffix}" for suffix in _TERMS.values()]:
        raise ScheduleError(f"{year} 年校历的三个学期不完整。")
    dates = list(periods.values())
    if any(a["end"] >= b["start"] for a, b in zip(dates, dates[1:])):
        raise ScheduleError(f"{year} 年校历学期重叠。")
    return periods


def _read_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        cached = json.loads(path.read_bytes())
        if cached["version"] != 1 or not isinstance(cached["years"], dict):
            raise ValueError
        if datetime.fromisoformat(cached["updated_at"]).tzinfo is None:
            raise ValueError
        for item in cached["years"].values():
            for period in item["terms"].values():
                date.fromisoformat(period["start"])
                date.fromisoformat(period["end"])
        return cached
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise ScheduleError("校历缓存格式无效，请用 --update-calendar 更新。") from exc


def update_calendar(cache: str | Path = CACHE) -> dict:
    """Discover published academic years and atomically refresh their dates."""
    path = Path(cache)
    with FileLock(str(path) + ".lock"):
        index = BeautifulSoup(download(_INDEX), "html.parser")
        pages = {}
        for link in index.select("a[href]"):
            match = _TITLE.fullmatch(link.get_text(" ", strip=True).split("(")[0].strip())
            url = urljoin(_INDEX, link["href"])
            if match and urlsplit(url).netloc == "registry.cuhk.edu.cn":
                pages[int(match[1])] = url
        if not pages:
            raise ScheduleError("教务处导航中未找到已发布校历。")
        try:
            years = _read_cache(path).get("years", {})
        except ScheduleError:
            years = {}
        now = datetime.now(timezone.utc).isoformat()
        for year, url in sorted(pages.items()):
            years[str(year)] = dict(source=url, updated_at=now, terms=_calendar_year(download(url), year))
        result = dict(version=1, updated_at=now, years=years)
        save_courses(result, path)
        return result


def current_term(day: date | None = None, *, cache: str | Path = CACHE) -> str:
    day = day or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    try:
        cached = _read_cache(Path(cache))
    except ScheduleError:
        cached = {}
    now = datetime.now(timezone.utc)
    if not cached or now - datetime.fromisoformat(cached["updated_at"]) >= _MAX_AGE:
        try:
            cached = update_calendar(cache)
        except (ScheduleError, OSError) as exc:
            print(f"校历更新失败，正在使用已有日期。{exc}", file=sys.stderr)
    periods = _BUILTIN.copy()
    for item in cached.get("years", {}).values():
        periods.update({code: (p["start"], p["end"]) for code, p in item["terms"].items()})
    for code, (start, end) in periods.items():
        if start <= day.isoformat() <= end:
            return code
    raise ScheduleError(f"{day} 处于学期间隔或校历尚未发布。请用 --term 指定 SIS 学期代码。")
