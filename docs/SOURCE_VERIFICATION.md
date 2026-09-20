# I0 来源契约验证简报

| 项目 | 内容 |
|---|---|
| 日期 | 2026-09-20（Asia/Shanghai 21:50–22:30） |
| 环境 | Docker dev 镜像（python:3.12-slim + requests）；`docker compose run --rm probe` 执行 |
| 复现 | `SEC_UA_EMAIL=<邮箱> docker compose run --rm probe`（脚本 `tools/probe/probe_sources.py`） |
| 产出 | `tests/fixtures/`（19 文件，清单见其 README）+ DESIGN §6/§7/§8/§9/附录 回填 |

---

## 1. 三市场契约验证结论

### US / SEC EDGAR —— 与设计假设一致，全部确认

- `company_tickers.json` 200（10,438 条）；类股为**连字符格式**（BRK-A/BF-B），GOOG/GOOGL 同 CIK 不同 ticker；
- `submissions` recent 并行数组 1,001 行（AAPL），17 键；`filings.files` 历史段存在（1,247 行，1994–2015），**历史文件顶层即并行数组**，老申报 `primaryDocument` 为空串（按需读历史需 index.json 兜底，默认场景不触历史）；
- 不存在的 CIK → 404 + XML 错误体；
- reportDate 权威性验证：Apple 财年 9 月止（10-K reportDate=2025-09-27）——不可假设日历季度；
- UA 规范生效（配置邮箱），全程无 403/429。

### CN / 巨潮资讯 —— 与候选契约一致，两处修正

- topSearch / hisAnnouncement 全通；orgId 直接使用返回值（gssh0/gssz0 前缀证实但不依赖）；
- **column=szse 与 sse 对沪市股票返回完全一致** → 统一 szse；
- 需 UA + Referer + X-Requested-With，首页预热后取 JSESSIONID/SF_cookie_4（是否强制未消融，保守保留预热）；
- 分页由 `totalAnnouncement`/`hasMore` 驱动；PDF magic `%PDF-` 验证通过；
- **标题新知**：公司名直接前缀无冒号；"第一季度报告"与"一季度报告"**两种变体并存**；摘要/英文版混于同类别结果（按标题区分，与设计一致）；
- 响应含 `announcementId`/`associateAnnouncement` 等扩展字段（修订关联可用）。

### HK / 披露易 —— **契约已迁移，设计重大修正**

- **旧 `titleSearcherJson.do` 已下线（404）**；v1.1 设计分析的"双重 JSON/数组形态"失效；
- 现行契约：**GET 深链 `titlesearch.xhtml` + 服务端渲染 HTML**；有效日期参数是 `from/to`（YYYYMMDD），fromDate/toDate 被忽略（参数矩阵实测）；
- 结果行含发布时间/代码（可能带人民币柜台第二代码）/简称/**子类别方括号文本**/PDF 链接/大小；子类别（`[年報]`/`[中期/半年度報告]`/`[環境、社會及管治資料/報告]`）是文档角色权威来源，需 HTML 实体反转义；
- 10 年窗口 24 条**单页全返**（站点显示上限 1000，超限切年窗）；**免 Cookie 免预热可行**（消融验证）；偶发 TLS 握手重置 → 显式重试必选；
- **QTR-HK 验证通过**：t1=10000 + title=業績 可召回 `[季度業績]` 公告，标题含中文数字明确期末日 → 可按 REQUIREMENTS §8 决策 1 作为显式类型启用（默认关）；
- **非日历年结公司**（0016，六月年结）标题为跨年标签（`2024/25 年報`）：日历期末不可得 → null + unknown + 警告（设计规则的真实场景，fixture 留证）；
- PDF magic `%PDF-1.7` 验证通过。

## 2. I0 DoD 核对

| # | DoD | 结果 |
|---|---|---|
| 1 | 每市场 ≥1 正常 + ≥1 异常 fixture | ✅ US 4 / CN 5 / HK 10（含无匹配、404、端点废弃、老申报空字段、跨年标题等边界） |
| 2 | 分页/扩窗方式确认并写入 DESIGN | ✅ CN：pageNum+hasMore；HK：单页全返+切年窗；US：recent 覆盖默认+files 按需 |
| 3 | DESIGN"待实测/候选"标记闭合 | ✅ 已回填为"已验证"；残留实现期确认项：北交所 column、CN Cookie 是否强制、HK 更正公告子类别形态（均不阻塞 I1） |
| 4 | CN/HK 请求头与 Cookie、SEC UA 配置实测记录 | ✅ 见 §1 与 DESIGN §6/§7/§8 |
| 5 | I1–I6 人日估算表产出 | ✅ 见 §3 |
| 6 | Docker 基座可用 | ✅ 全部探测经 `docker compose run --rm probe` 完成且可复现 |

## 3. I1–I6 估算（I0 后复核，单人开发）

| 迭代 | 规模复核 | 估算 | 依据 |
|---|---|---|---|
| I1 核心链路垂直切片（US） | L | **2 人日** | 契约全验证，模型/传输/Store/选择函数/CLI/单测从零搭骨架；HK 变更不影响 US |
| I2 CN 适配器 | M | **1 人日** | 契约与设计一致；标题解析按实测变体 |
| I3 HK 适配器 | M→M+ | **1.5 人日** | 契约改为 HTML 解析（HTML 行解析器 + 实体反转义 + 子类别映射），比原 JSON 方案略增；fixture 齐备 |
| I4 可靠性硬化 | M | **1.5 人日** | 恢复协议逐中断点测试 |
| I5 HTTP 服务与持久化任务 | L | **2 人日** | HTTP_API 验收场景多 |
| I6 一期验收与发布 | M | **1 人日** | 冒烟矩阵 + 基线报告 + 文档 |
| **合计（剩余）** | | **约 9 人日** | |

## 4. 风险与备注

1. **HK 契约新鲜度**：深链 GET 属页面书签能力而非承诺 API，可能再变。对策已内建：fixture 基线 + 陌生结构返回 `source_contract_changed` + probe 可复现。
2. **网络环境**：容器内偶发 TLS 握手重置（本机→披露易），重试 1–2 次即恢复；Docker Hub 直连不可用，已参数化 BASE_IMAGE（默认用本地 python:3.12-slim）与 PIP_INDEX_URL（默认清华源）。
3. **限速体感**：探测全程（约 40 个请求、间隔 ≥0.4s）无 429/封禁；维持设计初值 SEC 0.13s / CN 0.5s / HK 0.3s。
4. **SEC UA**：探测使用了仓库 git 配置中的联系邮箱（用户本人）；生产要求部署者显式配置（未配置则 US 功能明确报错，设计不变）。
5. 残留实现期确认项不阻塞 I1 开工（见 §2 第 3 行）。
