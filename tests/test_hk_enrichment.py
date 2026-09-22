"""HK 原文报告期富化与警告作用域（fake adapter + fake transport，不触网）。

覆盖任务四项发现中的三项：document 报告期来源、缓存命中回填、last_n 警告
只针对选中报告。PDF 用通过内容校验的合成体，原文提取被 monkeypatch。
"""
from __future__ import annotations

import pytest

from reports_fetcher import core as core_module
from reports_fetcher import document_period
from reports_fetcher.adapters.base import BaseMarketAdapter
from reports_fetcher.config import Config, HttpConfig
from reports_fetcher.core import FetchService
from reports_fetcher.models import (
    DiscoveryResult,
    DocumentRole,
    Market,
    OUTCOME_CACHED,
    OUTCOME_DOWNLOADED,
    PeriodSource,
    Report,
    ResolvedSymbol,
)
from reports_fetcher.selection import PERIOD_UNKNOWN_HINT
from reports_fetcher.store import Store
from tests.conftest import PDF_DOC, FakeResponse, UrlMapSession, make_transport


class _FakeHKAdapter(BaseMarketAdapter):
    market = Market.HK
    base_forms = {"ANNUAL", "INTERIM"}
    default_language_preference = ["zh", "en"]
    source_group = "hkex"

    def __init__(self, transport, config, reports):
        super().__init__(transport, config)
        self._reports = reports

    def resolve(self, normalized):
        return ResolvedSymbol(
            market=Market.HK, symbol=normalized.symbol,
            raw_inputs=[normalized.raw], display_name="騰訊控股",
            exchange="SEHK", source_issuer_id="7609", issuer_id="hkex:7609")

    def list_reports(self, symbol, query):
        return DiscoveryResult(reports=list(self._reports),
                               requested_count=query.last_n)

    def download_headers(self, report):
        return {}


def _hk_report(source_id, title, *, doc_type="ANNUAL", period=None,
               period_source=PeriodSource.UNKNOWN, filing_date="2026-08-25",
               role=DocumentRole.FULL_REPORT, warning="仅有年份标签、无期末日证据，不猜测"):
    metadata = {"subcategory": "年報"}
    if warning:
        metadata["period_warning"] = warning
    return Report(
        market=Market.HK, symbol="00700", source_id=source_id,
        source_url=f"https://www1.hkexnews.hk/{source_id}", title=title,
        doc_type=doc_type, source_form=doc_type, filing_date=filing_date,
        report_period=period, period_source=period_source, language="zh",
        document_role=role, source_issuer_id="7609", source_metadata=metadata)


def _service(tmp_path, reports, *, pdf: bytes = PDF_DOC):
    store = Store(tmp_path / "archive")
    config = Config()
    config.fetch.market_workers = 1
    session = UrlMapSession({
        r.source_url: FakeResponse(
            200, pdf, headers={"Content-Type": "application/pdf",
                               "Content-Length": str(len(pdf))},
            url=r.source_url)
        for r in reports})
    transport = make_transport(session, config=HttpConfig())
    service = FetchService(config, store, transport)
    return service, store


@pytest.fixture
def patch_adapter(monkeypatch):
    def install(reports):
        monkeypatch.setattr(
            core_module, "get_adapter",
            lambda market, transport, config: _FakeHKAdapter(
                transport, config, reports))
    return install


class TestDocumentEnrichment:
    def test_new_download_enriched_and_not_partial(self, tmp_path, monkeypatch,
                                                   patch_adapter):
        reports = [_hk_report("a.pdf", "2025 年報")]
        patch_adapter(reports)
        monkeypatch.setattr(document_period, "extract_report_period",
                            lambda path: "2025-12-31")
        service, store = _service(tmp_path, reports)
        try:
            batch = service.fetch(["0700.HK"], last_n=1)
            result = batch.results[0]
            assert result.items[0].outcome == OUTCOME_DOWNLOADED
            assert not any(PERIOD_UNKNOWN_HINT in w for w in result.warnings)
            assert result.status == "ok"  # 富化成功不得因陈旧警告降级
            row = store.connection().execute(
                "SELECT report_period, period_source FROM manifest "
                "WHERE source_id='a.pdf'").fetchone()
            assert row["report_period"] == "2025-12-31"
            assert row["period_source"] == "document"
        finally:
            service.close()

    def test_extraction_failure_is_non_fatal(self, tmp_path, monkeypatch,
                                             patch_adapter):
        reports = [_hk_report("a.pdf", "2025 年報")]
        patch_adapter(reports)
        monkeypatch.setattr(document_period, "extract_report_period",
                            lambda path: None)
        service, store = _service(tmp_path, reports)
        try:
            batch = service.fetch(["0700.HK"], last_n=1)
            result = batch.results[0]
            assert result.items[0].outcome == OUTCOME_DOWNLOADED
            assert any(PERIOD_UNKNOWN_HINT in w for w in result.warnings)
            assert result.status == "partial"
            row = store.connection().execute(
                "SELECT report_period, period_source FROM manifest "
                "WHERE source_id='a.pdf'").fetchone()
            assert row["report_period"] is None
            assert row["period_source"] == "unknown"
        finally:
            service.close()

    def test_cache_hit_backfills_existing_archive(self, tmp_path, monkeypatch,
                                                  patch_adapter):
        reports = [_hk_report("a.pdf", "2025 年報")]
        patch_adapter(reports)
        monkeypatch.setattr(document_period, "extract_report_period",
                            lambda path: None)
        service, store = _service(tmp_path, reports)
        try:
            first = service.fetch(["0700.HK"], last_n=1)
            assert first.results[0].status == "partial"
            assert any(PERIOD_UNKNOWN_HINT in w
                       for w in first.results[0].warnings)
            # 后续抓取：同一归档命中缓存，从已归档 PDF 回填，不重下
            monkeypatch.setattr(document_period, "extract_report_period",
                                lambda path: "2025-12-31")
            second = service.fetch(["0700.HK"], last_n=1)
            result = second.results[0]
            assert result.items[0].outcome == OUTCOME_CACHED
            assert not any(PERIOD_UNKNOWN_HINT in w for w in result.warnings)
            assert result.status == "ok"
            row = store.connection().execute(
                "SELECT report_period, period_source FROM manifest "
                "WHERE source_id='a.pdf'").fetchone()
            assert row["report_period"] == "2025-12-31"
            assert row["period_source"] == "document"
            # 不因元数据变化产生重复归档
            artifacts = store.connection().execute(
                "SELECT COUNT(*) n FROM artifacts").fetchone()["n"]
            assert artifacts == 1
            # 再次抓取：可信期不被来源未知候选覆盖
            third = service.fetch(["0700.HK"], last_n=1)
            assert third.results[0].status == "ok"
            assert not any(PERIOD_UNKNOWN_HINT in w
                           for w in third.results[0].warnings)
        finally:
            service.close()


class TestWarningScope:
    def test_last_n_1_only_warns_for_selected(self, tmp_path, monkeypatch,
                                              patch_adapter):
        reports = [
            _hk_report("new.pdf", "2026 年報", filing_date="2026-08-25"),
            _hk_report("mid.pdf", "2025 年報", filing_date="2025-08-25"),
            _hk_report("old.pdf", "2024 年報", filing_date="2024-08-25"),
        ]
        patch_adapter(reports)
        monkeypatch.setattr(document_period, "extract_report_period",
                            lambda path: None)
        service, _store = _service(tmp_path, reports)
        try:
            result = service.fetch(["0700.HK"], last_n=1).results[0]
            period_warnings = [w for w in result.warnings
                               if PERIOD_UNKNOWN_HINT in w]
            assert len(period_warnings) == 1  # 仅选中且仍未知的一份
            assert "1 份" in period_warnings[0]
            # 未选中的未知期质量信息在 coverage.notices 聚合一次
            notices = result.coverage["notices"]
            aggregated = [n for n in notices if "未选入 last_n" in n]
            assert len(aggregated) == 1
            assert "2" in aggregated[0]
        finally:
            service.close()
