"""cn_cninfo：巨潮契约解析（fixture 驱动，不触网；DESIGN §6 / §16）。

合成值说明：分页第二页、更新后/更正标题、无年份标题等边界行为无法从现有
fixture 取到真实样本，测试在真实响应结构（first_row_full 字段集）上构造
对应标题值——结构不自造（AGENTS 硬性约定第 5 条）。
"""
from __future__ import annotations

import json

import pytest

from reports_fetcher.adapters.cn_cninfo import CNCninfoAdapter, parse_cn_title
from reports_fetcher.config import HttpConfig
from reports_fetcher.models import (
    DocumentRole,
    Market,
    PeriodSource,
    ReportQuery,
    ResolveError,
    SourceContractChangedError,
)
from reports_fetcher.selection import select_reports
from reports_fetcher.symbol import normalize_symbol
from tests.conftest import (
    FakeResponse,
    UrlMapSession,
    load_fixture,
    make_config,
    make_transport,
)

TOPSEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
INDEX_URL = "http://www.cninfo.com.cn/new/index"
HISANN_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"


def _json_response(payload):
    return FakeResponse(200, json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"})


def _topsearch_payload(code: str):
    fixture = load_fixture("cn/topsearch_samples.json")
    key = {"600519": "sh_600519", "000001": "sz_000001"}[code]
    return fixture[key]


def _row_600519s() -> list[dict]:
    return load_fixture("cn/hisann_600519_p1.json")["rows_head"]


def _row_000001_full() -> dict:
    return load_fixture("cn/hisann_000001_p1.json")["first_row_full"]


def _hisann_response(rows, *, has_more=False, total=None):
    return _json_response({
        "totalAnnouncement": total if total is not None else len(rows),
        "hasMore": has_more,
        "announcements": rows,
    })


def _make_adapter(mapping):
    session = UrlMapSession(mapping)
    cfg = make_config()
    transport = make_transport(session, config=HttpConfig())
    return CNCninfoAdapter(transport, cfg), session


def _make_listing_session(rows=None, *, extra_urls=None):
    """构造 resolve + list 可用的 UrlMapSession（首页预热返回 200）。"""
    rows = rows if rows is not None else _row_600519s()
    mapping = {
        INDEX_URL: FakeResponse(200, b"<html>index</html>",
                                headers={"Content-Type": "text/html"}),
        TOPSEARCH_URL: _json_response(_topsearch_payload("600519")),
        HISANN_URL: _hisann_response(rows),
    }
    mapping.update(extra_urls or {})
    session = UrlMapSession(mapping)
    cfg = make_config()
    adapter = CNCninfoAdapter(make_transport(session, config=HttpConfig()), cfg)
    return adapter, session


def _resolve_and_list(adapter, *, last_n=4, forms=None, lookback=10, budget=100):
    resolved = adapter.resolve(normalize_symbol("600519"))
    return adapter.list_reports(
        resolved, ReportQuery(last_n=last_n, forms=forms,
                              max_lookback_years=lookback,
                              max_discovery_requests=budget))


class TestResolve:
    def test_sh_600519(self):
        adapter, session = _make_adapter({
            INDEX_URL: FakeResponse(200, b"<html>index</html>"),
            TOPSEARCH_URL: _json_response(_topsearch_payload("600519")),
        })
        resolved = adapter.resolve(normalize_symbol("600519.SH"))
        assert resolved.market is Market.CN
        assert resolved.symbol == "600519"
        assert resolved.source_issuer_id == "gssh0600519"
        assert resolved.issuer_id == "cninfo:gssh0600519"
        assert resolved.display_name == "贵州茅台"
        assert resolved.exchange == "SSE"  # 显式后缀信息保留给 resolve
        # 首页预热先于 topSearch（Cookie 收集）
        assert session.requests[0][1] == INDEX_URL
        assert session.requests[1][1] == TOPSEARCH_URL

    def test_sz_000001(self):
        adapter, _ = _make_adapter({
            INDEX_URL: FakeResponse(200, b""),
            TOPSEARCH_URL: _json_response(_topsearch_payload("000001")),
        })
        resolved = adapter.resolve(normalize_symbol("sz000001"))
        assert resolved.source_issuer_id == "gssz0000001"
        assert resolved.exchange == "SZSE"

    def test_nomatch_empty_array(self):
        # 不存在的代码 → 200 + []（fixture 留证），不是网络错误
        adapter, _ = _make_adapter({
            INDEX_URL: FakeResponse(200, b""),
            TOPSEARCH_URL: FakeResponse(200, b"[]",
                                        headers={"Content-Type": "application/json"}),
        })
        with pytest.raises(ResolveError) as ei:
            adapter.resolve(normalize_symbol("999999"))
        assert ei.value.code == "symbol_not_found"

    def test_contract_changed_on_non_list(self):
        adapter, _ = _make_adapter({
            INDEX_URL: FakeResponse(200, b""),
            TOPSEARCH_URL: _json_response({"unexpected": True}),
        })
        with pytest.raises(SourceContractChangedError):
            adapter.resolve(normalize_symbol("600519"))

    def test_warmup_failure_tolerated(self):
        adapter, session = _make_adapter({
            INDEX_URL: FakeResponse(500, b"boom"),
            TOPSEARCH_URL: _json_response(_topsearch_payload("600519")),
        })
        resolved = adapter.resolve(normalize_symbol("600519"))
        assert resolved.source_issuer_id == "gssh0600519"  # 预热失败继续直连


class TestTitleParsing:
    @pytest.mark.parametrize("title,doc_type,period", [
        ("贵州茅台2026年半年度报告", "H1", "2026-06-30"),
        ("贵州茅台2026年第一季度报告", "Q1", "2026-03-31"),
        ("2026年一季度报告", "Q1", "2026-03-31"),          # 000001 变体：无第、无公司前缀
        ("贵州茅台2025年第三季度报告", "Q3", "2025-09-30"),
        ("2025年三季度报告", "Q3", "2025-09-30"),           # 000001 变体
        ("贵州茅台2025年年度报告", "FY", "2025-12-31"),
        ("贵州茅台2025年年度报告（英文版）", "FY", "2025-12-31"),
        ("贵州茅台2025年年度报告摘要", "FY", "2025-12-31"),
        ("贵州茅台2025年半年度报告（更新后）", "H1", "2025-06-30"),
    ])
    def test_period_patterns(self, title, doc_type, period):
        parse = parse_cn_title(title)
        assert parse.doc_type == doc_type
        assert parse.report_period == period

    @pytest.mark.parametrize("title,role,language", [
        ("贵州茅台2026年半年度报告", DocumentRole.FULL_REPORT, "zh"),
        ("贵州茅台2026年半年度报告摘要", DocumentRole.SUMMARY, "zh"),
        ("贵州茅台2025年年度报告（英文版）", DocumentRole.FULL_REPORT, "en"),
        ("贵州茅台2025年半年度报告（更新后）", DocumentRole.AMENDMENT_FULL, "zh"),
        ("平安银行2025年年度报告更正公告", DocumentRole.NOTICE, "zh"),
        ("平安银行2025年年度报告补充公告", DocumentRole.NOTICE, "zh"),
    ])
    def test_role_and_language(self, title, role, language):
        parse = parse_cn_title(title)
        assert parse.document_role is role
        assert parse.language == language

    def test_yearless_title_keeps_period_unknown(self):
        parse = parse_cn_title("平安银行半年度报告")
        assert parse.doc_type == "H1"
        assert parse.report_period is None
        assert parse.notes

    def test_unrecognized_title(self):
        parse = parse_cn_title("关于公司股份回购进展的公告")
        assert parse.doc_type is None


class TestListReports:
    def test_candidates_from_fixture(self):
        adapter, _session = _make_listing_session()
        discovery = _resolve_and_list(adapter)
        # 8 行 fixture：全文×6（含英文版）+ 摘要×2，全部保留为候选
        assert len(discovery.reports) == 8
        by_role = {}
        for report in discovery.reports:
            by_role.setdefault(report.document_role, []).append(report)
        assert len(by_role[DocumentRole.FULL_REPORT]) == 6
        assert len(by_role[DocumentRole.SUMMARY]) == 2

    def test_filing_date_asia_shanghai_and_raw_preserved(self):
        adapter, _session = _make_listing_session()
        discovery = _resolve_and_list(adapter)
        first = discovery.reports[0]
        # 1786723200000ms → 2026-08-15（Asia/Shanghai）
        assert first.filing_date == "2026-08-15"
        assert first.source_metadata["announcementTime"] == 1786723200000
        assert first.period_source is PeriodSource.EXPLICIT_TITLE

    def test_source_id_and_url_from_adjunct(self):
        adapter, _session = _make_listing_session()
        discovery = _resolve_and_list(adapter)
        first = discovery.reports[0]
        assert first.source_id == "finalpage/2026-08-15/1225475868.PDF"
        assert first.source_url == ("http://static.cninfo.com.cn/"
                                    "finalpage/2026-08-15/1225475868.PDF")

    def test_form_data_contract(self):
        adapter, session = _make_listing_session()
        _resolve_and_list(adapter)
        hisann_calls = [r for r in session.requests if r[1] == HISANN_URL]
        assert len(hisann_calls) == 1
        form = hisann_calls[0][2]["data"]
        assert form["column"] == "szse"
        assert form["stock"] == "600519,gssh0600519"
        assert form["category"] == ("category_ndbg_szsh;category_bndbg_szsh;"
                                    "category_yjdbg_szsh;category_sjdbg_szsh")
        assert form["isHLtitle"] == "false"
        assert "~" in form["seDate"]
        assert form["pageNum"] == 1

    def test_selection_prefers_chinese_full(self):
        adapter, _session = _make_listing_session()
        discovery = _resolve_and_list(adapter, last_n=4)
        selection = select_reports(discovery.reports, ReportQuery(last_n=4),
                                   language_preference=["zh", "en"])
        # 组：H1 2026 / Q1 2026 / FY 2025 / Q3 2025 / H1 2025 → 取最新 4 组
        assert [(r.doc_type, r.report_period) for r in selection.selected] == [
            ("H1", "2026-06-30"), ("Q1", "2026-03-31"),
            ("FY", "2025-12-31"), ("Q3", "2025-09-30")]
        fy = [r for r in selection.selected if r.doc_type == "FY"][0]
        assert fy.language == "zh"  # 中文全文优先，英文回退
        # 偏好语言可用时的正常选择是说明性信息（PHASE1_REVIEW T6）
        assert any("多语言" in n for n in selection.notices)
        # 摘要绝不入选
        assert all(r.document_role is not DocumentRole.SUMMARY
                   for r in selection.selected)

    def test_updated_full_replaces_version_with_link(self):
        rows = _row_600519s() + [{
            # 真实结构上的合成值：更晚公告的"更新后"全文（DoD #1 场景）
            "secCode": "600519", "secName": "贵州茅台",
            "announcementTitle": "贵州茅台2025年半年度报告（更新后）",
            "announcementTime": 1764460800000,  # 2025-11-30
            "adjunctUrl": "finalpage/2025-11-30/1225000001.PDF",
        }]
        adapter, _session = _make_listing_session(rows)
        discovery = _resolve_and_list(adapter, last_n=5)
        amend = [r for r in discovery.reports if r.is_amendment]
        assert len(amend) == 1
        assert amend[0].revision_of == "finalpage/2025-08-13/1224462930.PDF"
        selection = select_reports(discovery.reports, ReportQuery(last_n=5),
                                   language_preference=["zh", "en"])
        h1_2025 = [r for r in selection.selected
                   if r.report_period == "2025-06-30"][0]
        # 更新后全文（更晚公告）替换默认版本，原稿作为版本关系保留在候选中
        assert h1_2025.source_id == "finalpage/2025-11-30/1225000001.PDF"
        assert h1_2025.is_amendment is True

    def test_correction_notice_kept_as_warning_not_full(self):
        rows = _row_600519s() + [{
            # 真实结构上的合成值：更正公告（notice，不得冒充全文）
            "secCode": "600519", "secName": "贵州茅台",
            "announcementTitle": "贵州茅台2025年年度报告更正公告",
            "announcementTime": 1777000000000,
            "adjunctUrl": "finalpage/2026-04-25/1225190001.PDF",
        }]
        adapter, _session = _make_listing_session(rows)
        discovery = _resolve_and_list(adapter)
        notices = [r for r in discovery.reports
                   if r.document_role is DocumentRole.NOTICE]
        assert len(notices) == 1
        assert notices[0].doc_type == "FY"  # 更正公告归属同组（年份+类型可解析）
        selection = select_reports(discovery.reports, ReportQuery(last_n=4),
                                   language_preference=["zh", "en"])
        fy = [r for r in selection.selected if r.doc_type == "FY"][0]
        assert fy.document_role is DocumentRole.FULL_REPORT  # 原全文仍可选
        assert any("未合并" in w for w in selection.warnings)  # 但必须警告

    def test_yearless_row_keeps_null_period_with_warning(self):
        row = dict(_row_000001_full())
        row["announcementTitle"] = "平安银行半年度报告"  # 合成值：无年份
        adapter, _session = _make_listing_session([row])
        discovery = _resolve_and_list(adapter, lookback=2)  # 单窗，避免扩窗重复
        assert len(discovery.reports) == 1
        report = discovery.reports[0]
        assert report.report_period is None
        assert report.period_source is PeriodSource.UNKNOWN
        assert any("报告期置空" in w for w in discovery.warnings)

    def test_forms_filter(self):
        adapter, _session = _make_listing_session()
        discovery = _resolve_and_list(adapter, forms=["FY"])
        assert {r.doc_type for r in discovery.reports} == {"FY"}

    def test_sz_row_full_fields(self):
        row = _row_000001_full()
        adapter, _session = _make_listing_session([row])
        discovery = _resolve_and_list(adapter)
        report = discovery.reports[0]
        assert report.doc_type == "H1"
        assert report.report_period == "2026-06-30"
        assert report.filing_date == "2026-08-15"
        assert report.source_metadata["announcementId"] == "1225475344"
        assert report.source_metadata["adjunctSize"] == 1439

    def test_null_announcements_tolerated(self):
        adapter, _session = _make_listing_session([])
        # 空结果：announcements null / []（结构合法，无报告不是错误）
        discovery = _resolve_and_list(adapter)
        assert discovery.reports == []
        assert discovery.exhausted is True

    def test_contract_changed_on_bad_shape(self):
        adapter2, _session2 = _make_adapter({
            INDEX_URL: FakeResponse(200, b""),
            TOPSEARCH_URL: _json_response(_topsearch_payload("600519")),
            HISANN_URL: _json_response({"totalAnnouncement": 1}),  # 缺 hasMore/announcements
        })
        resolved = adapter2.resolve(normalize_symbol("600519"))
        with pytest.raises(SourceContractChangedError):
            adapter2.list_reports(resolved, ReportQuery(last_n=4))


class TestPagination:
    def _page2_rows(self):
        # 真实结构上的合成值：更早的两个报告期（翻页第二页）
        return [
            {"secCode": "600519", "secName": "贵州茅台",
             "announcementTitle": "贵州茅台2025年第一季度报告",
             "announcementTime": 1744963200000,
             "adjunctUrl": "finalpage/2025-04-19/1224300001.PDF"},
            {"secCode": "600519", "secName": "贵州茅台",
             "announcementTitle": "贵州茅台2024年年度报告",
             "announcementTime": 1744300800000,
             "adjunctUrl": "finalpage/2025-04-11/1224200001.PDF"},
        ]

    def test_has_more_drives_next_page(self):
        responses = [_hisann_response(_row_600519s(), has_more=True, total=10),
                     _hisann_response(self._page2_rows(), has_more=False)]
        adapter, session = _make_adapter({
            INDEX_URL: FakeResponse(200, b""),
            TOPSEARCH_URL: _json_response(_topsearch_payload("600519")),
            HISANN_URL: (lambda method, url, **kw:
                         responses.pop(0)),
        })
        discovery = _resolve_and_list(adapter)
        hisann_calls = [r for r in session.requests if r[1] == HISANN_URL]
        assert [c[2]["data"]["pageNum"] for c in hisann_calls] == [1, 2]
        assert len(discovery.reports) == 10
        # 小窗（2 年）已满足 last_n=4，更早历史未检索 → exhausted=False
        assert discovery.exhausted is False
        assert discovery.truncated is False

    def test_budget_truncates(self):
        adapter, session = _make_adapter({
            INDEX_URL: FakeResponse(200, b""),
            TOPSEARCH_URL: _json_response(_topsearch_payload("600519")),
            HISANN_URL: _hisann_response(_row_600519s(), has_more=True, total=99),
        })
        discovery = _resolve_and_list(adapter, last_n=4, budget=1)
        assert discovery.truncated is True
        assert discovery.exhausted is False  # 预算耗尽不得宣称穷尽
        assert any("预算耗尽" in w for w in discovery.warnings)
        hisann_calls = [r for r in session.requests if r[1] == HISANN_URL]
        assert len(hisann_calls) == 1

    def test_window_widens_when_insufficient(self):
        # 小窗（last_n=8 → 3 年窗）组数不足 → 有界扩窗至回溯上限（DESIGN §5）
        adapter, session = _make_adapter({
            INDEX_URL: FakeResponse(200, b""),
            TOPSEARCH_URL: _json_response(_topsearch_payload("600519")),
            HISANN_URL: _hisann_response(_row_600519s()),
        })
        discovery = _resolve_and_list(adapter, last_n=8, lookback=10)
        hisann_calls = [r for r in session.requests if r[1] == HISANN_URL]
        assert len(hisann_calls) == 2  # 两个窗口各一页
        first_from = hisann_calls[0][2]["data"]["seDate"].split("~")[0]
        second_from = hisann_calls[1][2]["data"]["seDate"].split("~")[0]
        assert second_from < first_from  # 有界扩窗：第二窗起点更早
        assert discovery.exhausted is True

    def test_single_window_when_lookback_small(self):
        adapter, session = _make_adapter({
            INDEX_URL: FakeResponse(200, b""),
            TOPSEARCH_URL: _json_response(_topsearch_payload("600519")),
            HISANN_URL: _hisann_response(_row_600519s()),
        })
        discovery = _resolve_and_list(adapter, last_n=4, lookback=2)
        hisann_calls = [r for r in session.requests if r[1] == HISANN_URL]
        assert len(hisann_calls) == 1  # stage_years 已是 2 = 回溯上限，无需扩窗
        assert discovery.exhausted is True
