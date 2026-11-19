# cuhksz_sis_scraper

从香港中文大学（深圳）SIS 导出个人课表和学期开课目录，保存为 JSON。`sis-export` 导出个人各学期的选课记录和成绩；`sis-catalog` 查询开课、上课时间、lecture 对应的 tutorial，以及先修和同修要求。

## 推荐用法：配置 profile.toml

先安装 uv，准备 Python 3.10 或更高版本和 Google Chrome。在项目目录中运行：

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

- **登录**：账号密码一起留空时，程序优先复用已有会话。首次使用或登录过期时，在打开的学校页面选择「简体中文」并登录。需要自动登录时可同时填写两项；凭据以明文保存在本机，`profile.toml` 已加入 Git 忽略列表。
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

也可用 `--major`、`--cohort`、`--stream` 临时指定修读计划，或用 `--scheme` 读取本地计划 JSON、PDF 或官网计划链接。已有目录配合本地计划即可离线筛选。

## 继续中断的导出

实时查询目录时，每完成一个学科就保存一次进度。**重新运行原命令**即可从断点继续，全部完成后才替换输出文件。要从头刷新同一份目录，在原命令后加 `--restart`；要保留前一次进度并查询其他学期或范围，换一个输出路径。

## 浏览器设置

项目自带的 `browser.toml` 默认使用有窗口的 Google Chrome，并将登录会话保存在 `.sis-profile/`。需要后台运行时设 `headless = true`，并准备有效会话或在个人配置中填写登录凭据。若使用 Playwright 的 Chromium，先运行 `uv run playwright install chromium`，再从配置中移除 `channel` 行。使用 `--browser-config 路径` 可指定另一份浏览器配置。

更多选项见 `uv run sis-export --help` 和 `uv run sis-catalog --help`。
