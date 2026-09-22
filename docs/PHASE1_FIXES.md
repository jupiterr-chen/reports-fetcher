# 一期评审修复记录（PHASE1_REVIEW T1–T7）

| 项目 | 内容 |
|---|---|
| 日期 | 2026-09-21（Asia/Shanghai） |
| 基线 | 评审基线 `709bb72`（v1.0.0）→ 修复版本 **v1.0.1** |
| 任务书 | [PHASE1_REVIEW.md](PHASE1_REVIEW.md) |
| 验证 | 全量单测 **320 个全绿**（评审时 306；新增/改写 14 项任务专项测试）；容器内真实 HTTP 集成回归见 §8 |

---

## T1 · Store 启动生命周期（锁先于共享状态修改）

**修复**：`Store.__init__` 顺序改为 `acquire_owner_lock() → _init_schema() → _recover()`——构造即持锁，schema 变更与崩溃恢复全部在锁内；第二实例在构造期即被拒绝，此前不触碰数据库与临时文件。CLI/serve 调用点的显式 acquire 移除（构造已含）。

**验证**：`test_store.py::TestOwnerLock::test_second_store_construction_rejected_without_side_effects`——持锁实例有 registered 意图与在途 .part 时，第二实例构造报 `store_in_use` 且 intent 状态/临时文件不变；所有者退出后新实例正确恢复（清扫、intent→failed）。正常单实例抓取与重启恢复测试保留全绿。

## T2 · 常驻服务元数据缓存（发现会话）

**修复**：`FetchService.begin_discovery_session()`——适配器内 ticker 映射/submissions 缓存以"发现会话"为生命周期；`JobService.run_job` 每任务开启新会话。会话内共享（同批不重复请求），会话间重新获取；传输层与限速器保持共享。

**验证**：`test_jobs.py::TestDiscoverySessionRefresh`——同一常驻实例：任务 A（旧列表）→ 来源新增申报 → 任务 B（不同幂等键，refresh=True）发现新 source_id 并下载（downloaded=1/cached=4），submissions 请求恰 2 次；两 US 证券同任务共享 1 次 ticker 获取。真实 e2e：修复后 serve 抓到 HK 更早年份申报（见 §8）。

## T3 · 原子入队

**修复**：`JobService.submit` 的幂等查找、队列计数与 INSERT 纳入同一 `BEGIN IMMEDIATE` 写事务（立即取写锁；rollback/异常妥善处理，不把唯一约束异常当常规结果；事务内不联网）。

**验证**：`test_jobs.py::TestAtomicSubmit`（2 线程 barrier 确定性并发）：同键同请求 → 同一 job_id、仅 1 条任务；同键异请求 → 成功 + `JobConflictError`；不同键竞争最后一个名额 → 成功 + `QueueFullError`，queued 计数不超限。串行重放与跨 client 命名空间行为不变（既有测试）。

## T4 · SEC /A 完整性

**修复**：`us_edgar` 对 `/A` 不再凭 form 后缀标 `amendment_full`，改为 `document_role=unknown` + 警告"未确认全文重发"；保留 `source_form/is_amendment/revision_of`。选择函数：组内有原全文 → 选原全文并警告"未合并的修订"；仅缺证据修订 → 不计作完整财报（跳过并警告）。`amendment_full` 角色保留给未来可验证的完整重发分支。

**验证**：`test_us_edgar.py::test_amendment_keeps_as_unverified_relation`（原全文胜出 + 未合并警告）、`test_unverified_amendment_alone_not_full`；`test_selection.py::test_unverified_amendment_keeps_original`。原测试中"/A 直接断言为完整修订并胜出"的错误假设已移除。

## T5 · HK 单年标签不猜期末

**修复**：`parse_hk_title_period` 仅"明确期末日"（截至…止）设期（explicit_title）；单年标签与跨年标签均 → `null + unknown + 警告`（区分两种警告文案），不再拼接 12-31/06-30。模块 docstring 与 DESIGN §7 已澄清。

**验证**：`test_hk_hkexnews.py::TestTitlePeriod` 重写（7 种年标签形态均 unknown+警告；3 种明确日期仍正确）；列表测试改为期未知 + 按公告日排序。真实 e2e：00700 的 `中期報告 2026`/`2025 年報` 等全部带"仅有年份标签、无期末日证据"警告（§8）。

## T6 · 状态汇总按实际可用文件

**修复**：core `_finalize_status`：全部文件失败 → `failed`；有可用文件 + （失败项/质量警告/预算截断/历史不足）→ `partial`；否则 `ok`。`selection` 区分 **warnings（质量）** 与 **notices（说明）**：正常 last_n 截取、偏好语言正常选择归 notices，不降级状态、不影响 CLI 退出码；语言回退仍为质量警告。coverage 新增 `insufficient_history` 与 `notices`。jobs 持久化质量警告（partial 证券必然含可用文件）。

**验证**：`test_jobs.py::TestStatusMatrix`（评审小矩阵）：全下载失败 → job/symbol `failed`（report_ids 空）；部分失败 → `partial`（3 可用+1 失败）；干净任务 → `succeeded` 且 warnings 空、截取为说明；`last_n=20` 不足 → `partial` + `insufficient_history`。既有 AAPL 干净任务从 partial 修正为 succeeded。

## T7 · 文档路由鉴权（P2 一并修复）

**修复**：令牌模式下禁用 FastAPI 默认 docs/openapi/redoc 路由，显式注册受全局 dependency 保护的 `/openapi.json` 与 `/docs`（fastapi 0.141 无 `fastapi.docs` 模块，Swagger UI 以等价内联 HTML 提供）；`/redoc` 关闭（说明性入口，访问 /docs 即可）。本地模式行为不变。

**验证**：`test_api.py::TestDocsProtection`：无凭据 `/docs`、`/openapi.json` → 401、`/redoc` → 404；错误凭据 403；正确凭据 200 且 schema 含业务路径；本地模式三项均 200。

## 附 · 回归发现的契约补充（非任务书项）

修复期真实回归发现騰訊 2017–2021 年报使用**合并子类别** `年報 / 環境、社會及管治資料/報告`，原精确映射将其当未知跳过。已按包含关系扩展映射（含"年報"→ANNUAL、含"中期/半年度報告"→INTERIM；纯 ESG 子类别仍精确排除），并放宽 t1=40000 的保留过滤为映射后类型。测试：`test_combined_annual_esg_subcategory_mapped_as_annual`（真实结构上的实测值）；生产验证 §8（0700.HK last_n=8 → 8/8 归档、未知子类别警告清零）。

## 集成回归（评审"修复后复审"要求）

于修复版本、开发机容器（**非目标服务器**，DEPLOY.md 部署冒烟待目标机执行）：

1. 三市场各 1 份真实 HTTP 抓取（fixreg-001）：CN 600519 `succeeded`、US AAPL `succeeded`（正常截取不再降级 ✓T6）、HK 00700 `partial`（年标签警告 ✓T5），3/3 cached（既有归档），0 失败；
2. 同幂等键重放（已终态）→ **200 同 job** ✓；
3. 原文下载 → ETag 与 sha256 实测一致 ✓；
4. `docker compose restart serve` 后 job/reports/file 读取全部 200 ✓；
5. 0700.HK `last_n=8`（fixreg-002）→ 8/8（4 新下载含合并子类别旧年报 + 4 cached），未知子类别警告 0 ✓。

## 结论

T1–T7 修复阶段全量单测 320 全绿。后续独立复审执行 178 项相关测试并补验真实第二进程锁竞争、同常驻实例 refresh=True 发现新申报，均通过。**2026-09-21 已在目标服务器部署 v1.0.1 / 76fee13**，完成三市场 HTTP 下载（3/3）、幂等重放、缓存复用与重启后读取；镜像、任务 ID、入口及命令见 [DEPLOY.md](../DEPLOY.md)。以上开发机集成回归记录保留为历史，不与远端验证混同。
