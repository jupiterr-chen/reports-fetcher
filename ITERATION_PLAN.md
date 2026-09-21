# 一期迭代计划

| 项目 | 内容 |
|---|---|
| 版本 | v1.0（待确认） |
| 日期 | 2026-09-20 |
| 状态 | 待用户确认；确认后作为开发与交付的执行基线 |
| 关联 | [需求规格](REQUIREMENTS.md) · [架构](ARCHITECTURE.md) · [详细设计](DESIGN.md) · [HTTP 契约](HTTP_API.md) |

---

## 1. 划分原则

1. **垂直切片**：每迭代交付可运行、可演示的能力，不是只完成内部模块。
2. **风险前置**：来源契约是最大不确定性（非官方接口、可能风控），最先验证；服务化建立在已验证的抓取核心之上。
3. **原始需求最早满足**：I3 结束即达成"三市场 CLI 归档"的原始诉求；HTTP 与可靠性硬化随后补齐一期完整范围。中途任何暂停点都有可用产物。
4. **估算后置**：人日估算在 I0 完成后按迭代给出（REVIEW 已撤回 v1.0 的固定估算）；本计划先用 S（≤0.5 天）/ M（约 1 天）/ L（1–2 天）相对规模标记。
5. **迭代内范围冻结**：迭代内新想法进 backlog（§7），不扩当期范围；跨迭代调整需更新本文件并注明原因。

## 2. 总览与依赖

| 迭代 | 主题 | 规模 | 依赖 | 结束时新增可用能力 | 版本号 |
|---|---|---|---|---|---|
| I0 | 来源契约验证与 fixture 冻结 | S | — | 三市场契约事实可复核，后续迭代可估时 | —（产出报告） |
| I1 | 核心链路垂直切片（US 先行） | L | I0 | `fetch AAPL` 端到端可用 | 0.1.0 |
| I2 | CN 适配器（巨潮） | M | I1 | A 股财报归档可用 | 0.2.0 |
| I3 | HK 适配器（披露易） | M | I1 | 三市场 CLI 全可用（**原始需求达成**） | 0.3.0 |
| I4 | 可靠性硬化 | M | I1 | 长跑批量、崩溃恢复、内容版本安全 | 0.4.0 |
| I5 | HTTP 服务与持久化任务 | L | I1–I4 | 其他应用可按 HTTP_API.md 调用 | 0.5.0 |
| I6 | 一期验收与发布 | M | 全部 | 一期完成定义达成 | 1.0.0 |

```text
I0 → I1 → ┬─ I2 ─┬→ I4 → I5 → I6
          └─ I3 ─┘
```

I2 与 I3 相互独立、均只依赖 I1，可按顺序做也可并行做（单人开发建议 I2 → I3，CN 契约相对更稳）。

人日估算（I0 完成后复核，2026-09-20）：I1≈2、I2≈1、I3≈1.5（HK 契约改为 HTML 解析后上调）、I4≈1.5、I5≈2、I6≈1，剩余合计约 9 人日；依据与假设见 [docs/SOURCE_VERIFICATION.md](docs/SOURCE_VERIFICATION.md) §3。

## 3. 迭代详情

### I0 来源契约验证与 fixture 冻结

> **状态：✅ 已完成（2026-09-20）**。结论与 DoD 核对见 [docs/SOURCE_VERIFICATION.md](docs/SOURCE_VERIFICATION.md)，样本见 `tests/fixtures/`，DESIGN 已回填至 v1.3。要点：HK 披露易契约已迁移（旧 JSON 端点 404 → titlesearch.xhtml 深链 HTML），I3 估算上调至 1.5 人日，剩余迭代合计约 9 人日。

- **目标**：把三市场全部"待实测/候选契约"变为可复核事实，冻结选择规则测试用例。
- **范围内**：Docker 基座（Dockerfile dev 目标 + compose probe 服务 + .dockerignore），全部探测在容器内执行（主力技术方案，宿主机不装 venv）；一次性探测脚本（`tools/probe/`，不进生产包）；录制正常/异常响应 fixture；回填 DESIGN §6/§7/§8 与附录；确认 CN 翻页终止条件、HK recordCnt/分页或窗口切分方式、SEC filings.files 历史形态；实测限速初值与所需请求头/Cookie；复核 I1–I6 规模并给出人日估算表。
- **范围外**：任何生产代码；6-K/北交所探测（记入 backlog）。
- **交付物**：`tests/fixtures/`（真实样本+脱敏说明）、DESIGN 契约回填、《来源契约验证简报》（含限速实测值与后续估时）。
- **DoD**：
  1. 每市场 ≥1 正常 + ≥1 异常 fixture（必须包含 HK `result` 解码后的数组形态、SEC recent 并行数组形态）；
  2. 分页/扩窗方式确认并写入 DESIGN；
  3. DESIGN 中所有"待实测/候选"标记闭合（闭合或明确为阻断项）；
  4. CN/HK 请求头与 Cookie 要求、SEC UA 配置项有实测记录；
  5. I1–I6 人日估算表产出；
  6. Docker 基座可用：`docker compose run --rm probe …` 在容器内完成全部探测。
- **主要风险**：披露易/巨潮风控拦截 → 预案：记录 UA/Cookie/频率要求并调整限速初值；仍不可用则该市场标记阻断，不阻塞其余市场。

### I1 核心链路垂直切片（US 先行）

> **状态：✅ 已完成（2026-09-20，v0.1.0）**。DoD 全项核对：① `docker compose run --rm cli fetch AAPL --last 4` 端到端成功（4/4 归档，Apple 10-K 期末 2025-09-27 印证 9 月财年语义；宿主机文件/manifest/checksum 三方一致，8 artifacts 0 不一致）；② 立即重跑全部 `cached`、目录无重复；③ 强杀容器于下载中途（2 个 .part 半文件 + 3 条 downloading 现场）后重跑：intents 收敛、半文件清扫、4/4 重新归档、无脏 done；④ UA 未配置 → `ua_not_configured` 明确报错退出码 2；⑤ 单测 158 个全绿（symbol/period/selection/store/transport/us_edgar，fixtures 驱动不触网）。实现期新知：SEC 现代主文档为 Inline XBRL（XML 声明 + Workiva 注释开头），内容嗅探已按窗口探测（已回填 DESIGN §8）；文件下载同样必须携带 UA。多市场参数框架就位（CN/HK 符号报 `unsupported_market` 并提示 I2/I3）。

- **目标**：用契约最干净的美股打通"识别 → 发现 → 选择 → 下载 → 归档 → CLI"全链路，验证架构骨架。
- **范围内**：
  - Dockerfile 增加 runtime 目标与 compose cli 服务（归档目录 bind mount、配置经挂载/环境变量注入）；
  - `models.py / symbol.py / period.py` 基础（统一模型、市场识别、日期工具）；
  - `downloader.py` 传输层：来源组限速、显式重试循环（每次重试过限速器）、流式下载+字节上限+SHA-256、PDF/HTML 联合校验、重定向域名检查；
  - `store.py` 最小闭环：schema v1（schema_meta/manifest/artifacts/symbol_map，archive_intents 表一并建好）、upsert 取 report_id、临时文件原子提交、缓存命中（sha256 复核）；
  - `us_edgar.py`：ticker→CIK、submissions（recent + 按需 files）、form 过滤、修订版归基础类型、reportDate 语义；
  - 统一选择函数：分组、版本选择、排序截断的基础实现（市场特定规则仅 US 需要）；
  - CLI `fetch/list`（多市场参数框架就位，仅 US 生效）+ 配置加载；
  - 单测：symbol / selection / period / store / transport / us_edgar（fixture 驱动，不触网）。
- **范围外**：CN/HK 适配器；archive_intents 完整恢复协议（I4）；refresh 内容版本策略（I1 仅跳过已有，不做 refresh 参数）；jobs/HTTP（I5）。
- **DoD**：
  1. `docker compose run --rm cli fetch AAPL --last 4` 端到端成功（容器内执行 `python -m reports_fetcher`），宿主机归档目录 / manifest / checksum 三方一致；
  2. 立即重跑全部 `cached`，无重复记录；
  3. 强杀进程后重跑：无半文件、无脏 done（基础原子性：.part → 校验 → 原子改名 → 标记）；
  4. SEC User-Agent 未配置时 US 明确报错（NFR-5）；
  5. 上述单测模块全绿。
- **演示**：`docker compose run --rm cli fetch AAPL MSFT --last 4` → 展示宿主机 `reports/US/` 目录与 manifest。

### I2 CN 适配器（巨潮）

> **状态：✅ 已完成（2026-09-21，v0.2.0）**。DoD 全项核对：① 沪深 3 只样本（600519 沪/000001 深主板/300750 深创业板）e2e 各 4/4 归档正确（真实数据出现 FY 组中英文并存 → 中文全文优先 + 警告跳过 en 实测生效）；"更新后全文替换默认版本且保留 revision_of"以真实响应结构上的合成样本单测验证（当前抓取窗口内真实数据无更新后样本）；② `fetch 600519` 端到端可用，无年份标题 → `report_period=null + unknown + 警告`（不猜测）单测覆盖；③ 分页 hasMore 驱动 + 预算耗尽 `truncated` 且不宣称穷尽 + 小窗不足有界扩窗，fake 分页单测覆盖；④ announcementTime 毫秒原值保留在 source_metadata，filing_date 按 Asia/Shanghai 落库（实测 1786723200000 → 2026-08-15）。单测 196 个全绿；重跑全 cached；20 artifacts 三方一致 0 不匹配。标题双变体（第一季度/一季度、第三季度/三季度）与公司名前缀有无均实测正确。

- **范围内**：`cn_cninfo.py`（topSearch resolve、hisAnnouncement 列表、分页与有界扩窗）；CN 标题解析（Q1/H1/Q3/FY + 中文数字年份）；`document_role` 判定（原稿 / 更新后全文 / 更正通知 / 摘要 / 英文版）；语言选择（中文全文优先、英文回退）；fixture 单测。
- **DoD**：
  1. 沪深 ≥3 只样本（含 1 份"更新后/修订"样本）召回与选择正确，更新后全文正确替换默认版本且保留版本关系；
  2. `fetch 600519` 可用，未知报告期条目保留 null + 警告；
  3. 分页预算与 `truncated` 上报生效（fake 分页 fixture 驱动）；
  4. 公告时间按 Asia/Shanghai 语义落库，原值保留。
- **演示**：`docker compose run --rm cli fetch 600519 000001 --last 4`。

### I3 HK 适配器（披露易）

> **状态：✅ 已完成（2026-09-21，v0.3.0）——原始需求（三市场 CLI 归档）达成**。DoD 全项核对：① HK 3 只样本 e2e 4/4 归档（00700 騰訊日历年结 / 00005 匯豐 / 00016 新鴻基地產六月年结）；② 跨年标签（2024/25 年報 等）→ `report_period=null + unknown + 警告`，不猜 12-31（fixture 单测 + e2e 双验证，0016 全部 20 组均为 unknown + 警告留痕）；③ QTR-HK 结论落地：**启用为显式可选类型**（默认不启用，`--forms QTR-HK` 显式请求；fixture 单测 + 真网 e2e 验证：騰訊季度业绩公告期末日来自标题明确日期 2026-03-31/2025-09-30）；④ 三市场 CLI 冒烟全通（每市场 3 只 ×4 份，US AAPL/MSFT/BABA + CN 600519/000001/300750 + HK 00700/00005/00016）。单测 244 个全绿；38 artifacts 三方一致 0 不匹配；重跑全 cached。实现期实测新知（回填 DESIGN §7）：JSONP 尾部带分号（`);`）；匯豐中期标题为"2026年中期業績報告"（年份在前），已增加该变体并将跨年检测放宽为任何 `YYYY/NN` 斜杠年份。范围注记：迭代计划原范围文本中的"titleSearcherJson、JSONP 剥壳 + 双重 JSON 数组形态"系 I0 前旧契约描述，实际实现按 DESIGN §7 v1.3（titlesearch.xhtml HTML 深链）执行。

- **范围内**：`hk_hkexnews.py`（prefix.do stockId、titlesearch.xhtml HTML 深链解析、实体反转义 + 子类别权威映射、超站点单页上限年窗切分）；繁体标题解析（明确期末日、中文数字年份、ANNUAL/INTERIM/QTR-HK）；语言回退（中文优先）；非日历财年样本验证。
- **DoD**：
  1. HK ≥3 只样本（含 1 家非日历年结公司）正确归档；
  2. "二零二四年年報"类无明确期末日样本 → `report_period=null` + `period_source=unknown` + 警告，不猜 12-31；
  3. QTR-HK：fixture 验证结论明确（启用为显式可选类型，或维持不支持并记录原因）；
  4. 三市场 CLI 冒烟全通（每市场 ≥3 只）。
- **演示**：`docker compose run --rm cli fetch 0700.HK 0005.HK --last 4`。
- **备注**：I3 结束 = 原始需求（三市场 CLI 归档）达成，可提前实际使用；后续迭代属于一期补全。

### I4 可靠性硬化

> **状态：✅ 已完成（2026-09-21，v0.4.0）**。DoD 全项核对：① DESIGN §11.3/§11.4 中断场景测试齐备——下载中崩溃（I1 起有，I4 复核）、改名后崩溃（I1 起）、标记后崩溃（幂等重开测试）、文件被删（修复复用原 artifact ID）、文件损坏（检出 unavailable → 已校验临时文件覆盖修复）、ready 被外部篡改（拒绝覆盖）；② `--refresh` 落地：内容变化 → 新 artifact + 旧版本保留并按 ID 可读（checksum 一致，单测 + 离线集成测试）；内容未变 → 复用既有版本不重复归档（真网验证）；refresh 期间 manifest 保持 done、失败不降级已有有效内容；③ 批量长跑 30 只混合三市场：29 只归档（116 项）+ 1 只干净 `symbol_not_found`（00011 在 prefix.do 索引缺席，I4 实测留证 fixture），全程 0 失败，事后核验 118 artifacts **0 不一致 / 0 在途 intent / 0 unavailable / 0 downloading 残留 / 0 重复 source_id / 0 半文件**；④ 进程锁：归档根目录 flock 单写者（跨容器互斥已在主力环境实测，进程死亡自动释放），第二个写实例明确报 `store_in_use` 退出码 2（真容器并发验证）。实现注记：flock 跨容器互斥为 I4 实测结论（Windows Docker Desktop bind mount）；resolve 始终联网精确校验、不读缓存映射（symbol_map 仅记录，7 天过期字段写入），故无过期读路径。单测 258 个全绿（新增 14 个：恢复协议补测/锁/refresh/修复 + core 离线全链路集成）。

- **范围内**：archive_intents 完整恢复协议（DESIGN §11.3 各中断点的核对与收敛）；`--refresh` 与旧版本保留（多 artifact 并存、按 ID 读取）；丢失/损坏文件修复（unavailable → 恢复复用原 artifact）；symbol_map 过期与失效刷新；归档根目录进程锁（单实例写保护，为 I5 的 serve 复用）；日志完善（report/job 关联、候选排除理由）。
- **DoD**：
  1. DESIGN §11.3/§11.4 列出的每个中断场景有对应测试（下载后崩溃、改名后崩溃、标记后崩溃、文件被删、文件损坏）；
  2. refresh 后旧 artifact 仍可按 ID 读取且 checksum 一致，新内容生成新 artifact；
  3. 批量长跑（≥30 只代码混合三市场）无脏状态、无重复归档；
  4. 进程锁生效：第二个写实例明确报 `store_in_use`。
- **演示**：对 AAPL 执行 refresh 前后对比 artifact 版本列表。

### I5 HTTP 服务与持久化任务

> **状态：✅ 已完成（2026-09-21，v0.5.0）**。DoD 全项核对：① HTTP_API §8 验收场景通过——e2e（compose serve + curl）：提交 202/Location → 轮询至终态 → reports 过滤/游标分页 → 按 ID 下载（ETag=sha256、304、nosniff、attachment 含 filename* UTF-8、checksum 实测一致）；幂等重放未终态 202/终态 200、同键异参 409、无鉴权 401(WWW-Authenticate)/错凭据 403、队列上限 429+Retry-After、未知字段/非法参数/非法代码格式 422、非 JSON 415、无效游标 400、跨报告 artifact 404、文件损坏 409 file_not_available——均单测覆盖（306 个全绿，新增 48）；② CLI 与 HTTP 一致性：单测（同 request 选出相同 report_ids）+ e2e（任务 cached 项与 CLI 归档逐项一致）；③ 重启不丢任务、不重复执行：单测 + e2e 精准 kill（现场 running/attempt=1 → 恢复 attempt=2 → 3 证券 12 项无重复完成）；④ 慢下载期间健康检查/任务查询不被阻塞：单测（1.5s/文件慢响应下查询 <1s）。实现注记：serve 持归档根目录所有者锁，CLI 并发写明确报 store_in_use（实测）；本地无鉴权模式需回环监听或显式 RF_LOCAL_MODE=1（compose 端口仅发布 127.0.0.1）；jobs 三表经 schema v1→v2 增量迁移引入（v1 库自动升级，非重建）；OpenAPI/docs 令牌模式下受保护。

- **范围内**：`api.py / api_models.py / jobs.py`；FastAPI 路由、鉴权（回环 local / 令牌）、Problem Details、OpenAPI；JobService（幂等键 + request_hash、落库后 202、单执行器、队列上限、deadline/attempt、重启恢复 running→queued）；档案查询与按 ID 下载（路径边界、ETag/304、nosniff）；`serve` 命令；jobs 系列表经 schema_version 升级引入；Dockerfile server 目标与 compose serve 服务（端口仅发布到宿主 127.0.0.1）。
- **DoD**：
  1. [HTTP_API.md](HTTP_API.md) §8 全部验收场景通过；
  2. CLI 与 HTTP 对同一请求选出相同报告（一致性用例）；
  3. 任务入队后重启不丢任务、不重复执行已完成项；
  4. 慢下载期间任务查询与健康检查不被阻塞。
- **演示**：curl 提交任务 → 轮询 → reports 查询 → 按 ID 下载文件。

### I6 一期验收与发布

- **范围内**：三市场集成冒烟矩阵（样本清单见 DESIGN §16，含真实公司名与样本日期留档）；README、config.example、调用示例（curl / Python）；性能基线记录（NFR-8：环境、样本量、字节、首次/缓存耗时、重试次数）；文档-实现一致性核对；已知限制清单；tag `v1.0.0`。
- **DoD**：ARCHITECTURE §10 一期完成要求逐条核对通过，并在简报中留证。

## 4. 需求覆盖矩阵

主落地 = 该需求的验收在此迭代完成；"冒烟/验证"= 集成确认。

| 需求 | I0 | I1 | I2 | I3 | I4 | I5 | I6 |
|---|---|---|---|---|---|---|---|
| FR-1 识别 | 契约确认 | 主落地（US） | CN 用例 | HK 用例 | — | API 侧格式校验 | 冒烟 |
| FR-2 发现 | 分页方式确认 | 主落地（US files） | 主落地（CN） | 主落地（HK） | — | — | 冒烟 |
| FR-3 选择 | 用例冻结 | 框架 + US 规则 | CN 规则 | HK 规则 + QTR-HK 结论 | — | — | CLI/API 一致性 |
| FR-4 下载归档 | 限速初值 | 主落地 | — | — | — | — | 冒烟 |
| FR-5 幂等版本 | — | 最小闭环（去重/缓存/原子） | — | — | 完整（refresh/版本/修复） | — | 验证 |
| FR-6 隔离恢复 | — | 基础原子性 | — | — | 主落地 | 任务级恢复 | 验证 |
| FR-7 CLI | — | fetch/list 基础 | — | 完整参数/三市场 | --refresh | serve | 文档 |
| FR-8 HTTP | — | — | — | — | — | 主落地 | OpenAPI 核对 |
| FR-9 可观测 | — | 日志基础 | — | — | 关联 ID 完善 | request/job 关联 | — |
| NFR-1/2/3 | 限速实测 | 主落地 | — | — | 恢复相关预算 | 队列/请求体上限 | 基线记录 |
| NFR-4/5 安全合规 | — | UA 必配 | — | — | 进程锁 | 主落地 | README |
| NFR-6/7 环境与质量 | — | 3.11/依赖组/未知即 null | — | — | — | server 安装组 | 核对 |
| NFR-8 性能 | — | — | — | — | — | — | 基线报告 |

## 5. 可裁剪点与暂停点

- **I3 后暂停**：纯 CLI 使用者已获得原始需求全部能力（版本 0.3.0），后续迭代可暂停而不损失可用性。
- **I5 前的决策点**：若确认时暂无其他应用调用需求，I5/I6 可整体顺延（不改变一期定义，作为范围决策记录在本文件）。
- backlog 项（6-K 识别、北交所、QTR-HK 默认启用、定时增量、Excel 导出）不占用任何迭代容量，见 §7。

## 6. 变更控制

- 每迭代开始时范围冻结；迭代内发现的新需求一律进 §7 backlog，下个迭代再排。
- 每迭代结束更新本文档：状态列勾稽、DoD 结果留证（简报或测试记录链接）、估时偏差记录。
- 一期验收基线为 [REQUIREMENTS.md](REQUIREMENTS.md) v1.0；需求变更需同步更新需求规格版本号并注明决策原因。

## 7. Backlog（不排期，滚动维护）

| 项 | 来源 | 备注 |
|---|---|---|
| 6-K 业绩附件识别 | REVIEW §2 | 需申报目录/附件角色/报告期识别，单独验收 |
| 北交所支持 | 原 v1.0 P2 | column 契约待 I0 顺带记录 |
| QTR-HK 默认启用 | DESIGN §7 | 依赖 I3 验证结论 |
| 定时增量抓取（--since） | 原 v1.0 P1 | 调度由外部承担，本期仅记录 |
| 归档清单导出 Excel | 原 v1.0 P1 | — |
| 财报内容提取与基本面分析 | ANALYSIS_ROADMAP | 独立阶段，另立计划 |
