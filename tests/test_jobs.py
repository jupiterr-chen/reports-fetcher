"""JobService：持久化任务（HTTP_API §3/§4，DESIGN §14；fake transport 不触网）。"""
from __future__ import annotations

import json

import pytest

from reports_fetcher.config import Config, HttpConfig
from reports_fetcher.core import FetchService
from reports_fetcher.downloader import Transport
from reports_fetcher.jobs import (
    InvalidJobRequest,
    JobConflictError,
    JobService,
    QueueFullError,
)
from reports_fetcher.store import Store
from tests.conftest import UrlMapSession, make_transport
from tests.test_core_pipeline import _build_mapping
from tests.test_us_edgar import UA


def _config(**server_overrides) -> Config:
    config = Config()
    config.http.user_agent = UA
    config.fetch.market_workers = 1
    for key, value in server_overrides.items():
        setattr(config.server, key, value)
    return config


def _harness(tmp_path, *, config=None, doc_contents=None):
    config = config or _config()
    store = Store(tmp_path / "archive")
    session = UrlMapSession(_build_mapping(doc_contents))
    transport = make_transport(session, config=HttpConfig(user_agent=UA))
    service = FetchService(config, store, transport)
    jobs = JobService(config, store,
                      fetch_service_factory=lambda: service)
    return jobs, store, service, session


def _submit(jobs, key="k1", *, symbols=("AAPL",), last_n=4, forms=None,
            refresh=False, client="app-a"):
    return jobs.submit(client, key, symbols=list(symbols), last_n=last_n,
                       forms_by_market=forms, refresh=refresh)


class TestSubmit:
    def test_submit_creates_queued_job_with_effective_request(self, tmp_path):
        jobs, store, *_ = _harness(tmp_path)
        submitted = _submit(jobs, symbols=["AAPL", "aapl"])  # 规范化去重
        assert submitted.created and submitted.status == "queued"
        row = store.connection().execute(
            "SELECT * FROM jobs WHERE job_id=?", (submitted.job_id,)).fetchone()
        effective = json.loads(row["effective_request_json"])
        assert effective["symbols"] == ["US:AAPL"]
        assert effective["last_n"] == 4
        assert set(effective["forms_by_market"]["US"]) == {"10-Q", "10-K", "20-F"}
        assert effective["refresh"] is False

    def test_idempotent_replay_same_request(self, tmp_path):
        jobs, *_ = _harness(tmp_path)
        first = _submit(jobs)
        second = _submit(jobs)
        assert second.created is False
        assert second.job_id == first.job_id

    def test_same_key_different_request_conflicts(self, tmp_path):
        jobs, *_ = _harness(tmp_path)
        _submit(jobs)
        with pytest.raises(JobConflictError):
            _submit(jobs, last_n=2)

    def test_different_clients_same_key_independent(self, tmp_path):
        jobs, *_ = _harness(tmp_path)
        a = _submit(jobs, client="app-a")
        b = _submit(jobs, client="app-b")
        assert a.job_id != b.job_id

    def test_invalid_symbol_rejected_at_submit(self, tmp_path):
        jobs, *_ = _harness(tmp_path)
        with pytest.raises(InvalidJobRequest) as ei:
            _submit(jobs, symbols=["AAPL", "???"])
        assert ei.value.errors

    def test_forms_validation(self, tmp_path):
        jobs, *_ = _harness(tmp_path)
        with pytest.raises(InvalidJobRequest):
            _submit(jobs, forms={"XX": ["FY"]})           # 未知市场
        with pytest.raises(InvalidJobRequest):
            _submit(jobs, forms={"CN": []})               # 空数组
        with pytest.raises(InvalidJobRequest):
            _submit(jobs, forms={"US": ["6-K"]})          # 不支持的类型
        submitted = _submit(jobs, forms={"HK": ["QTR-HK"]})  # 显式可选类型
        assert submitted.created

    def test_queue_limit(self, tmp_path):
        config = _config(max_pending_jobs=1)
        jobs, *_ = _harness(tmp_path, config=config)
        _submit(jobs)
        with pytest.raises(QueueFullError):
            _submit(jobs, key="k2")


class TestExecution:
    def test_claim_and_run_job(self, tmp_path):
        jobs, store, *_ = _harness(tmp_path)
        submitted = _submit(jobs, symbols=["AAPL"], last_n=4)
        job_id = jobs.claim_next()
        assert job_id == submitted.job_id
        jobs.run_job(job_id)
        doc = jobs.get_job(job_id)
        assert doc["status"] in ("succeeded", "partial")
        assert doc["attempt"] == 1
        assert doc["summary"]["downloaded"] == 4
        sym = doc["results"][0]
        assert sym["status"] in ("succeeded", "partial")
        assert len(sym["report_ids"]) == 4
        assert all(i["artifact_id"] for i in sym["items"])
        assert doc["progress"] == {"symbols_total": 1, "symbols_finished": 1}

    def test_cli_api_consistency_same_reports(self, tmp_path):
        """DoD #2：CLI（直接 fetch）与 HTTP 任务对同一请求选出相同报告。"""
        jobs, store, service, *_ = _harness(tmp_path)
        direct = service.fetch(["AAPL"], last_n=4)
        direct_ids = sorted(i.report_id for i in direct.results[0].items)
        _submit(jobs, symbols=["AAPL"], last_n=4)
        job_id = jobs.claim_next()
        jobs.run_job(job_id)
        doc = jobs.get_job(job_id)
        assert sorted(doc["results"][0]["report_ids"]) == direct_ids

    def test_failure_isolation_across_symbols(self, tmp_path):
        # 第一个证券 resolve 失败（不在映射中），第二个正常完成
        jobs, *_ = _harness(tmp_path)
        _submit(jobs, symbols=["MSFT", "AAPL"])
        job_id = jobs.claim_next()
        jobs.run_job(job_id)
        doc = jobs.get_job(job_id)
        statuses = {r["symbol"]: r["status"] for r in doc["results"]}
        assert statuses["MSFT"] == "failed"
        # AAPL 带截断质量警告 → 按契约归 partial（HTTP_API §4）
        assert statuses["AAPL"] == "partial"
        assert doc["status"] == "partial"

    def test_job_deadline_exceeded(self, tmp_path):
        config = _config(job_deadline_seconds=0)
        jobs, *_ = _harness(tmp_path, config=config)
        submitted = _submit(jobs, symbols=["AAPL"])
        job_id = jobs.claim_next()
        jobs.run_job(job_id)
        doc = jobs.get_job(job_id)
        assert doc["status"] == "failed"
        assert doc["results"][0]["error"]["code"] == "job_deadline_exceeded"

    def test_status_aggregation_rules(self, tmp_path):
        """HTTP_API §4：全 no_reports → succeeded；混合 → partial。"""
        jobs, *_ = _harness(tmp_path)
        assert jobs._aggregate_status(["empty"], "x") == "succeeded"
        assert jobs._aggregate_status(["ok"], "x") == "succeeded"
        assert jobs._aggregate_status(["ok", "empty"], "x") == "partial"
        assert jobs._aggregate_status(["failed"], "x") == "failed"


class TestRecovery:
    def test_running_reset_to_queued(self, tmp_path):
        jobs, store, *_ = _harness(tmp_path)
        submitted = _submit(jobs)
        conn = store.connection()
        with conn:  # 模拟执行中崩溃现场
            conn.execute("UPDATE jobs SET status='running', attempt=1 "
                         "WHERE job_id=?", (submitted.job_id,))
        fresh = JobService(jobs.config, store,
                           fetch_service_factory=lambda: None)
        reset = fresh.recover()
        assert reset == 1
        row = conn.execute("SELECT status FROM jobs WHERE job_id=?",
                           (submitted.job_id,)).fetchone()
        assert row["status"] == "queued"
        # 重启不丢任务：领取执行可完成
        job_id = fresh.claim_next()
        assert job_id == submitted.job_id
        row = conn.execute("SELECT attempt FROM jobs WHERE job_id=?",
                           (submitted.job_id,)).fetchone()
        assert row["attempt"] == 2  # attempt 加一（含中断恢复）

    def test_attempt_exhausted_marks_failed(self, tmp_path):
        config = _config(max_job_attempts=2)
        jobs, store, *_ = _harness(tmp_path, config=config)
        submitted = _submit(jobs)
        conn = store.connection()
        with conn:
            conn.execute("UPDATE jobs SET status='running', attempt=2 "
                         "WHERE job_id=?", (submitted.job_id,))
        fresh = JobService(config, store, fetch_service_factory=lambda: None)
        fresh.recover()
        doc = fresh.get_job(submitted.job_id)
        assert doc["status"] == "failed"
        assert doc["summary"]["error"]["code"] == "recovery_exhausted"

    def test_restart_does_not_redo_completed(self, tmp_path):
        """DoD #3：入队后重启不丢任务、不重复执行已完成项。"""
        jobs, store, *_ = _harness(tmp_path)
        submitted = _submit(jobs)
        job_id = jobs.claim_next()
        jobs.run_job(job_id)
        artifacts_before = store.connection().execute(
            "SELECT COUNT(*) n FROM artifacts").fetchone()["n"]
        # 模拟重启：恢复 + 重新领取（无 queued 任务，无重复执行）
        fresh = JobService(jobs.config, store,
                           fetch_service_factory=lambda: None)
        assert fresh.recover() == 0
        assert fresh.claim_next() is None
        artifacts_after = store.connection().execute(
            "SELECT COUNT(*) n FROM artifacts").fetchone()["n"]
        assert artifacts_before == artifacts_after


class TestGetJob:
    def test_client_isolation(self, tmp_path):
        jobs, *_ = _harness(tmp_path)
        submitted = _submit(jobs, client="app-a")
        assert jobs.get_job(submitted.job_id, client_id="app-a") is not None
        assert jobs.get_job(submitted.job_id, client_id="app-b") is None

    def test_unknown_job(self, tmp_path):
        jobs, *_ = _harness(tmp_path)
        assert jobs.get_job("job_nope") is None
