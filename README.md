# reports-fetcher

输入 A股/港股/美股代码 → 获取最新 N 份定期财报原文 → 按 `市场/代码/财报日期` 归档到本地 + SQLite 索引。CLI 与 HTTP 双入口，**Docker 为主力运行方式**（宿主机仅要求 Docker）。

| 市场 | 来源 | 支持类型 |
|---|---|---|
| CN 沪深 | 巨潮资讯 | Q1 / H1 / Q3 / FY（修订版归入基础类型） |
| HK | 港交所披露易 | ANNUAL / INTERIM；QTR-HK 需显式请求（自愿披露） |
| US | SEC EDGAR | 10-Q / 10-K / 20-F（含 /A 修订版） |

核心数据质量规则：**未知即 null**——报告期解析不出就置空并附警告，绝不用公告日、12-31 或财季惯例猜造日期（港股非日历年结公司是真实场景）。

## 快速开始

```bash
# 1) 准备配置（可选；不复制也能用环境变量）
cp config.example.toml config.toml

# 2) 抓取（US 需要 SEC 真实联系邮箱）
SEC_UA_EMAIL=you@example.com docker compose run --rm cli \
    fetch AAPL 600519 0700.HK --last 4

# 3) 归档结果（宿主机 ./reports/）
#    reports/US/AAPL/2026-06-27__10-Q__aapl-20260627.htm__<report_id>__<artifact_id>.html
#    reports/CN/600519/2026-06-30__H1__贵州茅台2026年半年度报告__....pdf
#    reports/HK/00700/unknown__ANNUAL__2025 年報__....pdf（港股标题无期末日证据，见已知限制）
```

国内网络可覆盖 `BASE_IMAGE` / `PIP_INDEX_URL`（默认本地 `python:3.12-slim` + 清华 pip 源）。

## CLI

```bash
docker compose run --rm cli fetch <代码...> [--last N] [--forms A,B] [--refresh]
docker compose run --rm cli list   <代码...> [--last N]        # 联网预览，不下载
docker compose run --rm cli version
```

| 参数 | 语义 |
|---|---|
| `--last N` | 最新 N 个**逻辑报告组**（默认 4，范围 1–20）；不等于 N 个财季 |
| `--forms` | 基础类型白名单（单市场任务，如 `--forms 10-K,10-Q`） |
| `--refresh` | 重下候选并**保留旧内容版本**（内容变化产生新 artifact，旧版本仍可按 ID 读取） |
| `--out / --layout / --config` | 归档目录 / flat·nested 布局 / 配置文件 |

- 代码别名：`600519 / sh600519 / 600519.SH`、`0700.HK / 700`（补零 5 位）、`aapl / BRK-B`（连字符类股）均可用；北交所显式返回 `unsupported_market_segment`。
- 退出码：`0` 成功或纯空查询；`1` 有可用结果但存在质量警告/缺口（正常 `--last` 截取与语言选择属说明性信息，不计缺口）；`2` 全无结果且有执行错误 / 参数错误。
- 立即重跑全部命中缓存（SHA-256 复核）；单证券失败不影响整批；强杀后重跑自动收敛（无半文件、无脏 done）。

## HTTP 服务

```bash
docker compose up -d serve          # http://127.0.0.1:8000（仅发布到回环）
```

默认本地无鉴权模式（端口仅发布 127.0.0.1）；对外发布必须配置 `RF_API_TOKENS=client:token,…`（Bearer，每令牌映射 client_id；docs/OpenAPI 同受保护）。

```bash
# 提交任务（幂等键必传）
curl -s -X POST http://127.0.0.1:8000/api/v1/fetch-jobs \
  -H "Content-Type: application/json" -H "Idempotency-Key: demo-001" \
  -d '{"symbols": ["AAPL", "600519", "0700.HK"], "last_n": 4}'
# → 202 {"job_id": "job_…", "status": "queued", "status_url": "/api/v1/fetch-jobs/job_…"}

# 轮询至终态（queued → running → succeeded|partial|failed）
curl -s http://127.0.0.1:8000/api/v1/fetch-jobs/job_…

# 档案查询（只读，绝不隐式联网）与按 ID 下载（ETag=sha256，支持 304）
curl -s "http://127.0.0.1:8000/api/v1/reports?market=HK&symbol=00700&limit=20"
curl -s -o report.pdf "http://127.0.0.1:8000/api/v1/reports/<report_id>/file"
# 旧版本： /file?artifact_id=<artifact_id>
```

Python 调用示例：

```python
import time
import requests

BASE = "http://127.0.0.1:8000/api/v1"

job = requests.post(f"{BASE}/fetch-jobs",
                    json={"symbols": ["600519", "0700.HK"], "last_n": 4},
                    headers={"Idempotency-Key": "py-demo-001"}).json()
while True:  # 轮询建议从 2s 退避到 10s
    doc = requests.get(f"{BASE}/fetch-jobs/{job['job_id']}").json()
    if doc["status"] not in ("queued", "running"):
        break
    time.sleep(2)

for result in doc["results"]:
    for report_id in result["report_ids"]:
        detail = requests.get(f"{BASE}/reports/{report_id}").json()
        print(result["symbol"], detail["doc_type"], detail["report_period"])
```

任务语义（[HTTP_API.md](HTTP_API.md)）：幂等键 + request_hash（同键同参返回原任务、异参 409）；落库后才 202；单执行器 + 队列上限（429）；deadline/attempt；重启后 running 恢复为 queued 且不重复执行已完成项；慢下载期间健康检查与任务查询不被阻塞。错误统一 `application/problem+json`（RFC 9457 + `code/request_id/retryable`）。serve 持归档目录所有者锁，期间 CLI 写操作报 `store_in_use`。

## 归档与索引

```text
reports/
  <市场>/<代码>/<报告期或unknown>__<类型>__<标题>__<report_id>__<artifact_id>.<ext>
  archive.sqlite3    # manifest / artifacts / archive_intents / symbol_map / jobs*
```

- 去重键 `(market, symbol, source_id)`；`report_id` 持久化后不变；`artifact_id` 绑定内容 SHA-256——同来源内容变化保留多版本，旧版本按 ID 可读且 checksum 一致。
- Store 是唯一归档提交方：临时文件 → 校验 → 原子改名 → 标记 done；文件缺失/损坏自动检出并修复（复用原 artifact ID）。

## 配置

配置优先级：CLI 参数 > 环境变量（`SEC_UA_EMAIL`、`RF_API_TOKENS`、`RF_LOCAL_MODE`）> `config.toml` > 默认值。完整字段见 [config.example.toml](config.example.toml)（限速/超时/重试/回溯窗口/类型默认/任务上限等）。

## 开发与测试

```bash
docker compose run --rm test          # 全部单测（fixtures 驱动，不触网）
SEC_UA_EMAIL=you@example.com docker compose run --rm probe   # 来源契约探测（复现）
```

测试基于 `tests/fixtures/` 真实响应样本（I0 冻结 + 实现期补充）；解析分支禁止编造响应结构。

## 已知限制

- 6-K 业绩附件、40-F、北交所不在一期范围（显式报 `unsupported_form` / `unsupported_market_segment`）；QTR-HK 需显式请求且仅覆盖 `[季度業績]` 公告。
- 港股报告标题（单年 `2025 年報` 与跨年 `2024/25 年報` 均含）只有年份语义、无期末日证据 → `unknown` + 警告（不猜测，铁律）；仅业绩公告（`截至…止`）有明确期末日。归档后若能从 HK 年报/中期 PDF **原文**提取明确期末日，则以 `period_source=document` 补写（仅补未知，绝不覆盖可信期）；提取失败/歧义非致命。`filing_date` 只作文件名回退，绝不作报告期来源。
- 文件下载 `Content-Disposition` 使用可读且确定的名称 `market_symbol_doc_type_报告期|公告日|unknown_report_id.ext`（含 `filename*` UTF-8；历史版本追加 artifact_id），不使用中文标题前缀。
- 披露易 `prefix.do` 为去零前缀搜索，个别代码（如 00011）在其索引中缺席 → `symbol_not_found`（源站数据现实，已留 fixture）。
- SEC HTML 主文档为 Inline XBRL，一期不恢复图片/CSS 等离线资源。
- P0 单进程单执行器、共享档案库；无 Webhook/任务取消/多租户；来源请求合并计入各市场限速预算（SEC 域名合计 ≤8 req/s 保守值）。
- 性能为记录制目标（见 [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) 基线），不承诺所有外网条件下固定耗时。

## 文档

| 文档 | 内容 |
|---|---|
| [REQUIREMENTS.md](REQUIREMENTS.md) | 一期需求基线（FR/NFR） |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 架构与一期完成定义 |
| [DESIGN.md](DESIGN.md) | 详细设计 + 三市场实测契约（§6/§7/§8） |
| [HTTP_API.md](HTTP_API.md) | HTTP 契约与验收场景 |
| [ITERATION_PLAN.md](ITERATION_PLAN.md) | 迭代计划与各迭代验收记录 |
| [docs/SOURCE_VERIFICATION.md](docs/SOURCE_VERIFICATION.md) | I0 来源契约验证简报 |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | 一期验收简报（§10 逐条留证 + 冒烟矩阵 + 性能基线） |
| [integration-kit/README.md](integration-kit/README.md) | 离线本地联调套件（mock + 客户端 + 契约测试 + 生产切换） |
