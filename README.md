# cuhksz_sis_scraper

从香港中文大学（深圳）SIS 导出个人课表和学期开课目录，保存为 JSON。`sis-export` 导出个人各学期的选课记录和成绩；`sis-catalog` 查询开课、上课时间、lecture 对应的 tutorial，以及先修和同修要求。

## 推荐用法：配置 profile.toml

先安装 uv，准备 Python 3.10 或更高版本。两个命令默认使用 HTTP。只有人工认证或显式选择浏览器后端时才需要安装配置的浏览器。在项目目录中运行：

```sh
uv sync --locked
cp -n profile.example.toml profile.toml
chmod 600 profile.toml
```

编辑 `profile.toml`。下面以 2025 年入学、选择 Computer Engineering 方向的电子与计算机工程学生为例：

```toml
username = ""
password = ""
major = "电子与计算机工程"
cohort = 2025
stream = "Computer Engineering"
```

- **登录**　同时填写账号和密码后，首次导出通过学校 ADFS 登录，后续优先复用 HTTP 会话。两项都留空或遇到人工验证时，在打开的学校页面完成认证。程序随后关闭本次创建的标签页，通过 HTTP 继续导出。凭据以明文保存在本机，`profile.toml` 已加入 Git 忽略列表。
- **专业**：填好 `major` 后，目录查询默认按该专业的修读计划筛选。运行 `uv run sis-catalog --list-majors` 可查看支持的理工学院专业名。专业官网查询使用系统 `curl`，读取计划 PDF 需要 Poppler 的 `pdftotext`（macOS 可用 `brew install poppler` 安装）。
- **入学年份和方向**：`cohort` 决定使用哪一届的修读计划，省略时使用官网最新版本。`stream` 填计划中的英文方向名，省略时合并各方向。只导出个人课表时，可省略这三项专业设置。

保存后，在同一目录运行所需命令：

```sh
# 导出个人各学期的课表与成绩，写入 completed.json
uv run sis-export

# 按个人修读计划查询 2026–27 第二学期的开课
uv run sis-catalog --term 2620 -o my-catalog-2620.json
```

`--term` 指定查询开课的学期。省略时按深圳当地日期和学校校历选择本学期；在学期间隔可显式指定学期代码。两个命令均支持用 `-o` 指定输出文件，用 `--profile 路径` 选用另一份个人配置。

## 调整查询范围

临时查询所有专业时使用 `--all-majors`。要进一步限定学科，可多次传入 `--subject`：

```sh
uv run sis-catalog --all-majors --term 2620 -o catalog-2620.json
uv run sis-catalog --all-majors --term 2620 --subject ACT --subject CSC -o act-csc-2620.json
```

已有完整目录时，可以直接按个人配置筛选，免去再次登录 SIS：

```sh
uv run sis-catalog --from-catalog catalog-2620.json -o my-catalog-2620.json
```

也可用 `--major`、`--cohort`、`--stream` 临时指定修读计划，或用 `--scheme` 读取本地计划 JSON、PDF 或官网计划链接。`--from-catalog` 全程离线，可使用本地计划，或复用此前实时查询为同一专业、届别和方向保存的计划缓存。首次离线使用请通过 `--scheme` 提供本地文件。

## 继续中断的导出

实时查询目录时，每完成一个学科就保存一次进度。**重新运行原命令**即可从断点继续，全部完成后才替换输出文件。要从头刷新同一份目录，在原命令后加 `--restart`；要保留前一次进度并查询其他学期或范围，换一个输出路径。

## 浏览器设置

排障或核对结果时，可在任一命令后加 `--backend browser`。例如：

```sh
uv run sis-export --backend browser -o schedule-browser.json
uv run sis-catalog --backend browser --term 2620 -o catalog-browser.json
```

Python 的 `export_courses()` 和 `export_catalog()` 同样接受 `backend="http"`（默认）或 `backend="browser"`。使用已有 Playwright 页面时，继续调用 `fetch_courses(page)` 或 `fetch_catalog(page)`。

HTTP 会话保存在 `profile_dir/http/` 下，按个人配置文件路径和账号隔离。人工认证需将 `headless` 设为 `false`。

项目自带的 `browser.toml` 默认使用有窗口的 Google Chrome，并将登录会话保存在 `.sis-profile/`。需要后台运行时设 `headless = true`，并准备有效会话或在个人配置中填写登录凭据。若使用 Playwright 的 Chromium，先运行 `uv run playwright install chromium`，再从配置中移除 `channel` 行。使用 `--browser-config 路径` 可指定另一份浏览器配置。

更多选项见 `uv run sis-export --help` 和 `uv run sis-catalog --help`。

## 注册资格、班额和学期信息

目录与个人课表使用 JSON v3。更新后请重新导出，已有目录断点请使用 `--restart` 重建。只接受 v2 的规划器需要同步升级后才能导入。

目录的 `course.units` 是固定学分，`units_raw` 保留 SIS 学分原文（含浮动范围）。教学班的 `enrolled`、`capacity` 是读取时的已选人数和容量。未知数值为 null，实际零值为 0。课程的 `enrollment_requirements.restrictions` 保留年级、专业限制及条款原文；`parse_status: partial` 表示应核对完整原文。

目录与课表的每条 meeting 均保留时间和日期原文，提供 `weekdays`（周一至周日为 1–7）、`start_minutes`、`end_minutes`（从午夜起算）和 ISO 格式的 `start_date`、`end_date`。时间待定时保留该行。

运行 `uv run sis-term-info --term 2620` 可独立导出个人选课窗口、学分上下限与考试安排，默认文件为 `term-info-2620.json`。支持 `-o`、`--profile`、`--backend browser` 和 `--browser-config`。Python 可调用 `export_term_info(output, term="2620")` 或 `fetch_term_info(page, term="2620")`。学期报告使用独立的版本 1。窗口和考试分别标记 available（有记录）、empty（已显示空表）、unavailable（该学期或该项未提供）。解析失败时保留原输出。
