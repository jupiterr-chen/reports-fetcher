# 一期验收简报（I6）

| 项目 | 内容 |
|---|---|
| 日期 | 2026-09-21（Asia/Shanghai） |
| 版本 | v1.0.0 |
| 环境 | Windows 11 + Docker Desktop；容器 `python:3.12-slim`（Docker 主力，宿主机无 venv）；网络为国内家庭宽带 |
| 核对基准 | [ARCHITECTURE.md](../ARCHITECTURE.md) §10 一期完成要求；[REQUIREMENTS.md](../REQUIREMENTS.md) v1.0；[HTTP_API.md](../HTTP_API.md) §8 |
| 测试 | 306 个单测全绿（fixtures 驱动不触网；覆盖 symbol/period/selection/store/downloader/us/cn/hk/jobs/api/core 集成） |

---

## 1. ARCHITECTURE §10 一期完成要求逐条核对

| # | 要求 | 结果 | 证据 |
|---|---|---|---|
| 1 | 三市场承诺类型的文件可用 | ✅ | §2 冒烟矩阵：CN Q1/H1/Q3/FY、HK ANNUAL/INTERIM、US 10-Q/10-K/**20-F（BABA，3 月财年）** 全部 24/24 归档，文件/manifest/checksum 三方一致 0 不匹配 |
| 2 | 立即重跑不重复下载 | ✅ | 矩阵缓存重跑 24 项全 `cached`、无新增 artifact（I1–I5 各迭代 e2e 反复验证） |
| 3 | 同名/修订文件不覆盖 | ✅ | I4：`--refresh` 内容变化 → 新 artifact 并存、旧版本按 ID 可读 checksum 一致；ready 版本被外部篡改拒绝覆盖（单测）；文件名含 report_id/artifact_id 不因标题碰撞覆盖 |
| 4 | 单证券失败隔离 | ✅ | I5 单测（MSFT 失败不影响 AAPL）+ I4 批量长跑（30 只中 00011 源站索引缺席，其余 29 只正常归档） |
| 5 | HTTP 全调用链可用 | ✅ | I5 e2e（compose serve + curl）：提交 202/Location → 轮询终态 → reports 过滤/游标分页 → 按 ID 下载（ETag=sha256、304、nosniff、attachment、checksum 实测一致） |
| 6 | 故障/限流/空结果可区分 | ✅ | 稳定错误码体系（source_unavailable/source_rate_limited/source_contract_changed/no_reports/file_not_available/…）；源站 403/429/网络错误记录在任务结果而不伪装查询失败（单测覆盖） |
| 7 | 文档与实际契约一致 | ✅ | §4 一致性核对；实现期偏差全部回填设计文档并在 §5 留档 |
| 8 | 性能为记录环境后的目标 | ✅ | §3 性能基线（不承诺固定分钟数） |

## 2. 三市场集成冒烟矩阵（DESIGN §16 样本留档）

全新归档目录（`reports-acceptance/`）、`--last 4`、2026-09-21 实测。24/24 成功、0 失败。

| 市场 | 代码（公司） | 类型覆盖 | 样本（期 → 公告日 → 来源） | 期间判定 |
|---|---|---|---|---|
| US | AAPL（Apple） | 10-Q×3 + 10-K | 2026-06-27→2026-07-31→…/000032019326000020/aapl-20260627.htm；10-K 2025-09-27→2025-10-31 | source_field（9 月财年，非日历季） |
| US | BABA（Alibaba） | **20-F×4** | 2026-03-31→2026-05-20→…/000119312526231755/baba-20260331.htm（2023/24/25 年度类推） | source_field（3 月财年） |
| CN | 600519（贵州茅台，沪） | H1/Q1/FY/Q3 | H1 2026-06-30→2026-08-15→static.cninfo.com.cn/finalpage/2026-08-15/1225475868.PDF（其余类推） | explicit_title |
| CN | 000001（平安银行，深） | H1/Q1/FY/Q3 | H1 2026-06-30→2026-08-15→…/1225475344.PDF | explicit_title（"一季度报告"变体） |
| HK | 00700（騰訊控股） | INTERIM×2 + ANNUAL×2 | INTERIM 2026-06-30→2026-08-25→…/2026082500557_c.pdf；ANNUAL 2025-12-31→2026-04-09 | explicit_title（单年标签） |
| HK | 00016（新鴻基地產，**六月年结**） | INTERIM×2 + ANNUAL×2 | 2025/26 中期報告→2026-03-19→…/2026031900315_c.pdf（全部跨年标签） | **unknown + 警告**（不猜测，铁律真实场景） |

未支持项（按设计显式报告）：6-K/40-F/北交所 → `unsupported_form`/`unsupported_market_segment`；QTR-HK 显式可选（I3 真网验证：騰訊季度业绩公告期末来自标题明确日期）。

## 3. 性能基线（NFR-8，记录制）

| 指标 | 数值 |
|---|---|
| 环境 | 见表头；容器启动计入墙钟（约 1–2s） |
| 样本 | §2 矩阵：6 只 × 4 份 = 24 份文档 |
| 首次运行 | **121 s**（墙钟，含 2 次网络重试） |
| 缓存重跑 | **18 s**（24 项全 cached；耗时主要为三市场 resolve/list 元数据请求） |
| 总字节 | 136 MiB（PDF 为主 + SEC Inline XBRL HTML） |
| 请求数 | 约 38–40（推算口径：US 2×(1+1+4)、CN 2×(预热 1+topSearch 1+列表 1–2+文件 4)、HK 2×(prefix 1+检索 1+文件 4)；单次运行计数未内置指标，诚实记录口径） |
| 重试 | 2 次（网络错误，DEBUG 日志留证 /tmp 外不归档） |
| 待测目标对照 | 原"≤10 分钟/≤1 分钟"目标：首次 121s ≪ 10min ✅；缓存 18s 略超 1min（24 份、三市场元数据往返；样本量小时容器启动占比高）——按 NFR-8 记录制口径如实记录，不修改目标定义 |
| HTTP 不阻塞 | 慢下载（1.5s/文件）期间健康检查/任务查询 <1s（单测） |

## 4. 文档-实现一致性核对

- REQUIREMENTS FR-1..9 / NFR-1..8：全部落地（覆盖矩阵见 [ITERATION_PLAN.md](../ITERATION_PLAN.md) §4；NFR-5 UA 必配、NFR-6 Docker 主力+依赖分组、NFR-7 未知即 null 均有测试与 e2e 证据）。
- HTTP_API §8 六组验收场景：全部通过（I5 状态块留证：e2e + 单测）。
- DESIGN §6/§7/§8 来源契约：与实现一致；实现期新知均已回填（SEC Inline XBRL 嗅探、Archives 需 UA、CN 标题双变体、HK JSONP 分号/匯豐变体/prefix.do 前缀行为）。
- ARCHITECTURE §4 模块清单：与实际一致（新增 `selection.py` 承载统一选择函数，对应测试 test_selection.py，依赖方向不变）。

## 5. 实现期偏差与决策记录（均已回填文档）

1. `RF_LOCAL_MODE=1`（compose serve 默认）：容器内监听 0.0.0.0 但宿主仅发布 127.0.0.1 时显式声明本地模式（HTTP_API §7"回环可显式启用"的具体化）。
2. schema v1→v2 为增量迁移（jobs 三表自动加入既有库，非重建）；旧版本二进制打开新库明确报版本不兼容（实测）。
3. JobService 逐证券串行执行（每证券进度即时持久化）；任务内下载并行度 ≤3 不变，限速预算不受影响。
4. HK prefix.do 去零前缀搜索行为与 00011 索引缺席（fixture `prefix_search_variants.json`）；不取前缀近似命中。
5. CLI `--forms` 保持单市场语义；HTTP 用 `forms_by_market`（HTTP_API §3），空数组/未知类型 422。
6. GET reports 的 `warnings` 字段由持久化元数据派生（期未知/修订标记）；发现期过程警告在任务结果与日志中，不落报告行。

## 6. 已知限制清单（README 同步）

见 [README.md](../README.md)「已知限制」：6-K/40-F/北交所、QTR-HK 显式可选、非日历年结公司期未知、prefix.do 个别索引缺席、Inline XBRL 不恢复离线资源、P0 单进程单执行器、限速合并预算、性能记录制。

## 7. 结论

ARCHITECTURE §10 一期完成要求逐条核对通过。一期范围（I0–I6）全部交付：三市场 CLI 归档（原始需求，I3 达成）、可靠性硬化（I4）、HTTP 服务与持久化任务（I5）、验收发布（I6，本简报）。后续分析（内容提取/指标/基本面）按 [ANALYSIS_ROADMAP.md](../ANALYSIS_ROADMAP.md) 另立阶段。
