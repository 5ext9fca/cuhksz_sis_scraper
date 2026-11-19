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
