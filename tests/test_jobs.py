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

    def test_hk_effective_default_includes_qtr_hk_and_idempotency(self,
                                                                  tmp_path):
        """A11：省略 forms 的 request hash 使用 v1.0.3 新默认（含 QTR-HK）；
        显式旧类型（ANNUAL/INTERIM）仍可提交并稳定重放。"""
        jobs, store, *_ = _harness(tmp_path)

        def _effective(job_id):
            row = store.connection().execute(
                "SELECT effective_request_json FROM jobs WHERE job_id=?",
                (job_id,)).fetchone()
            return json.loads(row["effective_request_json"])

        omitted = _submit(jobs, key="hk-def-1", symbols=["0700.HK"])
        assert _effective(omitted.job_id)["forms_by_market"]["HK"] == \
            ["ANNUAL", "INTERIM", "QTR-HK"]
        # 省略 forms 与显式新默认视为同一请求（同键重放）
        explicit_default = _submit(jobs, key="hk-def-2",
                                   forms={"HK": ["ANNUAL", "INTERIM",
                                                 "QTR-HK"]},
                                   symbols=["0700.HK"])
        replay = _submit(jobs, key="hk-def-2", symbols=["0700.HK"])
        assert replay.created is False
        assert replay.job_id == explicit_default.job_id
        # 显式旧类型（不含 QTR-HK）是不同的有效请求，可提交并稳定重放
        old_forms = _submit(jobs, key="hk-old-1",
                            forms={"HK": ["ANNUAL", "INTERIM"]},
                            symbols=["0700.HK"])
        assert _effective(old_forms.job_id)["forms_by_market"]["HK"] == \
            ["ANNUAL", "INTERIM"]
        old_replay = _submit(jobs, key="hk-old-1",
                             forms={"HK": ["ANNUAL", "INTERIM"]},
                             symbols=["0700.HK"])
        assert old_replay.created is False
        assert old_replay.job_id == old_forms.job_id
        assert old_forms.job_id != explicit_default.job_id

    def test_queue_limit(self, tmp_path):
        config = _config(max_pending_jobs=1)
        jobs, *_ = _harness(tmp_path, config=config)
        _submit(jobs)
        with pytest.raises(QueueFullError):
            _submit(jobs, key="k2")


class TestExecution:
    def test_progress_counts_from_queued(self, tmp_path):
        """progress 从 queued 起即反映提交的规范化代码数（非已持久化结果数）。"""
        jobs, store, *_ = _harness(tmp_path)
        submitted = _submit(jobs, symbols=["AAPL", "aapl", "MSFT"])  # 去重 → 2
        doc = jobs.get_job(submitted.job_id)
        assert doc["progress"] == {"symbols_total": 2, "symbols_finished": 0}
        job_id = jobs.claim_next()
        assert jobs.get_job(job_id)["status"] == "running"
        assert jobs.get_job(job_id)["progress"] == {
            "symbols_total": 2, "symbols_finished": 0}

    def test_claim_and_run_job(self, tmp_path):
        jobs, store, *_ = _harness(tmp_path)
        submitted = _submit(jobs, symbols=["AAPL"], last_n=4)
        job_id = jobs.claim_next()
        assert job_id == submitted.job_id
        jobs.run_job(job_id)
        doc = jobs.get_job(job_id)
        # 正常截取为说明性信息（T6）：干净任务归 succeeded
        assert doc["status"] == "succeeded"
        assert doc["attempt"] == 1
        assert doc["summary"]["downloaded"] == 4
        sym = doc["results"][0]
        assert sym["status"] == "succeeded"
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
        # AAPL 仅有正常截取说明（T6 后不再降级）→ succeeded；任务含失败证券 → partial
        assert statuses["AAPL"] == "succeeded"
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


class TestDiscoverySessionRefresh:
    """PHASE1_REVIEW T2：常驻服务的元数据缓存按任务（发现会话）失效。"""

    def _newer_rows(self):
        from tests.test_us_edgar import _interleave_junk, _recent_fixture
        rows = _interleave_junk(_recent_fixture()["periodic_rows_head"])
        rows.insert(1, {  # 来源新发布的申报
            "form": "10-Q", "filingDate": "2026-10-30",
            "reportDate": "2026-09-26",
            "accessionNumber": "0000320193-26-000099",
            "primaryDocument": "aapl-20260926.htm",
        })
        return rows

    def test_next_job_sees_newly_published_filing(self, tmp_path):
        from tests.test_us_edgar import (
            SUBMISSIONS_URL,
            _json_response,
            _submissions_payload,
        )
        jobs, store, service, session = _harness(tmp_path)
        _submit(jobs, key="a")
        jobs.run_job(jobs.claim_next())
        first = jobs.get_job(jobs.store.connection().execute(
            "SELECT job_id FROM jobs ORDER BY created_at LIMIT 1"
        ).fetchone()["job_id"])
        new_source_id = "0000320193-26-000099/aapl-20260926.htm"
        assert all(i["source_id"] != new_source_id
                   for i in first["results"][0]["items"])

        # 来源新增申报：更换 submissions 响应后，新任务必须能看到
        session.mapping[SUBMISSIONS_URL] = _json_response(
            _submissions_payload(self._newer_rows()))
        from tests.conftest import FakeResponse, HTML_DOC
        new_doc_url = ("https://www.sec.gov/Archives/edgar/data/320193/"
                       "000032019326000099/aapl-20260926.htm")
        session.mapping[new_doc_url] = FakeResponse(
            200, HTML_DOC, headers={"Content-Type": "text/html",
                                    "Content-Length": str(len(HTML_DOC))},
            url=new_doc_url)
        _submit(jobs, key="b", last_n=5)
        jobs.run_job(jobs.claim_next())
        second = jobs.get_job(jobs.store.connection().execute(
            "SELECT job_id FROM jobs ORDER BY created_at LIMIT 1 OFFSET 1"
        ).fetchone()["job_id"])
        assert any(i["source_id"] == new_source_id
                   for i in second["results"][0]["items"])
        # refresh 不被旧列表阻挡：新申报下载、既有原文命中缓存
        assert second["summary"]["downloaded"] == 1
        assert second["summary"]["cached"] == 4
        # 每任务重新获取 submissions（两次请求），而非永久缓存
        submissions_calls = [r for r in session.requests
                             if r[1] == SUBMISSIONS_URL]
        assert len(submissions_calls) == 2

    def test_session_shares_ticker_map_within_job(self, tmp_path):
        from tests.test_us_edgar import (
            SUBMISSIONS_URL,
            TICKERS_URL,
            _json_response,
            _submissions_payload,
        )
        jobs, store, service, session = _harness(tmp_path)
        session.mapping["https://data.sec.gov/submissions/CIK0001652044.json"] \
            = _json_response(_submissions_payload([]))
        _submit(jobs, symbols=["AAPL", "GOOG"])
        jobs.run_job(jobs.claim_next())
        # 同一任务内两个 US 证券共享一次 ticker 映射获取
        ticker_calls = [r for r in session.requests if r[1] == TICKERS_URL]
        assert len(ticker_calls) == 1


class TestAtomicSubmit:
    """PHASE1_REVIEW T3：幂等查找、队列限额与插入在同一写事务内。"""

    @staticmethod
    def _run_concurrently(fn) -> list:
        import threading
        barrier = threading.Barrier(2)
        results: list = []

        def worker():
            barrier.wait()
            try:
                results.append(("ok", fn()))
            except Exception as e:  # noqa: BLE001
                results.append(("err", e))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def test_same_key_same_request_yields_single_job(self, tmp_path):
        jobs, store, *_ = _harness(tmp_path)
        results = self._run_concurrently(
            lambda: _submit(jobs, key="k", symbols=["AAPL"]))
        oks = [r[1] for r in results if r[0] == "ok"]
        assert len(oks) == 2, results
        assert len({s.job_id for s in oks}) == 1      # 同一 job_id
        assert sum(1 for s in oks if s.created) == 1  # 只创建一次
        n = store.connection().execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
        assert n == 1

    def test_same_key_different_request_one_conflict(self, tmp_path):
        jobs, *_ = _harness(tmp_path)
        outcomes: dict[int, object] = {}

        def worker(i):
            return i

        import threading
        barrier = threading.Barrier(2)

        def run(index, last_n):
            barrier.wait()
            try:
                _submit(jobs, key="k", symbols=["AAPL"], last_n=last_n)
                outcomes[index] = "ok"
            except JobConflictError:
                outcomes[index] = "conflict"

        threads = [threading.Thread(target=run, args=(i, 3 + i))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(outcomes.values()) == ["conflict", "ok"]

    def test_different_keys_compete_for_last_queue_slot(self, tmp_path):
        config = _config(max_pending_jobs=1)
        jobs, store, *_ = _harness(tmp_path, config=config)
        import threading
        barrier = threading.Barrier(2)
        outcomes: dict[int, str] = {}

        def run(index, key):
            barrier.wait()
            try:
                _submit(jobs, key=key)
                outcomes[index] = "ok"
            except QueueFullError:
                outcomes[index] = "full"

        threads = [threading.Thread(target=run, args=(i, f"k{i}"))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(outcomes.values()) == ["full", "ok"]
        n = store.connection().execute(
            "SELECT COUNT(*) n FROM jobs WHERE status='queued'").fetchone()["n"]
        assert n == 1  # 限额不可突破


class TestStatusMatrix:
    """PHASE1_REVIEW T6：按实际可用文件与质量缺口汇总状态。"""

    def test_all_downloads_failed_job_failed(self, tmp_path):
        from tests.conftest import FakeResponse
        from tests.test_us_edgar import TICKERS_URL, SUBMISSIONS_URL
        from tests.test_core_pipeline import _archive_urls
        jobs, store, service, session = _harness(tmp_path)
        for url in _archive_urls():
            session.mapping[url] = FakeResponse(403, b"Forbidden")
        _submit(jobs)
        jobs.run_job(jobs.claim_next())
        job_id = store.connection().execute(
            "SELECT job_id FROM jobs LIMIT 1").fetchone()["job_id"]
        doc = jobs.get_job(job_id)
        assert doc["status"] == "failed"
        assert doc["results"][0]["status"] == "failed"
        assert doc["summary"] == {"downloaded": 0, "cached": 0, "failed": 4}
        assert doc["results"][0]["report_ids"] == []

    def test_partial_download_failure_job_partial(self, tmp_path):
        from tests.conftest import FakeResponse
        from tests.test_core_pipeline import _archive_urls
        jobs, store, service, session = _harness(tmp_path)
        bad_url = _archive_urls()[0]
        session.mapping[bad_url] = FakeResponse(403, b"Forbidden")
        _submit(jobs)
        jobs.run_job(jobs.claim_next())
        job_id = store.connection().execute(
            "SELECT job_id FROM jobs LIMIT 1").fetchone()["job_id"]
        doc = jobs.get_job(job_id)
        assert doc["status"] == "partial"
        assert doc["summary"] == {"downloaded": 3, "cached": 0, "failed": 1}
        assert len(doc["results"][0]["report_ids"]) == 3

    def test_insufficient_history_is_partial(self, tmp_path):
        jobs, store, *_ = _harness(tmp_path)
        _submit(jobs, last_n=20)   # 来源只有 12 组 < 20
        jobs.run_job(jobs.claim_next())
        job_id = store.connection().execute(
            "SELECT job_id FROM jobs LIMIT 1").fetchone()["job_id"]
        doc = jobs.get_job(job_id)
        assert doc["status"] == "partial"
        coverage = doc["results"][0]["coverage"]
        assert coverage["insufficient_history"] is True
        assert coverage["selected"] == 12 < coverage["requested"] == 20

    def test_clean_job_succeeded_with_notice(self, tmp_path):
        jobs, store, *_ = _harness(tmp_path)
        _submit(jobs, last_n=4)
        jobs.run_job(jobs.claim_next())
        job_id = store.connection().execute(
            "SELECT job_id FROM jobs LIMIT 1").fetchone()["job_id"]
        doc = jobs.get_job(job_id)
        assert doc["status"] == "succeeded"
        assert doc["results"][0]["warnings"] == []
        assert doc["results"][0]["coverage"].get("notices")  # 截取为说明
