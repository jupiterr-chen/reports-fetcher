# 财报归档目录结构与知识库对接指南

> 位置：`/vol2/1000/10.Develop/reports-fetcher/reports`（SMB：`\\fnos\10.Develop\reports-fetcher\reports`）
> 生成者：reports-fetcher v1.0.5+；本文同步镜像于仓库 `docs/ARCHIVE_LAYOUT.md`（以仓库版为准）
> 面向读者：准备摄取本目录的**知识库/RAG 工具**及其集成开发者

## 0. 三十条秒版

1. **别只扫文件名——先读索引库 `archive.sqlite3`**，它是唯一权威元数据源；文件名只是冗余的可读编码。
2. 每份原文 = 一个 `artifact`（内容版本）；同一报告刷新后内容变化会**新增 artifact 并保留旧文件**，目录里文件数 ≥ 报告数是正常的。
3. `report_period`（财期期末）≠ `filing_date`（公告日），**检索/分期请用前者**。
4. 少量文件名以 `unknown__` 开头：该报告期不可知（数据质量铁律，不猜测），知识库侧请按"期未知"处理而非解析失败。
5. 本目录**只读**。服务在持续写入（含 SQLite WAL），SMB 远程直接打开 SQLite 有损坏风险——见 §6 的两种安全读法。

## 1. 目录布局

```text
reports/
├── archive.sqlite3            # 权威索引（SQLite WAL 模式；另有 -wal/-shm 伴生文件）
├── .tmp/                      # 服务下载中转（对接工具请忽略；.lock 为单写者锁）
├── CN/<6位代码>/              # A股（如 CN/600519）
├── HK/<5位代码>/              # 港股（补零5位，如 HK/01810、HK/00700）
└── US/<TICKER>/               # 美股（如 US/AAPL；类股含连字符如 BRK-B）
```

一层扁平布局（当前配置）；同公司文件全部平铺在其代码目录下，无更深层级。

## 2. 文件名语法（可解析，但推荐以库为准）

```text
{report_period|unknown}__{doc_type}__{title}__{report_id}__{artifact_id}.{ext}
  │                          │          │        │            └ 20位hex，内容版本ID
  │                          │          │        └ 20位hex，报告持久ID（重试不变）
  │                          │          └ 来源标题（已清理非法字符，仅供人读）
  │                          └ 基础类型（见 §3 枚举）
  └ 财期期末 YYYY-MM-DD；不可知时为字面量 unknown
```

- 分隔符固定为**双下划线 `__`**；title 内部不会出现 `__`（已清理）。
- 扩展名即内容类型：`.pdf`（CN/HK）或 `.html`（US，Inline XBRL 文档，见 §5）。
- 同一 `report_id` 出现多个文件 = 同一报告的多个内容版本（refresh 保留历史）；**当前版**以库中 `current_artifact_id` 为准，不要按文件名排序猜。
- 实例：
  - `2026-06-30__H1__贵州茅台2026年半年度报告__916deaafc98bfbaf5cfc__95c176a27a199ebe87b8.pdf`
  - `2025-12-31__ANNUAL__2025年度報告__ffd733761633f464…__….pdf`
  - `2026-06-27__10-Q__aapl-20260627.htm__7b726888c37d9415aafd__133984e518dbae8fa8e1.html`
  - `unknown__INTERIM__2025/26 中期報告__….pdf`（非日历年结公司，期不可知）

## 3. 市场与 doc_type 枚举

| market | doc_type | 含义 | 文件格式 | 报告期来源（period_source） |
|---|---|---|---|---|
| CN | `Q1` / `H1` / `Q3` / `FY` | 一季报/半年报/三季报/年报 | PDF | `explicit_title`（标题年份+类型） |
| HK | `ANNUAL` / `INTERIM` | 年报/中期报告 | PDF | `document`（PDF 原文提取）＞ `announcement_title`（業績公告标题兜底）＞ `unknown` |
| HK | `QTR-HK` | 季度业绩公告（自愿披露） | PDF | `explicit_title`（标题明确期末日） |
| US | `10-Q` / `10-K` / `20-F` | 季报/年报/外国私人发行人年报 | **HTML**（Inline XBRL） | `source_field`（SEC reportDate，权威） |

`language`：CN/HK 多为 `zh`（存在英文版时中文优先）；US 为 `en`。

## 4. 权威索引：`archive.sqlite3`

两张核心表（另有 jobs 任务表、archive_intents 恢复日志，对接无需关心）：

**manifest —— 一行 = 一份逻辑报告（报告身份）**

| 字段 | 说明 |
|---|---|
| `report_id` | 20 hex，持久 ID；由 (market, symbol, source_id) 派生，**重试/重跑不变** |
| `market` / `symbol` | 归一化代码（HK 5 位补零） |
| `source_id` | 来源去重键：CN=巨潮 adjunctUrl 路径；HK=披露易文件路径；US=`accessionNumber/primaryDocument` |
| `source_url` | 原文原始 URL（可追溯） |
| `title` / `doc_type` / `source_form` | 标题；基础类型；来源原始类型（US 如 `10-K/A`） |
| `filing_date` | 公告日（来源时区语义，日期粒度） |
| `report_period` | **财期期末**（YYYY-MM-DD）或 NULL（未知） |
| `period_source` | 报告期信任级别：`source_field` ＞ `explicit_title` ＞ `document` 同级…详见下 |
| `language` / `document_role` / `is_amendment` / `revision_of` | 语言；`full_report` 等；是否修订；修订指向的基础报告 source_id |
| `source_metadata_json` | 来源附加信息（HK 子类别/发布时间、US accession 等；HK 证据回填时含 `period_evidence` 指明报告期出处公告） |
| `status` | `done`（有可用文件）/ discovered / failed |
| `current_artifact_id` | **当前生效内容版本**（指向 artifacts） |

**artifacts —— 一行 = 一次内容版本（文件实体）**

| 字段 | 说明 |
|---|---|
| `artifact_id` | 20 hex，由 (report_id, 内容 sha256) 派生：**同内容重抓 ID 不变** |
| `report_id` | 所属报告（多版本对同一 report_id 共存，UNIQUE(report_id, sha256)） |
| `sha256` / `bytes` / `media_type` | 内容校验和 / 字节数 / `application/pdf` 或 `text/html` |
| `local_path` | 容器内路径 `/app/reports/…`（对接时按前缀替换为实际根目录） |
| `fetched_at` / `state` | 抓取时间（UTC ISO8601）；`ready` 为可用 |
| `source_url` / `final_url` | 请求 URL / 经重定向后的最终 URL |

`period_source` 信任级别（供知识库标注数据置信度）：
`source_field`（US 来源权威字段）≈ `explicit_title`（标题写明完整日期）≈ `document`（PDF 原文写明）≈ `announcement_title`（同期间業績公告标题写明，证据见 source_metadata.period_evidence）＞ `unknown`（**报告期未知，值为 NULL——请保留为"未知"，不要用 filing_date 或 12-31 补齐**；港股非日历年结公司是真实场景）。

## 5. 内容格式提醒（影响解析器选择）

- **US 的 `.html` 是 SEC Inline XBRL 文档**：以 XML 声明开头（`<?xml …?>` 后跟 `<html … xmlns="http://www.w3.org/1999/xhtml">`），正文含 XBRL 事实标签（`ix:nonFraction` 等）；普通 HTML 解析器可提取文本，结构化取数需 XBRL 感知解析。图片/CSS 等外链资源未本地化。
- **CN/HK 的 `.pdf`** 为源站原文；部分发行人（如小米）内嵌字体缺 ToUnicode 映射，**文本层可能是乱码**，需要 OCR 才能提取文本——对接知识库时建议对 PDF 文本做质量探测（可提取字符率），低质量样本走 OCR 通道。
- 每个文件的 `sha256` 在库中已有，摄取完成后可直接比对去重。

## 6. 对接方式（二选一或组合）

### 方式 A：NAS 本机 Docker 直读（推荐，增量同步友好）

知识库容器与归档同机时，直接把该目录只读挂载：

```yaml
volumes:
  - /vol2/1000/10.Develop/reports-fetcher/reports:/archive:ro
```

SQLite WAL 模式支持"单写多读"：本机文件系统上以普通只读连接打开是安全的（**不要**加 `immutable=1`，服务在写）：

```sql
-- 例：拉取自上次同步以来的新增/变更
SELECT m.report_id, m.market, m.symbol, m.doc_type, m.report_period,
       m.filing_date, m.language, a.artifact_id, a.sha256, a.bytes,
       a.media_type, a.fetched_at,
       '/archive' || substr(a.local_path, length('/app/reports')+1) AS local_path
FROM manifest m JOIN artifacts a ON a.artifact_id = m.current_artifact_id
WHERE m.status = 'done' AND a.state = 'ready'
  AND a.fetched_at > :last_sync_utc      -- 增量游标
ORDER BY a.fetched_at;
```

多版本策略二选一：(a) 只摄取 `current_artifact_id` 指向的当前版（推荐）；(b) 全量摄取所有 `state='ready'` 的 artifacts（历史证据完整，量更大）。

### 方式 B：局域网经 SMB（人读/拷贝为主）

路径 `\\fnos\10.Develop\reports-fetcher\reports`（需 NAS 用户凭据）。**SMB 下只浏览/复制文件，不要远程直接打开 `archive.sqlite3`**——网络文件锁不可靠且与写入方并发，有损坏风险。程序化取文件优先走服务的 HTTP API（内网隧道后）：

```
GET /api/v1/reports?market=HK&symbol=01810&limit=50   # 元数据+分页游标（只读，不触发抓取）
GET /api/v1/reports/{report_id}                        # 报告详情+全部版本
GET /api/v1/reports/{report_id}/file                   # 下载当前文件（ETag=sha256，支持 304）
GET /api/v1/reports/{report_id}/file?artifact_id=…     # 指定历史版本
```

HTTP 入口：NAS 本机 `http://127.0.0.1:8000`；其他机器经 SSH 隧道 `ssh -N -L 18000:127.0.0.1:8000 chen@192.168.1.150` 后访问 `http://127.0.0.1:18000`。OpenAPI：`/openapi.json`。

## 7. 不变量与禁令

**可依赖的保证**：
- `report_id` / `artifact_id` 稳定不变；已归档文件**内容不可变**（sha256 即身份），不会被覆盖或修改。
- 文件只增不删（refresh 产生新文件，旧文件保留）。
- `UNIQUE(market, symbol, source_id)`、`UNIQUE(report_id, sha256)` 库级去重。

**禁止**（会破坏归档）：
- 通过 SMB 或任何非服务进程**写入/删除/重命名**本目录内容，尤其 `archive.sqlite3*` 三个文件；
- 修改文件权限/属主（服务依赖当前权限模型）；
- 把 `.tmp/`、`.lock`、`archive.sqlite3-wal/-shm` 当作数据摄取。

若知识库工具会递归摄取整个目录，建议将 `README.md`、`.tmp/`、`archive.sqlite3*`、`.lock` 加入其排除清单，只摄取 `CN/ HK/ US/` 下的 `.pdf/.html`。

## 8. 对接清单（建议顺序）

1. 挂载/隧道确定访问方式（§6）。
2. 用 manifest 统计现状（market/doc_type/period 分布），规划语料规模。
3. 增量循环：按 `artifacts.fetched_at`（或 manifest.`updated_at`）轮询新文件 → 下载 → `sha256` 比对入库 → 文本抽取（PDF 质量探测 + US XBRL）。
4. 元数据透传到知识库：至少 `report_id / artifact_id / market / symbol / doc_type / report_period / period_source / filing_date / language`——后续分析要靠它们做时间轴与置信度过滤（`period_source=unknown` 的条目慎入同比计算）。
5. 需要抓新标的/新报告：不要绕过服务写目录，调用 `POST /api/v1/fetch-jobs` 让服务自己归档。
