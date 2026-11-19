# SIS 访客课程目录 HTTP 实验

在项目根目录运行。HTTP 模式使用 Playwright 的独立请求会话，从公开入口取得表单，按 SIS 返回的状态顺序翻页。此模式不启动 Chrome，也无需配置个人账号。

```sh
# 指定课程，保存课号、名称、描述及注册要求
uv run python experiments/catalog_http_bench.py --mode http \
  --course "ACT 3011" --course "CSC 1002" -o /tmp/sis-entries.json

# 每个学科读取前三门；用 --all 可遍历所选学科目录中列出的全部课程
uv run python experiments/catalog_http_bench.py --mode http --subject BIO --subject CSC --limit 3

# 使用本机 Chrome 对照相同的目录查询
uv run python experiments/catalog_http_bench.py --mode compare --subject BIO --limit 2 --pairs 3
```

未指定目标时读取 BIO 的第一门课。对照模式交替安排浏览器与 HTTP 的执行顺序，核对每门课的完整导出记录。标准输出给出各次导航耗时和每轮导航总耗时的中位数，计时范围不含启动和初始 GET。`-o` 在全部查询及对照成功后写出访客目录条目；学期教学班和 tutorial 配对仍通过主项目的 `sis-catalog` 获取。

实现参考了 Python 项目的表单导航和 TypeScript 客户端的表格行解析。[cuhksz-course-planner](https://github.com/unko-wn/cuhksz-course-planner/tree/HEAD/cuhk-sis-scraper) · [Joxos/sislib](https://github.com/Joxos/sislib/blob/HEAD/packages/cuhksz-sislib/src/peoplesoft/grid.ts)

## 正式导出入口的实测对照

2026-11-20 在同一台机器上顺序运行两个后端，逐字段比较完整报告。HTTP 冷启动使用空会话目录并通过 ADFS 密码登录，第二次运行复用刚保存的会话。浏览器使用已有登录状态。以下为各运行一次的观测值，“就绪”计至读到受保护组件，“全程”包括写出 JSON。

- 个人课表共五个学期。HTTP 冷启动就绪 1.66 秒，全程 4.53 秒，16 次请求。HTTP 复用就绪 0.50 秒，全程 3.36 秒，10 次请求。浏览器就绪 1.24 秒，全程 6.28 秒，48 次 SIS 请求。
- 学期 2620 的目录使用同一份本地计划，依次读取 ACT 3011 和 MAT 1002B，共 13 个教学班。HTTP 冷启动就绪 1.40 秒，全程 17.01 秒，34 次请求。HTTP 复用就绪 0.56 秒，全程 16.69 秒，28 次请求。浏览器就绪 2.75 秒，全程 19.06 秒，190 次 SIS 请求。

HTTP 请求数包含 ADFS、受保护组件和访客目录请求。浏览器请求数包含 SIS 页面加载的资源。四次 HTTP 导出均未启动 Chrome。两组对照报告完全相同，包括 ACT 3011 的先修和排除要求，以及 MAT 1002B 两个 lecture 各自限定的四个 tutorial。

### 三轮性能验收

在提交 `e9e4da3` 上使用上述样本，共执行 18 次正式导出。每轮均为 HTTP 密码登录准备空会话目录，紧接着复用该会话。浏览器复用已有登录状态，第二轮调换浏览器与 HTTP 的运行顺序。同一轮的三个结果逐字段相等，落盘 JSON 与返回值一致。12 次 HTTP 导出的 Chrome 启动次数均为零。

以下为三轮全程耗时中位数，括号内为最小值至最大值。就绪耗时也取中位数。

- 五学期课表。HTTP 冷启动 4.77 秒（4.29–4.83），复用 3.37 秒（3.31–3.44），浏览器 7.68 秒（6.83–8.02）。三者就绪耗时依次为 1.49、0.51、1.96 秒，每轮请求数依次为 16、10、48。
- 两科目录，共 13 个教学班。HTTP 冷启动 16.70 秒（16.30–16.98），复用 16.20 秒（15.68–16.35），浏览器 17.65 秒（17.02–18.06）。三者就绪耗时依次为 1.48、0.61、1.47 秒，每轮请求数依次为 34、28、190。

按全程中位数计算，复用 HTTP 比浏览器分别节省 56.1% 和 8.2%。三轮中两项任务的 HTTP 全程耗时均低于同轮浏览器。结论限于本次课表和指定课程样本。
