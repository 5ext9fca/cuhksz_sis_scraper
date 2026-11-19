import argparse
from dataclasses import replace
import json
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from .config import Profile
from .schedule import ScheduleError, export_courses


def _config_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", choices=("http", "browser"), default="http")
    parser.add_argument("--profile", type=Path, help="用户配置，默认 ./profile.toml")
    parser.add_argument("--browser-config", type=Path, help="浏览器配置，默认 ./browser.toml")


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 CUHK(SZ) SIS 的所有学期课程记录")
    parser.add_argument("-o", "--output", type=Path, default=Path("completed.json"))
    _config_options(parser)
    args = parser.parse_args()
    try:
        result = export_courses(args.output, profile=args.profile, browser_file=args.browser_config, backend=args.backend)
    except (PlaywrightError, ScheduleError, OSError) as exc:
        parser.exit(1, f"导出失败：{exc}\n")
    for term in result["terms"]:
        print(f"{term['term']}  {len(term['courses'])}")
    print(args.output.resolve())


def catalog_main() -> None:
    from .calendar import CACHE, current_term, update_calendar
    from .catalog import export_catalog, list_terms, list_subjects, normalize_course
    from .schemes import list_majors, profile_plan, filter_catalog
    from .schedule import save_courses

    parser = argparse.ArgumentParser(description="按学期和学科导出 SIS 开课及教学班信息")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--course", help="实时精确查询完整课号，例如 CSC1001")
    mode.add_argument("--list-terms", action="store_true", help="列出可查询的 SIS 学期")
    mode.add_argument("--list-subjects", action="store_true", help="列出所选学期的学科")
    mode.add_argument("--update-calendar", action="store_true", help="从教务处更新本地学期日期并退出")
    mode.add_argument("--list-majors", action="store_true", help="列出学院官网本科专业")
    parser.add_argument("--major", help="学院官网专业名称，或计划 JSON 中的专业名")
    parser.add_argument("--scheme", help="指定教务处计划页、PDF 网址、本地 PDF 或本地计划 JSON")
    parser.add_argument("--cohort", type=int, help="入学学年的起始年，如 2025，默认最新公布版本")
    parser.add_argument("--stream", help="计划中的英文专修方向，如 Computer Engineering")
    parser.add_argument("--restart", action="store_true", help="忽略已有进度，重新抓取全部指定学科")
    parser.add_argument("--from-catalog", type=Path, help="筛选已导出的目录 JSON")
    parser.add_argument("--term", help="SIS 学期代码，默认按深圳日期判断")
    parser.add_argument("--subject", action="append", help="学科代码，可重复指定，默认全部")
    parser.add_argument("-o", "--output", type=Path, help="默认 catalog.json，按专业筛选时默认 catalog-major.json")
    parser.add_argument("--all-majors", action="store_true", help="此次实时查询全部专业")
    _config_options(parser)
    args = parser.parse_args()
    listing = args.list_terms or args.list_subjects
    if listing or args.course is not None:
        if any((args.subject, args.major, args.scheme, args.cohort is not None, args.stream,
                args.from_catalog, args.all_majors, args.restart)):
            parser.error("单课和列表查询不能与学科、修读计划、离线筛选或断点选项组合")
        if (listing and args.output) or (args.list_terms and args.term):
            parser.error("列表输出到终端，仅 --list-subjects 接受 --term")
    if args.all_majors and any((args.major, args.stream, args.cohort, args.scheme, args.from_catalog)):
        parser.error("--all-majors 与修读计划筛选选项不能同时使用")
    try:
        if args.update_calendar:
            calendar = update_calendar()
            print(f"{CACHE.resolve()}  {', '.join(sorted(calendar['years']))}  {calendar['updated_at']}")
            return
        if args.list_majors:
            print("\n".join(list_majors()))
            return
        connection = dict(backend=args.backend, browser_file=args.browser_config, profile=args.profile)
        if listing:
            options = list_terms(**connection) if args.list_terms else list_subjects(args.term, **connection)
            for code, label in options:
                print(f"{code}  {label}")
            return
        if args.course is not None:
            course = normalize_course(args.course)
            term = args.term or current_term()
            if not (term.isascii() and term.isdecimal() and len(term) == 4):
                parser.error("--term 需要四位 SIS 学期代码")
            output = args.output or Path(f"catalog-{term}-{course.replace(' ', '')}.json")
            report = export_catalog(output, term=term, course=course, **connection)
            print(f"{report['term_label']}  {course}  {output.resolve()}")
            if not report["offerings"]:
                print("该学期未查到此课。")
            for offering in report["offerings"]:
                print("班号  班别  状态  已选／容量")
                for section in offering["sections"]:
                    counts = [str(section[k]) if section[k] is not None else "—" for k in ("enrolled", "capacity")]
                    print(f"{section['class_number']}  {section['section']}  {section['status']}  {'／'.join(counts)}")
            return
        profile = Profile.load(args.profile)
        if args.all_majors:
            profile = replace(profile, major="", stream="", cohort=None)
        plan = profile_plan(profile, major=args.major, source=args.scheme, cohort=args.cohort,
                            stream=args.stream, offline=bool(args.from_catalog))
        if args.from_catalog and plan is None:
            parser.error("--from-catalog 需要指定专业或修读计划")
        output = args.output or Path("catalog-major.json" if plan else "catalog.json")
        if args.from_catalog:
            if args.from_catalog.resolve() == output.resolve():
                parser.error("筛选输出须与输入目录使用不同文件")
            data = json.loads(args.from_catalog.read_text(encoding="utf-8"))
            report = filter_catalog(data, plan, args.subject)
            if args.term and args.term != report["term"]:
                parser.error("输入目录的学期与 --term 不一致")
            save_courses(report, output)
        else:
            report = export_catalog(output, profile=profile, browser_file=args.browser_config, backend=args.backend,
                                    term=args.term, subjects=args.subject, plan=plan, restart=args.restart)
    except (ScheduleError, PlaywrightError, OSError, ValueError) as exc:
        parser.exit(1, f"导出失败：{exc}\n")
    print(f"{report['term_label']}  {len(report['offerings'])} 门课程  {output.resolve()}")


def term_info_main() -> None:
    from .term_info import export_term_info
    parser = argparse.ArgumentParser(description="导出个人学期选课窗口、学分限制和考试安排")
    parser.add_argument("--term", required=True)
    parser.add_argument("-o", "--output", type=Path)
    _config_options(parser)
    args = parser.parse_args()
    output = args.output or Path(f"term-info-{args.term}.json")
    try:
        export_term_info(output, term=args.term, profile=args.profile, browser_file=args.browser_config, backend=args.backend)
    except (PlaywrightError, ScheduleError, OSError) as exc:
        parser.exit(1, f"导出失败：{exc}\n")
    print(output.resolve())
