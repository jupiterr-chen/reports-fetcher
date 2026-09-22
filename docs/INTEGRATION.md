# 当前机器访问、Mock 与生产联调

适用于已部署 v1.0.1（76fee13）。本目录附件不改变生产服务。生产目前只发布服务器本机 127.0.0.1:8000，没有可直接从其他机器访问的局域网 HTTP 地址。

## 0. 推荐入口：本地集成套件 `integration-kit/`（命令行优先，无需 Postman）

新的自包含套件在仓库根目录 [integration-kit/](../integration-kit/README.md)：内含
本地 mock HTTP 服务（仅回环 127.0.0.1:18765）、修正后的 OpenAPI、中文接口文档、
可运行示例客户端与可执行契约测试。开发调用方可以在**完全离线**条件下完成，
最后只改 `REPORTS_API_BASE_URL` 切到生产。两条命令：

```bash
docker compose -f integration-kit/compose.yaml up -d mock
docker compose -f integration-kit/compose.yaml run --rm test
```

本地文档 <http://127.0.0.1:18765/docs>。下文第 2、3 节的 Postman 附件属于早期
方案，保留作为历史记录；新联调请优先使用 `integration-kit/`。

## 1. 在当前 Windows 机器打开文档

打开 PowerShell，执行并保持窗口开启：

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:18000:127.0.0.1:8000 chen@192.168.1.150
```

连接成功后没有输出是正常现象。浏览器访问：

- 文档：<http://127.0.0.1:18000/docs>
- 健康检查：<http://127.0.0.1:18000/health/ready>
- 机器可读定义：<http://127.0.0.1:18000/openapi.json>

这是 `当前机器:18000 → SSH → 192.168.1.150:8000`，请求到达真实生产服务。关闭 PowerShell 窗口或按 Ctrl+C 会停止隧道；重启电脑后需要重新建立。

另开 PowerShell 验证：

```powershell
Invoke-RestMethod http://127.0.0.1:18000/health/ready
```

应返回 `status=ok`。若 SSH 提示端口已占用，将命令左侧 18000 改成 18001，浏览器及客户端 baseUrl 同步改为 18001。若连接被拒绝，先看 SSH 窗口是否退出；如果 health 和 openapi 正常但 docs 空白，可能是 Swagger UI 的 CDN 静态资源不可达，可以使用下面的本地接口定义导入调试工具。

本次尝试由 agent 创建后台隧道时，自动审批拒绝执行，未给出具体原因，因此没有自动创建隧道。用户手动执行以上命令即可；本说明不代表隧道已经运行。

## 2. 已准备的导入附件

| 文件 | 用途 |
|---|---|
| [reports-fetcher.postman_collection.json](integration/reports-fetcher.postman_collection.json) | 6 个请求、带具名响应示例，含必填幂等头；推荐作为联调入口 |
| [mock.postman_environment.json](integration/mock.postman_environment.json) | Mock 环境模板；需要填入你创建的 mock server URL |
| [production-tunnel.postman_environment.json](integration/production-tunnel.postman_environment.json) | 生产隧道环境，baseUrl 为 http://127.0.0.1:18000 |
| [openapi.production.json](integration/openapi.production.json) | 从运行中的 v1.0.1 原样导出的 OpenAPI，可供支持 OpenAPI 3.1 的工具导入 |

Collection 没有自动执行脚本、没有密钥，默认指向 mock 占位域名，不会自动向生产发任务。只创建了本地文件，未向 Postman 云端上传、未创建或启动 mock server。

当前自动 OpenAPI 存在文档缺口：未声明必填 Idempotency-Key、终态重放的 200 和全部业务错误，422 仍是框架默认 schema，文件响应描述也不完整。导入后不能把自动生成示例视为完整行为契约；Collection 已补上主要示例，最终以 [HTTP_API.md](../HTTP_API.md) 及实际响应为准。

## 3. Mock 调试：先验证调用方行为

1. 在 Postman Import 中导入 Collection 和两个环境 JSON。
2. 使用该 Collection 创建一个 Mock Server，复制工具返回的 URL，填入 MOCK 环境的 `baseUrl`。这里的 baseUrl 只放协议和主机，不含 `/api/v1`。
3. 选择 MOCK 环境。Collection 的请求自带禁用的 `x-mock-response-name` 头；仅在 mock 环境启用，并填入下表中的具体示例名。
4. `jobId=job_mock_001`、`reportId=mock_report_001` 保留默认值。先发提交请求，再按需选择轮询响应场景。

| 请求 | x-mock-response-name | 检查调用方 |
|---|---|---|
| 02 Submit | submit-queued | 接收 202，保存 job_id/status_url，开始轮询 |
| 02 Submit | submit-replay-finished | 接受 200 重放结果，不误判提交失败 |
| 02 Submit | submit-missing-key / submit-invalid | 正确显示 400 / 422，不盲目重试 |
| 02 Submit | submit-conflict | 409 时检查 key 与请求体对应关系 |
| 02 Submit | submit-queue-full | 429 时读取 Retry-After，稍后重试 |
| 03 Poll | job-queued / job-running | 继续等待，限制轮询频率 |
| 03 Poll | job-succeeded | 停止轮询，使用 report_ids |
| 03 Poll | job-partial-unknown-period | 停止轮询，保留可用文件与质量警告 |
| 03 Poll | job-failed | HTTP 200 中的业务失败，停止轮询并读取明细 |
| 03 Poll | job-no-reports | 正常结束但无匹配报告，不无限等待 |
| 04 List | reports-one / reports-empty | 有档案与空列表分别展示 |
| 05 Metadata | report-detail / report-detail-unknown-period | report_period=null 能正常处理；partial 场景搭配后者 |
| 06 File | file-html | 按二进制/文件处理响应，不尝试 JSON 解析 |

这些是静态示例，不会自动模拟数据库、来源抓取或 queued→running→终态的时间推进。调状态时切换 `x-mock-response-name`；自动化测试可在调用方的 HTTP stub 中按顺序返回这些示例。文件示例是明确标注 MOCK 的 78 字节 HTML，不是真实财报；其字节数和 SHA-256 与示例 metadata 一致。

如果选择 private Postman mock，按工具要求使用 Postman 的 `x-api-key`。它只用于 mock 服务，不能作为生产应用令牌，也不要在切换生产时发送。

Postman 官方说明：[从 Collection/示例创建 Mock Server](https://learning.postman.com/v11/docs/design-apis/mock-apis/set-up-mock-servers)、[按响应名选择示例](https://learning.postman.com/docs/design-apis/mock-apis/matching-algorithm)、[私有 Mock 调用](https://learning.postman.com/docs/design-apis/mock-apis/mock-server-calls)。

## 4. 接口对接的最小流程

调用方配置 `REPORTS_API_BASE_URL`（不带 /api/v1）和可选的应用 token，业务代码只使用这一配置拼接地址。

1. POST `/api/v1/fetch-jobs`，Content-Type=application/json，必传 `Idempotency-Key`。
2. 保存 job_id，按 status_url 轮询；2 秒开始，逐步退避到 10 秒，设置调用方整体等待上限。
3. `succeeded`、`partial`、`failed` 都是终态，读到就停止轮询。`GET job` 的 HTTP 200 仅表示查询成功。
4. 从 results[].report_ids 获取可用报告；必要时查询 metadata，再 GET `/api/v1/reports/{report_id}/file` 下载。
5. `GET /api/v1/reports` 只查已有档案，不触发来源抓取。不要用它替代 POST 抓取任务。

每次新的业务动作生成一个幂等 key；同一动作因网络超时重试时保留原 key 和相同 body。改变查询参数时用新 key。不要在每一次网络重试时自动生成新的 UUID，否则会产生重复任务。

`last_n` 是最多 N 组财报，不等于 N 个季度。`refresh=false` 保留默认值；真实调试一般无需强制重新下载。`partial` 可能只有日期未知等警告，不应把所有文件都丢弃；根据 report_ids、items 和 warnings 决定后续操作。

后端应用可直接使用 HTTP 客户端。若调用方是浏览器前端，建议由自己的后端代理调用；目前服务未配置跨域 CORS，应用凭据也不应写进浏览器代码。本机 Docker 中的 127.0.0.1 指容器自身，不能直接照抄宿主机地址，需要使用容器到宿主的网络入口。

## 5. 切换生产并实际验证

先确保 SSH 隧道已建立。Postman 选择 `PRODUCTION via SSH` 环境，禁用所有 `x-mock-*` 和 mock 专用 `x-api-key` 头。当前生产回环模式不用 Authorization；以后启用应用认证时再配置 Bearer token。

先做只读验证：在 03 Poll 的 `jobId` 填入已验收任务 `job_85794434f82f40cfbbb6`，或在 05/06 的 `reportId` 填入已有 AAPL 报告 `7b726888c37d9415aafd`，即可查询真实结果与下载。不要把 mock ID 直接用于生产。

随后做一次新任务联调：选择 02 Submit，保持默认 `symbols=["AAPL"]`、`last_n=1`、`refresh=false`，将 idempotencyKey 改成应用自己的唯一 key，发送并把返回 job_id 填入环境变量。重复发送相同 body/key 应返回同一 job_id；第一次返回 202，终态重放返回 200。

不用 Postman 时，可在隧道运行期间在另一个 PowerShell 窗口执行：

```powershell
$apiBase = 'http://127.0.0.1:18000'
$requestHeaders = @{ 'Idempotency-Key' = "myapp-$([guid]::NewGuid())" }
$requestBody = @{ symbols = @('AAPL'); last_n = 1; refresh = $false } | ConvertTo-Json
$acceptedJob = Invoke-RestMethod -Method Post -Uri "$apiBase/api/v1/fetch-jobs" -ContentType 'application/json' -Headers $requestHeaders -Body $requestBody
$acceptedJob

# 隔几秒查询一次，直到 status 是 succeeded / partial / failed。
$jobStatus = Invoke-RestMethod -Uri "$apiBase$($acceptedJob.status_url)"
$jobStatus | ConvertTo-Json -Depth 12

# 有可用 report_ids 时才下载；后缀按 metadata/Content-Type 决定。
$availableReports = @($jobStatus.results | ForEach-Object { $_.report_ids })
if ($availableReports.Count -gt 0) {
    $reportId = $availableReports[0]
    Invoke-RestMethod -Uri "$apiBase/api/v1/reports/$reportId"
    $reportOutput = Join-Path $env:TEMP "$reportId-$([guid]::NewGuid()).bin"
    Invoke-WebRequest -Uri "$apiBase/api/v1/reports/$reportId/file" -OutFile $reportOutput
    Write-Output "Downloaded: $reportOutput"
}
```

要重试提交，请重用已经生成的 `$requestHeaders` 与 `$requestBody`，不要重新执行生成 UUID 的那一行。前面的示例故意不无限自动轮询；手工反复执行查询行即可。

现有服务器没有直接对局域网开放的生产 URL；`http://127.0.0.1:18000` 已经是在验证生产，只是通过 SSH 转发。若未来要求各机器长期直连 `192.168.1.150`，需要单独配置稳定的 HTTPS 入口和应用令牌，并切换 baseUrl；本次没有改变服务器监听或认证配置。
