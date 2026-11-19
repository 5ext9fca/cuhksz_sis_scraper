# sis_scrawler 用户指南

用 `sis-export` 从港中大（深圳）SIS 导出个人各学期的课程记录，用 `sis-catalog` 查询某个学期的开课、教学班和上课时间。两个命令都将结果保存为 JSON。

## 首次使用

准备 uv 和 Python 3.10 或更高版本。在项目目录运行以下命令，使用本机的 Google Chrome 导出个人课表：

```sh
uv sync --locked
uv run sis-export -o ../completed.json
```

如果学校要求认证，在打开的浏览器中选择「简体中文」并登录。导出结束后，可在上级目录打开 `completed.json`。下次运行会复用项目目录的 `.sis-profile/`。如果使用 Playwright 自带的 Chromium，先运行 `uv run playwright install chromium`，并在 `browser.toml` 中省略 `channel`。

## 导出个人课表

`sis-export` 默认导出本科生各学期的记录，包括已注册、已退课和候补课程。省略 `-o` 时写入当前目录的 `completed.json`。命令结束时会列出各学期的课程数和输出路径。

在 JSON 的 `terms[].courses` 中可查看课程名称、状态、学分、评分方式和成绩。每门课的 `sections[].meetings` 保存班别及其排课。空成绩为 `null`，空学期的课程列表为 `[]`。

## 按学期搜索开课及教学班

```sh
# 本学期全部学科和专业
uv run sis-catalog --all-majors -o catalog.json

# 2026–27 第一学期，仅查询会计与计算机科学
uv run sis-catalog --term 2610 --subject ACT --subject CSC -o act-csc.json
```

默认按深圳当地日期选择学期，从教学首日算到期末考试最后一天，含首尾两天。学期间隔、下一学年校历尚未发布或查询其他学期时，用 `--term` 指定 SIS 学期代码。重复传入 `--subject` 可查询多个学科。

自动选学期时，程序通过系统 `curl` 从教务处发现已发布的校历，缓存超过 7 天后会重新查询。网络不可用时使用已有缓存或内置日期。运行 `uv run sis-catalog --update-calendar` 可立即更新。项目目录下的 `.sis-calendar.json` 记录各学年的官网来源、更新时间和学期起止日期。

输出默认写入 `catalog.json`。`subjects` 列出已查询的学科，包括返回零门课程的学科。`courses[].sections` 包含班号、区段、期次（`session`）、状态和排课（`meetings`）。查询包含开放、关闭和候补班。个人课表和开课目录都在整次导出成功后才替换目标文件。

### 中断后继续导出

实时查询目录时，每个学科完整读完后都会保存进度。例如输出为 `catalog.json` 时，进度保存在同目录的 `catalog.json.progress.json`。中断后重新运行原命令即可继续。已完成的学科保留上次读取时的班级状态，部分读取的学科会重新查询。全部学科完成并写出结果后，进度文件会自动清除。

学期、学科范围或修读计划版本发生变化时，请指定新的输出路径。如需刷新已读取的班级状态或放弃旧进度，可对原命令加 `--restart`。Python 调用 `export_catalog(..., restart=True)` 的效果相同。

## 按专业修读计划筛选

先列出理工学院的本科专业，再用专业名查询。下载官网计划需要系统 `curl`，读取 PDF 需要 Poppler 的 `pdftotext`（macOS 可用 `brew install poppler`）。

```sh
uv run sis-catalog --list-majors
uv run sis-catalog --major 电子与计算机工程 --cohort 2025 \
  --stream "Computer Engineering" --term 2610 -o ece.json
```

`--cohort 2025` 选择 **2025–26 学年入学**的修读计划，`--term 2610` 选择 **2026–27 第一学期实际开设**的课程。省略 `--cohort` 时采用官网公布的最新入学版本。省略 `--stream` 时合并该专业各方向。指定方向时使用计划中的英文名称。可再传入 `--subject` 缩小查询范围。

已有 `catalog.json` 时，加 `--from-catalog` 即可直接筛选该文件，免去再次登录 SIS。以下示例分别使用官网和本地的计划：

```sh
uv run sis-catalog --from-catalog catalog.json --major 电子与计算机工程 \
  --cohort 2025 --stream "Computer Engineering" -o ece-web.json

uv run sis-catalog --from-catalog catalog.json --scheme ../major_schemes.json \
  --major 计算机工程 -o ece-local.json
```

`--scheme` 接受教务处的计划附件列表页、PDF 直链、本地 PDF 或本地计划 JSON。使用计划 JSON 时，`--major` 填写文件内的专业或方向名称。本地目录搭配本地计划可离线筛选。

专业筛选默认输出 `catalog-major.json`。`study_plan` 记录计划来源、版本、所选方向和匹配课号。每门课的 `plan_matches` 列出纳入依据，包含要求类别、适用方向和 PDF 页码（从第一页起计）。例如查看 `courses[0]["plan_matches"]` 即可定位第一门课的原文。同一课号可有多条依据。使用本地计划 JSON 时，`path` 指向原文件中的条目。完整计划的逐课依据位于 `study_plan.course_evidence`。匹配范围包括计划列明的学院课、必修课、选修课和明确允许的等效选项。涉及学分组合、先修条件或个别审批时，请以原修读计划和 SIS 选课要求为准。

## 用户与浏览器配置

命令和 Python 导出 API 默认从当前工作目录读取 `profile.toml` 与 `browser.toml`。目录筛选只读取用户配置。要指定其他路径，使用 `--profile 路径` 和 `--browser-config 路径`。

### profile.toml

首次使用可复制 `profile.example.toml` 为 `profile.toml`，并执行 `chmod 600 profile.toml`。该文件保存明文凭据，已列入 `.gitignore`。从旧版迁移时，将 `.sis-credentials.json` 的账号和密码填入对应字段。

```toml
username = ""
password = ""
major = "电子与计算机工程"
cohort = 2025
stream = "Computer Engineering"
```

账号与密码同时填写可在会话失效时自动登录，同时留空则复用有效会话或在有窗口模式下手动登录。`major`、`cohort` 和 `stream` 均可省略。`cohort` 是入学年份，查询学期由 `--term` 或 Python 的 `term=` 指定，省略时按校历判断。

配置专业后，实时查询和 `--from-catalog` 均使用该专业筛选。可用 `--major`、`--cohort`、`--stream` 临时覆盖，或用 `--all-majors` 实时查询全部专业。指定本地 PDF 或 JSON 时，文件本身决定版本。

```sh
# 使用示例中的个人专业，查询 2026–27 第二学期
uv run sis-catalog --term 2620 -o ece-2620.json
```

### browser.toml

```toml
channel = "chrome"
headless = false
profile_dir = ".sis-profile"
login_timeout = 180
```

`channel` 支持 `chrome`、`msedge`、`chromium`，省略时使用 Playwright 安装的 Chromium。`headless` 控制后台运行。后台登录需要有效会话或已配置的账号密码。`profile_dir` 是保存会话的浏览器目录，相对路径以浏览器配置文件所在目录为准。`login_timeout` 以秒为单位。原有的浏览器命令行选项和 `--credentials` 已迁移到这两个文件。

## Python API

`export_courses` 和 `export_catalog` 负责打开浏览器、写入文件，并返回同一份数据。两者均接受可选的 `profile="path/to/profile.toml"` 和 `browser_file="path/to/browser.toml"` 参数。显式传给 `export_catalog` 的 `plan` 优先于个人专业。

```python
from sis_scrawler import export_catalog, export_courses

schedule = export_courses("../completed.json")
catalog = export_catalog("ece.json", term="2620")
```

如果自行管理 Playwright 同步 `Page`，在简体中文「我的课程表」页调用 `fetch_courses(page)`，在「课程搜索」页调用 `fetch_catalog(page, plan=plan)`。可用 `filter_catalog(catalog, plan)` 筛选已有目录字典，用 `save_courses(report, path)` 将结果写入文件。需要人工认证时，调用方可捕获 `LoginRequired`。
