# reports-fetcher 详细设计

| 项目 | 内容 |
|---|---|
| 版本 | v1.4（v1.3 + RF-HK-QTR-DEFAULT-001：HK 默认季度类型与混合选择两阶段） |
| 日期 | 2026-09-20（v1.3）；2026-09-23（v1.4） |
| 状态 | 实现中；三市场来源契约已于 I0 实测验证（样本见 tests/fixtures/，结论见 docs/SOURCE_VERIFICATION.md） |
| 上游 | [ARCHITECTURE.md](./ARCHITECTURE.md)、[REQUIREMENTS.md](./REQUIREMENTS.md) |
| 配套 | [ITERATION_PLAN.md](./ITERATION_PLAN.md)、[HTTP_API.md](./HTTP_API.md)、[REVIEW.md](./REVIEW.md)、[ANALYSIS_ROADMAP.md](./ANALYSIS_ROADMAP.md) |

本文修订原 v1.0 的矛盾及实现缺口，评审依据见 REVIEW.md。§6/§7/§8 的来源契约已于 2026-09-20 在 Docker 容器内实测验证并回填；仍标注”待实测”的仅剩实现期确认项（北交所 column、CN Cookie 是否强制等）。后续财报内容分析不在本文一期实现范围内。

## 1. 模块与依赖方向

```text
reports-fetcher/
  ARCHITECTURE.md / DESIGN.md / HTTP_API.md
  REVIEW.md / ANALYSIS_ROADMAP.md
  pyproject.toml / config.example.toml / README.md   # 实施阶段产出
  reports_fetcher/
    __init__.py / __main__.py
    cli.py / api.py / api_models.py / jobs.py
    config.py / models.py / symbol.py / period.py
    core.py / downloader.py / store.py
    adapters/__init__.py / base.py
    adapters/cn_cninfo.py / hk_hkexnews.py / us_edgar.py
  tests/
    fixtures/                                      # 实测来源响应及异常样本
    test_symbol.py / test_selection.py / test_period.py
    test_downloader.py / test_store.py / test_jobs.py / test_api.py
    adapters/
```

依赖方向：`CLI → core`；`API → jobs → core`；`core → adapters / downloader / store`；下层只依赖通用模型与配置，不 import API/CLI。API 档案读取直接通过 Store 的只读接口；所有抓取用例共用 core。Python 基线 3.11+，核心 requests，服务安装组 FastAPI/Pydantic/Uvicorn，测试工具单列，实施时固定可复现版本。

## 2. 证券识别与规范化

| 输入 | 输出与约束 |
|---|---|
| `600519`、`sh600519`、`600519.SH`、`600519.SS` | CN / 600519，显式沪市信息保留给 resolve |
| `000001.SZ`、`sz000001` | CN / 000001；不能误写成固定示例 300750 |
| `0700.HK`、`0700:HK`、`700`、`00700` | HK / 00700，一期统一补零至 5 位；后缀仍需验证合法长度 |
| `AAPL`、`aapl` | US / AAPL，按官方 ticker 映射确认 |
| US 类股符号 | 保留来源允许的连字符等；常见点号别名仅在精确映射明确时转换，不能无条件替换 |
| `bj...` / `.BJ` 或来源确认为北交所 | P0 返回 unsupported_market_segment；不落到沪深查询 |
| 未知后缀、路径字符、超长或空代码 | SymbolError / invalid_symbol |

纯数字 6 位先视为 CN 候选；前缀只能辅助识别，不能代表有效 A 股或具体交易所。联网 resolve 校验市场、证券类型与代码精确匹配，不接受搜索结果第一条模糊命中。停牌/退市与不存在不同，来源可检索时仍允许历史报告。

批量规范化后按 `(market, symbol)` 去重，保留输入顺序与所有原始别名。API 提交时做格式和已知能力检查；需要联网才能判断的 unsupported/not_found 在任务结果中报告。

## 3. 统一数据模型

以下为字段契约，数据库与 API 可采用不同结构但必须无损映射。可未知字段统一用 null，不混用空字符串、0 或公告日期作为占位。

| 对象 | 必要字段与语义 |
|---|---|
| ResolvedSymbol | market、symbol、raw_inputs、display_name、exchange（可空）、source_issuer_id（orgId/stockId/CIK）、issuer_id（带来源命名空间，非全球公司统一 ID） |
| Report | market、symbol、source_id、source_url、title、doc_type（基础类型）、source_form（原类型，如 10-K/A）、filing_date、report_period（可空）、period_source、language、document_role、is_amendment、revision_of（可空）、source_issuer_id、source_metadata |
| DiscoveryResult | reports、requested_count、selected_count、searched_from/to、exhausted、truncated、warnings |
| DownloadedFile | temp_path、sha256、bytes、media_type、final_url；仅代表已校验临时文件，不代表归档成功 |
| FetchResult | market、symbol、status、items（report_id/source_id/outcome/error）、coverage、warnings、error |

`period_source = source_field | explicit_title | document | unknown`，不再使用存在多种拼写的 inferred 布尔字段。一期不实现按法定披露窗口反推期末。`document` 仅用于 HK 年报/中期报告：报告期在来源元数据中未知时，从**已校验归档的本地 PDF 原文**提取明确期末日（中文"截至…止年度/六個月"、英文"for the … ended / as at <date>"），校验为真实日历日期；提取失败或文本歧义（多个矛盾日期）非致命，保持 null + unknown。`document` 只在报告期仍未知时写入，绝不覆盖既有的可信非 unknown 报告期。`filing_date` 仅可作为文件名回退，绝不作 report_period 来源。公告时间若有原始时刻/时区则保存在 source_metadata；运行时间统一 UTC。`document_role = full_report | amendment_full | notice | summary | unknown`，不明确的完整性不能伪装 full_report。

report_id 在候选报告首次持久化时分配并绑定来源去重键；相同来源重试返回原 ID。artifact_id 标识一次已存内容版本；原文改变而 source_id 不变时产生新 artifact。同份报告的多证券别名暂仍按市场/代码建档，未来通过 issuer_id 映射汇合，不宣称已解决跨市场实体合并。

## 4. 异常与结果

| 类别 | 稳定 code 示例 | 行为 |
|---|---|---|
| 输入/能力 | invalid_symbol、unsupported_market_segment、unsupported_form | 请求格式错误可提前拒绝；单证券联网发现的问题不阻断其他证券 |
| 解析证券 | symbol_not_found、ambiguous_symbol | 不把搜索空白或网络错误合并成代码不存在 |
| 来源 | source_unavailable、source_rate_limited、source_contract_changed | 有界重试，保留来源错误；不能返回空列表伪装正常 |
| 解析报告 | unknown_period、unknown_document_role | 能保留的候选记录附警告，无法证明为全文则不计作成功全文 |
| 下载/存储 | download_invalid、download_too_large、store_unavailable、file_not_available | 保存任务错误，已有有效版本不得降级为失败 |

没有符合条件的报告是 `no_reports`，不是 DataSourceError。多证券结果及任务状态聚合按 HTTP_API.md §4；CLI 对有可用结果且存在警告/缺口返回退出码 1，全无结果且有执行错误返回 2，纯空查询正常结束返回 0 并明确说明。

## 5. 适配器及选择契约

```python
class BaseMarketAdapter:
    def resolve(self, raw_symbol: str) -> ResolvedSymbol: ...
    def list_reports(self, symbol: ResolvedSymbol,
                     query: ReportQuery) -> DiscoveryResult: ...
```

`ReportQuery` 至少含 last_n、forms、语言偏好及有界发现预算。每个适配器负责从源站获取足够候选并规范化，统一选择函数负责分组、版本选择、排序和截断。forms 在筛选阶段实际生效，不能只存在于方法签名。

选择流程：来源类型筛选 → 排除已明确的摘要/非财报 → 识别完整修订版 → 逻辑报告分组 → 按语言选择 → 按公告时间选择最新可确认完整版本 → 排序取 N。语言优先级 CN/HK 中文优先、英文回退；不因为英文标题就丢弃唯一全文。

分组键为 `(market, symbol, report_period, doc_type, statement_scope_if_known)`，不同类型不可只因期末相同而合并；已确认的跨语言修订关联优先处理。日期未知时每个 source_id 独立，不把空日期条目合成一份。未知期候选置于已知期之后，结果必须有质量警告。

默认只下载每组一个所选完整版本，保留发现到的版本关系；历史已归档版本不会删除。仅有修订通知时仍可选择原全文，但必须警告未合并修订，后续分析不能假定已获得最新有效数据。含 /A 不自动代表完整重发，需来源/文档特征确认，否则标为 notice/unknown。

发现过程按公告日分页时，不能在看到 N 条后就假定已取得最新 N 个报告期。必须完成声明检索窗口的分页，再排序；窗口不足 N 时有界扩窗。默认最多回溯 10 年、每证券 100 次发现请求（均可配置，包含检索/验证请求）；预算到限即 truncated，不宣称完整。未来按日期区间补取可单独演进。

### 5.1 混合选择两阶段数据流（v1.4，RF-HK-QTR-DEFAULT-001）

HK 默认类型为 `ANNUAL/INTERIM/QTR-HK` 后，选择面临的数据现实是：QTR-HK 标题含明确期末日（发现阶段即有可信 `report_period`），而年报/中报标题常只有年份（发现阶段 `null + unknown`）。若直接混合排序，"已知期在前、未知期置后"的规则会让 `last_n` 被季度公告占满（生产已复现，冷库/热库皆然——库内富化的报告期发生在选择之后）。因此在 `select_reports()` **之前**插入两阶段准备（`core.FetchService._prepare_selection_periods`）：

1. **阶段 A（所有市场）**：按 `(market, symbol, source_id)` 从 Store **只读回填**库内可信报告期（`report_period` 非空且 `period_source != unknown`；未知不覆盖）。热库由此与冷库获得一致的选择输入；`list` 预览只做这一步（不下载原文，冷库预览的混合排序仍偏向已知期，属已文档化的预览边界）。
2. **阶段 B（仅 HK ANNUAL/INTERIM 仍未知者）**：
   - 已归档候选（manifest done + 当前 artifact ready）：从**本地已校验 PDF** 提取明确期末日（不触网），`enrich_report_period` 仅补未知；
   - 未归档候选：按公告时间倒序**每类型预选至多 `last_n` 个**（公告时间只限定工作量，绝不写成报告期、不改变 `period_source`），经传输层下载为已校验临时文件后提取期末日；提取成功即写回候选（`period_source=document`）并 upsert 落库——**未入选候选同样落库**（discovered 行带期），下次运行阶段 A 直接回填，不再重复预取。
3. 预取临时文件：入选则由 `_run_download` **复用提交**（不重复下载）；未入选/缓存短路由 fetch 收尾统一清理；崩溃遗留的 `.tmp/*.part` 由 Store 恢复清扫收敛。预取不登记 archive_intents，Store 仍是唯一归档提交方。
4. 预取语义可追溯：预取数量/失败/时限跳过以说明性 notice 记入 `coverage.notices`，不计入 `downloaded/cached/failed`，预取失败不阻止其他类型（候选保留未知期参与选择）。**但判期最终失败不得静默漏掉近期完整报告**：预取后报告期仍未知的 ANNUAL/INTERIM 候选在统一选择中被置后，若已知期候选已满足 `last_n` 即被排除——此时以公告时间作保守代理，凡失败候选公告时间不早于最旧入选报告者，视为**可能遗漏最新完整报告**的质量缺口，记入 `warnings` 并使该证券 `partial`（其他可用类型继续返回，不产生 failed item 伪装）；更早的失败候选仍仅记 notice、不降级。
5. 边界：非日历年结公司（如 0016 六月年结）由原文提取真实期末（能提取则 `document`，否则 `null+unknown` 置后）；下载有界性为每类型 ≤`last_n`；选择函数本身不变（不修改 `_group_order_key()`，不用 `filing_date` 冒充 `report_period`）。

验收基线：0700.HK 省略 forms、`last_n=4`，冷库与缓存重跑都返回 `2026-06-30 INTERIM、2026-03-31 QTR-HK、2025-12-31 ANNUAL、2025-09-30 QTR-HK`（report_id 顺序一致）。

## 6. CN：巨潮适配器（I0 已验证契约，2026-09-20）

| 步骤 | 已验证请求与关键字段 |
|---|---|
| resolve | POST `http://www.cninfo.com.cn/new/information/topSearch/query`，表单 keyWord/maxNum；返回数组按 code 精确匹配读取 orgId（实测沪市 `gssh0600519`、深市 `gssz0000001`——不推导、直接使用返回值）；不存在的代码返回 200 + `[]`（见 fixture `topsearch_nomatch`） |
| list | POST `http://www.cninfo.com.cn/new/hisAnnouncement/query`，表单 stock=`code,orgId`、pageNum/pageSize、column、category、seDate、tabName=fulltext、isHLtitle=false |
| 文件 | `http://static.cninfo.com.cn/` + adjunctUrl（实测 magic `%PDF-`）；source_id 为规范化 adjunctUrl |

已验证行为与字段：

- **column**：对沪市股票 `szse` 与 `sse` 返回完全一致（同字节数），统一使用 `szse`；北交所 column 值留待实现期确认；
- **Cookie/请求头**：先 GET 首页预热（取得 JSESSIONID、SF_cookie_4）后接口畅通；裸 curl 缺 UA/Referer 会 403。是否强制 Cookie 未消融，保守保留预热一次；
- 分页：`totalAnnouncement` / `hasMore` 驱动 pageNum 递增，取满或窗口耗尽即止；
- 响应行可用字段（实测）：`announcementId`（稳定公告 ID）、`associateAnnouncement`（关联公告，修订关联线索）、`shortTitle`、`adjunctType`、`adjunctSize`；`announcementTypeName` 实测为 null，不可依赖；
- **标题形态**（isHLtitle=false 时为”公司名+报告名”、无冒号分隔）：`贵州茅台2026年半年度报告`、`…报告摘要`、`…（英文版）`；**季度标题两种变体并存**——600519 用”第一季度报告”、000001 用”一季度报告”，解析须同时覆盖；
- `announcementTime` 为毫秒时间戳 → Asia/Shanghai 公告日期，原值保留在 source_metadata。

同一报告期的原稿、更新后全文与更正通知分别分类（document_role），不使用”命中更正/修订就一律排除”的正则。

## 7. HK：披露易适配器（I0 已验证契约，2026-09-20）

> **重大契约变化**：旧 `titleSearcherJson.do` 端点已下线（404，fixture 留证）。现行契约为 `titlesearch.xhtml` **GET 深链 + 服务端渲染 HTML**（v1.1 设计的”双重 JSON/数组形态”分析对象已失效）。

| 步骤 | 已验证请求与关键字段 |
|---|---|
| resolve | GET `https://www1.hkexnews.hk/search/prefix.do?callback=callback&lang=ZH&type=A&name=00700&market=SEHK`；JSONP 剥壳（只剥已知包装，绝不 eval）；返回**五位 code**（如 `00016`）与 `stockId`，按 code 精确匹配；不存在代码返回空 `stockInfo`（fixture 留证）。**I4 实测补充（2026-09-21）**：prefix.do 为去零前缀式搜索——返回非空但无精确命中也按 symbol_not_found 处理（00011 在 type=A 索引缺席为真实样本，fixture `prefix_search_variants.json` 留证），不得取前缀近似命中 |
| list | GET `https://www1.hkexnews.hk/search/titlesearch.xhtml`，参数：`lang=ZH&category=0&market=SEHK&searchType=1&documentType=-1&t1code=40000&t2Gcode=-2&t2code=-2&stockId=<内部ID>&title=<可空>&from=YYYYMMDD&to=YYYYMMDD`。**有效日期参数是 from/to；fromDate/toDate 会被忽略**（参数矩阵实测） |
| 文件 | `file_link` 形如 `/listedco/listconews/sehk/2026/0825/2026082500557_c.pdf`，与官方基址 URL join（`_c`=中文版）；source_id 为规范化文件路径 |

**响应解析（服务端渲染 HTML，fixture 含原始页）**：

- 总数标记：`共有 N 紀錄`；
- 结果行 `<tr>` 字段：`發放時間 DD/MM/YYYY HH:MM`（Asia/Hong_Kong）、股份代號（**可能含人民币柜台第二代码如 `00700 80700`，只取主代码**）、股份簡稱、headline 文本（`財務報表/環境、社會及管治資料 - [子類別]`）、文档链接（标题 + PDF href + 附件大小）；
- **子類別（方括号文本，经 HTML 实体反转义后）是 document_role / doc_type 的权威来源**，优于标题正则：
  - `[年報]` → ANNUAL；`[中期/半年度報告]` → INTERIM；`[環境、社會及管治資料/報告]` → 排除（非财报正文，但与财报同在 t1=40000，不可只按类别判定）；
  - t1=10000（公告及通告）下：`[季度業績]` / `[中期業績]` / `[末期業績/…]` → 业绩公告（QTR-HK 相关）；
- HTML 实体（`&#x2f;` 等）必须先反转义再匹配。

**已验证行为**：

- 单页返回全部结果：10 年窗口 24 条一次返回；站点显示上限 1000 条（页面配置 `ViewMoreRecords`），超限场景按年切窗，不做 load-more 模拟；
- **免 Cookie/免预热可直接深链检索**（全新会话消融验证通过）；偶发 TLS 握手层重置（网络抖动），显式重试即可恢复——重试属传输层必选项；
- **QTR-HK 可行且默认启用**：`t1=10000` + `title=業績` 可召回季度业绩公告，标题含中文数字明确期末日（”截至二零二六年三月三十一日止三個月業績公佈”）→ v1.0.3 起纳入 HK 默认类型（REQUIREMENTS v1.1 FR-2；无季度披露的发行人正常跳过，混合选择两阶段见 §5.1）；
- 标题形态（I3 实测 2026-09-21；**PHASE1_REVIEW T5 修正规则**）：样本中观察到的形态有 `中期報告 2026`、`2025 年報`、**匯豐年份在前的 `2026年中期業績報告(附僱員股份計劃)`、`2016年報及賬目(…)`**、**非日历年结公司（0016，六月年结）的跨年标签 `2024/25 年報`、`2025/26 中期報告`**。**通用规则：样本中的日历年结公司形态不能推广为期末推导依据——单年标签与跨年标签都只有年份语义、无期末日证据，一律 `report_period=null + period_source=unknown + 警告`（不按 12-31/06-30 拼接）；仅"明确期末日"（截至…止，如业绩公告标题）设期（explicit_title）**。跨年判定以任何 `YYYY/NN` 斜杠年份为准；JSONP 包装实际形态为 `callback( … );`（尾部带分号）；
- 子类别映射（**修复期回归补充，2026-09-21**）：除精确键（`[年報]`/`[中期/半年度報告]`/`[季度業績]`、纯 ESG 排除）外，旧年份存在**合并子类别** `[年報 / 環境、社會及管治資料/報告]`（騰訊 2017–2021 实测）——按包含关系判定：含"年報"→ANNUAL、含"中期報告/半年度報告"→INTERIM；纯 ESG 子类别仍精确排除；
- PDF 实测 magic `%PDF-1.7`。

**残留实现期确认项**：更正/补充公告的子类别形态与修订关联表达（探测样本未含），实现时以 `associateAnnouncement` 同思路观察源字段，缺依据则按 notice 处理并警告。

## 8. US：SEC EDGAR（I0 已验证契约，2026-09-20）

1. `https://www.sec.gov/files/company_tickers.json`（实测 10,438 条）构建 ticker→CIK 映射：**类股为连字符格式（BRK-A/BRK-B/BF-B）；GOOG 与 GOOGL 为不同 ticker、同一 CIK**——按 ticker 精确匹配即可，不做点号转换。CIK 保留补零形式作身份。
2. `https://data.sec.gov/submissions/CIK{cik:010d}.json`：recent 并行数组（实测 AAPL 1001 行、17 键，含 isXBRL/primaryDocument 等）；zip 前必须校验数组等长。**不存在的 CIK 返回 404 + XML 错误体**（data.sec.gov 为对象存储支撑，fixture 留证），按 ResolveError 处理。
3. `filings.files` 实测存在（`[{name, filingCount, filingFrom, filingTo}]`，Apple 历史段 1247 行、1994–2015）；历史 JSON **顶层即并行数组、无 filings/recent 包裹**；含 `primaryDocument` 字段但**老申报（1990 年代）为空串**——按需读历史时空 primaryDocument 以 `Archives/edgar/data/{cik}/{acc去连字符}/index.json` 兜底或跳过并警告。默认场景（最新 N 份）recent 必然覆盖，不触历史文件。
4. 原文路径：`https://www.sec.gov/Archives/edgar/data/{cik}/{accession去连字符}/{primaryDocument}`；source_id=`accessionNumber/primaryDocument`。**reportDate 为权威期末，不可假设日历季度**（实测 Apple 财年 9 月止：10-K reportDate=2025-09-27）。**文件下载请求同样必须携带 UA**（I1 实测：无 UA 的 Archives 请求 403）；**现代主文档为 Inline XBRL，以 XML 声明 + 供应商注释（如 Workiva）开头、其后才是 `<html>`**（I1 实测 2026-09-20），内容类型嗅探须在开头窗口内探测而非仅看首标签。
5. 默认支持基础类型 10-Q/10-K/20-F；修订申报（10-K/A、10-Q/A 实测存在于 recent）归入基础类型并保留 source_form/is_amendment，不能把 /A 直接覆盖全文。

reportDate 非空且合法时作为期末；缺失则 null。filingDate 仍为来源提交日期，不转为服务器本地日期，也不替代 reportDate。10-K 不作为独立 Q4 指标数据使用。

6-K 不是稳定的季度报告入口，原稿用主文档文件名查 results/quarterly 不足以识别财务附件。P0 明确不接受 6-K 请求；后续需要申报文件目录、附件角色、报告期与财务内容识别后单独验收。不再承诺 ADR 自动取到 20-F/6-K 四个季度。40-F 等其他类型也不默认纳入。

全部请求使用真实配置的应用名和联系邮箱 User-Agent；示例邮箱只能出现在配置说明，未配置时 US 功能明确报错。SEC 域名共享预算；当前进程之外同一使用方的请求也须纳入运维预算。

官方依据：[EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)、[Developer Resources](https://www.sec.gov/about/developer-resources)。

## 9. 日期、类型和语言

| 来源 | doc_type | 日期来源 |
|---|---|---|
| CN | Q1 / H1 / Q3 / FY | 明确标题年份和类型；异常则未知 |
| HK | ANNUAL / INTERIM / QTR-HK（v1.0.3 起均默认） | 明确期末日或可靠来源字段；ANNUAL/INTERIM 可经 §5.1 两阶段判期（document），禁止财年猜测 |
| US | 10-Q / 10-K / 20-F | 来源 reportDate；未知留空 |

`period.py` 提供日期校验和中文年份数字转换等通用函数；市场标题规则放适配器或市场专用模块。繁简体需要实际字符/转换覆盖，不能在注释说兼容而正则只含”個”。

I0 实测补充（2026-09-20，样本见 tests/fixtures/）：

- CN 标题变体：`第一季度报告`（600519）与 `一季度报告`（000001）**并存**；公司名直接作前缀、无冒号；
- HK 文档角色以检索结果的**子类别文本**为权威（`[年報]`/`[中期/半年度報告]`/`[環境、社會及管治資料/報告]`），标题正则仅辅助；HTML 实体（`&#x2f;`）先反转义；
- HK 非日历年结公司标题为跨年标签（`2024/25 年報`）：doc_type 可定，日历期末不可得 → null + unknown + 警告；
- HK 业绩公告标题含中文数字明确期末（`截至二零二六年三月三十一日止三個月`）：QTR-HK 的 period_source=explicit_title 可行。

已知报告期先倒序，再按 filing_date 倒序、source_id 确定稳定次序；未知报告期单独排最后。filing_date 也未知的候选保留来源顺序信息和警告，不补当前日期。存储排序不能依赖 `~` 或 unknown 文件名前缀。

## 10. 传输层与下载器

### 10.1 统一请求路径

每个市场线程持有自己的 requests.Session；不跨线程共享可变 Session。所有外部 HTTP（resolve、列表、文件、重试、重定向）走同一 Transport。requests/urllib3 禁用隐式自动重试，由显式重试循环负责每次调用限速器，避免外部 wait 只约束第一次请求。

按来源组而非单 host 限速：SEC 相关域名共享 0.13 秒，CN 站点/静态站共享 0.5 秒，HK 查询/文件站共享 0.3 秒；间隔是保守配置值，实测可调整。线程安全的单调时钟最小间隔器即可，无需声称为令牌桶。

最多 3 次重试（首次 + 3 次，共 4 次尝试），指数退避并加抖动，429/指定 5xx 尊重 Retry-After。GET 及已知只读查询 POST 可重试，不能把该策略用于任意写接口。403/验证码/结构变化作为可诊断错误，避免重试风暴。所有等待受任务 deadline 约束。

### 10.2 流式读取与校验

- 连接/读取超时默认 10/120 秒；读取超时不等于总时限，额外应用每文件和每任务截止时间。
- 使用 stream，边写唯一临时文件边累计实际字节和 SHA-256；默认上限 200 MiB，缺 Content-Length 也执行，不先把全文放进内存。
- Content-Length 仅在同一传输表示下比较；有 Content-Encoding 自动解压时不得把解压后长度与压缩长度直接比较。检查异常中断，并记录可获得的传输长度和存储长度。
- PDF 检查签名及完整性特征；HTML 检查结构、来源申报/文档特征与已知错误/验证页面。200/Content-Type/magic bytes 都不能单独证明有效；可疑响应报 download_invalid。
- 手动校验每次重定向的官方域名、HTTPS 和非私网地址，限制跳转次数；Cookie/源站头不能泄露给未允许目标。

下载器返回 DownloadedFile，不提交最终文件，不更新 manifest。失败临时文件不标为 done；重试使用独立临时名。P0 断点恢复是“已完成文件复用，未完成文件重新下载”，不承诺 HTTP Range 字节续传。

## 11. Store、文件版本与恢复

### 11.1 路径

```text
flat:   {out}/{market}/{symbol}/{period_or_unknown}__{doc_type}__{title}__{report_id}__{artifact_id}.{ext}
nested: {out}/{market}/{symbol}/{period_or_unknown}/{doc_type}__{title}__{report_id}__{artifact_id}.{ext}
```

ID 为受控不透明标识，实际实现确定固定编码长度；始终参与文件名，不能靠标题碰巧唯一。清理 Windows/Linux 非法字符、控制字符、保留设备名、首尾空白/点号；限制标题和整路径长度。路径不足容纳 ID 时拒绝过长 out 配置，而不是再次碰撞。扩展名根据已验证媒体类型生成，源 URL 后缀仅作提示；`10-K/A` 不直接进入文件名。

HTTP 下载（Content-Disposition）使用**可读且确定**的名称：`{market}_{symbol}_{doc_type}_{报告期|公告日|unknown}_{report_id}.{ext}`，同时给出 ASCII 回退与 `filename*=UTF-8''`（RFC 6266）。报告期未知时才退化为公告日，二者都无则字面 `unknown`；不使用中文标题前缀，避免下划线噪声。历史版本（非当前 artifact）名称追加 artifact_id，避免同名版本歧义；归档本地路径仍以 `report_id__artifact_id` 保证唯一。

### 11.2 数据表责任

| 表 | 最小内容与约束 |
|---|---|
| schema_meta | schema_version；启动时检查兼容性，不静默重建旧库 |
| manifest | report_id 主键；market/symbol/source_id 唯一；Report 元数据；status=discovered/downloading/done/failed；current_artifact_id 可空；created_at/updated_at UTC |
| artifacts | artifact_id 主键；report_id 外键；sha256、bytes、media_type、local_path、source_url/final_url、fetched_at；state=staged/ready/unavailable；同 report_id+sha256 唯一 |
| archive_intents | attempt_id 主键；report_id、artifact_id、temp_path、目标相对路径、预计 sha/bytes、state；用于识别数据库/改名之间的中断 |
| symbol_map | market/symbol 唯一；source_issuer_id、display/exchange、updated_at/expires_at；来源内部 ID 视为不透明值 |
| jobs | job_id、client_id、idempotency_key、request_hash、effective_request_json、status、attempt、deadline、时间/汇总；client_id+idempotency_key 唯一 |
| job_symbols | job_id+规范化证券键唯一；状态、coverage、warnings、resolve/list 错误；允许 report 尚不存在的失败 |
| job_items | job_id+report_id 唯一；pending/running/downloaded/cached/failed、尝试/错误、artifact_id；下载失败不破坏全局已完成报告 |

这是实施所需的表契约，具体迁移 DDL 在实现阶段补齐；每个连接启用 foreign_keys、busy_timeout，库启用 WAL。校验 status/check 约束和外键关系；更新时间不能替代首次披露日期。source_metadata 只保存必要来源字段，不保存 Cookie/凭据。

### 11.3 归档提交时序

1. upsert 候选 report，得到稳定 report_id；若有 ready 文件且大小/checksum 复核通过，返回 cached。P0 以 SHA-256 校验避免同大小损坏被当作命中，可在以后明确权衡检查成本。
2. 分配 attempt_id/artifact_id、唯一临时路径，登记下载尝试。首次归档标 downloading；刷新已有有效文件时 manifest 继续 done，失败只记任务尝试。
3. 传输层写到同卷临时文件，校验/flush 后返回摘要；Store 在短事务中保存 staged artifact 及完整 archive intent，再提交事务。
4. Store 唯一负责把临时文件原子改名到唯一目标，校验已有同名目标，不能覆盖不同内容。若相同 report+sha 已有 ready 版本，复用该 artifact，临时文件登记为可运维回收。
5. 最终短事务标 artifact ready、manifest done/current_artifact_id，并更新任务项成功。当前有效文件版本在此切换。
6. 归档提交（或缓存复核）完成后，仅当报告期仍未知时，可对 HK 年报/中期 PDF 从**已校验本地原文**提取明确期末日写入 manifest（period_source=document）。该步只更新 manifest 元数据，不新增/移动/删除归档文件，也不改变原子提交/恢复语义；提取失败或歧义非致命。后续来源刷新时，未知候选不得覆盖库中既有的可信非 unknown 报告期。缓存命中同样可回填，从而不重下即可补齐既有归档。

文件系统与 SQLite 无共同事务。重启先核对 intent：目标存在且校验一致则补记 ready；只有完整临时文件则重试提交；两者都缺失/损坏则记录失败并按预算重抓。遗留文件不自动删除或递归清理；intent 保留诊断。数据库 done 但文件缺失时标 artifact unavailable；若当前文件不可用且无其他有效版本，manifest 转 failed 并清空 current_artifact_id，HTTP 下载返回 file_not_available。后续重抓若 checksum 与已有 unavailable artifact 一致，修复原 artifact 文件并恢复 ready，复用其 ID，避免违反 report_id+sha256 唯一约束。

相同 source_id 刷新但 bytes/checksum 改变，保留旧 artifact、创建新 artifact；不同 source_id 的修订报告是新 report，通过 revision_of（关系可确认时）链接。默认只复用本地已有版本，不声称能发现源站同 URL 的静默替换；用户可 refresh 触发检查。

### 11.4 并发和恢复边界

单进程并不等于可跨线程任意共享 sqlite3.Connection。每线程独立连接、短事务，写入争用采用 busy_timeout 和有界重试；网络下载不得持有写事务。任务领取采用条件更新并检查影响行数。

一个归档根目录一个进程级所有者锁，运行 service 时禁止独立 CLI 同库写入。SQLite 约束与 source_id 去重只是最后防线，不能替代队列所有权。重启 running 任务的回收规则按 HTTP_API.md §4。不引入“数据库整库锁 + 网络操作”的长锁。

## 12. CLI

```text
python -m reports_fetcher fetch 600519 0700.HK AAPL --last 4
python -m reports_fetcher list AAPL --last 4
python -m reports_fetcher serve --host 127.0.0.1 --port 8000
python -m reports_fetcher version
```

| 参数 | 语义 |
|---|---|
| symbols / --file | 二选一；文本一行一个代码，支持注释；若支持 CSV，须明确 symbol 列契约，不把整行当代码 |
| --last | 默认 4，范围 1–20，与 HTTP 一致 |
| --forms | 单市场任务的基础类型白名单；多市场任务不接受易歧义的混用，使用各市场默认/配置 |
| --out / --layout / --config | 仅本地 CLI/服务运维选项，不开放给 HTTP 客户端 |
| --refresh | 重下候选并保留旧内容版本，代替语义不明的 --overwrite |
| --market-workers | 1–3；不能提升同源限速预算 |
| --verbose | 增加诊断日志，凭据仍脱敏 |

list 联网预览元数据和覆盖信息，不下载原文，可更新 symbol_map；必须说明该缓存副作用。服务持有目录锁时，独立 CLI list 也不得更新同库，返回 store_in_use。服务调用方通过 HTTP 查询已归档内容。

输出 downloaded/cached/failed、质量警告及无报告说明；API 与 CLI 共用 FetchResult 语义。退出码按 §4，参数错误返回 2。serve 仅使用一个 Uvicorn worker。

## 13. 配置

```toml
[general]
out_dir = "./reports"
layout = "flat"

[http]
user_agent = ""                 # US 使用前配置真实应用名和联系邮箱
max_file_bytes = 209715200       # 200 MiB，实际读取也检查
connect_timeout_seconds = 10
read_timeout_seconds = 120
file_deadline_seconds = 600
max_retries = 3

[http.source_intervals]
sec = 0.13
cninfo = 0.5
hkex = 0.3

[fetch]
last_n = 4
market_workers = 3
max_lookback_years = 10
max_discovery_requests_per_symbol = 100

[fetch.default_forms]
CN = ["Q1", "H1", "Q3", "FY"]
HK = ["ANNUAL", "INTERIM", "QTR-HK"]   # v1.0.3：默认纳入自愿季度业绩
US = ["10-Q", "10-K", "20-F"]

[server]
host = "127.0.0.1"
port = 8000
max_pending_jobs = 100
job_deadline_seconds = 1800
max_job_attempts = 3
max_symbols_per_job = 50

[log]
level = "INFO"
file = "./reports-fetcher.log"
```

CLI 显式参数 > 环境覆盖 > 配置文件 > 默认值；argparse 未提供的参数不得用自身默认值误覆盖配置。API 业务参数以客户端显式值 > 服务生效默认值解析并持久化，资源上限由服务端控制。令牌由外部机密/环境配置映射 client_id，不进示例配置或日志。容器为主力运行方式（ARCHITECTURE §3）：配置文件经 bind mount（容器内固定 /app/config.toml）、敏感项经环境变量注入；归档根目录与日志目录同样经挂载持久化到宿主机。

来源组包含查询及文件域名，允许列表需实测备案。symbol_map 建议默认 7 天过期；明确的映射失效刷新一次，空结果先区分无报告与错误，不随意删除已有身份关联。

## 14. 调度和持久化任务

FetchService：规范化/分组 → 逐市场串行 resolve/list → 报告期两阶段准备（§5.1）→ 统一选择 → upsert 报告 → 校验缓存/下载（判期预取文件复用提交）→ Store 归档 → 汇总。单文件失败继续该证券其他文件，单证券失败继续整个市场；预取临时文件在任务收尾统一清理（未入选不产生孤儿文件/脏 done）。

JobService 在事务中校验幂等键并保存生效请求与 queued 状态，提交后返回 202。任务执行器有界领取一个任务，再调用 FetchService；持久化每证券/文件进度，HTTP 查询不等待任务完成。数据库中的 queued 是唯一待办依据，内存信号只作唤醒。

重启先做存储恢复再领取任务，running 任务回到 queued 并增加 attempt，复用已完成文件；超过 deadline/attempt 上限终结并保留部分结果。完成状态与错误码按 [HTTP_API.md](./HTTP_API.md)；后台线程异常必须记录终态或由恢复机制识别，不能永久 running。

## 15. 日志与运维

标准 logging，控制台 INFO、滚动文件可启用 DEBUG。包含 request_id、job_id、report_id、market、source、status、elapsed_ms、retry_count；HTTP 日志不输出 Authorization/Cookie 或未经处理的响应正文。

记录排除/保留候选的理由、检索范围、分页次数和未知期警告，使“为何少一份”可解释。监测队列深度、成功/失败、下载字节和源站限流。live/ready 区分进程与本地依赖；不对外部源站反复发健康探测。

一期不自动删除旧任务、版本或临时文件；可列出运维待回收项，实际删除/递归移动遵守机器级安全规则并另行授权。

## 16. 验证设计

| 模块 | 必须验证的行为 |
|---|---|
| symbol | HK 所有别名归一；US 来源类股符号；非法值、显式市场冲突及北交所不支持 |
| selection/period | 同期不同类型、完整修订/通知区分、语言回退、非日历财年、未知日期保留、forms 生效；混合选择两阶段（冷/热库一致、有界预取、未入选候选清理，A1–A10） |
| adapters | CN/HK 真实 fixture、分页/扩窗与截断；HK 数组嵌套；SEC recent+历史文件及数组长度校验 |
| transport | resolve/list/重试/重定向都经过预算；无长度超大流、压缩长度、200 错误 HTML、超时与 429 |
| store | 同名来源不覆盖、内容版本保留、丢失/损坏文件修复、每个提交中断点恢复、线程连接与目录锁 |
| jobs/API | 并发请求幂等、入队后重启、任务失败隔离、受理持久化、状态聚合、鉴权/限额、ID 下载及路径边界 |

离线测试不触网，fake transport/clock 驱动失败和限速场景；实现时必要的 fixture 不能全由设计者猜造。HTTP 验收场景见 HTTP_API.md §8。

真实冒烟候选：沪深各有样本，HK 年报/中报及非日历财年，US 10-Q/10-K 和一家 20-F 发行人。公司具体申报形态以验证当时实际披露为准，BABA 可作候选但不预设其必有 6-K 四期结果。

验收以本次选择的逻辑报告组数 ≤ N 为准，保留历史版本后目录文件总数可以超过 N。逐一检查原文有效、来源/期末一致、manifest/current artifact 与磁盘 checksum 一致、立即重跑命中缓存、refresh 保留旧版本，以及 HTTP 全调用链。记录样本日期、URL、表单类型、实际结果及未支持项。

性能先记录环境和基线：例如三市场各 10 只、每只 4 份约 120 份文档，记录总字节、请求数、首次/缓存运行耗时及重试。原 ≤10 分钟/≤1 分钟保留为待测目标，不能当所有外网条件下的硬保证；API 查询响应不应随下载任务阻塞。

## 17. 实施顺序与迭代对应

实施按 [ITERATION_PLAN.md](./ITERATION_PLAN.md) 的迭代 I0–I6 推进（需求基线见 [REQUIREMENTS.md](./REQUIREMENTS.md)）；本章只维护"设计章节 → 首次落地迭代"的对应关系，里程碑细节与 DoD 以迭代计划为准。

| 设计章节 | 首次落地迭代 |
|---|---|
| §2 证券识别与规范化 | I1（US）；CN/HK 用例在 I2/I3 |
| §3 统一数据模型 | I1 |
| §4 异常与结果 | I1 起逐步引入，I6 核对全集 |
| §5 适配器及选择契约 | I1（框架与基础选择）；CN/HK 市场规则在 I2/I3 |
| §6 CN 巨潮 / §7 HK 披露易 / §8 US EDGAR | I2 / I3 / I1 |
| §9 日期、类型和语言 | I1 基础；I2/I3 繁简体与市场规则 |
| §10 传输层与下载器 | I1 |
| §11 Store、版本与恢复 | I1 最小闭环；I4 完整恢复协议与 refresh 版本策略 |
| §12 CLI | I1 起步（fetch/list）；I3 完整参数；I5 增加 serve |
| §13 配置 | I1 起步，随迭代补齐分组 |
| §14 调度与持久化任务 | FetchService 在 I1；JobService 与 deadline/attempt 在 I5 |
| §15 日志与运维 | I1 基础；I4/I5 补关联 ID 与运维项 |
| §16 验证设计 | 各迭代按其 DoD 执行；I6 汇总冒烟与基线 |

先完成 I0（来源契约验证与 fixture 冻结）再估时；原 2.5 人日估算撤回。财报提取/比较/分析另走 [ANALYSIS_ROADMAP.md](./ANALYSIS_ROADMAP.md)，不在一期混入实施计划。

## 附录：来源样例维护约定

I0 已于 2026-09-20 录制真实响应样本并存入 `tests/fixtures/`（19 个文件：US×4 / CN×5 / HK×10，清单与脱敏说明见该目录 README）；本文 §6/§7/§8 的示例值与 fixture 一致，可直接作为解析测试基准。HK 旧 `titleSearcherJson.do` 的双重 JSON 形态已随端点下线失效，仅保留 404 证据 fixture；现行 HK 契约为 titlesearch.xhtml 深链 HTML。

后续契约变化的处理：probe 复现 → 新增带日期的新 fixture（旧样本保留为结构版本历史）→ 测试按 fixture 分支适配。仅保留必要公开元数据；不把 User-Agent 联系信息、Cookie 或服务令牌存入测试样本。
