"""us_edgar：SEC 契约解析（fixture 驱动，不触网；DESIGN §8 / §16）。"""
from __future__ import annotations

import pytest

from reports_fetcher.adapters.us_edgar import USEdgarAdapter, _iter_parallel_rows
from reports_fetcher.config import HttpConfig
from reports_fetcher.models import (
    DocumentRole,
    Market,
    PeriodSource,
    ReportQuery,
    ResolveError,
    SourceContractChangedError,
    UaNotConfiguredError,
)
from reports_fetcher.selection import select_reports
from reports_fetcher.symbol import normalize_symbol
from tests.conftest import (
    FakeResponse,
    UrlMapSession,
    load_fixture,
    make_transport,
)

UA = "reports-fetcher/0.1.0 (test@example.com)"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0000320193.json"
HISTFILE_URL = "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json"


def _ticker_payload():
    fixture = load_fixture("us/company_tickers_sample.json")
    return {str(i): hit for i, hit in enumerate(fixture["hits"])}


def _recent_fixture():
    return load_fixture("us/submissions_aapl_recent.json")


def _interleave_junk(periodic_rows):
    """在定期行之间穿插非定期 form，模拟真实 recent 的形态分布。"""
    rows = []
    for i, row in enumerate(periodic_rows):
        rows.append({
            "form": "4", "filingDate": row["filingDate"], "reportDate": "",
            "acceptanceDateTime": f"2026-01-0{i+1}T00:00:00.000Z",
            "accessionNumber": f"0000320193-26-junk{i:04d}",
            "primaryDocument": "x-form4.xml",
        })
        rows.append(row)
    return rows


def _recent_arrays(rows, keys):
    return {k: [r.get(k, "") for r in rows] for k in keys}


def _submissions_payload(rows, files=None):
    recent = _recent_fixture()
    return {
        "name": recent["name"], "cik": recent["cik"],
        "tickers": recent["tickers"], "exchanges": ["Nasdaq"],
        "filings": {
            "recent": _recent_arrays(rows, recent["recent_keys"]),
            "files": files if files is not None else recent["filings_files"],
        },
    }


def _hist_payload():
    fixture = load_fixture("us/submissions_aapl_histfile.json")
    return {k: [r.get(k, "") for r in fixture["tail_rows"]]
            for k in fixture["top_level_keys"]}


def _make_adapter(mapping, *, user_agent=UA, config=None):
    session = UrlMapSession(mapping)
    from tests.conftest import make_config
    cfg = config or make_config(user_agent=user_agent)
    transport = make_transport(session, config=HttpConfig(user_agent=user_agent))
    return USEdgarAdapter(transport, cfg), session


def _json_response(payload):
    import json
    return FakeResponse(200, json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"})


def _adapter_with_recent(rows=None, extra_urls=None, **kwargs):
    recent = _recent_fixture()
    rows = rows if rows is not None else _interleave_junk(recent["periodic_rows_head"])
    mapping = {
        TICKERS_URL: _json_response(_ticker_payload()),
        SUBMISSIONS_URL: _json_response(_submissions_payload(rows)),
    }
    mapping.update(extra_urls or {})
    return _make_adapter(mapping, **kwargs)


class TestResolve:
    def test_aapl(self):
        adapter, session = _adapter_with_recent()
        resolved = adapter.resolve(normalize_symbol("aapl"))
        assert resolved.market is Market.US
        assert resolved.symbol == "AAPL"
        assert resolved.source_issuer_id == "0000320193"
        assert resolved.issuer_id == "sec:0000320193"
        assert resolved.display_name == "Apple Inc."
        assert resolved.exchange == "Nasdaq"
        # UA 必须随全部请求发送（NFR-5）
        assert all(r[2]["headers"]["User-Agent"] == UA for r in session.requests)

    def test_class_share_hyphen_ticker(self):
        adapter, _ = _adapter_with_recent(extra_urls={
            "https://data.sec.gov/submissions/CIK0001067983.json":
                _json_response(_submissions_payload(
                    _recent_fixture()["periodic_rows_head"])),
        })
        resolved = adapter.resolve(normalize_symbol("BRK-B"))
        assert resolved.source_issuer_id == "0001067983"

    def test_unknown_ticker(self):
        adapter, _ = _make_adapter({
            TICKERS_URL: _json_response(_ticker_payload()),
        })
        with pytest.raises(ResolveError) as ei:
            adapter.resolve(normalize_symbol("ZZZZ"))
        assert ei.value.code == "symbol_not_found"

    def test_missing_ua_fails_before_any_request(self):
        adapter, session = _make_adapter(
            {TICKERS_URL: _json_response(_ticker_payload())}, user_agent="")
        with pytest.raises(UaNotConfiguredError):
            adapter.resolve(normalize_symbol("AAPL"))
        assert session.requests == []  # 未配置 UA 时不发任何请求

    def test_submissions_404_xml_is_resolve_error(self):
        notfound = load_fixture("us/submissions_notfound.json")
        adapter, _ = _make_adapter({
            TICKERS_URL: _json_response(_ticker_payload()),
            SUBMISSIONS_URL: FakeResponse(
                notfound["status"], notfound["body_head"].encode("utf-8"),
                headers={"Content-Type": notfound["content_type"]}),
        })
        with pytest.raises(ResolveError) as ei:
            adapter.resolve(normalize_symbol("AAPL"))
        assert ei.value.code == "symbol_not_found"


class TestListReports:
    def test_candidates_from_fixture(self):
        adapter, _ = _adapter_with_recent()
        resolved = adapter.resolve(normalize_symbol("AAPL"))
        discovery = adapter.list_reports(resolved, ReportQuery(last_n=4))
        # 12 条定期行（10-Q×9 + 10-K×3），穿插的 form 4 被过滤
        assert len(discovery.reports) == 12
        assert {r.doc_type for r in discovery.reports} == {"10-Q", "10-K"}
        for report in discovery.reports:
            accession, doc = report.source_id.split("/")
            assert accession.startswith("0000320193-")
            assert doc.endswith(".htm")
            assert report.language == "en"
            assert report.document_role is DocumentRole.FULL_REPORT

    def test_reportdate_is_authoritative_period(self):
        adapter, _ = _adapter_with_recent()
        resolved = adapter.resolve(normalize_symbol("AAPL"))
        discovery = adapter.list_reports(resolved, ReportQuery(last_n=4))
        ten_k = [r for r in discovery.reports
                 if r.source_id.startswith("0000320193-25-000079")]
        assert ten_k and ten_k[0].report_period == "2025-09-27"  # 财年 9 月止
        assert ten_k[0].period_source is PeriodSource.SOURCE_FIELD
        assert ten_k[0].filing_date == "2025-10-31"

    def test_selection_last_4(self):
        adapter, _ = _adapter_with_recent()
        resolved = adapter.resolve(normalize_symbol("AAPL"))
        discovery = adapter.list_reports(resolved, ReportQuery(last_n=4))
        selection = select_reports(
            discovery.reports, ReportQuery(last_n=4),
            language_preference=["en"])
        assert [r.report_period for r in selection.selected] == [
            "2026-06-27", "2026-03-28", "2025-12-27", "2025-09-27"]
        assert selection.selected[-1].doc_type == "10-K"

    def test_array_length_mismatch_is_contract_change(self):
        recent = _recent_fixture()
        rows = _interleave_junk(recent["periodic_rows_head"])
        arrays = _recent_arrays(rows, recent["recent_keys"])
        arrays["filingDate"] = arrays["filingDate"][:-1]  # 长度不等
        session = UrlMapSession({
            TICKERS_URL: _json_response(_ticker_payload()),
            SUBMISSIONS_URL: _json_response({
                "name": "Apple Inc.", "cik": "0000320193", "tickers": ["AAPL"],
                "filings": {"recent": arrays, "files": []}}),
        })
        from tests.conftest import make_config
        adapter = USEdgarAdapter(
            make_transport(session, config=HttpConfig(user_agent=UA)),
            make_config(user_agent=UA))
        resolved = adapter.resolve(normalize_symbol("AAPL"))
        with pytest.raises(SourceContractChangedError):
            adapter.list_reports(resolved, ReportQuery(last_n=4))

    def test_amendment_kept_as_unverified_relation(self):
        """PHASE1_REVIEW T4：/A 元数据无完整性证据 → 不覆盖原全文。

        保留 source_form/is_amendment/revision_of 与警告；组内有原全文时
        选择原全文并警告未合并；缺证据的修订不能独自计作完整财报。
        """
        rows = _interleave_junk(_recent_fixture()["periodic_rows_head"])
        rows.insert(1, {  # 插在首条定期行前：同组修订版
            "form": "10-K/A", "filingDate": "2025-12-15",
            "reportDate": "2025-09-27",
            "acceptanceDateTime": "2025-12-15T18:00:00.000Z",
            "accessionNumber": "0000320193-25-000099",
            "primaryDocument": "aapl-20250927x10ka.htm",
        })
        adapter, _ = _adapter_with_recent(rows)
        resolved = adapter.resolve(normalize_symbol("AAPL"))
        discovery = adapter.list_reports(resolved, ReportQuery(last_n=4))
        amend = [r for r in discovery.reports if r.is_amendment]
        assert len(amend) == 1
        assert amend[0].doc_type == "10-K"          # /A 归入基础类型
        assert amend[0].source_form == "10-K/A"
        assert amend[0].document_role is DocumentRole.UNKNOWN  # 未确认全文
        assert amend[0].revision_of == "0000320193-25-000079/aapl-20250927.htm"
        assert any("未确认全文重发" in w for w in discovery.warnings)
        selection = select_reports(discovery.reports, ReportQuery(last_n=4),
                                   language_preference=["en"])
        chosen = [r for r in selection.selected
                  if r.report_period == "2025-09-27"]
        # 原全文胜出；未合并修订必须警告
        assert chosen[0].source_id == "0000320193-25-000079/aapl-20250927.htm"
        assert chosen[0].document_role is DocumentRole.FULL_REPORT
        assert any("未合并" in w for w in selection.warnings)

    def test_unverified_amendment_alone_not_full(self):
        """组内仅有缺证据修订（无原全文）→ 不计作完整财报。"""
        row = {
            "form": "10-K/A", "filingDate": "2025-12-15",
            "reportDate": "2025-09-27",
            "accessionNumber": "0000320193-25-000099",
            "primaryDocument": "aapl-20250927x10ka.htm",
        }
        adapter, _ = _adapter_with_recent([row])
        resolved = adapter.resolve(normalize_symbol("AAPL"))
        discovery = adapter.list_reports(resolved, ReportQuery(last_n=4))
        selection = select_reports(discovery.reports, ReportQuery(last_n=4),
                                   language_preference=["en"])
        assert selection.selected_count == 0
        assert any("未确认修订" in w for w in selection.warnings)

    def test_empty_reportdate_becomes_unknown_with_warning(self):
        rows = [dict(r) for r in _interleave_junk(_recent_fixture()["periodic_rows_head"])]
        periodic = next(r for r in rows if r["form"] == "10-Q")
        periodic["reportDate"] = ""
        adapter, _ = _adapter_with_recent(rows)
        resolved = adapter.resolve(normalize_symbol("AAPL"))
        discovery = adapter.list_reports(resolved, ReportQuery(last_n=4))
        target = [r for r in discovery.reports
                  if r.source_id == f"{periodic['accessionNumber']}/{periodic['primaryDocument']}"]
        assert target and target[0].report_period is None
        assert target[0].period_source is PeriodSource.UNKNOWN
        assert any("reportDate" in w for w in discovery.warnings)


class TestHistoricalFiles:
    def _adapter_with_files(self, *, lookback, last_n=20, budget=100):
        adapter, session = _adapter_with_recent(extra_urls={
            HISTFILE_URL: _json_response(_hist_payload())})
        return adapter, session, ReportQuery(
            last_n=last_n, max_lookback_years=lookback,
            max_discovery_requests=budget)

    def test_files_fetched_on_demand_when_recent_insufficient(self):
        adapter, session, query = self._adapter_with_files(lookback=40)
        resolved = adapter.resolve(normalize_symbol("AAPL"))
        discovery = adapter.list_reports(resolved, query)
        urls = [r[1] for r in session.requests]
        assert HISTFILE_URL in urls  # recent 12 组 < last_n 20 → 按需读历史
        # 老申报 primaryDocument 为空串 → 跳过并警告（fixture 留证）
        assert any("primaryDocument 为空" in w for w in discovery.warnings)
        assert len(discovery.reports) == 12  # 历史行全部跳过，无新增候选
        assert discovery.exhausted is True
        assert discovery.truncated is False

    def test_files_skipped_within_lookback_window(self):
        adapter, session, query = self._adapter_with_files(lookback=10)
        resolved = adapter.resolve(normalize_symbol("AAPL"))
        discovery = adapter.list_reports(resolved, query)
        urls = [r[1] for r in session.requests]
        assert HISTFILE_URL not in urls  # filingTo 2015 < 10 年回溯线，不读
        assert discovery.exhausted is True

    def test_discovery_budget_truncates(self):
        adapter, _session, query = self._adapter_with_files(lookback=40, budget=1)
        resolved = adapter.resolve(normalize_symbol("AAPL"))
        discovery = adapter.list_reports(resolved, query)
        assert discovery.truncated is True
        assert discovery.exhausted is False  # 预算耗尽不得宣称窗口穷尽
        assert any("预算耗尽" in w for w in discovery.warnings)


class TestIterParallelRows:
    def test_yields_rows(self):
        arrays = {"form": ["10-K", "4"], "accessionNumber": ["a1", "a2"],
                  "primaryDocument": ["d1", "d2"]}
        rows = list(_iter_parallel_rows(arrays, source="t"))
        assert rows == [
            {"form": "10-K", "accessionNumber": "a1", "primaryDocument": "d1"},
            {"form": "4", "accessionNumber": "a2", "primaryDocument": "d2"},
        ]

    def test_mismatch_raises(self):
        arrays = {"form": ["10-K"], "accessionNumber": ["a1", "a2"]}
        with pytest.raises(SourceContractChangedError):
            list(_iter_parallel_rows(arrays, source="t"))

    def test_non_dict_raises(self):
        with pytest.raises(SourceContractChangedError):
            list(_iter_parallel_rows(["not", "dict"], source="t"))
