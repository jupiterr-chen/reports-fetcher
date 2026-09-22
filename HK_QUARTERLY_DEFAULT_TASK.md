# HK 季度业绩默认获取与混合选择修复任务书

| 项目 | 内容 |
|---|---|
| 任务编号 | RF-HK-QTR-DEFAULT-001 |
| 建议版本 | v1.0.3 |
| 优先级 | P1：当前默认行为不满足“已披露季度材料应完整获取” |
| 状态 | 待实现；生产问题已复现，能力本身可用 |
| 范围 | HK 默认类型、混合报告选择、HTTP/CLI 契约、integration-kit、测试与文档 |
| 非范围 | 财报内容解析、指标计算、基本面分析；US 6-K；定时调度 |

## 1. 结论与用户要求

当前问题**尚未解决**：HK 适配器已经能够抓取 `QTR-HK`，但生产默认类型只有
`ANNUAL`、`INTERIM`。调用方不显式传 `forms_by_market.HK` 时，0700.HK 每年只得到
年报和中报。

本任务确认以下产品要求：

1. HK 默认发现类型改为 `ANNUAL`、`INTERIM`、`QTR-HK`。
2. 发行人已披露季度业绩材料时，服务默认将其纳入候选并按最新财务报告期选择。
3. 发行人没有季度材料时正常跳过，不报错、不产生“缺少季报”质量 warning；仍返回
   可用的年报和中报。
4. 是否只使用某种报告由调用方决定。显式 `forms_by_market` 必须继续支持：
   - `{"HK":["ANNUAL","INTERIM"]}`：只取年报/中报；
   - `{"HK":["QTR-HK"]}`：只取季度业绩；
   - 省略 HK 配置：使用三种默认类型。
5. `QTR-HK` 保持真实来源语义：它是港交所“季度業績”公告，不改名冒充法定
   `ANNUAL`/`INTERIM` 全文。港股季度披露属自愿行为，不假定每家公司都有。

## 2. 生产复现证据（2026-09-22）

### 2.1 原默认任务

任务 `job_2563e8787f4042c3a3be`，0700.HK，默认 HK 类型，结果为：

| doc_type | report_period | title |
|---|---|---|
| INTERIM | 2026-06-30 | 中期報告 2026 |
| ANNUAL | 2025-12-31 | 2025 年報 |
| INTERIM | 2025-06-30 | 中期報告 2025 |

该任务 `cached=3`、`failed=0`，证明年报/中报链路正常，但默认未检索季度业绩。

### 2.2 显式季度任务

任务 `job_0636cd75c81f4158ab79` 使用：

```json
{
  "symbols": ["0700.HK"],
  "last_n": 4,
  "forms_by_market": {"HK": ["QTR-HK"]},
  "refresh": false
}
```

结果 `succeeded`，`downloaded=4`、`failed=0`、无 warning：

| doc_type | report_period | period_source | title |
|---|---|---|---|
| QTR-HK | 2026-03-31 | explicit_title | 截至二零二六年三月三十一日止三個月業績公佈 |
| QTR-HK | 2025-09-30 | explicit_title | 截至二零二五年九月三十日止三個月及九個月業績公佈 |
| QTR-HK | 2025-03-31 | explicit_title | 截至二零二五年三月三十一日止三個月業績公佈 |
| QTR-HK | 2024-09-30 | explicit_title | 截至二零二四年九月三十日止三個月及九個月業績公佈 |

季度获取功能本身已经可用。

### 2.3 不能只改默认配置的原因

任务 `job_5fab32fb3eb94f419c5d` 同时请求：

```json
{"HK": ["ANNUAL", "INTERIM", "QTR-HK"]}
```

尽管生产库已经有年报和中报，`last_n=4` 仍选出四份 `QTR-HK`，没有穿插年报/
中报。根因是：

1. HKEX 列表中的年报/中报标题经常只有年份，发现阶段 `report_period=null`；
2. `QTR-HK` 标题含明确期末日，发现阶段已有可信 `report_period`；
3. `select_reports()` 在归档和 PDF 正文报告期富化前执行，并把所有已知期报告排在
   未知期报告之前；
4. 年报/中报的缓存报告期同步及 PDF 正文富化发生在选择之后，无法影响本轮混合排序。

所以直接把 `QTR-HK` 加入默认值会让默认结果偏向季度公告，仍不满足完整报告序列。

## 3. 目标行为

### 3.1 默认行为

`forms_by_market` 省略时，默认值为：

```json
{
  "CN": ["Q1", "H1", "Q3", "FY"],
  "HK": ["ANNUAL", "INTERIM", "QTR-HK"],
  "US": ["10-Q", "10-K", "20-F"]
}
```

在冷库和热库中，0700.HK、`last_n=4` 都必须得到相同的最新四个财务报告期：

1. `2026-06-30` — `INTERIM`
2. `2026-03-31` — `QTR-HK`
3. `2025-12-31` — `ANNUAL`
4. `2025-09-30` — `QTR-HK`

不能因为年报/中报在发现阶段暂时没有报告期而将其排除。

### 3.2 没有季报的发行人

- 默认查询中的 QTR-HK 搜索返回零候选属于正常情况。
- 继续使用 ANNUAL/INTERIM 候选完成选择，不产生错误或“缺少季报” warning。
- 若年报/中报历史足够，允许用更早的年报/中报填满 `last_n`。
- 若所有默认类型合计仍不足 `last_n`，沿用既有 `insufficient_history` 语义；原因是
  总历史不足，不应表述成季度披露失败。
- 显式只请求 `QTR-HK` 且没有结果时，返回正常的 `no_reports`/`no_matching_reports`
  结果，不得伪装网络错误。

### 3.3 数据质量约束

- 继续执行“未知即 null”：不得用公告日、标题年份、12-31 或财季惯例写入
  `report_period`。
- `filing_date` 可以用于**有界候选预选**，不能写成报告期，也不能改变
  `period_source`。
- QTR-HK 的期末日必须来自标题明确日期或未来经验证的权威来源字段。
- ANNUAL/INTERIM 期末日可从既有 manifest 可信值同步，或从已校验 PDF 正文提取；
  不覆盖既有可信期末日。
- 未选中候选不污染任务 warnings。

## 4. 实现要求

### T1：启用 HK 默认季度类型

更新运行配置、任务生效默认值、CLI、HTTP 文档和 integration-kit：HK 默认类型包含
`QTR-HK`。显式 forms 行为保持不变；幂等 request hash 必须包含新的生效默认值。

### T2：修复混合选择的冷库/热库一致性

在最终 `last_n` 截断前，使 HK 混合候选具备可比较的可信报告期。推荐采用有界两阶段
选择，允许实现者提出等价方案，但必须满足本任务全部验收：

1. 发现 ANNUAL、INTERIM、QTR-HK 全部候选；
2. 按 `(market, symbol, source_id)` 从 Store 回填已归档候选的可信
   `report_period/period_source`，然后再做最终选择；
3. 对冷库中仍未知的 ANNUAL/INTERIM，每种类型按公告时间预选至多 `last_n` 个近期
   候选，用已校验 PDF 提取期末日；这里的公告时间只用于限定工作量，不是报告期；
4. 复用已下载并校验的临时文件，最终选中时不得重复下载；未选中的临时文件不得形成
   orphan artifact、done 脏记录或残留 `.part`；Store 仍是唯一归档提交方；
5. 把三类候选按可信 `report_period` 合并、分组、倒序选择最新 N 个逻辑报告期；
6. 提取后仍未知的候选保留 null 并置后，只有最终选中且仍未知的报告才产生 warning；
7. 冷库首跑与缓存重跑必须返回相同的 report_id 顺序和报告期序列。

若采用不同实现，必须在 DESIGN.md 说明如何同时解决：冷库、热库、非日历年结、下载
有界性、临时文件恢复及 Store 单提交方约束。仅修改 `_group_order_key()` 或用
`filing_date` 冒充 `report_period` 不予验收。

### T3：明确任务统计与候选预取语义

- `summary.downloaded/cached/failed` 和 `results[].items` 只统计对调用方产出的最终报告，
  或在契约中明确预取报告如何计数；不得下载了额外文件却对外完全不可追溯。
- 推荐最终只归档所选报告；用于判期的未选中临时文件在任务完成前清理。
- 预取失败不得阻止其他可用类型；按最终可用文件聚合 succeeded/partial/failed。
- 任务超时或重启必须收敛，不得遗留永久 running 或未登记文件。

### T4：保持来源分类准确

- HKEX `t1=10000 + title=業績` 中仅 `[季度業績]` 映射为 `QTR-HK`。
- `[中期業績]`、`[末期業績]` 继续跳过，避免与 INTERIM/ANNUAL 正文重复。
- ESG、摘要、通知等仍按现有规则排除。

### T5：契约与版本同步

同步以下内容，建议版本提升至 v1.0.3：

- `REQUIREMENTS.md`：HK 默认类型与“自愿披露、无则跳过”规则；
- `ARCHITECTURE.md`、`DESIGN.md`：混合选择的两阶段数据流和边界；
- `HTTP_API.md`：默认 forms、显式覆盖、无季度候选语义；
- `README.md`、`config.example.toml`；
- `integration-kit/` 的 API、OpenAPI、mock 默认值、cases、示例和 agent 指南；
- `pyproject.toml`、包版本及运行 OpenAPI 版本。

## 5. 验收矩阵

所有解析测试使用真实 HKEX fixture，不触网。不要为测试编造不存在的 HKEX 响应结构。

| 编号 | 场景 | 验收结果 |
|---|---|---|
| A1 | 0700 冷库，省略 forms，last_n=4 | 精确返回 H1-2026、Q1-2026、FY-2025、Q3-2025；顺序按报告期倒序 |
| A2 | A1 立即重跑 | 4 份均 cached；report_id、顺序、期末和 warnings 与 A1 一致 |
| A3 | 0700 显式 ANNUAL+INTERIM | 不出现 QTR-HK；保持原有完整报告行为 |
| A4 | 0700 显式 QTR-HK | 返回季度业绩；期末来自 explicit_title |
| A5 | 无季度 fixture，省略 forms | 正常返回年报/中报；无缺季报错误和 warning |
| A6 | 无季度 fixture，显式仅 QTR-HK | 正常 no_reports/no_matching_reports，不是失败 |
| A7 | 0016.HK 非日历年结 | 不猜 12-31；能提取则 document，否则 null+unknown；混合排序不破坏现有规则 |
| A8 | HKEX 同时返回季度/中期/末期业绩公告 | 只将季度业绩纳入 QTR-HK，中期/末期公告不与正文重复 |
| A9 | 未选中候选期末未知 | 只进入 coverage.notices 聚合，不进入 warnings |
| A10 | 预取中断/失败/重启 | 无孤儿文件、脏 done、永久 running；已有可用报告不被丢弃 |
| A11 | HTTP 默认值和幂等 | 省略 forms 的 request hash 使用新默认；同 key 重放稳定；显式旧类型可正常重放 |
| A12 | integration-kit | mock 默认 HK 包含 QTR-HK，测试覆盖有/无季度及显式覆盖；示例可直接运行 |

最低验证命令：

```sh
docker compose run --rm test
docker compose -f integration-kit/compose.yaml run --rm test
```

全量通过后再做一次真实 0700.HK 冷库或隔离归档验证。未经用户明确授权不得部署生产、
提交、推送或删除文件。

## 6. 完成定义（DoD）

- T1–T5 全部完成，A1–A12 有自动化证据；
- Docker 全量测试和 integration-kit 测试通过；
- 真实 0700 默认请求返回四个连续可用财务报告期，结果符合 A1；
- 无季度发行人正常降级，不把自愿披露缺失当错误；
- 冷库/热库结果一致，未新增重复下载、孤儿文件或错误 warning；
- 文档、配置、mock、OpenAPI 和版本号一致；
- 形成评审汇报：变更文件、测试结果、真实验证任务 ID、已知限制及部署建议。

## 7. 可直接交给开发 Agent 的提示词

```text
请在 reports-fetcher 仓库实现 HK_QUARTERLY_DEFAULT_TASK.md（任务
RF-HK-QTR-DEFAULT-001）的全部内容。开始前阅读 AGENTS.md、REQUIREMENTS.md、
DESIGN.md、HTTP_API.md 和 ITERATION_PLAN.md。

核心目标：HK 默认类型改为 ANNUAL、INTERIM、QTR-HK；发行人有季度业绩时默认纳入，
没有时正常跳过。不能只改 config，因为生产已复现混合选择会把年报/中报全部排到已知
期末的 QTR-HK 后面。必须修复冷库和热库下的混合选择，使 0700.HK、last_n=4 默认
精确返回 2026-06-30 INTERIM、2026-03-31 QTR-HK、2025-12-31 ANNUAL、
2025-09-30 QTR-HK。

遵守未知即 null、Store 唯一归档提交方、Docker 主力、fixture 测试不触网等项目约定。
不得用 filing_date/标题年份/12-31 猜造 report_period。显式 forms 必须仍可只取完整报告
或只取 QTR-HK。没有季度候选不是错误，也不产生缺季报 warning。

按任务书 A1–A12 实现有意义的测试，运行 Docker 全量测试和 integration-kit 契约测试，
再用隔离归档做一次真实 0700.HK 验证。更新需求、架构、设计、HTTP、README、配置、
integration-kit/OpenAPI 和版本号。不要部署生产、提交、推送或删除文件；完成后只汇报
实际差异、测试证据、真实验证结果、残余风险和建议发布版本。
```
