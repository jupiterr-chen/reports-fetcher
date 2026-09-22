# 给开发 agent 的即用提示词与联调指南

本文件可以直接粘贴给负责开发 HTTP 客户端的 AI agent。目标：**完全离线**用本地
mock 把客户端开发、测试完，最后只改环境变量切到生产。

---

## 一、可直接使用的提示词（复制给 agent）

> 你在为一个服务开发 HTTP 客户端。服务是 reports-fetcher，接口前缀 `/api/v1`，
> 契约见本目录 `API.md` 与 `openapi.json`。
>
> 请按顺序阅读：`README.md` → `API.md` → `openapi.json` → `cases.json`。
> 然后：
> 1. 在本机启动 mock：`docker compose -f integration-kit/compose.yaml up -d mock`；
>    文档 <http://127.0.0.1:18765/docs>，契约
>    <http://127.0.0.1:18765/openapi.json>。
> 2. 用任意语言实现一个客户端类，配置只有 `base_url` 和可选 `token`。
> 3. 实现并自测以下行为（对应 `cases.json` 的 `test_cases`）：
>    - 提交任务（必带 `Idempotency-Key`）→ 有界轮询到终态；
>    - 正确处理 **HTTP 200 但任务 `failed`**；
>    - `partial` 时**保留可用文件**，读取 `warnings` 与 `report_period=null`；
>    - 下载文件字节并核对 `sha256` 与 `ETag`，支持 `If-None-Match` → 304；
>    - 幂等：同键同请求复用同一 `job_id`；同键异请求识别 409；
>    - 错误：400/415/422/429 的 `application/problem+json`，429 读 `Retry-After`；
>    - 有界等待：对 `slow` 场景在预算内超时退出，不无限轮询。
> 4. 用 mock 专用头 `X-Mock-Scenario` 覆盖 `success/partial/failed/no_reports/
>    queue_full/slow` 场景自测；**这些头与 `/__mock/*` 绝不能进入生产代码路径**。
> 5. 完成后，把 base URL 从 `http://127.0.0.1:18765` 改成生产地址即可，
>    代码不变。
>
> 约束：不要实现任何来源适配器（SEC/HKEX/CNINFO），不要引入 Postman，
> 不要访问外网，不要修改 mock 服务与主项目业务代码。

---

## 二、阅读顺序与用途

| 顺序 | 文件 | 你要从中得到什么 |
|---|---|---|
| 1 | `README.md` | 两条命令、示例客户端、复制套件、生产切换 |
| 2 | `API.md` | 精确的路由、请求头、字段、状态语义、错误映射 |
| 3 | `openapi.json` | 机器可读契约（含必填幂等头、problem、文件响应、304） |
| 4 | `cases.json` | 与语言无关的场景表与测试用例清单 |
| 5 | `client/example_client.py` | 参考实现（提交→轮询→下载→校验→退出码） |

## 三、必须覆盖的集成测试用例

在**真实 mock HTTP 端点**上跑（不要只测内部函数）：

1. 健康：`/health/live`、`/health/ready` 返回 200。
2. happy path：202 → 轮询到 `succeeded` → 列表 → 详情 → 下载 → `sha256` 匹配。
3. 进度：能观察到 `queued`、`running`、终态。
4. 幂等重放：同键同请求 pending 202 / terminal 200，`job_id` 不变。
5. 冲突：同键异请求 409 `idempotency_conflict`。
6. 校验：缺 key 400、超长 key 400、空 symbols 422、`last_n` 越界 422、
   未知字段 422、非 JSON 415、非法 JSON 422。
7. `partial`：终态 `partial`，含未知报告期警告，**仍下载到可用文件**。
8. `failed`：`GET job` HTTP 200，`status=failed`，`error.retryable=true`。
9. `no_reports`：终态 `succeeded`，证券 `no_reports`。
10. `queue_full`：429 + `Retry-After` + `code=queue_full`。
11. 未知 ID：job/report/file 均 404 `not_found`；artifact 不属该报告 404。
12. 条件下载：`If-None-Match` 命中 → 304（无 body）。
13. 有界超时：`slow` 场景 + 短预算 → 客户端按时退出。
14. 有效默认值幂等：省略 forms 与显式默认 forms 视为同一请求。

## 四、切到生产：只改环境

- 隧道（Windows 前台）：`ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18000:127.0.0.1:8000 chen@192.168.1.150`
- 宿主机/原生客户端：`REPORTS_API_BASE_URL=http://127.0.0.1:18000`
- Docker 客户端：容器 `localhost` 不是 Windows 宿主，需宿主网关映射
  （如 `host.docker.internal`）。不要把容器 localhost 当宿主。
- 生产目前本地无 token；将来启用认证再配 `REPORTS_API_TOKEN`。
- 切生产时清空所有 `X-Mock-Scenario`，不得调用 `/__mock/*`。
- 首次验证用显式只读/小任务：`GET /health/ready`、`GET /api/v1/reports`，
  再提交 `symbols=["AAPL"], last_n=1, refresh=false` 并读取 `warnings`。

## 五、明确不做的事

- 不实现任何来源适配器（不接触 SEC/HKEX/CNINFO），客户端只调用 HTTP 接口。
- 不强制使用 Postman；本套件是命令行/代码优先。
- 不把 mock 的合成数据当作真实财报。
- 不在生产无条件调用 `/__mock/reset` 或发送 `X-Mock-Scenario`。
