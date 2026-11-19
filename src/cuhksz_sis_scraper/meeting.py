"""Normalize the time and date cells without losing their original text."""
from dataclasses import asdict, dataclass, field
from datetime import datetime
import re


@dataclass
class Meeting:
    days_and_times: str | None
    room: str | None
    instructor: str | None
    date_range: str | None
    weekdays: list[int] = field(init=False, default_factory=list)
    start_minutes: int | None = field(init=False, default=None)
    end_minutes: int | None = field(init=False, default=None)
    start_date: str | None = field(init=False, default=None)
    end_date: str | None = field(init=False, default=None)

    def __post_init__(self):
        time = re.fullmatch(r"(.+?)\s*(\d{1,2}):([0-5]\d)\s*[-–—]\s*(\d{1,2}):([0-5]\d)", self.days_and_times or "")
        if time:
            day = r"(?:星期|周|週)([一二三四五六日天])"
            if not re.sub(day + r"|[\s,，、]", "", time[1]):
                days = ["一二三四五六日".index(d.replace("天", "日")) + 1 for d in re.findall(day, time[1])]
                start = int(time[2]) * 60 + int(time[3])
                end = int(time[4]) * 60 + int(time[5])
                if days and start < end <= 1440:
                    self.weekdays = sorted(set(days))
                    self.start_minutes, self.end_minutes = start, end
        dates = re.fullmatch(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*[-–—]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})", self.date_range or "")
        if dates:
            try:
                first, last = [datetime.strptime(d.replace("/", "-"), "%Y-%m-%d").date() for d in dates.groups()]
            except ValueError:
                return
            if first <= last:
                self.start_date, self.end_date = first.isoformat(), last.isoformat()

    @classmethod
    def from_record(cls, record):
        meeting = cls(*(record[k] for k in ("days_and_times", "room", "instructor", "date_range")))
        if asdict(meeting) != record:
            raise ValueError("排课时间与原文不符。")
        return meeting
