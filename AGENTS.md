# AGENTS.md — reports-fetcher 项目约定（供 AI 会话开场阅读）

一句话：输入 A股/港股/美股代码 → 获取最新 N 份定期财报原文 → 按 市场/代码/财报日期 归档 + SQLite 索引；CLI + HTTP 双入口，Docker 为主力运行方式。

## 当前状态（2026-09-21）——**一期 v1.0.1（评审修复后）**

- I0–I6 全部交付：三市场 CLI 归档（I3 起可用）、可靠性硬化（I4）、HTTP 服务（I5）、验收发布（I6）。
- PHASE1_REVIEW T1–T7 已修复；独立复审 178 项定向测试通过，v1.0.1（76fee13）已部署至 chen@192.168.1.150:/home/chen/dev/reports-fetcher，三市场 HTTP 抓取、缓存、幂等及重启读取通过，记录见 [DEPLOY.md](DEPLOY.md)。服务仅发布宿主 127.0.0.1:8000。
- 验收留证：[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)（ARCHITECTURE §10 逐条核对 + 冒烟矩阵 + 性能基线 + 偏差记录）；使用文档：[README.md](README.md)。
- **后续**：内容提取/指标/基本面分析按 [ANALYSIS_ROADMAP.md](ANALYSIS_ROADMAP.md) 另立计划（二期+）；backlog 见 ITERATION_PLAN §7。
- CLI：`SEC_UA_EMAIL=<邮箱> docker compose run --rm cli fetch AAPL 600519 0700.HK --last 4`；HTTP：`docker compose up -d serve`（127.0.0.1:8000）。

## 文档地图（动手前先读）

| 文件 | 用途 |
|---|---|
| `ITERATION_PLAN.md` | 迭代划分与 DoD——**工作节奏的唯一依据**；每迭代结束更新其状态 |
| `REQUIREMENTS.md` | 一期需求基线（FR/NFR）与验收口径 |
| `DESIGN.md`（v1.3） | 详细设计；§6/§7/§8 为已实测验证的来源契约 |
| `HTTP_API.md` | 一期 HTTP 契约（I5 落地） |
| `docs/SOURCE_VERIFICATION.md` | I0 验证简报：三市场契约结论、DoD 核对、估算依据 |
| `ARCHITECTURE.md` / `REVIEW.md` / `ANALYSIS_ROADMAP.md` | 架构、评审历史、后续分析路线（二期+，勿混入一期） |

## 硬性约定

1. **Docker 主力**：一切执行（探测/CLI/测试/服务）都在容器内，宿主机不装 venv。常用：
   - 探测：`SEC_UA_EMAIL=<邮箱> docker compose run --rm probe`
   - 单测：`docker compose run --rm test`
   - CLI：`SEC_UA_EMAIL=<邮箱> docker compose run --rm cli fetch AAPL --last 4`（config.toml 由 config.example.toml 复制生成，已 gitignore）
   - 国内网络：`BASE_IMAGE`/`PIP_INDEX_URL` 可覆盖（默认本地 `python:3.12-slim` + 清华源）
2. **数据质量铁律**：未知即 null——报告期解析不出就 `period_source=unknown` + 警告，禁止用公告日/12-31/财季惯例猜造日期（HK 非日历年结公司是真实场景，见 fixture）。
3. **Store 是唯一归档提交方**：下载器只产出已校验临时文件；先落盘原子改名、后标记 done。source_id 去重键：(market, symbol, source_id)。
4. **传输层统一**：所有源站请求（含重试/分页/重定向）过共享限速器（SEC 域名组合计 ≤8 req/s）；显式重试循环，不用 urllib3 自动重试。HK 偶发 TLS 重置，必须重试。
5. **测试不触网**：解析分支必须有 `tests/fixtures/` 真实样本支撑，禁止编造响应结构。
6. **迭代纪律**：迭代内范围冻结；新想法进 `ITERATION_PLAN.md` §7 backlog。每迭代结束：DoD 核对留证 → 更新迭代计划状态 → git commit + push → 向用户做阶段汇报。
7. **HK 契约警觉**：现行契约是 `titlesearch.xhtml` GET 深链 + 服务端 HTML（2026-09 实测），旧 `titleSearcherJson.do` 已 404。陌生结构返回 `source_contract_changed`，不得伪装空结果。

## 环境备忘

- 宿主机 Windows + Git Bash；Bash 每条命令后 cwd 会重置，用绝对路径或先 `cd "/d/2.Develop/7.zcode/reports-fetcher"`。
- git 身份已配置（Jupiter Chen）；远程 `git@github.com:jupiterr-chen/reports-fetcher.git`（main 分支，SSH 可用）。
- SEC UA 必须真实联系邮箱（用户 git 邮箱可用）；未配置则 US 功能明确报错。
- 归档/日志/*.sqlite3* 不进版本库（.gitignore 已配）；`tools/probe/out/` 是探测原始产物（脱敏后才进 fixtures）。
