"""Parse the SIS enrollment requisite without discarding its original wording."""
import re
from dataclasses import dataclass


@dataclass
class EnrollmentRequirements:
    raw_text: str | None
    parse_status: str
    prerequisite: dict | None = None
    corequisite: dict | None = None
    exclusion: dict | None = None


_LABEL = re.compile(
    r"(?<!\w)(?:\d+[.)、]\s*)?(pre[- ]?requisites?|co[- ]?requisites?|exclusions?|"
    r"先修(?:课程|要求)?|同修(?:课程|要求)?|互斥(?:课程)?|排除(?:课程)?)\s*[:：]", re.I)
_TOKEN = re.compile(r"[A-Z]{2,}\s*\d{4}[A-Z]*|\band\b|\bor\b|[()]|及|且|或", re.I)


def _expression(text: str) -> dict:
    """Only interpret a complete expression of course codes, AND, OR and parentheses."""
    tokens = []
    pos = 0
    for match in _TOKEN.finditer(text):
        if text[pos:match.start()].strip():
            return {"text": text}
        tokens.append(match[0].upper())
        pos = match.end()
    if text[pos:].strip():
        return {"text": text}

    def parse(start: int, level: int) -> tuple[dict, int]:
        if level == 2:
            token = tokens[start]
            if token == "(":
                value, end = parse(start + 1, 0)
                if tokens[end] != ")":
                    raise ValueError
                return value, end + 1
            code = re.fullmatch(r"([A-Z]+)\s*(\d{4}[A-Z]*)", token)
            if not code:
                raise ValueError
            return {"course_code": f"{code[1]} {code[2]}"}, start + 1
        values = []
        value, end = parse(start, level + 1)
        values.append(value)
        operators = ("OR", "或") if level == 0 else ("AND", "且", "及")
        while end < len(tokens) and tokens[end] in operators:
            value, end = parse(end + 1, level + 1)
            values.append(value)
        if len(values) == 1:
            return values[0], end
        return {"any_of" if level == 0 else "all_of": values}, end

    try:
        result, end = parse(0, 0)
        if end == len(tokens):
            return result
    except (ValueError, IndexError):
        pass
    return {"text": text}


def parse_requirements(raw: str | None) -> EnrollmentRequirements:
    if not raw:
        return EnrollmentRequirements(raw, "empty" if raw == "" else "unavailable")
    result = EnrollmentRequirements(raw, "partial")
    labels = list(_LABEL.finditer(raw))
    if not labels:
        return result
    complete = not raw[:labels[0].start()].strip()
    for i, label in enumerate(labels):
        end = labels[i + 1].start() if i + 1 < len(labels) else len(raw)
        clause = raw[label.end():end].strip().rstrip(";；.。").strip()
        name = label[1].lower()
        key = ("prerequisite" if name.startswith(("pre", "先")) else
               "corequisite" if name.startswith(("co", "同")) else "exclusion")
        if getattr(result, key) is not None:
            # Repeated headings do not specify the relation between the clauses.
            setattr(result, key, {"text": raw})
            complete = False
            continue
        value = _expression(clause)
        setattr(result, key, value)
        complete = complete and "text" not in value
    result.parse_status = "parsed" if complete else "partial"
    return result
