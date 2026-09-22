# 一期 HTTP 服务契约

日期：2026-09-20；**I5 已实现（2026-09-21，v0.5.0）**，验收记录见 [ITERATION_PLAN.md](./ITERATION_PLAN.md) I5 状态；需求基线 [REQUIREMENTS.md](./REQUIREMENTS.md)。上游：[ARCHITECTURE.md](./ARCHITECTURE.md)；内部设计：[DESIGN.md](./DESIGN.md)。实现注记：本地无鉴权模式适用于回环监听或部署侧显式声明端口仅发布回环（环境变量 `RF_LOCAL_MODE=1`，compose serve 默认）；令牌经环境变量 `RF_API_TOKENS=client:token,…` 注入。

## 1. 交付范围与运行方式

其他应用通过 HTTP 提交财报获取任务，查询进度、检索已归档报告、下载原文。接口前缀 `/api/v1`，JSON UTF-8；OpenAPI 及交互文档分别位于 `/openapi.json`、`/docs`。实现建议 FastAPI + Uvicorn；同步 requests 在后台线程执行，不阻塞 HTTP 事件循环。主力以 Docker 容器交付（Dockerfile server 目标 + compose serve 服务，端口仅发布到宿主 127.0.0.1，见架构 §3）。

P0 为单进程、单归档目录、单使用方服务，可有多个已授权应用客户端。SQLite 保存任务、任务明细和归档元数据，一个执行器依次消费任务；每个任务内部最多三个市场线程。暂不提供 Webhook、取消任务、多租户隔离、分布式队列或公开文件托管。

## 2. 接口清单

| 方法 | 路径 | 语义 / 成功状态 |
|---|---|---|
| POST | `/api/v1/fetch-jobs` | 提交抓取任务；持久化成功后返回 202 |
| GET | `/api/v1/fetch-jobs/{job_id}` | 查询状态、进度、各证券结果和报告 ID；200 |
| GET | `/api/v1/reports` | 查询本地档案，绝不隐式联网抓取；200 |
| GET | `/api/v1/reports/{report_id}` | 元数据、所有已保存文件版本、来源和警告；200 |
| GET | `/api/v1/reports/{report_id}/file` | 下载该报告当前可用文件；200；可指定 `artifact_id` 获取旧版本 |
| GET | `/health/live` | 进程存活；200 |
| GET | `/health/ready` | 数据库、归档目录和执行器可用；200 / 503，不探测外部站点 |

“远程发现报告但不下载”一期保留 CLI `list`，不混入 GET 档案查询。未来如需要，可单独增加有明确网络语义的检索任务。

## 3. 提交与轮询示例

```http
POST /api/v1/fetch-jobs
Authorization: Bearer <configured-token>
Content-Type: application/json
Idempotency-Key: app-a-20260920-001

{
  "symbols": ["600519", "0700.HK", "AAPL"],
  "last_n": 4,
  "forms_by_market": {
    "CN": ["Q1", "H1", "Q3", "FY"],
    "HK": ["ANNUAL", "INTERIM"],
    "US": ["10-Q", "10-K", "20-F"]
  },
  "refresh": false
}
```

- `symbols`：1–50 项，单项最多 32 字符；规范化后重复项合并，结果保留输入别名。格式检查在提交时做，联网 resolve 在执行时做。
- `last_n`：默认 4，范围 1–20；按设计中的报告分组和最新版本规则选择，不保证有 N 个财季。
- `forms_by_market`：可省略；显式市场及类型必须在服务支持列表内；省略的市场用默认值。空数组、未知类型、一期未支持的 6-K/北交所，以及经 fixture 验证启用前的 QTR-HK，显式请求返回 422，不静默忽略。`10-K` 等基础类型同时匹配其可识别修订版。
- `refresh`：默认 false；true 重新验证并下载候选文件，保留旧文件版本；不改变报告组选择规则。
- 请求体上限默认 64 KiB；拒绝未知字段。HTTP 不接受客户端 URL、Cookie、代理、任意 headers、输出目录、本地路径或配置文件路径。

```http
HTTP/1.1 202 Accepted
Location: /api/v1/fetch-jobs/job_example
Retry-After: 2
X-Request-ID: req_example

{
  "job_id": "job_example",
  "status": "queued",
  "submitted_at": "2026-09-20T03:00:00Z",
  "status_url": "/api/v1/fetch-jobs/job_example"
}
```

轮询建议从 2 秒逐步退避至 10 秒，读到终态后停止。客户端超时可用原幂等键重试提交。

```json
{
  "job_id": "job_example",
  "status": "partial",
  "progress": {"symbols_total": 3, "symbols_finished": 3},
  "summary": {"downloaded": 4, "cached": 4, "failed": 0},
  "results": [
    {"market": "CN", "symbol": "600519", "status": "succeeded", "report_ids": ["r_cn_1", "r_cn_2", "r_cn_3", "r_cn_4"], "warnings": []},
    {"market": "HK", "symbol": "00700", "status": "succeeded", "report_ids": ["r_hk_1", "r_hk_2", "r_hk_3", "r_hk_4"], "warnings": []},
    {"market": "US", "symbol": "AAPL", "status": "failed", "report_ids": [], "error": {"code": "source_unavailable", "retryable": true}}
  ]
}
```

以上 ID 为示意；`summary` 统计文件处理结果，证券 resolve/list 阶段失败另见 `results`，因此不强求文件失败数等于证券失败数。单证券的 `items` 还应逐条给出 `report_id`（若已发现）、`source_id`、`status=downloaded|cached|failed` 和稳定错误码。进度在发现完成前不承诺总文件数。

`progress.symbols_total` 等于提交时规范化去重后的代码数，从 queued 起即正确（不依赖已持久化结果）；`progress.symbols_finished` 只统计已持久化的证券结果。因此单代码任务在 queued/running 且尚无结果时应返回 `{"symbols_total": 1, "symbols_finished": 0}`，终态时 finished 等于该任务实际产出的证券结果数。

质量警告只针对**选中且产出可用文件**的报告：未选入 `last_n` 的候选报告期未知等信息在 `coverage.notices` 聚合至多一次，不逐条进入 `warnings`。HK 归档 PDF 若在抓取后能从原文提取明确期末日，将补写 `report_period`/`period_source=document` 并撤下陈旧的未知期警告；已成功补全的报告不因该陈旧警告降级为 partial。`filing_date` 仅可作为文件名回退，绝不作 `report_period` 来源。

## 4. 状态、幂等与重启

任务状态：`queued → running → succeeded | partial | failed`。证券状态同样使用 succeeded/partial/failed，另允许 `no_reports`。

- `succeeded`：各证券处理完成，无失败或覆盖警告；已有完整文件命中缓存算成功。
- `partial`：至少有可用结果，同时存在失败、无报告、检索被截断或报告期不明等质量警告。
- `failed`：没有可用报告且至少发生一个执行错误。
- 全部证券正常检索但没有匹配报告：任务 succeeded，证券 no_reports，并给出 `no_matching_reports` 说明；不足 N 份但有文件：partial，说明 `insufficient_history`。所有终态都须提供结果和覆盖信息，不能把源站错误归为 no_reports。

必传 `Idempotency-Key`（1–128 个可打印 ASCII 字符）。唯一键为 `(client_id, idempotency_key)`；规范化参数及生效默认值生成 request_hash，连同新任务在同一事务写入。相同键/相同请求返回原任务，未结束返回 202、已结束返回 200；相同键/不同请求返回 409。已保存的生效默认值用于重放比较，部署配置改变不能改变老任务含义。P0 保留任务与幂等记录，不自动过期清理；存储压力下拒绝新任务并提示运维处理。

请求幂等与文件去重是两回事：不同键可以产生不同任务，但文件以 source_id、已有完成状态及文件验证避免重复归档。默认最多 100 个 queued/running 任务，超过返回 429 + Retry-After。计数检查、幂等查找和入队在事务中完成。

任务落库后才能返回 202。执行器从 SQLite 取任务，不能仅依赖进程内 BackgroundTasks。进程重启后将遗留 running 重置为 queued，保留已完成明细，按归档恢复规则继续，attempt 加一。P0 单实例锁确保不会抢占仍在运行的实例。执行是可重试的，归档提交幂等，不宣称端到端恰好一次。

默认单任务总时限 30 分钟、最大执行尝试 3 次（含中断恢复），均可由运维配置。第一次领取时写入 started_at/deadline，排队时间不计入执行时限；重启恢复不延长原 deadline。到限后保留已完成结果，将未完成项记为 job_deadline_exceeded/recovery_exhausted，按结果聚合终态，不能永久停在 running。

## 5. 档案查询与文件响应

`GET /api/v1/reports?market=HK&symbol=00700&limit=20&cursor=...` 支持市场、规范化代码、类型、期末日期区间过滤。`limit` 默认 20，最大 100。只查询归档元数据；返回 `items` 与不透明 `next_cursor`。按不可变入库序号倒序分页，游标固定首屏最大序号并绑定筛选条件，翻页期间新增档案不会造成重复。按期末过滤时排除未知期末的记录。

报告字段至少包括：`report_id`、`market`、`symbol`、`issuer_id`、`source_id`、`source_url`、`title`、`doc_type`、`report_period`（可 null）、`period_source`、`filing_date`、`language`、`is_amendment`、`status`、`artifact_id`、`sha256`、`bytes`、`fetched_at`、`warnings`、`download_url`。GET reports 默认展示已有可用文件的记录；任务明细负责未完成/失败结果，详情可展示已发现但尚无文件的记录。相同报告期不同披露版本可以同时出现，不能把分页列表误认为 N 个逻辑报告组。

`report_id` 是 manifest 持久化的不透明标识，重试不变；不同 source_id 是不同报告。`artifact_id` 表示同一来源文件的某次内容版本，绑定 checksum。响应不泄露服务器绝对路径。抓取时间为 UTC RFC 3339，日期值为 YYYY-MM-DD，不把 filing_date 当 report_period。

文件必须由 ID 查数据库映射到受控目录，规范化路径并检查包含关系与链接目标，不能把客户端参数拼接为路径。默认下载当前已验证文件，可指定属于该报告的 artifact_id；不存在返回 404，尚未完成或文件缺失返回 409 `file_not_available`，不隐式触发抓取。

响应含正确 Content-Type、Content-Length、`Content-Disposition: attachment`、`X-Content-Type-Options: nosniff`、基于 SHA-256 的 ETag；匹配 If-None-Match 返回 304。`Content-Disposition` 使用可读且确定的名称 `{market}_{symbol}_{doc_type}_{报告期|公告日|unknown}_{report_id}.{ext}`（同时给出 ASCII 回退与 `filename*=UTF-8''`），历史版本（非当前 artifact）追加 artifact_id 以避免歧义；不使用中文标题前缀。HTM 默认附件下载，不在服务同源执行脚本；一期不保证离线恢复原站的图片/CSS/链接资源。

## 6. 统一错误契约

错误使用 `application/problem+json`，采用 [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html) 的基础字段，扩展 `code`、`request_id`、`retryable`、`errors`（可选字段级校验信息）。框架默认校验异常也转换为同一格式。

```json
{
  "type": "about:blank",
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "last_n must be between 1 and 20",
  "instance": "/api/v1/fetch-jobs",
  "code": "invalid_request",
  "request_id": "req_example",
  "retryable": false
}
```

| 状态 | 场景 |
|---|---|
| 400 | 无效 JSON、缺失/格式错误的幂等键、无效游标 |
| 401 / 403 | 凭据缺失或无效 / 有身份但无权限；401 带 WWW-Authenticate |
| 404 | job/report/artifact 不存在 |
| 409 | 幂等键参数冲突、文件当前不可用 |
| 413 / 415 | 请求体超限 / 非 JSON 提交 |
| 422 | 参数值或组合不受支持 |
| 429 | 队列或客户端配额用尽，附 Retry-After |
| 503 | 存储或执行器尚不可用；入队失败不得返回任务已接受 |
| 500 | 未预期内部错误；不返回堆栈、凭据或本地路径 |

已接受任务的源站 403/429/网络失败记录在任务结果，不把它映射成查询接口自身失败；GET job 在任务 failed 时仍返回 200。

## 7. 配置与访问边界

容器内监听 `0.0.0.0`，默认仅发布到宿主 `127.0.0.1`（compose 端口映射）；非回环发布必须配置应用令牌与 HTTPS（可由反向代理终止），启动检查拒绝裸露无鉴权配置。令牌通过环境变量/外部机密配置加载，每个应用映射 client_id；不写入 URL 或日志。回环地址可显式启用本地无鉴权模式，此时使用固定 client_id=local，所有本地调用共享任务及幂等键命名空间。P0 授权应用共享同一个档案库，可读取共享报告，任务读权限限定为提交应用。健康接口仅返回简要状态；docs/OpenAPI 在非回环部署同样受保护。

不接受用户指定抓取 URL；来源适配器产生的 URL、每次重定向目标都检查 HTTPS、官方域名允许列表和非私网目标，允许列表根据实测回填；禁止把认证令牌转发给源站。关闭任意来源 CORS，只按实际调用应用配置。

## 8. 一期接口验收

1. 提交 → 202/Location → 轮询终态 → reports 查询 → ID 下载完整文件，CLI/API 选出相同报告。
2. 同键同参数并发提交仅有一个任务；同键不同参数 409；不同键重复抓取复用文件。
3. 任务入队后重启不丢任务；下载后、数据库完成前崩溃可恢复；一条失败不阻止其他证券。
4. 覆盖无报告、历史不足、部分成功、源站限流/错误页、无鉴权、超量参数、未知字段和目录越界。
5. 慢下载期间健康检查和任务查询仍能返回；所有实际源站请求（含分页、重试、重定向）共享限速。
6. OpenAPI 的示例、字段、枚举、状态码与实际响应一致；保留旧 artifact 后可通过 ID 读取并校验 checksum。

FastAPI 的 OpenAPI/JSON Schema 支持见[官方文档](https://fastapi.tiangolo.com/features/)。框架选型不替代上述持久化、权限和任务语义的实现。
