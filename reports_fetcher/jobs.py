"""JobService：持久化任务（DESIGN §14，HTTP_API §3/§4）。

- 提交：格式校验 + 规范化生效请求 → request_hash；幂等查找、队列计数、
  入队同事务完成（HTTP_API §4）；
- 执行：单执行器线程从 SQLite 领取（条件更新），逐证券调用 FetchService
  并即时持久化每证券/文件进度；首次领取写入 deadline（恢复不延长）；
- 恢复：启动时 running → queued（attempt 达上限则 recovery_exhausted），
  保留已完成明细；归档提交幂等，不宣称端到端恰好一次；
- 限额：queued/running 总量上限（429 语义由 API 层映射）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from reports_fetcher.config import Config
from reports_fetcher.core import BatchResult, FetchService
from reports_fetcher.downloader import Transport
from reports_fetcher.models import (
    FetchResult,
    OUTCOME_CACHED,
    OUTCOME_DOWNLOADED,
    OUTCOME_FAILED,
    ReportsFetcherError,
    StoreError,
)
from reports_fetcher.symbol import batch_normalize
from reports_fetcher.store import Store

logger = logging.getLogger("reports_fetcher.jobs")

_RETRYABLE_CODES = {"source_unavailable", "source_rate_limited",
                    "job_deadline_exceeded"}
_MARKET_FORMS = {
    "CN": {"Q1", "H1", "Q3", "FY"},
    "HK": {"ANNUAL", "INTERIM", "QTR-HK"},   # QTR-HK 显式可选（I3 验证启用）
    "US": {"10-Q", "10-K", "20-F"},
}


class InvalidJobRequest(ReportsFetcherError):
    code = "invalid_request"

    def __init__(self, message: str, *, errors: list[dict] | None = None):
        super().__init__(message)
        self.errors = errors or []


class JobConflictError(ReportsFetcherError):
    """相同幂等键、不同请求（HTTP 409）。"""

    code = "idempotency_conflict"


class QueueFullError(ReportsFetcherError):
    """queued/running 超上限（HTTP 429）。"""

    code = "queue_full"


@dataclass
class SubmittedJob:
    job_id: str
    status: str
    created: bool          # False = 幂等重放
    finished: bool         # 重放且已终态（HTTP 200；未终态 202）
    submitted_at: str = ""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


class JobService:
    """单进程、单归档目录、单执行器的任务服务（P0）。"""

    def __init__(self, config: Config, store: Store,
                 fetch_service_factory=None) -> None:
        self.config = config
        self.store = store
        self._factory = fetch_service_factory or (
            lambda: FetchService(config, store, Transport(config.http)))
        self._wake = threading.Event()
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor_error: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 提交

    def _effective_forms(self, forms_by_market: dict | None) -> dict[str, list[str]]:
        defaults = self.config.fetch.default_forms
        effective: dict[str, list[str]] = {}
        for market in ("CN", "HK", "US"):
            explicit = (forms_by_market or {}).get(market)
            effective[market] = sorted(explicit) if explicit is not None \
                else list(defaults.get(market, sorted(_MARKET_FORMS[market])))
        return effective

    def _validate_forms(self, forms_by_market: dict | None) -> None:
        if not forms_by_market:
            return
        errors: list[dict] = []
        for market, forms in forms_by_market.items():
            if market not in _MARKET_FORMS:
                errors.append({"field": f"forms_by_market.{market}",
                               "detail": "未知市场（支持 CN/HK/US）"})
                continue
            if not forms:
                errors.append({"field": f"forms_by_market.{market}",
                               "detail": "空数组不被接受（HTTP_API §3）"})
                continue
            unknown = [f for f in forms if f not in _MARKET_FORMS[market]]
            if unknown:
                errors.append({
                    "field": f"forms_by_market.{market}",
                    "detail": f"不支持的基础类型 {unknown}"
                              f"（{market} 支持 {sorted(_MARKET_FORMS[market])}）"})
        if errors:
            raise InvalidJobRequest("forms_by_market 不合法", errors=errors)

    def submit(self, client_id: str, idempotency_key: str, *,
               symbols: list[str], last_n: int, forms_by_market: dict | None,
               refresh: bool) -> SubmittedJob:
        """格式校验（联网 resolve 在执行期）→ 幂等/限额/入队单事务。"""
        ordered, _aliases, invalid = batch_normalize(list(symbols))
        if invalid:
            raise InvalidJobRequest(
                "symbols 存在无法识别的代码",
                errors=[{"field": "symbols",
                         "detail": f"{raw}: {err}"} for raw, err in invalid])
        if not ordered:
            raise InvalidJobRequest("symbols 不能为空")
        if len(ordered) > self.config.server.max_symbols_per_job:
            raise InvalidJobRequest(
                f"symbols 去重后最多 {self.config.server.max_symbols_per_job} 项",
                errors=[{"field": "symbols",
                         "detail": f"got {len(ordered)}"}])
        self._validate_forms(forms_by_market)

        effective = {
            "symbols": [f"{norm.market.value}:{norm.symbol}" for norm in ordered],
            "last_n": last_n,
            "forms_by_market": self._effective_forms(forms_by_market),
            "refresh": bool(refresh),
        }
        request_hash = hashlib.sha256(json.dumps(
            effective, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

        conn = self.store.connection()
        now = _utcnow()
        with conn:
            row = conn.execute(
                "SELECT job_id, status, request_hash FROM jobs "
                "WHERE client_id=? AND idempotency_key=?",
                (client_id, idempotency_key)).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise JobConflictError(
                        "相同幂等键已被不同请求占用（HTTP_API §4）",
                        detail=f"job_id={row['job_id']}")
                finished = row["status"] in ("succeeded", "partial", "failed")
                submitted_at = conn.execute(
                    "SELECT submitted_at FROM jobs WHERE job_id=?",
                    (row["job_id"],)).fetchone()["submitted_at"]
                return SubmittedJob(row["job_id"], row["status"],
                                    created=False, finished=finished,
                                    submitted_at=submitted_at)
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs "
                "WHERE status IN ('queued','running')").fetchone()["n"]
            if pending >= self.config.server.max_pending_jobs:
                raise QueueFullError(
                    f"待处理任务已达上限（{self.config.server.max_pending_jobs}）")
            job_id = f"job_{uuid.uuid4().hex[:20]}"
            conn.execute(
                """INSERT INTO jobs(
                       job_id, client_id, idempotency_key, request_hash,
                       effective_request_json, status, attempt,
                       submitted_at, created_at, updated_at)
                   VALUES(?,?,?,?,?, 'queued', 0, ?, ?, ?)""",
                (job_id, client_id, idempotency_key, request_hash,
                 json.dumps(effective, ensure_ascii=False), now, now, now))
        self._wake.set()
        logger.info("任务入队 %s (client=%s symbols=%d)", job_id, client_id,
                    len(ordered))
        return SubmittedJob(job_id, "queued", created=True, finished=False,
                            submitted_at=now)

    # ------------------------------------------------------------ 执行器

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_executor,
                                        name="job-executor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_flag.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def recover(self) -> int:
        """启动恢复：遗留 running → queued（attempt 达上限则终结）。

        必须在执行器启动前调用；P0 单实例锁（Store owner lock）保证
        不会与仍在运行的实例并发。返回重置数量。
        """
        conn = self.store.connection()
        now = _utcnow()
        reset = 0
        with conn:
            rows = conn.execute(
                "SELECT job_id, attempt FROM jobs WHERE status='running'"
            ).fetchall()
            for row in rows:
                if row["attempt"] >= self.config.server.max_job_attempts:
                    conn.execute(
                        """UPDATE jobs SET status='failed', finished_at=?,
                               summary_json=?, updated_at=? WHERE job_id=?""",
                        (now, json.dumps(
                            {"error": {"code": "recovery_exhausted",
                                       "retryable": False}}),
                         now, row["job_id"]))
                else:
                    conn.execute(
                        "UPDATE jobs SET status='queued', updated_at=? "
                        "WHERE job_id=?", (now, row["job_id"]))
                    reset += 1
        if rows:
            logger.warning("恢复：running 任务 %d 个（重置 %d）", len(rows), reset)
        return reset

    def _run_executor(self) -> None:
        while not self._stop_flag.is_set():
            job_id = self.claim_next()
            if job_id is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            try:
                self.run_job(job_id)
            except Exception as e:  # noqa: BLE001 - 执行器不能因单任务崩溃
                logger.exception("任务执行异常 %s: %s", job_id, e)
                self._fail_job(job_id, getattr(e, "code", "internal_error"),
                               str(e))

    def claim_next(self) -> str | None:
        """条件更新领取一个 queued 任务；首次领取写 deadline（恢复不延长）。"""
        conn = self.store.connection()
        now = _utcnow()
        with conn:
            row = conn.execute(
                "SELECT job_id, attempt FROM jobs WHERE status='queued' "
                "ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            if row["attempt"] >= self.config.server.max_job_attempts:
                self._fail_job(row["job_id"], "recovery_exhausted",
                               "尝试次数用尽（含中断恢复）", conn=conn)
                return None
            deadline = (_utcnow_dt() + timedelta(
                seconds=self.config.server.job_deadline_seconds)
            ).isoformat(timespec="seconds")
            conn.execute(
                """UPDATE jobs SET status='running', attempt=attempt+1,
                       started_at=COALESCE(started_at, ?),
                       deadline=COALESCE(deadline, ?), updated_at=?
                   WHERE job_id=? AND status='queued'""",
                (now, deadline, now, row["job_id"]))
            logger.info("领取任务 %s (attempt=%d)", row["job_id"],
                        row["attempt"] + 1)
            return row["job_id"]

    # ------------------------------------------------------------ 执行

    def run_job(self, job_id: str) -> None:
        conn = self.store.connection()
        job = conn.execute("SELECT * FROM jobs WHERE job_id=?",
                           (job_id,)).fetchone()
        if job is None:
            raise StoreError("任务不存在", detail=f"job_id={job_id}")
        effective = json.loads(job["effective_request_json"])
        symbols = [entry.split(":", 1)[1]
                   for entry in effective["symbols"]]
        deadline_iso = conn.execute(
            "SELECT deadline FROM jobs WHERE job_id=?",
            (job_id,)).fetchone()["deadline"]
        deadline_ts = None
        if deadline_iso:
            try:
                remaining = (datetime.fromisoformat(deadline_iso)
                             - _utcnow_dt()).total_seconds()
                deadline_ts = time.monotonic() + max(0.0, remaining)
            except ValueError:  # pragma: no cover
                deadline_ts = None

        service = self._factory()
        summary = {"downloaded": 0, "cached": 0, "failed": 0}
        symbol_statuses: list[str] = []
        try:
            for raw_symbol in symbols:
                batch: BatchResult = service.fetch(
                    [raw_symbol], last_n=effective["last_n"],
                    forms_by_market=effective["forms_by_market"],
                    refresh=effective.get("refresh", False),
                    deadline_ts=deadline_ts)
                result = batch.results[0] if batch.results else None
                self._persist_symbol(job_id, result)
                if result is not None:
                    for item in result.items:
                        if item.outcome == OUTCOME_DOWNLOADED:
                            summary["downloaded"] += 1
                        elif item.outcome == OUTCOME_CACHED:
                            summary["cached"] += 1
                        else:
                            summary["failed"] += 1
                    symbol_statuses.append(result.status)
        except ReportsFetcherError as e:
            # 任务级失败（如 UA 未配置）：保留已完成的证券明细
            logger.error("任务级失败 %s: %s %s", job_id, e.code, e)
            self._finish_job(job_id, "failed",
                             {**summary, "error": {"code": e.code,
                                                   "retryable": e.code in
                                                   _RETRYABLE_CODES}})
            return

        status = self._aggregate_status(symbol_statuses, job_id)
        self._finish_job(job_id, status, summary)
        logger.info("任务完成 %s status=%s summary=%s", job_id, status, summary)

    def _aggregate_status(self, symbol_statuses: list[str],
                          job_id: str) -> str:
        usable = any(s in ("ok", "partial") for s in symbol_statuses)
        any_failed = any(s == "failed" for s in symbol_statuses)
        if not usable and any_failed:
            return "failed"
        if symbol_statuses and all(s == "empty" for s in symbol_statuses):
            return "succeeded"  # 全部正常检索但无报告（HTTP_API §4）
        gaps = any(s in ("failed", "empty", "partial") for s in symbol_statuses)
        conn = self.store.connection()
        has_warnings = conn.execute(
            "SELECT COUNT(*) AS n FROM job_symbols WHERE job_id=? "
            "AND warnings_json IS NOT NULL AND warnings_json != '[]'",
            (job_id,)).fetchone()["n"] > 0
        return "partial" if (gaps or has_warnings) else "succeeded"

    def _persist_symbol(self, job_id: str, result: FetchResult | None) -> None:
        if result is None:
            return
        status_map = {"ok": "succeeded", "partial": "partial",
                      "failed": "failed", "empty": "no_reports"}
        coverage = {k: v for k, v in result.coverage.items()}
        coverage.setdefault("requested", 0)
        conn = self.store.connection()
        now = _utcnow()
        with conn:
            conn.execute(
                """INSERT INTO job_symbols(
                       job_id, market, symbol, status, coverage_json,
                       warnings_json, error_code, display_name)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id, market, symbol) DO UPDATE SET
                       status=excluded.status,
                       coverage_json=excluded.coverage_json,
                       warnings_json=excluded.warnings_json,
                       error_code=excluded.error_code,
                       display_name=excluded.display_name""",
                (job_id, result.market.value, result.symbol,
                 status_map.get(result.status, "failed"),
                 json.dumps(coverage, ensure_ascii=False),
                 json.dumps(result.warnings, ensure_ascii=False),
                 result.error, result.display_name))
            for item in result.items:
                conn.execute(
                    """INSERT INTO job_items(
                           job_id, report_id, source_id, outcome, artifact_id,
                           error_code, error_detail)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(job_id, report_id) DO UPDATE SET
                           outcome=excluded.outcome,
                           artifact_id=excluded.artifact_id,
                           error_code=excluded.error_code,
                           error_detail=excluded.error_detail""",
                    (job_id, item.report_id, item.source_id, item.outcome,
                     None, item.error, item.detail))
                if item.outcome != OUTCOME_FAILED and item.local_path:
                    artifact = conn.execute(
                        "SELECT current_artifact_id FROM manifest "
                        "WHERE report_id=?", (item.report_id,)).fetchone()
                    if artifact and artifact["current_artifact_id"]:
                        conn.execute(
                            "UPDATE job_items SET artifact_id=? "
                            "WHERE job_id=? AND report_id=?",
                            (artifact["current_artifact_id"], job_id,
                             item.report_id))

    def _finish_job(self, job_id: str, status: str, summary: dict) -> None:
        conn = self.store.connection()
        now = _utcnow()
        with conn:
            conn.execute(
                """UPDATE jobs SET status=?, summary_json=?, finished_at=?,
                       updated_at=? WHERE job_id=?""",
                (status, json.dumps(summary, ensure_ascii=False), now, now,
                 job_id))

    def _fail_job(self, job_id: str, code: str, detail: str, *,
                  conn=None) -> None:
        conn = conn or self.store.connection()
        now = _utcnow()
        with conn:
            conn.execute(
                """UPDATE jobs SET status='failed', finished_at=?,
                       summary_json=?, updated_at=? WHERE job_id=?""",
                (now, json.dumps(
                    {"error": {"code": code, "retryable":
                               code in _RETRYABLE_CODES, "detail": detail}},
                    ensure_ascii=False), now, job_id))

    # ------------------------------------------------------------ 查询

    def get_job(self, job_id: str, client_id: str | None = None) -> dict | None:
        """任务状态文档（HTTP_API §3/§4）。client_id 不符返回 None（404）。"""
        conn = self.store.connection()
        job = conn.execute("SELECT * FROM jobs WHERE job_id=?",
                           (job_id,)).fetchone()
        if job is None:
            return None
        if client_id is not None and job["client_id"] != client_id:
            return None
        symbols = conn.execute(
            "SELECT * FROM job_symbols WHERE job_id=? "
            "ORDER BY rowid", (job_id,)).fetchall()
        # 任务明细按 manifest 归属到证券（report_id → market/symbol）
        item_rows = conn.execute(
            """SELECT i.*, m.market AS m_market, m.symbol AS m_symbol
               FROM job_items i LEFT JOIN manifest m ON m.report_id = i.report_id
               WHERE i.job_id=? ORDER BY i.rowid""", (job_id,)).fetchall()
        items_by_symbol: dict[tuple[str, str], list[dict]] = {}
        for row in item_rows:
            if row["m_market"] is None:
                continue  # 报告行不存在（不应发生）：跳过展示
            entry = {
                "report_id": row["report_id"],
                "source_id": row["source_id"],
                "status": row["outcome"],
                "artifact_id": row["artifact_id"],
            }
            if row["error_code"]:
                entry["error"] = {
                    "code": row["error_code"],
                    "retryable": row["error_code"] in _RETRYABLE_CODES}
            items_by_symbol.setdefault(
                (row["m_market"], row["m_symbol"]), []).append(entry)
        results = []
        for sym in symbols:
            sym_items = items_by_symbol.get((sym["market"], sym["symbol"]), [])
            report_ids = [i["report_id"] for i in sym_items
                          if i["status"] != OUTCOME_FAILED]
            error = None
            if sym["error_code"]:
                error = {"code": sym["error_code"],
                         "retryable": sym["error_code"] in _RETRYABLE_CODES}
            results.append({
                "market": sym["market"],
                "symbol": sym["symbol"],
                "display_name": sym["display_name"],
                "status": sym["status"],
                "report_ids": report_ids,
                "items": sym_items,
                "coverage": json.loads(sym["coverage_json"] or "{}"),
                "warnings": json.loads(sym["warnings_json"] or "[]"),
                "error": error,
            })
        summary = json.loads(job["summary_json"]) if job["summary_json"] else None
        if summary is None:
            summary = {"downloaded": 0, "cached": 0, "failed": 0}
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "attempt": job["attempt"],
            "submitted_at": job["submitted_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "deadline": job["deadline"],
            "progress": {"symbols_total": len(symbols),
                         "symbols_finished": len(symbols)},
            "summary": summary,
            "results": results,
        }

    def executor_healthy(self) -> bool:
        return (self._thread is not None and self._thread.is_alive()
                and self._executor_error is None)
