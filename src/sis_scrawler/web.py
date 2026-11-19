"""Download public university documents independently of the SIS session."""
import subprocess
from urllib.parse import quote, urlsplit

from .login import ScheduleError


def download(url: str) -> bytes:
    if urlsplit(url).scheme not in {"https", "http"}:
        raise ScheduleError("下载链接须为 HTTP 或 HTTPS。")
    url = quote(url, safe=":/?&=%+;,@!$'()*~-")
    try:
        return subprocess.run(
            ["curl", "-q", "-fgsSL", "--proto", "=https,http", "--proto-redir", "=https,http",
             "--max-time", "45", "--", url], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ScheduleError(f"无法用 curl 下载学校网页或附件：{url}") from exc
