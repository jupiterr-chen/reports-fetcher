"""core 全链路离线集成测试：fake transport 驱动 download → cached → refresh。

US 契约 fixture + 真实 Store/Selection/Core 编排，验证 FR-5/FR-6 行为闭环
（不触网；下载体为通过内容校验的合成 HTML）。
"""
from __future__ import annotations

import hashlib
import os

import pytest

from reports_fetcher.config import Config, HttpConfig
from reports_fetcher.core import FetchService
from reports_fetcher.models import (
    OUTCOME_CACHED,
    OUTCOME_DOWNLOADED,
    ReportsFetcherError,
)
from reports_fetcher.store import Store
from tests.conftest import (
    FakeResponse,
    HTML_DOC,
    UrlMapSession,
    make_transport,
)
from tests.test_us_edgar import (
    SUBMISSIONS_URL,
    TICKERS_URL,
    UA,
    _interleave_junk,
    _json_response,
    _recent_fixture,
    _submissions_payload,
    _ticker_payload,
)

_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data/320193/"

HTML_DOC_V2 = HTML_DOC + b"<!-- version two -->" + b"</body></html>"


def _archive_urls() -> list[str]:
    recent = _recent_fixture()
    return [f"{_ARCHIVE_BASE}{row['accessionNumber'].replace('-', '')}/"
            f"{row['primaryDocument']}"
            for row in recent["periodic_rows_head"]]


def _build_mapping(doc_contents: dict[str, bytes] | None = None) -> dict:
    mapping = {
        TICKERS_URL: _json_response(_ticker_payload()),
        SUBMISSIONS_URL: _json_response(_submissions_payload(
            _interleave_junk(_recent_fixture()["periodic_rows_head"]))),
    }
    for url in _archive_urls():
        body = (doc_contents or {}).get(url, HTML_DOC)
        mapping[url] = FakeResponse(
            200, body,
            headers={"Content-Type": "text/html",
                     "Content-Length": str(len(body))},
            url=url)
    return mapping


def _service(tmp_path, doc_contents=None) -> tuple[FetchService, Store]:
    store = Store(tmp_path / "archive")
    config = Config()
    config.fetch.market_workers = 1  # 假 session 串行更稳
    session = UrlMapSession(_build_mapping(doc_contents))
    transport = make_transport(session, config=HttpConfig(user_agent=UA))
    return FetchService(config, store, transport), store


class TestFetchPipeline:
    def test_fetch_download_then_cached(self, tmp_path):
        service, store = _service(tmp_path)
        try:
            batch = service.fetch(["AAPL"], last_n=4)
            result = batch.results[0]
            assert result.status in ("ok", "partial")
            assert len(result.items) == 4
            assert all(i.outcome == OUTCOME_DOWNLOADED for i in result.items)
            for item in result.items:
                assert item.local_path and os.path.isfile(item.local_path)
            # 重跑：全部缓存命中，无新增 artifact
            batch2 = service.fetch(["AAPL"], last_n=4)
            assert all(i.outcome == OUTCOME_CACHED
                       for i in batch2.results[0].items)
            artifacts = store.connection().execute(
                "SELECT COUNT(*) AS n FROM artifacts").fetchone()["n"]
            assert artifacts == 4
        finally:
            service.close()

    def test_refresh_identical_content_reuses_no_duplicate(self, tmp_path):
        service, store = _service(tmp_path)
        try:
            service.fetch(["AAPL"], last_n=4)
            batch = service.fetch(["AAPL"], last_n=4, refresh=True)
            items = batch.results[0].items
            assert all(i.outcome == OUTCOME_DOWNLOADED for i in items)
            assert all("内容未变化" in (i.detail or "") for i in items)
            artifacts = store.connection().execute(
                "SELECT COUNT(*) AS n FROM artifacts").fetchone()["n"]
            assert artifacts == 4  # 内容一致：复用，不重复归档
        finally:
            service.close()

    def test_refresh_changed_content_creates_new_artifact_keeps_old(
            self, tmp_path):
        service1, store = _service(tmp_path)
        try:
            service1.fetch(["AAPL"], last_n=1)
        finally:
            service1.close()
        first_report = store.connection().execute(
            """SELECT m.report_id, a.artifact_id, a.sha256 FROM manifest m
               JOIN artifacts a ON a.artifact_id = m.current_artifact_id
               WHERE m.symbol='AAPL'""").fetchone()
        # 源站内容变化：同一 report 产生新 artifact，旧版本按 ID 可读
        changed_url = _archive_urls()[0]
        service2, store2 = _service(tmp_path, doc_contents={
            changed_url: HTML_DOC_V2})
        try:
            batch = service2.fetch(["AAPL"], last_n=1, refresh=True)
            item = batch.results[0].items[0]
            assert item.outcome == OUTCOME_DOWNLOADED
            assert "内容未变化" not in (item.detail or "")
            row = store2.connection().execute(
                """SELECT a.artifact_id FROM manifest m
                   JOIN artifacts a ON a.artifact_id = m.current_artifact_id
                   WHERE m.report_id=?""",
                (first_report["report_id"],)).fetchone()
            assert row["artifact_id"] != first_report["artifact_id"]
            # 旧 artifact 仍可按 ID 读取且 checksum 一致（DoD #2）
            path, old_row = store2.open_artifact(first_report["artifact_id"])
            assert old_row["sha256"] == first_report["sha256"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == \
                   first_report["sha256"]
            versions = store2.connection().execute(
                "SELECT COUNT(*) AS n FROM artifacts WHERE report_id=?",
                (first_report["report_id"],)).fetchone()["n"]
            assert versions == 2
        finally:
            service2.close()

    def test_owner_lock_second_writer_rejected(self, tmp_path):
        service, store = _service(tmp_path)
        try:
            store.acquire_owner_lock()
            rival = Store(tmp_path / "archive")
            with pytest.raises(ReportsFetcherError) as ei:
                rival.acquire_owner_lock()
            assert ei.value.code == "store_in_use"
        finally:
            service.close()  # 释放锁
        fresh = Store(tmp_path / "archive")
        fresh.acquire_owner_lock()
        fresh.close()
