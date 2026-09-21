"""HTTP API（HTTP_API 全契约；FastAPI/Pydantic/Uvicorn 仅 server 安装组）。

- 同步抓取在单执行器后台线程执行，事件循环只做 SQLite 短读与文件流，
  慢下载不阻塞健康检查与任务查询（DoD #4）；
- 错误统一 application/problem+json（RFC 9457 + code/request_id/retryable）；
- 鉴权：令牌模式（Bearer，每令牌映射 client_id）或回环本地模式
  （client_id=local）；非回环且未配置令牌时拒绝启动（HTTP_API §7）；
- 档案查询绝不隐式联网；文件经数据库 ID 映射到受控路径，
  ETag/304/nosniff/attachment。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from reports_fetcher import __version__
from reports_fetcher.api_models import (
    ArtifactOut,
    FetchJobRequest,
    JobAccepted,
    JobStatusOut,
    ReportDetailOut,
    ReportListItem,
    ReportListOut,
)
from reports_fetcher.config import Config
from reports_fetcher.jobs import (
    InvalidJobRequest,
    JobConflictError,
    JobService,
    QueueFullError,
)
from reports_fetcher.models import FileNotAvailableError, StoreError

logger = logging.getLogger("reports_fetcher.api")

REQUEST_BODY_LIMIT = 64 * 1024  # HTTP_API §3
_IDEMPOTENCY_RE = re.compile(r"^[!-~]{1,128}$")  # 1-128 个可打印 ASCII


class ApiError(Exception):
    """转换为 problem+json 的 API 错误（RFC 9457 扩展）。"""

    def __init__(self, *, status: int, code: str, title: str, detail: str,
                 retryable: bool = False, errors: list | None = None,
                 headers: dict | None = None):
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.retryable = retryable
        self.errors = errors
        self.headers = headers or {}


class _AuthContext:
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id


def _parse_tokens(raw: str) -> dict[str, str]:
    """解析 'client:token,client:token' → {token: client_id}。"""
    tokens: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        client, token = pair.split(":", 1)
        if client.strip() and token.strip():
            tokens[token.strip()] = client.strip()
    return tokens


def create_app(config: Config, job_service: JobService,
               tokens: dict[str, str] | None = None) -> FastAPI:
    """tokens 非空 → 令牌模式（全路由含 docs 受保护）；否则本地模式。"""

    # ------------------------------------------------------------ 鉴权

    def auth(request: Request) -> _AuthContext:
        if tokens:
            header = request.headers.get("Authorization", "")
            scheme, _, credential = header.partition(" ")
            if scheme.lower() != "bearer" or not credential:
                raise ApiError(
                    status=401, code="unauthorized", title="Unauthorized",
                    detail="缺少 Bearer 凭据",
                    headers={"WWW-Authenticate": "Bearer"})
            client = tokens.get(credential.strip())
            if client is None:
                raise ApiError(
                    status=403, code="forbidden", title="Forbidden",
                    detail="凭据无效或无权限",
                    headers={"WWW-Authenticate": "Bearer"})
            return _AuthContext(client_id=client)
        return _AuthContext(client_id="local")

    app = FastAPI(
        title="reports-fetcher",
        version=__version__,
        # PHASE1_REVIEW T7：框架自动文档路由不受全局 dependencies 保护；
        # 令牌模式下禁用默认路由，改为下方显式注册的受保护版本。
        # redoc 在令牌模式下直接关闭（说明性入口，访问 /docs 即可）。
        docs_url="/docs" if not tokens else None,
        redoc_url="/redoc" if not tokens else None,
        openapi_url="/openapi.json" if not tokens else None,
        dependencies=[Depends(auth)] if tokens else None,
    )
    app.state.config = config
    app.state.jobs = job_service
    app.state.tokens = tokens or {}

    if tokens:
        from fastapi.openapi.utils import get_openapi as build_openapi
        from fastapi.responses import HTMLResponse

        # fastapi 0.141 无 fastapi.docs 模块：内联等价的 Swagger UI 入口
        swagger_html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>reports-fetcher docs</title>"
            "<link rel='stylesheet' href='https://cdn.jsdelivr.net/npm/"
            "swagger-ui-dist@5/swagger-ui.css'></head>"
            "<body><div id='swagger-ui'></div>"
            "<script src='https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/"
            "swagger-ui-bundle.js'></script>"
            "<script>SwaggerUIBundle({url:'/openapi.json',"
            "dom_id:'#swagger-ui'});</script></body></html>")

        @app.get("/openapi.json", include_in_schema=False)
        def protected_openapi():
            return build_openapi(title=app.title, version=app.version,
                                 routes=app.routes)

        @app.get("/docs", include_in_schema=False)
        def protected_docs():
            return HTMLResponse(swagger_html)

    # ------------------------------------------------------------ 中间件

    @app.middleware("http")
    async def request_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or \
            f"req_{uuid.uuid4().hex[:16]}"
        request.state.request_id = request_id
        content_length = request.headers.get("Content-Length")
        if content_length and content_length.isdigit() \
                and int(content_length) > REQUEST_BODY_LIMIT:
            response = _problem_response(
                request, ApiError(status=413, code="payload_too_large",
                                  title="Content Too Large",
                                  detail=f"请求体超过 {REQUEST_BODY_LIMIT} 字节"))
            response.headers["X-Request-ID"] = request_id
            return response
        if request.method == "POST" \
                and request.url.path == "/api/v1/fetch-jobs":
            content_type = request.headers.get("Content-Type", "")
            if content_type.split(";")[0].strip() != "application/json":
                response = _problem_response(request, ApiError(
                    status=415, code="unsupported_media_type",
                    title="Unsupported Media Type",
                    detail="Content-Type 必须为 application/json"))
                response.headers["X-Request-ID"] = request_id
                return response
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    # ------------------------------------------------------------ 错误契约

    def _problem_response(request: Request, err: ApiError) -> JSONResponse:
        problem = {
            "type": "about:blank",
            "title": err.title,
            "status": err.status,
            "detail": err.detail,
            "instance": str(request.url.path),
            "code": err.code,
            "request_id": getattr(request.state, "request_id", ""),
            "retryable": err.retryable,
        }
        if err.errors:
            problem["errors"] = err.errors
        return JSONResponse(status_code=err.status,
                            content=problem,
                            media_type="application/problem+json",
                            headers=err.headers)

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return _problem_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request,
                                 exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e.get("loc", [])[1:]),
                   "detail": e.get("msg", "")} for e in exc.errors()]
        return _problem_response(request, ApiError(
            status=422, code="invalid_request",
            title="Unprocessable Content",
            detail="请求参数校验失败", errors=errors))

    @app.exception_handler(StoreError)
    async def store_error_handler(request: Request, exc: StoreError):
        status = 503 if exc.code in ("store_unavailable", "store_in_use") else 500
        return _problem_response(request, ApiError(
            status=status, code=exc.code, title="Server Error",
            detail=str(exc), retryable=status >= 500))

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, exc: Exception):  # noqa: BLE001
        logger.exception("未预期错误: %s", exc)
        return _problem_response(request, ApiError(
            status=500, code="internal_error", title="Internal Server Error",
            detail="内部错误（不泄露堆栈/路径）"))

    @app.exception_handler(FileNotAvailableError)
    async def file_na_handler(request: Request, exc: FileNotAvailableError):
        return _problem_response(request, ApiError(
            status=409, code="file_not_available", title="Conflict",
            detail=str(exc)))

    # ------------------------------------------------------------ 任务

    @app.post("/api/v1/fetch-jobs", status_code=202,
              response_model=JobAccepted)
    def submit_job(payload: FetchJobRequest, request: Request,
                   response: Response,
                   auth_ctx: _AuthContext = Depends(auth)):
        key = request.headers.get("Idempotency-Key", "")
        if not key:
            raise ApiError(status=400, code="missing_idempotency_key",
                           title="Bad Request", detail="缺少 Idempotency-Key")
        if not _IDEMPOTENCY_RE.match(key):
            raise ApiError(
                status=400, code="invalid_idempotency_key", title="Bad Request",
                detail="Idempotency-Key 须为 1-128 个可打印 ASCII 字符")
        try:
            submitted = app.state.jobs.submit(
                auth_ctx.client_id, key,
                symbols=payload.symbols,
                last_n=payload.last_n,
                forms_by_market=payload.forms_by_market,
                refresh=payload.refresh)
        except InvalidJobRequest as e:
            raise ApiError(status=422, code="invalid_request",
                           title="Unprocessable Content", detail=str(e),
                           errors=[{"field": err.get("field", ""),
                                    "detail": err.get("detail", "")}
                                   for err in e.errors]) from e
        except JobConflictError as e:
            raise ApiError(status=409, code=e.code, title="Conflict",
                           detail=str(e)) from e
        except QueueFullError as e:
            raise ApiError(status=429, code=e.code,
                           title="Too Many Requests", detail=str(e),
                           retryable=True,
                           headers={"Retry-After": "10"}) from e
        response.headers["Location"] = f"/api/v1/fetch-jobs/{submitted.job_id}"
        response.headers["Retry-After"] = "2"
        if not submitted.created and submitted.finished:
            response.status_code = 200  # 幂等重放已终态
        return JobAccepted(
            job_id=submitted.job_id, status=submitted.status,
            submitted_at=submitted.submitted_at,
            status_url=f"/api/v1/fetch-jobs/{submitted.job_id}")

    @app.get("/api/v1/fetch-jobs/{job_id}", response_model=JobStatusOut)
    def get_job(job_id: str, request: Request,
                auth_ctx: _AuthContext = Depends(auth)):
        doc = app.state.jobs.get_job(job_id, client_id=auth_ctx.client_id)
        if doc is None:
            raise ApiError(status=404, code="not_found", title="Not Found",
                           detail=f"任务不存在: {job_id}")
        return doc

    # ------------------------------------------------------------ 档案查询

    @app.get("/api/v1/reports", response_model=ReportListOut)
    def list_reports(request: Request, market: str | None = None,
                     symbol: str | None = None, doc_type: str | None = None,
                     period_from: str | None = None,
                     period_to: str | None = None,
                     limit: int = 20, cursor: str | None = None,
                     auth_ctx: _AuthContext = Depends(auth)):
        if not (1 <= limit <= 100):
            raise ApiError(status=422, code="invalid_request",
                           title="Unprocessable Content",
                           detail="limit 范围为 1-100")
        max_row = _decode_cursor(cursor) if cursor else None
        if cursor and max_row is None:
            raise ApiError(status=400, code="invalid_cursor",
                           title="Bad Request", detail="无效游标")
        if market and market not in ("CN", "HK", "US"):
            raise ApiError(status=422, code="invalid_request",
                           title="Unprocessable Content", detail="未知市场")
        for label, value in (("period_from", period_from),
                             ("period_to", period_to)):
            if value and not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
                raise ApiError(status=422, code="invalid_request",
                               title="Unprocessable Content",
                               detail=f"{label} 须为 YYYY-MM-DD")
        store = app.state.jobs.store
        conn = store.connection()
        sql = """SELECT m.rowid AS rowid, m.*, a.artifact_id, a.sha256,
                        a.bytes, a.fetched_at
                 FROM manifest m JOIN artifacts a
                   ON a.artifact_id = m.current_artifact_id
                 WHERE m.status='done' AND a.state='ready'"""
        params: list = []
        if market:
            sql += " AND m.market=?"
            params.append(market)
        if symbol:
            sql += " AND m.symbol=?"
            params.append(symbol)
        if doc_type:
            sql += " AND m.doc_type=?"
            params.append(doc_type)
        if period_from:
            sql += " AND m.report_period IS NOT NULL AND m.report_period>=?"
            params.append(period_from)
        if period_to:
            sql += " AND m.report_period IS NOT NULL AND m.report_period<=?"
            params.append(period_to)
        if max_row is not None:
            sql += " AND m.rowid<?"   # 严格小于：游标行本身不重复返回
            params.append(max_row)
        sql += " ORDER BY m.rowid DESC LIMIT ?"
        params.append(limit + 1)
        rows = conn.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [ReportListItem(
            report_id=r["report_id"], market=r["market"], symbol=r["symbol"],
            issuer_id=None, source_id=r["source_id"], source_url=r["source_url"],
            title=r["title"], doc_type=r["doc_type"],
            report_period=r["report_period"], period_source=r["period_source"],
            filing_date=r["filing_date"], language=r["language"],
            is_amendment=bool(r["is_amendment"]), status=r["status"],
            artifact_id=r["artifact_id"], sha256=r["sha256"], bytes=r["bytes"],
            fetched_at=r["fetched_at"], warnings=_report_warnings(r),
            download_url=f"/api/v1/reports/{r['report_id']}/file",
        ) for r in rows]
        next_cursor = _encode_cursor(rows[-1]["rowid"]) if has_more and rows \
            else None
        return ReportListOut(items=items, next_cursor=next_cursor)

    @app.get("/api/v1/reports/{report_id}", response_model=ReportDetailOut)
    def report_detail(report_id: str,
                      auth_ctx: _AuthContext = Depends(auth)):
        conn = app.state.jobs.store.connection()
        row = conn.execute("SELECT * FROM manifest WHERE report_id=?",
                           (report_id,)).fetchone()
        if row is None:
            raise ApiError(status=404, code="not_found", title="Not Found",
                           detail=f"报告不存在: {report_id}")
        artifacts = conn.execute(
            "SELECT * FROM artifacts WHERE report_id=? "
            "ORDER BY fetched_at DESC", (report_id,)).fetchall()
        return ReportDetailOut(
            report_id=row["report_id"], market=row["market"],
            symbol=row["symbol"], source_id=row["source_id"],
            source_url=row["source_url"], title=row["title"],
            doc_type=row["doc_type"], source_form=row["source_form"],
            report_period=row["report_period"],
            period_source=row["period_source"],
            filing_date=row["filing_date"], language=row["language"],
            is_amendment=bool(row["is_amendment"]),
            revision_of=row["revision_of"], status=row["status"],
            current_artifact_id=row["current_artifact_id"],
            warnings=_report_warnings(row),
            artifacts=[ArtifactOut(
                artifact_id=a["artifact_id"], sha256=a["sha256"],
                bytes=a["bytes"], media_type=a["media_type"],
                fetched_at=a["fetched_at"], state=a["state"],
                is_current=a["artifact_id"] == row["current_artifact_id"],
            ) for a in artifacts])

    @app.get("/api/v1/reports/{report_id}/file")
    def report_file(report_id: str, request: Request,
                    artifact_id: str | None = None,
                    auth_ctx: _AuthContext = Depends(auth)):
        store = app.state.jobs.store
        conn = store.connection()
        if artifact_id is not None:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?",
                (artifact_id,)).fetchone()
            if row is None or row["report_id"] != report_id:
                raise ApiError(
                    status=404, code="not_found", title="Not Found",
                    detail=f"artifact 不存在或不属于该报告: {artifact_id}")
        else:
            manifest = conn.execute(
                "SELECT current_artifact_id FROM manifest WHERE report_id=?",
                (report_id,)).fetchone()
            if manifest is None:
                raise ApiError(status=404, code="not_found",
                               title="Not Found",
                               detail=f"报告不存在: {report_id}")
            if not manifest["current_artifact_id"]:
                raise ApiError(status=409, code="file_not_available",
                               title="Conflict", detail="当前无可用文件版本")
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?",
                (manifest["current_artifact_id"],)).fetchone()
        try:
            path, artifact = store.open_artifact(row["artifact_id"])
        except FileNotAvailableError as e:
            raise ApiError(status=409, code="file_not_available",
                           title="Conflict", detail=str(e)) from e
        etag = f'"{artifact["sha256"]}"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        filename = _download_filename(artifact)
        return FileResponse(
            path, media_type=artifact["media_type"],
            headers={
                "ETag": etag,
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": filename,
            })

    # ------------------------------------------------------------ 健康

    @app.get("/health/live", response_model=None)
    def health_live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready():
        problems = []
        try:
            app.state.jobs.store.connection().execute(
                "SELECT 1").fetchone()
        except Exception as e:  # noqa: BLE001
            problems.append(f"db: {e}")
        if not app.state.jobs.executor_healthy():
            problems.append("executor not running")
        if problems:
            return JSONResponse(status_code=503, content={
                "status": "unavailable", "detail": "; ".join(problems)})
        return {"status": "ok"}

    return app


# ---------------------------------------------------------------- 工具

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _encode_cursor(rowid: int) -> str:
    return base64.urlsafe_b64encode(str(rowid).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return int(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception:  # noqa: BLE001 - 任何解码失败都按无效游标
        return None


def _report_warnings(row) -> list[str]:
    """从已持久化字段派生质量警告（HTTP_API §5 warnings）。"""
    warnings: list[str] = []
    if row["period_source"] == "unknown" or not row["report_period"]:
        warnings.append("报告期未知（period_source=unknown）")
    if row["is_amendment"]:
        warnings.append("修订版本（is_amendment）")
    return warnings


def _download_filename(artifact: dict) -> str:
    """附件文件名：ASCII 回退 + filename* UTF-8（中文文件名安全，RFC 6266）。"""
    base = artifact["local_path"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = base.replace('"', "_") or "report"
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", base) or "report"
    return (f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(base)}")


def start_server(config: Config, tokens: dict[str, str] | None) -> None:
    """serve 命令入口：启动检查 + 单 Uvicorn worker（DESIGN §12）。

    本地无鉴权模式的适用条件（HTTP_API §7）：监听回环地址，或部署侧
    显式声明端口仅发布到回环（RF_LOCAL_MODE=1，如 compose 的
    127.0.0.1 端口映射）；否则必须配置应用令牌。
    """
    import os

    import uvicorn

    host = config.server.host
    explicit_local = os.environ.get("RF_LOCAL_MODE", "").strip() == "1"
    if not tokens and not _is_loopback(host) and not explicit_local:
        raise StoreError(
            "非回环监听必须配置应用令牌（RF_API_TOKENS），或显式声明"
            "端口仅发布到回环（RF_LOCAL_MODE=1）",
            code="config_invalid")
    from reports_fetcher.core import FetchService
    from reports_fetcher.downloader import Transport

    store = _open_locked_store(config)
    service = FetchService(config, store, Transport(config.http))
    jobs = JobService(config, store,
                      fetch_service_factory=lambda: service)
    jobs.recover()
    jobs.start()
    app = create_app(config, jobs, tokens)
    uvicorn.run(app, host=host, port=config.server.port, workers=1,
                log_level=config.log.level.lower())


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def _open_locked_store(config: Config):
    """serve 独占归档根目录所有者锁（DESIGN §11.4：serve 与 CLI 互斥）。

    Store 构造即持锁（PHASE1_REVIEW T1：schema/恢复在锁内）。
    """
    from reports_fetcher.store import Store

    return Store(Path(config.general.out_dir), layout=config.general.layout)


def load_tokens_from_env() -> dict[str, str] | None:
    """环境变量 'RF_API_TOKENS=client:token,client:token'（HTTP_API §7）。"""
    import os
    raw = os.environ.get("RF_API_TOKENS", "").strip()
    if not raw:
        return None
    tokens = _parse_tokens(raw)
    return tokens or None


def _parse_tokens(raw: str) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        client, token = pair.split(":", 1)
        if client.strip() and token.strip():
            tokens[token.strip()] = client.strip()
    return tokens
