# reports-fetcher 本地联调套件（integration-kit）

这是一个**可独立复制、完全离线**的联调套件：用本地 mock 服务实现 reports-fetcher
v1.0.2 的 HTTP 契约，配套中文接口文档、可运行示例客户端和可执行的契约测试。
开发 AI agent 无需 Postman、无需联网、无需接触生产，即可把 HTTP 客户端开发完，
最后只改一个 base URL 就能切到生产。

- 契约基准：`HTTP_API.md` + v1.0.2 运行实现；机器可读定义见
  [openapi.json](openapi.json)（已修正自动 OpenAPI 的缺口）。
- 套件只依赖 Python 标准库与 `python:3.12-slim` 镜像，不 import 主项目、
  不访问 SEC/HKEX/CNINFO 或任何外网。

## 1. 两条命令快速开始（仓库根目录执行）

```bash
docker compose -f integration-kit/compose.yaml up -d mock
docker compose -f integration-kit/compose.yaml run --rm test
```

- 第一条启动 mock，监听 **http://127.0.0.1:18765**（仅回环）。
- 第二条在同一 compose 网络内对真实 mock HTTP 端点跑全部契约测试，最后输出
  `OK`（全部通过；测试数量随回归用例增加，不在此写死）。
- compose 项目名固定为 `reports-fetcher-integration-kit`，端口用 18765，
  不会影响正在运行的主服务（主服务是 8000）。

打开文档（无需联网、无 CDN）：<http://127.0.0.1:18765/docs>，
机器可读契约：<http://127.0.0.1:18765/openapi.json>。

## 2. 运行示例客户端

```bash
docker compose -f integration-kit/compose.yaml run --rm client
```

它会：提交一个任务 → 有界轮询到终态 → 下载所有可用文件到
`integration-kit/examples/out/` → 校验 SHA-256 → 打印警告。
默认 `symbols=AAPL, last_n=4`，退出码：`0` 成功、`1` partial（有可用文件+警告）、
`2` failed/校验失败、`3` 超时、`4` HTTP/连接错误。

演示 partial（保留可用文件、带未知报告期警告，退出 1）：

```bash
# Git Bash / Linux / macOS
REPORTS_MOCK_SCENARIO=partial docker compose -f integration-kit/compose.yaml run --rm client
# PowerShell
$env:REPORTS_MOCK_SCENARIO='partial'; docker compose -f integration-kit/compose.yaml run --rm client
```

演示有界超时（slow 场景 + 2 秒预算，退出 3，不会无限等待）：

```bash
# Git Bash
REPORTS_MOCK_SCENARIO=slow REPORTS_CLIENT_TIMEOUT=2 docker compose -f integration-kit/compose.yaml run --rm client
# PowerShell
$env:REPORTS_MOCK_SCENARIO='slow'; $env:REPORTS_CLIENT_TIMEOUT='2'; docker compose -f integration-kit/compose.yaml run --rm client
```

可调环境变量：`REPORTS_CLIENT_SYMBOLS`、`REPORTS_CLIENT_LAST_N`、
`REPORTS_CLIENT_TIMEOUT`、`REPORTS_CLIENT_POLL`、`REPORTS_CLIENT_OUT`、
`REPORTS_API_BASE_URL`、`REPORTS_API_TOKEN`、`REPORTS_MOCK_SCENARIO`。
`REPORTS_MOCK_SCENARIO` 是 **mock 专用**，生产环境绝不设置。

## 3. 单独复制套件到别的项目

整个 `integration-kit/` 目录是自包含的，直接复制即可：

```bash
cp -r integration-kit /path/to/your-project/
cd /path/to/your-project
docker compose -f integration-kit/compose.yaml up -d mock
docker compose -f integration-kit/compose.yaml run --rm test
```

compose 里的挂载用相对路径 `./`，会相对 `compose.yaml` 所在目录解析，因此
复制到任何位置都能跑；项目名固定，不依赖仓库根目录。若网络拉不动基础镜像，
可先 `docker pull python:3.12-slim`，或用 `BASE_IMAGE=<已加载镜像> ...` 覆盖。

## 4. 干净停止

```bash
docker compose -f integration-kit/compose.yaml stop
```

只停止容器，不删除容器、镜像、网络或任何文件。若想重新开始，再执行
`up -d mock` 即可；mock 状态是纯内存的，容器重启后清空。
（请勿使用 `down -v` / `rm`，本套件不需要也不做任何删除。）

## 5. Mock 场景与控制面（绝不发往生产）

提交任务时用请求头 `X-Mock-Scenario` 选择场景；缺省 `success`。

| 场景 | 行为 |
|---|---|
| `success` | 全部证券成功，各产出 `last_n` 份可用报告 |
| `partial` | 有可用文件，且含 `report_period=null` / `period_source=unknown` 警告；任务 `partial` |
| `failed` | `GET job` 返回 HTTP 200，但任务 `failed`，证券错误 `source_unavailable`（retryable） |
| `no_reports` | 任务 `succeeded`，证券 `no_reports`，警告 `no_matching_reports` |
| `queue_full` | 提交即 429 + `Retry-After`，不创建任务 |
| `slow` | 像 success，但 queued/running 阶段被拉长（默认 8000ms，有限预算），用于测客户端超时 |

控制面：`POST /__mock/reset`（仅清空本进程内存状态，不删文件）、
`GET /__mock/scenarios`（机器可读场景表）。场景在提交时按任务快照，不同客户端/
测试之间不会互相污染全局开关。

场景名与期望行为另见 [cases.json](cases.json)，可被任意语言复用。

## 6. Mock 能力边界（明确简化，不假装完全兼容）

- **纯内存、单进程**：无 SQLite、无真实来源适配器、无归档目录；容器重启即清空。
- **进度是时间驱动的**：queued→running→终态由 `MOCK_PHASE_MS`（默认 500ms，
  终态在 2×phase）决定，不是真实抓取；`slow` 由 `MOCK_SLOW_MS`（默认 8000ms）决定。
  它永远会到达终态，不会因意外卡死。
- **数据全部是合成 MOCK**：标题、source_id、source_url、报告期均为合成值，
  文件名/内容明确标注 `MOCK`，不是真实财报；来源域名使用保留域 `mock.invalid`。
- **未实现**：Webhook、取消任务、多租户、真实限速、job deadline/attempt 恢复、
  `refresh` 产生新 artifact 版本、CNINFO/HKEX/SEC 抓取、`/redoc`。
- **校验为基本子集**：symbol 格式仅做本地识别，重复别名按规范化结果去重（保留
  输入顺序）；`symbols` 1–50 项、单项 ≤32 字符；`forms_by_market` 做支持列表校验
  （HK 的 `QTR-HK` 仅可显式请求，不在默认值内）；未知字段、空 symbols、
  `last_n` 越界、非 JSON 媒体类型、非法 JSON 会被拒绝。
- **coverage 字段对齐权威实现**：使用 `requested/selected/total_groups/exhausted/
  truncated/searched_from/searched_to/insufficient_history/notices`；mock 不进行
  真实检索窗口，故 `searched_from/searched_to` 恒为 `null`（已文档化）。
- **progress 契约**：`symbols_total` 为规范化去重后的提交代码数（queued 起即正确，
  单代码运行中为 1），`symbols_finished` 为已持久化结果数（运行中未出结果为 0）。
- **文件响应头对齐**：`Content-Disposition` 使用可读确定名
  `{market}_{symbol}_{doc_type}_{报告期|公告日|unknown}_{report_id}.{ext}` 并带
  `filename*=UTF-8''`；历史版本追 artifact_id。mock 只保留单一 artifact，
  历史版本命名分支不在此演示。
- **period_source**：权威取值为 `source_field | explicit_title | document | unknown`；
  mock 只产生 `source_field`/`explicit_title`/`unknown`（不解析 PDF 原文，故不产生
  `document`；该值仅在真实服务的 HK 归档后富化出现）。
- **可选鉴权（简化）**：设 `MOCK_API_TOKEN` 后 `/api/v1/*` 需要
  `Authorization: Bearer <token>`（默认不启用，即本地无鉴权模式）。
- 因此：mock 用于验证**客户端行为**（轮询、幂等、错误处理、文件字节与校验），
  不用于验证服务端的持久化、恢复、限速等语义。

## 7. 切换生产（只改环境，不改代码）

生产：`chen@192.168.1.150`，服务只发布该机回环 `127.0.0.1:8000`，本地无 token 模式。
在 Windows 前台建立 SSH 隧道：

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18000:127.0.0.1:8000 chen@192.168.1.150
```

- **原生/宿主机客户端**：`REPORTS_API_BASE_URL=http://127.0.0.1:18000`。
- **Docker 容器客户端**：容器里的 `127.0.0.1` 是容器自身，不是 Windows 宿主机，
  不能直接照抄。需要把宿主网关映射进容器，例如
  `--add-host=host.docker.internal:host-gateway` 并把 base URL 设为
  `http://host.docker.internal:18000`（并确认隧道监听在宿主的 127.0.0.1，
  必要时让容器经宿主代理访问）。**不要**把容器 localhost 当成 Windows 宿主。

切换时：清空所有 `X-Mock-Scenario` 等 mock 专用头，不要调用任何 `/__mock/*`。
先用只读方式验证（`GET /health/ready`、`GET /api/v1/reports`），再做一次显式
小任务：`symbols=["AAPL"]、last_n=1、refresh=false`，并读取 `warnings`。
本次交付**没有**建立隧道、**没有**请求生产。

## 8. 文件清单

| 文件 | 用途 |
|---|---|
| [API.md](API.md) | 完整中文接口契约：路由、请求头、请求/响应、状态语义、幂等、错误码、文件下载 |
| [openapi.json](openapi.json) | 修正后的机器可读契约（含必填幂等头、problem、文件响应、304） |
| [cases.json](cases.json) | 任意语言可复用的场景与测试用例清单 |
| [AGENT_INSTRUCTIONS.md](AGENT_INSTRUCTIONS.md) | 给开发 agent 的即用提示词与阅读顺序 |
| [compose.yaml](compose.yaml) | 自包含 compose（mock / test / client） |
| `mock/server.py` | 标准库 mock 服务（含 `/__mock/*` 控制面） |
| `client/reports_client.py` | 可移植客户端库（仅标准库，生产同样可用） |
| `client/example_client.py` | 可运行示例客户端 |
| `tests/test_contract.py` | 真实 HTTP 契约测试（含延迟响应/传输超时回归） |
| `mock/fixtures/` | 明确标注 MOCK 的合成文件（HTML/PDF） |
