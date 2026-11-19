"""Discover undergraduate study schemes and match their course codes to SIS."""
from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .config import Profile
from .login import ScheduleError
from .web import download as _download

PROGRAMMES = "https://sse.cuhk.edu.cn/Undergraduateprogrammes"
_CODE = re.compile(r"([A-Z]{2,8})\s*(\d{4}[A-Z]?)")
_STREAM = re.compile(r"^([A-Za-z][A-Za-z &/-]+?)\s+(?:Stream|Concentration)\s*:?$", re.I | re.M)
_YEARS = re.compile(r"(?<!\d)((?:19|20)\d\d)\s*[-–—至/]\s*(?:20)?\d{2}(?!\d)")


@dataclass(frozen=True)
class StudyPlan:
    major: str
    source: str
    edition: str
    stream: str | None
    codes: frozenset[str]
    evidence: dict[str, list[dict]] = field(default_factory=dict, compare=False, hash=False)
    revision: str = ""

    def annotate(self, course: dict) -> dict:
        code = "".join(course["course_code"].upper().split())
        return {**course, "plan_matches": self.evidence.get(code, [])}

    def matches(self, code: str) -> bool:
        return "".join(code.upper().split()) in self.codes

    @property
    def subjects(self) -> set[str]:
        return {re.match(r"[A-Z]+", code)[0] for code in self.codes}

    def metadata(self) -> dict:
        return dict(major=self.major, source=self.source, edition=self.edition,
                    stream=self.stream, course_codes=sorted(self.codes),
                    sha256=self.revision, course_evidence=self.evidence)


def _html(url: str) -> BeautifulSoup:
    return BeautifulSoup(_download(url).decode("utf-8-sig"), "html.parser")


def list_majors() -> dict[str, str]:
    links = _html(PROGRAMMES).select(".list-item .item-body .title a[href]")
    majors = {a.get_text(strip=True): urljoin(PROGRAMMES, a["href"]) for a in links}
    if not majors:
        raise ScheduleError("专业列表为空，请检查学院网页。")
    return majors


def _major_page(major: str) -> tuple[str, str]:
    majors = list_majors()
    names = [name for name in majors if major in (name, name.split("（")[0])]
    if len(names) != 1:
        raise ScheduleError("请选择学院专业：" + "、".join(majors))
    name = names[0]
    page = majors[name]
    links = {a.get_text(strip=True): urljoin(page, a["href"])
             for a in _html(page).select(".field-body a[href]") if "修读计划" in a.get_text()}
    if len(links) == 1:
        return name, next(iter(links.values()))
    for title, url in links.items():
        if name.split("（")[0] in title:
            return name, url
    raise ScheduleError(f"{name} 的修读计划链接不明确，可用 --scheme 指定教务处页面或 PDF。")


def _attachment(page: str, cohort: int | None) -> tuple[str, str]:
    candidates = []
    for row in _html(page).select(".list-content"):
        link = row.select_one('a[href*=".pdf" i]')
        label = row.select_one(".list-title")
        if not link or not label:
            continue
        title = label.get_text(strip=True)
        years = [int(year) for year in _YEARS.findall(title)]
        if not years:
            continue
        ongoing = re.search(r"以后|之后|以後|之後|thereafter|onwards", title, re.I)
        if cohort is None or cohort in years or (ongoing and cohort >= max(years)):
            candidates.append((max(years), title, urljoin(page, link["href"])))
    if not candidates:
        raise ScheduleError(f"教务处未列出对应入学年份的修读计划：{page}")
    newest = max(item[0] for item in candidates)
    matches = [(title, url) for year, title, url in candidates if year == newest]
    if len(matches) != 1:
        raise ScheduleError("同一入学年份有多个附件，请用 --scheme 指定 PDF。")
    return matches[0]


def _clean_scheme(text: str) -> str:
    # Preserve physical page breaks, including those inside a course list.
    pages = []
    for page in text.split("\f"):
        lines = []
        for line in page.splitlines():
            line = line.strip()
            if re.fullmatch(r"\d{1,3}", line) or line.lower().startswith("last update"):
                continue
            line = re.sub(r"\s{3,}\d{1,3}\s*$", "", line)
            line = re.sub(r"(?<=\w)\[[a-z]\]", "", line, flags=re.I)
            lines.append(re.sub(r"(\d)(?=[A-Z]{2,8}\d{4})", r"\1, ", line))
        pages.append("\n".join(lines))
    return "\n\f\n".join(pages)


def _occurrences(text: str, first_page: int):
    explicit = r"(?<![A-Za-z0-9])([A-Z]{2,8})\s*(\d{4}[A-Z]?)(?![A-Za-z0-9])"
    tail = re.compile(r"\s*(?:[,;/]|\bor\b|\band\b)\s*(\d{4}[A-Z]?)(?![A-Za-z0-9])")
    for first in re.finditer(explicit, text):
        prefix, number = first.groups()
        yield prefix + number, first_page + text.count("\f", 0, first.start(2))
        pos = first.end()
        while more := tail.match(text, pos):
            yield prefix + more[1], first_page + text.count("\f", 0, more.start(1))
            pos = more.end()


_REQUIREMENT = re.compile(
    r"^(?:\d+[.)]\s*)?(School Package|(?:Major )?Required Courses?|(?:Major )?Elective Courses?)"
    r"\s*:", re.I | re.M)


def _pdf_evidence(body: str, notes: str, first_page: int, notes_page: int, stream: str | None):
    evidence = defaultdict(list)

    def record(code, page, category, track):
        entry = {"requirement": category, "page": page, "stream": track}
        if entry not in evidence[code]:
            evidence[code].append(entry)

    headings = list(_STREAM.finditer(body))
    names = [heading[1].strip() for heading in headings]
    if stream:
        wanted = stream.casefold().removesuffix(" stream")
        stream = next((name for name in names if name.casefold() == wanted), None)
        if stream is None:
            raise ScheduleError("计划中可选的方向：" + "、".join(names))
    starts = [(0, None)] + [(h.end(), name) for h, name in zip(headings, names)]
    ends = [h.start() for h in headings] + [len(body)]
    for (start, track), end in zip(starts, ends):
        if stream and track and track != stream:
            continue
        block = body[start:end]
        category = "主修课程要求"
        offset = 0
        for heading in [*_REQUIREMENT.finditer(block), None]:
            stop = heading.start() if heading else len(block)
            page = first_page + body.count("\f", 0, start + offset)
            for code, page_no in _occurrences(block[offset:stop], page):
                record(code, page_no, category, track)
            if heading:
                label = heading[1].lower()
                category = {"school": "学院课", "required": "必修", "elective": "选修"}[
                    label.removeprefix("major ").split()[0]]
                offset = heading.end()
    for match in re.finditer(r"choose to take either\s+([^.;]+?)\s+to fulfill", notes, re.I | re.S):
        choices = list(_occurrences(match[1], notes_page + notes.count("\f", 0, match.start(1))))
        tracks = {entry["stream"] for code, _ in choices for entry in evidence.get(code, [])
                  if entry["requirement"] != "等效选项"}
        for track in sorted(tracks, key=lambda name: name or ""):
            for code, page_no in choices:
                record(code, page_no, "等效选项", track)
    return stream, dict(evidence)


def _pdf_plan(data: bytes, source: str, edition: str, major: str | None,
              stream: str | None) -> StudyPlan:
    if not data.startswith(b"%PDF-"):
        raise ScheduleError(f"下载内容不是 PDF：{source}")
    try:
        text = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", "-", "-"],
                              input=data, capture_output=True, check=True, timeout=45).stdout.decode()
    except FileNotFoundError as exc:
        raise ScheduleError("读取 PDF 需要 Poppler 的 pdftotext。") from exc
    except (subprocess.SubprocessError, UnicodeError) as exc:
        raise ScheduleError(f"PDF 文本提取失败：{source}") from exc
    english = re.split(r"\(Study Scheme\s*[-–—]\s*Chinese\)|Recommended Course Pattern",
                       _clean_scheme(text), maxsplit=1, flags=re.I)[0]
    start = re.search(r"(?im)^Major Programme Requirements?[ \t]*$", english)
    if not start:
        raise ScheduleError("无法识别 PDF 中的英文主修课程要求。")
    header = english[:start.end()]
    title = re.search(r"Programme Title\s*\n\s*([^\n]+)", header, re.I)
    applicability = re.search(r"Applicable to students admitted[^\n]+", header, re.I)
    requirements = english[start.end():]
    note = re.search(r"(?im)^(?:Explanatory\s+)?Notes?\s*:", requirements)
    body = requirements[:note.start()] if note else requirements
    notes = requirements[note.end():] if note else ""
    page = 1 + header.count("\f")
    notes_page = page + body.count("\f")
    stream, evidence = _pdf_evidence(body, notes, page, notes_page, stream)
    if not evidence:
        raise ScheduleError("主修课程要求中未找到可识别的课程代码。")
    return StudyPlan(major or (title[1].strip() if title else Path(source).stem), source,
                     edition or (applicability[0].strip() if applicability else ""), stream,
                     frozenset(evidence), evidence, sha256(data).hexdigest())


def _json_plan(path: Path, major: str | None) -> StudyPlan:
    try:
        data = path.read_bytes()
        schemes = json.loads(data)
    except (UnicodeError, ValueError) as exc:
        raise ScheduleError("无法读取专业修读计划 JSON。") from exc
    if not isinstance(schemes, dict) or major not in schemes:
        names = "、".join(schemes) if isinstance(schemes, dict) else ""
        raise ScheduleError("请用 --major 选择 JSON 中的专业：" + names)

    evidence = defaultdict(list)

    def extract(node: dict, path_keys: list[str]) -> None:
        for key, value in node.items():
            if _CODE.fullmatch(key.upper()):
                category = path_keys[0] if path_keys else "主修课程要求"
                evidence["".join(key.upper().split())].append(
                    {"requirement": category, "path": [major, *path_keys, key]})
            elif isinstance(value, dict):
                extract(value, [*path_keys, key])

    if isinstance(schemes[major], dict):
        extract(schemes[major], [])
    if not evidence:
        raise ScheduleError("所选专业的 JSON 中没有课程代码。")
    return StudyPlan(major, str(path.resolve()), "", None, frozenset(evidence),
                     dict(evidence), sha256(data).hexdigest())


def load_study_plan(major: str | None = None, *, source: str | Path | None = None,
                    cohort: int | None = None, stream: str | None = None) -> StudyPlan:
    """Resolve an SSE major, a registry attachment page, a PDF, or local major_schemes JSON."""
    if source is None:
        if not major:
            raise ScheduleError("请指定专业名称或修读计划。")
        major, source = _major_page(major)
    source = str(source)
    remote = urlsplit(source).scheme in {"https", "http"}
    suffix = Path(urlsplit(source).path).suffix.lower()
    edition = ""
    if remote and suffix != ".pdf":
        edition, source = _attachment(source, cohort)
    elif cohort is not None:
        raise ScheduleError("--cohort 用于在教务处的多个 PDF 版本中选择，请省略或使用专业名称。")
    if not remote and suffix == ".json":
        if stream:
            raise ScheduleError("JSON 中的方向用 --major 指定。")
        return _json_plan(Path(source), major)
    data = _download(source) if remote else Path(source).read_bytes()
    return _pdf_plan(data, source, edition, major, stream)


def profile_plan(profile: Profile, *, major: str | None = None, source: str | None = None,
                 cohort: int | None = None, stream: str | None = None) -> StudyPlan | None:
    # Direct documents already identify an edition. JSON keys name their own majors.
    suffix = Path(urlsplit(source).path).suffix.lower() if source else ""
    if not source or suffix == ".json":
        major = major or profile.major or None
    if not major and not source:
        if stream or cohort is not None:
            raise ScheduleError("请在 profile.toml 或 --major 中指定专业。")
        return None
    if cohort is None and suffix not in {".json", ".pdf"}:
        cohort = profile.cohort
    if stream is None and suffix != ".json" and (not major or major == profile.major):
        stream = profile.stream or None
    return load_study_plan(major, source=source, cohort=cohort, stream=stream)


def filter_catalog(report: dict, plan: StudyPlan, subjects: list[str] | None = None) -> dict:
    """Apply the same exact course-code filter to an existing SIS catalog."""
    selected = list(report["subjects"])
    if subjects is not None:
        requested = {s.strip().upper() for s in subjects}
        if missing := requested - set(selected):
            raise ScheduleError("输入目录未查询这些学科：" + "、".join(sorted(missing)))
        selected = [s for s in selected if s in requested]
    prefixes = plan.subjects
    selected = [s for s in selected if s in prefixes]
    courses = [plan.annotate(c) for c in report["courses"] if c["course_code"].split()[0] in selected
               and plan.matches(c["course_code"])]
    return {**report, "subjects": selected, "courses": courses, "study_plan": plan.metadata()}
