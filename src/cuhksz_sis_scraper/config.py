"""User profile and browser configuration, resolved from the working directory."""
from dataclasses import dataclass, field
from pathlib import Path
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .login import ScheduleError


def _read(path: Path, *, optional: bool, fields: set[str]) -> dict:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        if optional:
            return {}
        raise ScheduleError(f"找不到配置文件 {path}。") from None
    except (ValueError, UnicodeError):
        raise ScheduleError(f"{path} 的 TOML 格式无效。") from None
    if "term" in data:
        raise ScheduleError("查询学期请用 --term 或 API 的 term 参数指定。")
    if data.keys() - fields:
        raise ScheduleError(f"{path} 含未知设置，支持的键为 {', '.join(sorted(fields))}。")
    return data


def _string(data: dict, key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ScheduleError(f"配置项 {key} 需要字符串。")
    return value


@dataclass(frozen=True)
class Profile:
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    major: str = ""
    cohort: int | None = None
    stream: str = ""
    source: Path = field(default_factory=lambda: Path("profile.toml").resolve(), repr=False, compare=False)

    @classmethod
    def load(cls, source: "Profile | str | Path | None" = None) -> "Profile":
        if isinstance(source, cls):
            return source
        path = Path(source or "profile.toml").expanduser()
        data = _read(path, optional=source is None, fields={"username", "password", "major", "cohort", "stream"})
        username = _string(data, "username").strip()
        password = _string(data, "password")
        if bool(username) != bool(password):
            raise ScheduleError("请在 profile.toml 中同时填写 username 和 password，或同时留空。")
        cohort = data.get("cohort")
        if cohort is not None and (type(cohort) is not int or not 1000 <= cohort <= 9999):
            raise ScheduleError("cohort 应为四位入学年份，例如 2025。")
        return cls(username, password, _string(data, "major").strip(), cohort,
                   _string(data, "stream").strip(), path.resolve())


@dataclass(frozen=True)
class Browser:
    profile_dir: Path
    channel: str | None
    headless: bool
    login_timeout_ms: int

    @classmethod
    def load(cls, source: str | Path | None = None) -> "Browser":
        path = Path(source or "browser.toml").expanduser()
        data = _read(path, optional=source is None, fields={"channel", "headless", "profile_dir", "login_timeout"})
        channel = _string(data, "channel")
        if channel not in {"", "chrome", "msedge", "chromium"}:
            raise ScheduleError("channel 支持 chrome、msedge 或 chromium。")
        headless = data.get("headless", False)
        timeout = data.get("login_timeout", 180)
        directory = _string(data, "profile_dir", ".sis-profile")
        if type(headless) is not bool or type(timeout) is not int or timeout <= 0 or not directory.strip():
            raise ScheduleError("headless 应为布尔值，login_timeout 应为正整数秒，profile_dir 应为非空路径。")
        return cls((path.parent / Path(directory).expanduser()).resolve(), channel or None,
                   headless, timeout * 1000)
