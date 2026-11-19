"""Persist completed subject batches until the catalog is published."""
from hashlib import sha256
import json
from pathlib import Path

from .schedule import ScheduleError, save_courses


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode()).hexdigest()


class CatalogProgress:
    def __init__(self, output: Path):
        self.path = output.with_name(output.name + ".progress.json")
        self.scope: dict = {}
        self.batches: dict[str, list[dict]] = {}

    def restore(self, scope: dict) -> None:
        self.scope = scope
        if not self.path.exists():
            self.batches = {}
            return
        try:
            data = json.loads(self.path.read_bytes())
            if data["version"] != 1 or data["scope"] != scope:
                raise ScheduleError("断点的学期、查询范围或修读计划版本不同。"
                                    "请换用其他输出路径，或用 --restart 重新抓取。")
            batches = data["batches"]
            if (not isinstance(batches, dict) or not set(batches) <= set(scope["subjects"])
                    or data["checksum"] != _digest(batches)):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise ScheduleError("断点文件损坏，请用 --restart 重新抓取。") from exc
        self.batches = batches

    def commit(self, subject: str, courses: list[dict]) -> None:
        self.batches[subject] = courses
        save_courses(dict(version=1, scope=self.scope, batches=self.batches,
                          checksum=_digest(self.batches)), self.path)
