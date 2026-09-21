# 一期实现审查与修复任务书

审查日期：2026-09-21（Asia/Shanghai）。基线：`709bb72b798efbec115fa886b1aa51bdb7d06231`，版本 `1.0.0`。

## 结论与验证范围

**主流程可用，但当前版本不通过部署验收。** 发现 6 项 P1 问题，涉及归档安全、常驻服务的数据新鲜度、并发提交、报告真实性和失败状态；另有 1 项 P2 文档接口鉴权问题。先修复 P1，再按本文做少量回归。P2 在对外开放前修复；首次部署保留现有回环端口绑定。

本次仅审查和验证，未修改业务代码、未部署、未修改目标服务器上的文件或服务。环境预检查及后续部署步骤见 [DEPLOY.md](../DEPLOY.md)。

已执行：

- Docker 内定向测试：`test_core_pipeline.py`、`test_store.py`、`test_jobs.py`、`test_api.py`，**85 passed，17.82 秒**；2 条测试依赖弃用警告，不阻断。
- 小规模离线复现：复用现有 fixture、Fake Transport 和真实 SQLite/Store/JobService，在容器 `/tmp` 中独立建库；并发测试仅 2 个请求，用 barrier 固定交错顺序，不做压力测试。
- 本机 Docker 内真实抓取：`600519`、`0700.HK`、`AAPL` 各 `last_n=1`，**3/3 下载、内容校验、归档成功**。依次耗时 3.73 / 10.35 / 4.33 秒。使用真实 SEC 联系邮箱，但未在文档中记录邮箱。文件仅存于独立临时容器，未写入现有归档库。
- 真实来源 ID：CN `finalpage/2026-08-15/1225475868.PDF`；HK `listedco/listconews/sehk/2026/0825/2026082500557_c.pdf`；US `0000320193-26-000020/aapl-20260627.htm`。
- 三次真实抓取的证券状态均为 `partial`；全部含正常 `last_n` 截取提示，不能据此宣称完整状态契约通过，见 T6。

未做全量重复测试、长时间压测、多公司批量下载、二期财报内容解析测试。三市场真实抓取是在开发机容器执行，**不代表目标服务器出网已经验证**。

## T1 · P1 · 取得所有者锁之前已修改在途归档

位置：`reports_fetcher/store.py:240–252,341–403`；调用方 `reports_fetcher/api.py:503–509`、CLI 的 Store 初始化流程。

`Store.__init__()` 先执行 schema 初始化和 `_recover()`，调用方随后才执行 `acquire_owner_lock()`。当服务正在下载、用户又启动同归档目录的 CLI 或另一个服务时，第二个实例会先废弃第一个实例的注册意图，甚至清扫临时文件，最后才报 `store_in_use`。锁虽然拒绝了第二个写实例，却没有保护启动恢复。

**已复现**：第一个 Store 持锁并 `register_download()`；第二个 Store 使用同一目录构造，再申请锁：

```text
intent before = registered
intent after second Store construction = failed
second acquire_owner_lock = store_in_use
```

本次只验证状态被改写，没有对真实归档或下载文件做破坏性实验；临时文件删除风险来自 `_recover()` / `_sweep_tmp()` 的代码路径。

修复范围：Store 启动生命周期及 CLI/HTTP 入口。确保对共享库的 schema 变更、恢复、临时文件收敛均在取得同一所有者锁后执行；初始化失败释放自身资源。无需增加分布式锁或支持多写实例。

验收：一个真实持锁进程有 registered/staged 意图和临时文件时，第二进程只能返回 `store_in_use`，数据库状态和文件校验值均不变；所有者退出后，新实例仍能正确恢复崩溃遗留状态。保留正常单实例抓取、重启恢复测试。

## T2 · P1 · 常驻 HTTP 服务永久缓存 SEC 申报列表

位置：`reports_fetcher/adapters/us_edgar.py:118–135`；`reports_fetcher/core.py:80–84`；`reports_fetcher/api.py:488–491`。

服务复用同一个 FetchService 和 adapter；`_submissions_cache` 没有过期或按任务失效机制。首次查询某 CIK 后，新任务一直读旧列表。`refresh=True` 只重下载旧候选，不能发现后来发布的报告。一次性 CLI 正常不代表长期 HTTP 服务正常。

**已复现**：同一 Harness/服务中，第一任务使用真实 fixture 的较旧行；随后将来源响应切换成包含较新报告的完整 fixture，再提交不同幂等键且 `refresh=True` 的任务：

```text
first report = 0000320193-26-000013/aapl-20260328.htm
after refresh = 0000320193-26-000013/aapl-20260328.htm
submissions HTTP requests across both jobs = 1
```

修复范围：明确 metadata cache 生命周期，优先按任务/抓取调用复用同一次 resolve/list 的响应，下一任务重新获取；若采用 TTL，必须明确 refresh 绕过策略。保留共享限速器，不要为了刷新缓存重新创建独立限速预算。顺便明确 ticker 映射更新策略，不要求引入缓存服务。

验收：同一常驻实例的任务 A 查询旧列表，来源增加报告后任务 B 能发现新 source_id；refresh 不受旧 metadata cache 阻挡；同任务不产生不必要重复请求，本地相同原文仍 cached。

## T3 · P1 · 幂等检查、队列限额和插入并非原子操作

位置：`reports_fetcher/jobs.py:160–199`。

`with conn:` 没有在前置 SELECT 前取得写事务。两个 HTTP 请求可以同时通过幂等键和队列计数检查，再分别 INSERT。同键请求触发未转换的 SQLite 唯一约束异常，不满足重放协议；不同键请求可以突破队列上限。

**已复现**：两条线程各自使用真实 SQLite 连接，在读完 pending 数量后同步放行，仅调整执行时序：

```text
same client/key: one created, one IntegrityError (jobs.client_id, jobs.idempotency_key)
different keys, max_pending_jobs=1: both created, queued=2
```

修复范围：JobService 提交事务。可用覆盖读取与写入的短 `BEGIN IMMEDIATE` 事务，或与单实例架构一致且完整覆盖临界区的互斥策略；妥善处理 rollback、数据库忙和异常，不把唯一约束异常作为常规 API 结果。不要在数据库事务中联网。

验收：2 个同键同请求同时提交，得到同一 job_id、只有 1 条任务；同键不同请求得到成功 + 409；不同键竞争最后一个队列名额得到成功 + 429。串行重放与跨 client 命名空间行为不变。无需压测。

## T4 · P1 · SEC `/A` 未确认全文即覆盖原完整报告

位置：`reports_fetcher/adapters/us_edgar.py:304–305`；`reports_fetcher/selection.py:24,84–115`；现有错误预期 `tests/test_us_edgar.py:207–229`。

adapter 仅根据 form 后缀 `/A` 就设 `document_role=amendment_full`；selector 随后按公告时间选中修订版，替代同组原全文。SEC 申报元数据的这些字段没有证明修订主文档完整重发三张报表。部分修订会因此被当作完整财报，破坏一期“获取全文”承诺，也污染后续分析输入。

**静态证据**：该分类分支未读取任何正文或完整性证据；现有测试自己插入一条 `/A` 元数据，直接断言它是完整修订版并胜过原稿。该测试不能充当真实完整修订的依据。本项未额外访问或声称验证过某一真实 `/A` 正文。

设计依据：DESIGN §5、§8 明确要求 `/A` 不自动代表完整重发。

修复范围：对缺少完整性证据的修订保留 metadata 和关系，标 unknown/notice 并警告；同组有原全文时继续选原全文，明确修订未合并。只有可验证的完整重发才升级角色。不要扩展成二期语义分析引擎，也不要把所有修订静默丢弃。

验收：真实部分修订 fixture 不替代原全文；缺证据的修订不能独自计作完整财报；保留 source_form/is_amendment/revision_of。若支持完整重发，另用真实证据验证该分支。修正现有测试中的错误假设。

## T5 · P1 · HK 单年份标题被猜成固定日历期末

位置：`reports_fetcher/adapters/hk_hkexnews.py:290–301`。

`parse_hk_title_period()` 未接收公司财政年结信息，仅凭单年份标题就拼接 12-31 或 06-30，并声称 `explicit_title`。年份标签没有提供实际月日；几个日历年结公司的样本不能推广成所有港股的规则。现有跨年标签保护没有覆盖单年份命名的非日历财年报告。

**已复现**：

```text
2025 年報     -> 2025-12-31 / explicit_title / no warning
中期報告 2025 -> 2025-06-30 / explicit_title / no warning
```

依据：AGENTS.md 数据质量铁律、DESIGN §9 的 HK 规则均禁止按 12-31 / 财季惯例猜日期。错误日期会参与分组、排序、命名和将来的历史对比。

修复范围：明确期末日或可靠来源字段才设日期；仅有年份标签则 null/unknown + 警告，保留 source_id 和报告类型。若要使用已验证的主体财政日历，需证明其适用期间与来源，不能硬编码个别股票名单。同步澄清 DESIGN §7 的样本描述与通用规则，修正对应测试。

验收：明确日期仍正确；单年份无期末证据、跨年标签均 unknown；未知期不同 source_id 不被合并；真实样本支持的主体日期规则若保留，须可追溯。本期无需解析整份 PDF 来强求日期非空。

## T6 · P1 · 全部下载失败却返回 partial

位置：`reports_fetcher/core.py:358–362`；`reports_fetcher/jobs.py:346–361`；正常选择提示在 `reports_fetcher/selection.py:122–125,152–154`。

Core 只判断“是否全部未失败”，否则统一设 partial；JobService 又把任何 partial 证券当作有可用结果。这会让调用方误判可分析文件已准备好，也影响 CLI 退出码。HTTP_API §4 要求“没有可用报告且发生执行错误”返回 failed。

**已复现**：一个证券选 1 份报告，其文件响应 403：

```text
job.status = partial
symbol.status = partial
summary = {downloaded: 0, cached: 0, failed: 1}
report_ids = []
```

修复范围：按实际 downloaded/cached 数量与执行错误汇总状态，不通过 partial 字符串反推有文件。区分正常取最新 N 份、正常语言选择的说明信息与会影响完整性的质量警告。本次三市场成功下载仍出现 partial，其中 US 仅因正常 `last_n=1` 截取；这也应在同一任务修正。实际历史不足则按契约给出 `insufficient_history`。

验收只需小矩阵：全失败→failed；至少一个可用文件并有错误→partial；正常满足 N→succeeded；正常全无报告→succeeded/no_reports；不足 N 或报告期未知→partial。CLI 退出码与 HTTP 终态保持语义一致，失败明细不丢失。

## T7 · P2 · 自动文档与 OpenAPI 绕过应用令牌校验

位置：`reports_fetcher/api.py:109–115`。

FastAPI 全局 dependency 保护业务路由，没有保护框架自动注册的文档路由。token 模式未认证请求的实际结果：

```text
/api/v1/reports -> 401
/docs           -> 200
/openapi.json   -> 200
/redoc          -> 200
```

这没有证明归档原文泄露，但不满足 HTTP_API §7 的 docs/OpenAPI 受保护承诺。回环首次部署可暂缓，非回环开放前需修复。

修复范围：禁用默认文档路由后显式注册受保护路由，或使用一致的认证中间件；一并处理默认 `/redoc`。不要把令牌放入 URL。若令牌模式选择直接关闭交互文档，需要同步说明访问方式。

验收：token 模式无 token/错 token 不得读 schema 或文档；正确凭据按声明策略可访问；本地模式与业务路由、健康检查策略一致。无需引入账号系统。

## 可直接交给 agent 的提示词

建议按 A → B → C → D → E → F 的顺序集成，或用独立工作区避免共享文件冲突；不需要同时启动多个 agent。每个任务只补能证明问题已修好的少量测试。

### A：归档锁（T1）

```text
请修复 docs/PHASE1_REVIEW.md 的 T1。你负责 Store 启动/恢复生命周期、必要的 CLI/HTTP 调用点及对应少量测试。先读 AGENTS.md、DESIGN §11.4 和任务书，不重写架构。重点保证所有共享状态修改均发生在取得所有者锁之后，第二实例被拒绝时不能改数据库或临时文件。你不是唯一修改者，不撤销其他人的改动。用 Docker 做定向验证，包含真实第二进程锁竞争和一次合法恢复；不要做压力测试、不要部署。交付修改摘要、测试结果及涉及文件。
```

### B：常驻 US 来源刷新与修订完整性（T2、T4）

```text
请修复 docs/PHASE1_REVIEW.md 的 T2 和 T4。你负责 us_edgar adapter、必要的 FetchService 缓存生命周期/selection 联动、US fixture 与定向测试。常驻服务后续任务必须看得到新申报，refresh 必须刷新发现信息，保持统一限速和原文缓存；/A 没有正文或可靠来源证据时不能标为 amendment_full 或替换原全文，必须保留修订关系与警告。遵守 AGENTS.md，不编造真实来源证据，不扩展二期内容分析。你不是唯一修改者，不撤销其他人的改动。Docker 中只跑相关测试，不部署；交付缓存语义、修订规则和验证结果。
```

### C：港股期间真实性（T5）

```text
请修复 docs/PHASE1_REVIEW.md 的 T5。你负责 hk_hkexnews 日期解析、相关 fixture/测试及 DESIGN §7 的事实澄清。单年份标题不能推出 12-31/06-30；没有可靠期末证据时保持 null/unknown 和警告，保留报告类型及 source_id，明确日期正常解析。不要硬编码公司列表或实现 PDF 分析，不用公告日回填。你不是唯一修改者，不撤销其他人的改动。遵守 AGENTS.md，Docker 中定向验证，不部署；交付证据规则与测试结果。
```

### D：原子入队（T3）

```text
请修复 docs/PHASE1_REVIEW.md 的 T3。你负责 JobService.submit、必要的错误映射和小规模并发回归。把幂等读取、容量判断和插入纳入同一有效临界区/事务，保证同键同请求重放、同键异请求 409、最后一个名额竞争 429。只用 2 请求的确定性交错测试，不压测、不加外部队列，不把网络请求放进事务。你不是唯一修改者，不撤销其他人的改动；尤其保留 Store 锁修复。遵守 AGENTS.md，Docker 定向验证，不部署，给出原子性边界和结果。
```

### E：任务状态（T6）

```text
请修复 docs/PHASE1_REVIEW.md 的 T6。你负责 Core/JobService 状态汇总、selection 提示的必要调整、CLI 状态联动和小矩阵测试。以实际可用原文与错误决定 succeeded/partial/failed，不能把 partial 等同于有文件；正常满足最新 N 份和正常语言选择不应被伪装为质量缺口，历史不足要明确说明。严格按 HTTP_API §4 保留全空和未知期语义。你不是唯一修改者，不撤销其他人的改动，保留入队原子性修复。只做 Docker 定向测试，不重构数据模型、不部署；交付状态矩阵与结果。
```

### F：文档路由鉴权（T7）

```text
请修复 docs/PHASE1_REVIEW.md 的 T7。你负责 api.py 的 docs/openapi/redoc 认证策略、相关说明与少量 HTTP 测试。不要假定 FastAPI dependencies 自动保护框架文档路由；令牌模式应认证或明确关闭所有文档入口，本地模式仍按文档工作。令牌不得放 URL，不增加账号系统。你不是唯一修改者，不撤销其他人的改动。遵守 AGENTS.md，只做 Docker 定向验证，不部署；交付无凭据/错误凭据/有效凭据结果。
```

## 修复后复审与部署交接

新提交上执行受影响的定向测试及 T1–T6 最小回归；无需为每个任务重复跑全量测试或重复真实下载。集成结束后进行一次三市场各 1 份真实 HTTP 抓取、同幂等键重放、原文下载及服务重启后读取验证。修复必须基于新提交复审，不能沿用本次 85 项通过作为上线凭证。

之后按 DEPLOY.md 完成目标机部署与冒烟，回填实际提交、镜像 ID、命令、结果和访问方式，再将“一期完成”的验收记录更新为修复后的事实。
