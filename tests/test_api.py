"""HTTP API（HTTP_API §6/§8；TestClient + fake transport，不触网）。"""
from __future__ import annotations

import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from reports_fetcher.api import create_app
from reports_fetcher.config import Config, HttpConfig
from reports_fetcher.core import FetchService
from reports_fetcher.jobs import JobService
from reports_fetcher.store import Store
from tests.conftest import (
    CountingLimiter,
    FakeResponse,
    HTML_DOC,
    UrlMapSession,
    make_transport,
)
from tests.test_core_pipeline import HTML_DOC_V2, _archive_urls, _build_mapping
from tests.test_us_edgar import UA


def _config(**server_overrides) -> Config:
    config = Config()
    config.http.user_agent = UA
    config.fetch.market_workers = 1
    for key, value in server_overrides.items():
        setattr(config.server, key, value)
    return config


class Harness:
    def __init__(self, tmp_path, *, config=None, tokens=None,
                 doc_contents=None):
        self.config = config or _config()
        self.store = Store(tmp_path / "archive")
        self.session = UrlMapSession(_build_mapping(doc_contents))
        transport = make_transport(self.session,
                                   config=HttpConfig(user_agent=UA))
        self.service = FetchService(self.config, self.store, transport)
        self.jobs = JobService(self.config, self.store,
                               fetch_service_factory=lambda: self.service)
        self.app = create_app(self.config, self.jobs, tokens=tokens)
        self.client = TestClient(self.app)

    def submit(self, key="k1", *, symbols=("AAPL",), last_n=4, forms=None,
               refresh=False, headers=None):
        body = {"symbols": list(symbols), "last_n": last_n, "refresh": refresh}
        if forms is not None:
            body["forms_by_market"] = forms
        return self.client.post(
            "/api/v1/fetch-jobs", json=body,
            headers={"Idempotency-Key": key, **(headers or {})})

    def run_to_completion(self, key="k1", **kwargs):
        resp = self.submit(key, **kwargs)
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        job_id = self.jobs.claim_next()
        self.jobs.run_job(job_id)
        return job_id


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.jobs.stop()


class TestSubmit:
    def test_accepted_202_with_location(self, h):
        resp = h.submit()
        assert resp.status_code == 202
        assert resp.headers["Location"].startswith("/api/v1/fetch-jobs/job_")
        assert resp.headers["Retry-After"] == "2"
        body = resp.json()
        assert body["status"] == "queued"
        assert body["status_url"].endswith(body["job_id"])

    def test_missing_idempotency_key_400(self, h):
        resp = h.client.post("/api/v1/fetch-jobs",
                             json={"symbols": ["AAPL"]})
        assert resp.status_code == 400
        assert resp.headers["content-type"].startswith(
            "application/problem+json")
        assert resp.json()["code"] == "missing_idempotency_key"

    def test_bad_idempotency_key_400(self, h):
        resp = h.submit(key="k" * 129)  # 超 128 字符
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_idempotency_key"

    def test_unknown_field_422(self, h):
        resp = h.client.post(
            "/api/v1/fetch-jobs", json={"symbols": ["AAPL"], "evil": 1},
            headers={"Idempotency-Key": "kx"})
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_request"

    @pytest.mark.parametrize("body", [
        {"symbols": []},
        {"symbols": ["AAPL"], "last_n": 21},
        {"symbols": ["AAPL"], "last_n": 0},
        {"symbols": ["###"]},          # 格式非法（提交期拒绝）
    ])
    def test_validation_422(self, h, body):
        resp = h.client.post(
            "/api/v1/fetch-jobs", json=body,
            headers={"Idempotency-Key": "kv"})
        assert resp.status_code == 422
        problem = resp.json()
        assert problem["code"] == "invalid_request"
        assert problem["request_id"]

    def test_unsupported_form_422(self, h):
        resp = h.submit(forms={"US": ["6-K"]})
        assert resp.status_code == 422

    def test_non_json_415(self, h):
        resp = h.client.post(
            "/api/v1/fetch-jobs", content=b"symbols=AAPL",
            headers={"Idempotency-Key": "kc",
                     "Content-Type": "application/x-www-form-urlencoded"})
        assert resp.status_code == 415

    def test_replay_unfinished_202_finished_200(self, h):
        first = h.submit()
        job_id = first.json()["job_id"]
        replay = h.submit()
        assert replay.status_code == 202
        assert replay.json()["job_id"] == job_id
        claimed = h.jobs.claim_next()
        h.jobs.run_job(claimed)
        finished = h.submit()
        assert finished.status_code == 200

    def test_conflict_409(self, h):
        h.submit()
        resp = h.submit(last_n=2)
        assert resp.status_code == 409
        assert resp.json()["code"] == "idempotency_conflict"

    def test_queue_full_429(self, tmp_path):
        harness = Harness(tmp_path, config=_config(max_pending_jobs=1))
        try:
            assert harness.submit(key="a").status_code == 202
            resp = harness.submit(key="b")
            assert resp.status_code == 429
            assert resp.headers["Retry-After"]
            assert resp.json()["code"] == "queue_full"
        finally:
            harness.jobs.stop()


class TestAuth:
    def test_local_mode_no_auth_needed(self, h):
        assert h.client.get("/health/live").status_code == 200

    def test_token_mode(self, tmp_path):
        harness = Harness(tmp_path, tokens={"s3cret": "app-a"})
        try:
            no_auth = harness.client.post(
                "/api/v1/fetch-jobs", json={"symbols": ["AAPL"]},
                headers={"Idempotency-Key": "k"})
            assert no_auth.status_code == 401
            assert no_auth.headers.get("WWW-Authenticate") == "Bearer"
            wrong = harness.client.post(
                "/api/v1/fetch-jobs", json={"symbols": ["AAPL"]},
                headers={"Idempotency-Key": "k",
                         "Authorization": "Bearer nope"})
            assert wrong.status_code == 403
            ok = harness.client.post(
                "/api/v1/fetch-jobs", json={"symbols": ["AAPL"]},
                headers={"Idempotency-Key": "k",
                         "Authorization": "Bearer s3cret"})
            assert ok.status_code == 202
            # 任务读权限限定提交应用
            job_id = ok.json()["job_id"]
            invisible = TestClient(create_app(
                harness.config,
                JobService(harness.config, harness.store),
                tokens={"other": "app-b"}))
            resp = invisible.get(
                f"/api/v1/fetch-jobs/{job_id}",
                headers={"Authorization": "Bearer other"})
            assert resp.status_code == 404
        finally:
            harness.jobs.stop()


class TestJobQuery:
    def test_get_job_shape(self, h):
        job_id = h.run_to_completion()
        resp = h.client.get(f"/api/v1/fetch-jobs/{job_id}")
        assert resp.status_code == 200
        doc = resp.json()
        assert doc["job_id"] == job_id
        assert doc["status"] in ("succeeded", "partial")
        assert doc["summary"]["downloaded"] == 4
        item = doc["results"][0]["items"][0]
        assert item["status"] in ("downloaded", "cached")
        assert item["artifact_id"]

    def test_get_job_404_problem(self, h):
        resp = h.client.get("/api/v1/fetch-jobs/job_nope")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith(
            "application/problem+json")
        assert resp.json()["code"] == "not_found"


class TestReports:
    def test_list_filters_and_shape(self, h):
        h.run_to_completion()
        resp = h.client.get("/api/v1/reports",
                            params={"market": "US", "symbol": "AAPL"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 4
        item = body["items"][0]
        for field in ("report_id", "market", "symbol", "source_id",
                      "doc_type", "report_period", "period_source",
                      "artifact_id", "sha256", "bytes", "download_url"):
            assert field in item
        assert item["download_url"].startswith("/api/v1/reports/")

    def test_cursor_pagination_no_duplicates(self, h):
        h.run_to_completion()
        seen: set[str] = set()
        cursor = None
        pages = 0
        while True:
            params = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            body = h.client.get("/api/v1/reports", params=params).json()
            for item in body["items"]:
                assert item["report_id"] not in seen
                seen.add(item["report_id"])
            pages += 1
            cursor = body["next_cursor"]
            if not cursor:
                break
            assert pages < 10
        assert len(seen) == 4 and pages == 2

    def test_period_filter_excludes_unknown(self, h):
        h.run_to_completion()
        body = h.client.get("/api/v1/reports",
                            params={"period_from": "2026-01-01",
                                    "period_to": "2026-12-31"}).json()
        assert body["items"]
        assert all(i["report_period"] for i in body["items"])

    def test_invalid_cursor_400_bad_limit_422(self, h):
        assert h.client.get("/api/v1/reports",
                            params={"cursor": "!!!"}).status_code == 400
        assert h.client.get("/api/v1/reports",
                            params={"limit": 101}).status_code == 422

    def test_detail_with_versions(self, tmp_path):
        harness = Harness(tmp_path)
        try:
            harness.run_to_completion()
            items = harness.client.get("/api/v1/reports").json()["items"]
            report_id = items[0]["report_id"]
            resp = harness.client.get(f"/api/v1/reports/{report_id}")
            assert resp.status_code == 200
            doc = resp.json()
            assert doc["report_id"] == report_id
            assert doc["artifacts"] and doc["artifacts"][0]["is_current"]
        finally:
            harness.jobs.stop()

    def test_detail_404(self, h):
        assert h.client.get("/api/v1/reports/r_nope").status_code == 404


class TestFileDownload:
    def test_download_headers_and_304(self, h):
        h.run_to_completion()
        item = h.client.get("/api/v1/reports").json()["items"][0]
        url = f"/api/v1/reports/{item['report_id']}/file"
        resp = h.client.get(url)
        assert resp.status_code == 200
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert "attachment" in resp.headers["content-disposition"]
        etag = resp.headers["etag"]
        assert etag == f'"{item["sha256"]}"'
        assert hashlib.sha256(resp.content).hexdigest() == item["sha256"]
        not_modified = h.client.get(url, headers={"If-None-Match": etag})
        assert not_modified.status_code == 304

    def test_download_old_artifact_by_id(self, h):
        """DoD/§8-6：refresh 后旧 artifact 可按 ID 读取并校验 checksum。"""
        h.run_to_completion()
        changed = _archive_urls()[0]
        body_v2 = HTML_DOC_V2
        h.session.mapping[changed] = FakeResponse(
            200, body_v2,
            headers={"Content-Type": "text/html",
                     "Content-Length": str(len(body_v2))}, url=changed)
        job_id = h.submit(key="refresh1", refresh=True).json()["job_id"]
        job_id = h.jobs.claim_next()
        h.jobs.run_job(job_id)
        items = h.client.get("/api/v1/reports").json()["items"]
        versioned = None
        for item in items:
            detail = h.client.get(
                f"/api/v1/reports/{item['report_id']}").json()
            if len(detail["artifacts"]) == 2:
                versioned = detail
                break
        assert versioned is not None, "refresh 后应存在双版本报告"
        old = [a for a in versioned["artifacts"] if not a["is_current"]][0]
        resp = h.client.get(
            f"/api/v1/reports/{versioned['report_id']}/file",
            params={"artifact_id": old["artifact_id"]})
        assert resp.status_code == 200
        assert hashlib.sha256(resp.content).hexdigest() == old["sha256"]

    def test_artifact_of_other_report_404(self, h):
        h.run_to_completion()
        items = h.client.get("/api/v1/reports").json()["items"]
        a, b = items[0], items[1]
        resp = h.client.get(f"/api/v1/reports/{b['report_id']}/file",
                            params={"artifact_id": a["artifact_id"]})
        assert resp.status_code == 404

    def test_corrupted_file_409(self, h):
        h.run_to_completion()
        item = h.client.get("/api/v1/reports").json()["items"][0]
        from pathlib import Path
        # 通过 manifest 拿本地路径并损坏（模拟磁盘损坏现场）
        row = h.store.connection().execute(
            "SELECT local_path FROM artifacts WHERE artifact_id=?",
            (item["artifact_id"],)).fetchone()
        Path(row["local_path"]).write_bytes(b"corrupt")
        resp = h.client.get(f"/api/v1/reports/{item['report_id']}/file")
        assert resp.status_code == 409
        assert resp.json()["code"] == "file_not_available"


class TestHealth:
    def test_live_ready(self, h):
        assert h.client.get("/health/live").status_code == 200
        h.jobs.start()
        assert h.client.get("/health/ready").status_code == 200

    def test_ready_503_when_executor_stopped(self, h):
        resp = h.client.get("/health/ready")
        assert resp.status_code == 503
        assert resp.json()["status"] == "unavailable"


class TestNonBlocking:
    def test_health_during_slow_download(self, tmp_path):
        """DoD #4：慢下载期间任务查询与健康检查不被阻塞。"""
        harness = Harness(tmp_path)
        try:
            # 将归档下载替换为真实耗时的慢响应（1.5s/文件）
            for url, resp in list(harness.session.mapping.items()):
                if "/Archives/" not in url:
                    continue

                def slow(method, u, _r=resp, **kw):
                    time.sleep(1.5)
                    return _r
                harness.session.mapping[url] = slow
            harness.jobs.start()
            assert harness.submit(key="slow").status_code == 202
            deadline = time.monotonic() + 5
            running = False
            while time.monotonic() < deadline:
                row = harness.store.connection().execute(
                    "SELECT status FROM jobs LIMIT 1").fetchone()
                if row and row["status"] == "running":
                    running = True
                    break
                time.sleep(0.05)
            assert running
            job_id = harness.store.connection().execute(
                "SELECT job_id FROM jobs LIMIT 1").fetchone()["job_id"]
            start = time.monotonic()
            assert harness.client.get("/health/live").status_code == 200
            assert harness.client.get("/health/ready").status_code == 200
            jobs_resp = harness.client.get(f"/api/v1/fetch-jobs/{job_id}")
            assert jobs_resp.status_code == 200
            elapsed = time.monotonic() - start
            assert elapsed < 1.0, f"查询被阻塞 {elapsed:.2f}s"
        finally:
            harness.jobs.stop(timeout=15)


class TestDocsProtection:
    """PHASE1_REVIEW T7：令牌模式下 docs/OpenAPI/redoc 受保护。"""

    def test_token_mode_docs_protected(self, tmp_path):
        harness = Harness(tmp_path, tokens={"s3cret": "app-a"})
        try:
            # 无凭据：文档与 schema 均拒绝；redoc 直接关闭
            assert harness.client.get("/docs").status_code == 401
            assert harness.client.get("/openapi.json").status_code == 401
            assert harness.client.get("/redoc").status_code == 404
            # 错误凭据
            assert harness.client.get(
                "/docs", headers={"Authorization": "Bearer nope"}
            ).status_code == 403
            # 正确凭据可访问
            headers = {"Authorization": "Bearer s3cret"}
            docs = harness.client.get("/docs", headers=headers)
            spec = harness.client.get("/openapi.json", headers=headers)
            assert docs.status_code == 200 and "swagger" in docs.text.lower()
            assert spec.status_code == 200
            assert "/api/v1/fetch-jobs" in spec.json()["paths"]
        finally:
            harness.jobs.stop()

    def test_local_mode_docs_open(self, h):
        assert h.client.get("/docs").status_code == 200
        assert h.client.get("/openapi.json").status_code == 200
        assert h.client.get("/redoc").status_code == 200
