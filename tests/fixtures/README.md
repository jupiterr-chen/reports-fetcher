# 来源响应 Fixture（I0 产出）

- 抓取时间：2026-09-20（Asia/Shanghai，约 21:50–22:15）
- 抓取环境：Docker dev 镜像（python:3.12-slim）内运行 `tools/probe/probe_sources.py` 及补充探测
- 脱敏说明：所有文件均为公开披露元数据，未包含 Cookie、令牌或个人联系信息
- 契约结论：见 [docs/SOURCE_VERIFICATION.md](../../docs/SOURCE_VERIFICATION.md)；探测脚本可复现：`docker compose run --rm probe`
- 用途：适配器与解析器单元测试的离线样本；**测试不得触网**

## 清单

| 文件 | 市场 | 内容 | 正常/异常 |
|---|---|---|---|
| `us/company_tickers_sample.json` | US | ticker→CIK 全表节选（含类股 BRK-A/BF-B/GOOG/GOOGL 命中） | 正常 |
| `us/submissions_aapl_recent.json` | US | AAPL submissions：recent 键集、1001 行、form 分布、定期报告行、filings.files | 正常 |
| `us/submissions_aapl_histfile.json` | US | 历史文件（顶层并行数组、1247 行、1994 年老申报 primaryDocument 为空串） | 边界 |
| `us/submissions_notfound.json` | US | 不存在的 CIK → 404 + XML 错误体（data.sec.gov 为 S3 支撑） | 异常 |
| `cn/topsearch_samples.json` | CN | topSearch 600519/000001 → orgId（gssh0/gssz0 前缀） | 正常 |
| `cn/topsearch_nomatch.json` | CN | 不存在代码 → 200 + 空数组 | 异常 |
| `cn/hisann_600519_p1.json` | CN | hisAnnouncement 完整字段清单 + 前 8 行（含摘要/英文版变体、"第一季度报告"标题） | 正常 |
| `cn/hisann_000001_p1.json` | CN | 平安银行定期报告（"一季度报告"标题变体 + 首行完整原始字段） | 正常 |
| `cn/pdf_check.json` | CN | static.cninfo.com.cn PDF magic bytes（%PDF-） | 校验 |
| `hk/prefix_samples.json` | HK | prefix.do 00700/0016 → stockId（返回五位 code `00016`） | 正常 |
| `hk/prefix_nomatch.json` | HK | 不存在代码 → 200 + 空 stockInfo（JSONP） | 异常 |
| `hk/prefix_search_variants.json` | HK | prefix.do 去零前缀搜索行为 + 00011 索引缺席（I4 实测 2026-09-21，适配器按精确匹配判 not_found） | 边界 |
| `hk/search_00700_40000_3y.json` | HK | 深链检索解析后行集（含子类别/发布时间/PDF 链接/文件大小） | 正常 |
| `hk/search_00700_40000_3y.raw.html` | HK | 上述检索的服务端渲染原始 HTML（解析器测试基准） | 正常 |
| `hk/search_0016_40000_3y.json` | HK | 非日历年结（六月）公司：跨年标题 `2024/25 年報` 形态 | 边界 |
| `hk/search_0016_40000_3y.raw.html` | HK | 上述检索原始 HTML（I3 实现期自 tools/probe/out 提升，2026-09-20 记录） | 边界 |
| `hk/search_00700_10000_yeji_2026.json` | HK | t1=10000 + title=業績：[季度業績]/[中期業績] 公告（QTR-HK 验证） | 正常 |
| `hk/search_00700_10000_yeji_2026.raw.html` | HK | 上述检索原始 HTML（I3 实现期自 tools/probe/out 提升，2026-09-20 记录） | 正常 |
| `hk/search_00700_40000_10y.json` | HK | 10 年窗口 24 条单页全返（分页结论） | 行为 |
| `hk/ablation_nocookie.json` | HK | 免 Cookie/免预热深链检索可行 | 行为 |
| `hk/deprecated_endpoint.json` | HK | 旧 titleSearcherJson.do → 404（契约迁移证据） | 异常 |
| `hk/pdf_check.json` | HK | PDF magic bytes（%PDF-1.7） | 校验 |

## 维护约定

- 源站契约再次变化时：先用 probe 复现 → 新增带日期后缀的新 fixture → 旧 fixture 保留（结构版本历史），测试按 fixture 分支适配。
- 不要手工编造响应结构；解析分支必须有对应真实样本。
