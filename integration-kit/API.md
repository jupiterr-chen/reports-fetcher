# reports-fetcher HTTP 接口契约（v1.0.3，联调版）

本文档面向**调用方**，与本地 mock 和真实生产**同一套契约**。字段形状以运行实现
（`reports_fetcher/api_models.py`、`reports_fetcher/api.py`、`reports_fetcher/jobs.py`）
为准；机器可读定义见 [openapi.json](openapi.json)。

- 前缀：`/api/v1`；编码：UTF-8 JSON。
- Base URL 由 `REPORTS_API_BASE_URL` 提供，**不含** `/api/v1`。
- 鉴权：本地回环模式无需令牌；令牌模式用 `Authorization: Bearer <token>`。
- 错误统一 `application/problem+json`（RFC 9457 + `code`/`request_id`/`retryable`）。

## 1. 路由一览

| 方法 | 路径 | 成功状态 | 语义 |
|---|---|---|---|
| POST | `/api/v1/fetch-jobs` | 202 / 200 | 提交抓取任务；落库后 202，幂等重放已终态返回 200 |
| GET | `/api/v1/fetch-jobs/{job_id}` | 200 | 查询任务状态、进度、各证券结果与报告 ID |
| GET | `/api/v1/reports` | 200 | 查询本地档案，**绝不隐式联网** |
| GET | `/api/v1/reports/{report_id}` | 200 | 报告元数据与文件版本 |
| GET | `/api/v1/reports/{report_id}/file` | 200 / 304 | 下载当前（或指定 `artifact_id`）文件 |
| GET | `/health/live` | 200 | 进程存活 |
| GET | `/health/ready` | 200 / 503 | 数据库/目录/执行器可用；不探测外部站点 |

## 2. 必填/常用请求头

| 头 | 位置 | 说明 |
|---|---|---|
| `Idempotency-Key` | POST fetch-jobs | **必填**，1–128 个可打印 ASCII 字符 |
| `Content-Type: application/json` | POST fetch-jobs | 非 JSON 返回 415 |
| `Authorization: Bearer <token>` | 全部 `/api/v1/*` | 仅令牌模式 |
| `X-Request-ID` | 全部 | 可选；会回显到响应与 problem 体 |
| `If-None-Match` | 文件 GET | 命中 ETag 返回 304 |
| `X-Mock-Scenario` | **仅 mock** | 绝不可发往生产，见 §9 |

## 3. 提交任务 `POST /api/v1/fetch-jobs`

请求体（拒绝未知字段）：

```json
{
  "symbols": ["600519", "0700.HK", "AAPL"],
  "last_n": 4,
  "forms_by_market": {
    "CN": ["Q1", "H1", "Q3", "FY"],
    "HK": ["ANNUAL", "INTERIM", "QTR-HK"],
    "US": ["10-Q", "10-K", "20-F"]
  },
  "refresh": false
}
```

| 字段 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `symbols` | string[] | — | 1–50 项，单项 ≤32 字符；规范化后去重合并 |
| `last_n` | int | 4 | 1–20；是“最多 N 个逻辑报告组”，**不等于 N 个财季** |
| `forms_by_market` | object\|null | 省略 | 键限 CN/HK/US；值非空且在支持列表内；空数组/未知类型 422。省略时的生效默认：CN `Q1/H1/Q3/FY`、HK `ANNUAL/INTERIM/QTR-HK`（v1.0.3 起）、US `10-Q/10-K/20-F` |
| `refresh` | bool | false | true 时重新验证/下载候选并保留旧版本 |

提交期只做格式检查；联网解析在执行期进行。`symbols` 为空、`last_n` 越界、
未知字段、非法 JSON、非法媒体类型都会被拒绝（见 §7）。

成功响应（新建或重放未终态任务）：

```http
HTTP/1.1 202 Accepted
Location: /api/v1/fetch-jobs/job_example
Retry-After: 2
X-Request-ID: req_example
Content-Type: application/json

{
  "job_id": "job_example",
  "status": "queued",
  "submitted_at": "2026-09-20T03:00:00+00:00",
  "status_url": "/api/v1/fetch-jobs/job_example"
}
```

**幂等重放**：相同 `(client_id, Idempotency-Key)` 且有效请求相同时返回原任务；
任务未结束返回 **202**，已结束返回 **200**（body 仍是 `JobAccepted`）。
相同键但请求不同返回 **409** `idempotency_conflict`。

**有效默认值参与比较**：省略 `last_n`/`refresh`/`forms_by_market` 时，会先补齐为
生效默认值再计算请求哈希。因此“省略 forms”与“显式写默认 forms”视为同一请求。

## 4. 查询任务 `GET /api/v1/fetch-jobs/{job_id}`

返回 `JobStatusOut`：

```json
{
  "job_id": "job_example",
  "status": "partial",
  "attempt": 1,
  "submitted_at": "2026-09-20T03:00:00+00:00",
  "started_at": "2026-09-20T03:00:01+00:00",
  "finished_at": "2026-09-20T03:00:04+00:00",
  "deadline": "2026-09-20T03:30:00+00:00",
  "progress": {"symbols_total": 2, "symbols_finished": 2},
  "summary": {"downloaded": 3, "cached": 1, "failed": 0},
  "results": [
    {
      "market": "CN",
      "symbol": "600519",
      "display_name": null,
      "status": "succeeded",
      "report_ids": ["r_cn_1", "r_cn_2"],
      "items": [
        {"report_id": "r_cn_1", "source_id": "cn:...", "status": "downloaded",
         "artifact_id": "a_cn_1", "error": null}
      ],
      "coverage": {"requested": 4, "selected": 2, "total_groups": 2,
                   "exhausted": true, "truncated": false,
                   "searched_from": null, "searched_to": null,
                   "insufficient_history": false, "notices": []},
      "warnings": [],
      "error": null
    },
    {
      "market": "HK",
      "symbol": "00700",
      "status": "partial",
      "report_ids": ["r_hk_1"],
      "items": [],
      "coverage": {"requested": 4, "selected": 1, "total_groups": 1,
                   "exhausted": true, "truncated": false,
                   "searched_from": null, "searched_to": null,
                   "insufficient_history": false, "notices": []},
      "warnings": ["报告期未知（period_source=unknown）"],
      "error": null
    }
  ]
}
```

**关键语义**：

- `status` 取值：`queued → running → succeeded | partial | failed`。
- **HTTP 200 ≠ 任务成功**。即使任务 `failed`，查询接口仍返回 200；
  必须读 `status` 与 `results[].error`，不能只看状态码。
- 证券 `status`：`succeeded | partial | failed | no_reports`。
- `items[].status`：`downloaded | cached | failed`（文件级结果）。
- `summary` 统计文件处理结果；证券级 resolve/list 失败另见 `results`，
  因此文件失败数不必然等于证券失败数。
- `progress.symbols_total` 是提交时规范化去重后的代码数，**从 queued 起即正确**
  （不依赖已持久化结果）；`progress.symbols_finished` 只统计已持久化的证券结果。
  单代码任务在 queued/running 且尚无结果时返回 `{"symbols_total":1,
  "symbols_finished":0}`，终态时 finished 等于实际产出的证券结果数。
- 质量 `warnings` 只针对选中且产出可用文件的报告；未选入 `last_n` 的候选
  报告期未知等信息在 `coverage.notices` 聚合至多一次，不逐条进入 `warnings`。
  若 HK 归档 PDF 能在抓取后提取明确期末日，`period_source` 会是 `document`
  且不再给出未知期警告（见 `report_period`/`period_source` 枚举）。
- `coverage` 为开放字典，权威字段名（v1.0.3 `core.py`）为
  `requested / selected / total_groups / exhausted / truncated /
  searched_from / searched_to / insufficient_history / notices`；
  **没有 `returned` 字段**。mock 中 `searched_from/searched_to` 恒为 `null`
  （不进行真实检索窗口），属于已文档化的简化，见 README §6。
- **HK 季度语义（v1.0.3）**：省略 forms 时 HK 默认含 `QTR-HK`（自愿披露的季度业绩）。
  发行人没有季度材料时正常跳过——不算错误、无“缺少季报”警告，年报/中报照常填满
  `last_n`。显式 `{"HK":["ANNUAL","INTERIM"]}` 只取完整报告；显式
  `{"HK":["QTR-HK"]}` 只取季度业绩，对无季度披露发行人是正常的
  `no_reports`/`no_matching_reports` 结果（不是失败）。省略 forms 与显式
  写出生效默认值视为同一请求；显式旧类型集合是不同请求（同键 409）。
- `report_period` 可能为 `null`（未知即 null，禁止猜测），此时
  `period_source="unknown"` 且 `warnings` 给出说明；**这类报告仍可能有可用文件**。
  `period_source` 取值：`source_field | explicit_title | document | unknown`；
  `filing_date` 只作文件名回退，绝不作报告期来源。

## 5. 档案查询

### `GET /api/v1/reports`

查询参数：`market`（CN/HK/US）、`symbol`、`doc_type`、`period_from`、
`period_to`（YYYY-MM-DD）、`limit`（默认 20，1–100）、`cursor`（不透明游标）。
返回：

```json
{
  "items": [
    {
      "report_id": "r_...", "market": "US", "symbol": "AAPL",
      "issuer_id": null, "source_id": "mock:...",
      "source_url": "https://mock.invalid/...",
      "title": "[MOCK] AAPL 10-Q ...",
      "doc_type": "10-Q", "report_period": "2026-06-30",
      "period_source": "source_field", "filing_date": "2026-08-14",
      "language": "en", "is_amendment": false, "status": "done",
      "artifact_id": "a_...", "sha256": "...", "bytes": 711,
      "fetched_at": "2026-09-20T03:00:04+00:00", "warnings": [],
      "download_url": "/api/v1/reports/r_.../file"
    }
  ],
  "next_cursor": null
}
```

- **只读**：该接口绝不创建任务、绝不联网抓取。
- 分页：按入库序号倒序；`next_cursor` 不透明，游标绑定筛选条件，
  翻页期间新增档案不会造成重复。
- 按期末过滤时排除 `report_period` 为 null 的记录。
- 非法游标 400 `invalid_cursor`；`limit` 越界/未知市场/非法日期 422。

### `GET /api/v1/reports/{report_id}`

返回 `ReportDetailOut`（含 `artifacts[]` 各版本、`current_artifact_id`、
`warnings`）。`report_period` 可为 `null`。未知 ID 返回 404 `not_found`。

## 6. 文件下载 `GET /api/v1/reports/{report_id}/file`

- 返回**真实文件字节**，不是 JSON。`?artifact_id=<id>` 可下载历史版本。
- 响应头：`Content-Type`（`application/pdf` 或 `text/html`）、`Content-Length`、
  `X-Content-Type-Options: nosniff`、`ETag`（带引号的 SHA-256），以及
  `Content-Disposition: attachment; filename="..."`。
- 文件名可读且确定：`{market}_{symbol}_{doc_type}_{报告期|公告日|unknown}_{report_id}.{ext}`
  （例如 `US_AAPL_10-Q_2026-06-30_<report_id>.html`）；同时携带
  `filename*=UTF-8''`。报告期未知才用公告日，二者都无则 `unknown`；历史版本
  （非当前 artifact）追加 artifact_id 以避免歧义。**不要依赖固定旧格式**，
  一律以响应头为准。
- **条件请求**：带上一次响应的 `If-None-Match: "<sha256>"`，命中返回 **304**
  （无 body，仅 `ETag`）。
- 未知报告/未知 artifact/artifact 不属于该报告：404 `not_found`。
- 文件当前不可用（未完成/缺失）：409 `file_not_available`；不隐式触发抓取。
- 校验：下载后应计算 `sha256(bytes)` 并与 metadata/列表中的 `sha256` 比对。

## 7. 错误契约与状态码映射

错误体：

```json
{
  "type": "about:blank",
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "last_n must be between 1 and 20",
  "instance": "/api/v1/fetch-jobs",
  "code": "invalid_request",
  "request_id": "req_example",
  "retryable": false,
  "errors": [{"field": "last_n", "detail": "..."}]
}
```

| 状态 | 场景 / code |
|---|---|
| 400 | 缺少/格式错误幂等键（`missing_idempotency_key` / `invalid_idempotency_key`）、非法游标（`invalid_cursor`） |
| 401 / 403 | 令牌模式凭据缺失/无效（`unauthorized` / `forbidden`），401 带 `WWW-Authenticate` |
| 404 | `not_found`（job/report/artifact 不存在） |
| 409 | 幂等键冲突（`idempotency_conflict`）、文件不可用（`file_not_available`） |
| 413 | 请求体超过 64 KiB（`payload_too_large`） |
| 415 | 非 JSON 提交（`unsupported_media_type`） |
| 422 | 参数/组合不受支持（`invalid_request`），含未知字段、空 symbols、`last_n` 越界、不支持类型、**非法 JSON** |
| 429 | 队列/配额用尽（`queue_full`），带 `Retry-After` |
| 503 | 存储/执行器不可用（`store_unavailable`）；入队失败不会返回“已接受” |
| 500 | 未预期内部错误（`internal_error`），不返回堆栈/路径 |

> 注：`HTTP_API.md` §6 的表格把“无效 JSON”列为 400，但 v1.0.3 运行实现
> （FastAPI 校验）实际返回 **422 `invalid_request`**；本套件与修正后的
> `openapi.json` 以运行实现为准，按 422 处理。已在 mock 与测试中体现。

已接受任务的源站失败（403/429/网络）记录在**任务结果**里，不会映射为查询接口
自身的失败。

## 8. 幂等与重试规则

- 唯一键：`(client_id, Idempotency-Key)`。本地模式 `client_id=local`。
- 同键同有效请求：返回原任务（未终态 202 / 已终态 200）。
- 同键不同请求：409。**不要**在同一业务动作的网络重试中更换 key。
- 不同 key 会创建不同任务；但文件按来源去重，重复任务会命中缓存
  （`items[].status="cached"`）。
- 轮询建议从 2s 逐步退避到 10s，读到终态即停，并设置整体等待上限。

## 9. Mock 专用控制面（绝不可发往生产）

- 请求头 `X-Mock-Scenario`：`success`（默认）、`partial`、`failed`、
  `no_reports`、`queue_full`、`slow`。
- `POST /__mock/reset`：仅清空 mock 进程内存状态，不删文件、不触生产。
- `GET /__mock/scenarios`：机器可读场景表。
- 场景在提交时按任务快照，互不干扰；正常客户端代码不应包含任何 `/__mock/*`
  调用，也不应在生产无条件发送 `X-Mock-Scenario`。

## 10. 最小客户端流程

1. `POST /api/v1/fetch-jobs`，带 `Idempotency-Key`，保存 `job_id`/`status_url`。
2. 轮询 `GET status_url`，退避到 10s，设整体上限；终态为
   `succeeded|partial|failed`（**都停止轮询**）。
3. 从 `results[].report_ids` 取可用报告；`partial` 也要保留可用文件。
4. 需要时 `GET /api/v1/reports/{id}` 读元数据/警告，再 `GET .../file` 下载并校验
   `sha256`。
5. 需要历史版本时带 `artifact_id`；重复下载可带 `If-None-Match`。
