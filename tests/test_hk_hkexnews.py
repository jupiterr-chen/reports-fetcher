"""hk_hkexnews：披露易契约解析（fixture 驱动，不触网；DESIGN §7 / §16）。

原始 HTML fixture 是解析器测试基准；解析结果与 I0 录制的解析后行集
（*.json fixture）交叉验证。超限切窗场景在真实结构上替换总数标记值。
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from reports_fetcher.adapters.hk_hkexnews import (
    HKHkexnewsAdapter,
    _split_year_windows,
    parse_hk_title_period,
    parse_search_page,
    strip_jsonp,
)
from reports_fetcher.config import HttpConfig
from reports_fetcher.models import (
    DocumentRole,
    Market,
    PeriodSource,
    ReportQuery,
    ResolveError,
    SourceContractChangedError,
)
from reports_fetcher.period import chinese_small_number
from reports_fetcher.selection import select_reports
from reports_fetcher.symbol import normalize_symbol
from tests.conftest import FakeResponse, make_config, make_report, make_transport

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hk"

PREFIX_URL_PREFIX = "https://www1.hkexnews.hk/search/prefix.do"
SEARCH_URL_PREFIX = "https://www1.hkexnews.hk/search/titlesearch.xhtml"


class PrefixMapSession:
    """按 URL 前缀匹配响应（日期参数随运行日变化，不能精确匹配）。"""

    def __init__(self, mapping: list[tuple[str, object]]):
        self.mapping = list(mapping)
        self.requests: list[tuple] = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        for prefix, response in self.mapping:
            if url.startswith(prefix):
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(method, url, **kwargs)
                return response
        raise AssertionError(f"未预期的请求: {url}")

    def mount(self, *args, **kwargs):
        pass

    def close(self):
        pass


def _html_response(text: str) -> FakeResponse:
    return FakeResponse(200, text.encode("utf-8"),
                        headers={"Content-Type": "text/html"})


def _raw(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _jsonp_response(payload: str) -> FakeResponse:
    return FakeResponse(200, payload.encode("utf-8"),
                        headers={"Content-Type": "text/javascript"})


_PREFIX_00700_BODY = (
    'callback({"more":"1","stockInfo":'
    '[{"stockId":7609,"code":"00700","name":"騰訊控股"}]});\r\n')
_PREFIX_0016_BODY = (
    'callback({"more":"1","stockInfo":'
    '[{"stockId":25,"code":"00016","name":"新鴻基地產"}]});\r\n')


def _make_adapter(mapping):
    session = PrefixMapSession(mapping)
    adapter = HKHkexnewsAdapter(
        make_transport(session, config=HttpConfig()), make_config())
    return adapter, session


def _resolve_00700(adapter):
    return adapter.resolve(normalize_symbol("0700.HK"))


class TestStripJsonp:
    def test_known_wrapper(self):
        assert strip_jsonp('callback({"a":1});') == '{"a":1}'
        assert strip_jsonp('  callback({"a":1})  \r\n') == '{"a":1}'

    def test_unknown_wrapper_rejected(self):
        with pytest.raises(SourceContractChangedError):
            strip_jsonp('evil_alert({"a":1});')
        with pytest.raises(SourceContractChangedError):
            strip_jsonp('not jsonp at all')


class TestChineseSmallNumber:
    @pytest.mark.parametrize("text,expected", [
        ("一", 1), ("三", 3), ("十", 10), ("十一", 11), ("十二", 12),
        ("二十", 20), ("二十一", 21), ("三十一", 31), ("5", 5),
    ])
    def test_values(self, text, expected):
        assert chinese_small_number(text) == expected

    @pytest.mark.parametrize("text", ["", "零", "一百", "廿一", "0", "100"])
    def test_invalid(self, text):
        assert chinese_small_number(text) is None


class TestTitlePeriod:
    """PHASE1_REVIEW T5：仅明确期末日可设期；单年/跨年标签均 unknown。"""

    @pytest.mark.parametrize("title", [
        "中期報告 2026", "2025 年報", "二零二四年年報",
        "中期報告 二零二五年",
        # 匯豐形態（I3 e2e 实测 2026-09-21）：年份在前的中期業績報告
        "2026年中期業績報告(附僱員股份計劃)",
        "2024年中期業績報告",
        "2016年報及賬目(附僱員股份計劃)",
    ])
    def test_year_labels_without_end_date_are_unknown(self, title):
        parsed, source, warning = parse_hk_title_period(title)
        assert parsed is None
        assert source is PeriodSource.UNKNOWN
        assert warning and "不猜测" in warning

    @pytest.mark.parametrize("title", [
        "2024/25 年報", "2025/26 中期報告", "2024/25年中期業績報告",
    ])
    def test_cross_year_labels_null_with_warning(self, title):
        parsed, source, warning = parse_hk_title_period(title)
        assert parsed is None
        assert source is PeriodSource.UNKNOWN
        assert warning and "跨年标签" in warning

    @pytest.mark.parametrize("title,period", [
        ("截至二零二六年三月三十一日止三個月業績公佈", "2026-03-31"),
        ("截至二零二五年十二月三十一日止年度全年業績公佈", "2025-12-31"),
        ("截至二零二六年六月三十日止三個月及六個月業績公佈", "2026-06-30"),
    ])
    def test_explicit_end_dates(self, title, period):
        parsed, source, warning = parse_hk_title_period(title)
        assert parsed == period
        assert source is PeriodSource.EXPLICIT_TITLE
        assert warning is None

    def test_no_year(self):
        parsed, source, warning = parse_hk_title_period("季度報告")
        assert parsed is None and source is PeriodSource.UNKNOWN and warning


class TestParseSearchPage:
    def _cross_check(self, raw_name: str, parsed_name: str):
        raw = _raw(raw_name)
        fixture = json.loads((FIXTURES / parsed_name).read_text(encoding="utf-8"))
        record_count, rows = parse_search_page(raw)
        assert record_count == int(fixture["record_count"])
        assert len(rows) == len(fixture["rows"])
        for got, want in zip(rows, fixture["rows"]):
            assert got["release_datetime"] in want["release_datetime"]
            assert got["stock_code"] == \
                   want["stock_code"].split(":")[1].strip().split()[0]
            # 子类别经实体反转义后一致（&#x2f; → /）
            assert want["subcategory"].replace("&#x2f;", "/") in got["headline"]
            assert got["title"] == want["title"].replace("&#x2f;", "/")
            assert got["file_link"] == want["file_link"]
            assert got["file_size"] == want["file_size"]
        return rows

    def test_00700_3y_cross_validation(self):
        rows = self._cross_check("search_00700_40000_3y.raw.html",
                                 "search_00700_40000_3y.json")
        # 人民币柜台第二代码只取主代码
        assert all(r["stock_code"] == "00700" for r in rows)

    def test_0016_3y_cross_validation(self):
        self._cross_check("search_0016_40000_3y.raw.html",
                          "search_0016_40000_3y.json")

    def test_yeji_cross_validation(self):
        self._cross_check("search_00700_10000_yeji_2026.raw.html",
                          "search_00700_10000_yeji_2026.json")

    def test_missing_record_count_is_contract_change(self):
        with pytest.raises(SourceContractChangedError):
            parse_search_page("<html><body>no marker here</body></html>")


class TestResolve:
    def _prefix_body(self, entry: str) -> str:
        return f'callback({{"more":"1","stockInfo":[{entry}]}});\r\n'

    def test_00700(self):
        adapter, session = _make_adapter([
            (PREFIX_URL_PREFIX,
             _jsonp_response(self._prefix_body(
                 '{"stockId":7609,"code":"00700","name":"騰訊控股"}'))),
        ])
        resolved = adapter.resolve(normalize_symbol("0700.HK"))
        assert resolved.market is Market.HK
        assert resolved.symbol == "00700"
        assert resolved.source_issuer_id == "7609"
        assert resolved.issuer_id == "hkex:7609"
        assert resolved.display_name == "騰訊控股"
        assert resolved.exchange == "SEHK"
        url = session.requests[0][1]
        assert "name=00700" in url and "market=SEHK" in url

    def test_0016_five_digit_code(self):
        adapter, _ = _make_adapter([
            (PREFIX_URL_PREFIX,
             _jsonp_response(self._prefix_body(
                 '{"stockId":25,"code":"00016","name":"新鴻基地產"}'))),
        ])
        resolved = adapter.resolve(normalize_symbol("16"))
        assert resolved.symbol == "00016"

    def test_nomatch_empty_stockinfo(self):
        adapter, _ = _make_adapter([
            (PREFIX_URL_PREFIX, _jsonp_response(
                'callback({"more":"1","stockInfo":[]});\r\n')),
        ])
        with pytest.raises(ResolveError) as ei:
            adapter.resolve(normalize_symbol("99999"))
        assert ei.value.code == "symbol_not_found"

    def test_bad_jsonp_is_contract_change(self):
        adapter, _ = _make_adapter([
            (PREFIX_URL_PREFIX, _html_response("<html>error</html>")),
        ])
        with pytest.raises(SourceContractChangedError):
            adapter.resolve(normalize_symbol("00700"))


class TestListReports:
    def test_00700_default_forms(self):
        adapter, session = _make_adapter([
            (PREFIX_URL_PREFIX, _jsonp_response(_PREFIX_00700_BODY)),
            (SEARCH_URL_PREFIX,
             _html_response(_raw("search_00700_40000_3y.raw.html"))),
        ])
        resolved = _resolve_00700(adapter)
        discovery = adapter.list_reports(resolved, ReportQuery(last_n=4))
        # 9 行中 ESG×3 排除 → ANNUAL×3 + INTERIM×3
        assert len(discovery.reports) == 6
        assert {r.doc_type for r in discovery.reports} == {"ANNUAL", "INTERIM"}
        assert all(r.document_role is DocumentRole.FULL_REPORT
                   for r in discovery.reports)
        # 单年标签无期末日证据 → 期未知 + 警告（PHASE1_REVIEW T5）
        by_title = {r.title: r for r in discovery.reports}
        assert by_title["中期報告 2026"].report_period is None
        assert by_title["2025 年報"].report_period is None
        assert all(r.period_source is PeriodSource.UNKNOWN
                   for r in discovery.reports)
        # 候选级期末说明随报告携带，不再逐条进入 discovery.warnings
        # （核心在原文富化后仅对选中报告告警 / 未选中 aggregate 一次）
        assert any("年份标签" in (r.source_metadata.get("period_warning") or "")
                   for r in discovery.reports)
        assert not any("年份标签" in w for w in discovery.warnings)
        # 發放時間 DD/MM/YYYY → Asia/Hong_Kong 公告日
        assert by_title["中期報告 2026"].filing_date == "2026-08-25"
        assert by_title["中期報告 2026"].source_metadata["subcategory"] == "中期/半年度報告"
        assert by_title["中期報告 2026"].source_url == (
            "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0825/"
            "2026082500557_c.pdf")
        # v1.0.3：默认类型含 QTR-HK → 两类检索都发起；该 fixture 的 40000
        # 页无 [季度業績] 行，QTR-HK 候选为 0 属正常。
        # RF-HK-PERIOD-EVIDENCE-001：業績检索先行（判期证据须在 40000
        # 候选构建前就绪）。
        search_calls = [r for r in session.requests
                        if r[1].startswith(SEARCH_URL_PREFIX)]
        assert len(search_calls) == 2
        assert "t1code=10000" in search_calls[0][1]
        assert "t1code=40000" in search_calls[1][1]
        assert re.search(r"from=\d{8}&to=\d{8}", search_calls[1][1])

    def test_00700_selection_last_4(self):
        adapter, _ = _make_adapter([
            (PREFIX_URL_PREFIX, _jsonp_response(_PREFIX_00700_BODY)),
            (SEARCH_URL_PREFIX,
             _html_response(_raw("search_00700_40000_3y.raw.html"))),
        ])
        resolved = _resolve_00700(adapter)
        discovery = adapter.list_reports(resolved, ReportQuery(last_n=4))
        selection = select_reports(discovery.reports, ReportQuery(last_n=4),
                                   language_preference=["zh", "en"])
        # 期均未知 → 按公告日倒序（T5 后的预期形态）
        assert [(r.doc_type, r.report_period, r.filing_date)
                for r in selection.selected] == [
            ("INTERIM", None, "2026-08-25"), ("ANNUAL", None, "2026-04-09"),
            ("INTERIM", None, "2025-08-26"), ("ANNUAL", None, "2025-04-08")]

    def test_0016_non_calendar_year_null_periods(self):
        adapter, _session = _make_adapter([
            (PREFIX_URL_PREFIX, _jsonp_response(_PREFIX_0016_BODY)),
            (SEARCH_URL_PREFIX,
             _html_response(_raw("search_0016_40000_3y.raw.html"))),
        ])
        resolved = adapter.resolve(normalize_symbol("0016.HK"))
        discovery = adapter.list_reports(resolved, ReportQuery(last_n=4))
        assert len(discovery.reports) == 6  # ESG×3 排除
        for report in discovery.reports:
            # 跨年标签：日历期末不可得 → null + unknown + 警告（不猜 12-31）
            assert report.report_period is None
            assert report.period_source is PeriodSource.UNKNOWN
            assert report.doc_type in ("ANNUAL", "INTERIM")
            # 跨年说明随报告携带，不逐条污染 discovery.warnings
            assert "跨年标签" in (report.source_metadata.get("period_warning") or "")
        assert not any("跨年标签" in w for w in discovery.warnings)
        # 未知期置后 + 警告：选择仍可工作（按公告日排序）
        selection = select_reports(discovery.reports, ReportQuery(last_n=4),
                                   language_preference=["zh", "en"])
        assert len(selection.selected) == 4
        assert any("报告期未知" in w for w in selection.warnings)

    def test_qtr_hk_explicit_search(self):
        adapter, session = _make_adapter([
            (PREFIX_URL_PREFIX, _jsonp_response(_PREFIX_00700_BODY)),
            (SEARCH_URL_PREFIX,
             _html_response(_raw("search_00700_10000_yeji_2026.raw.html"))),
        ])
        resolved = _resolve_00700(adapter)
        discovery = adapter.list_reports(
            resolved, ReportQuery(last_n=4, forms=["QTR-HK"]))
        # 显式请求 QTR-HK：仅 t1=10000 + title=業績 检索
        search_calls = [r for r in session.requests
                        if r[1].startswith(SEARCH_URL_PREFIX)]
        assert len(search_calls) == 1
        assert "t1code=10000" in search_calls[0][1]
        assert "title=" in search_calls[0][1]
        # 中期業績/末期業績跳过，仅季度業績入候选；期末日来自标题明确日期
        assert len(discovery.reports) == 1
        qtr = discovery.reports[0]
        assert qtr.doc_type == "QTR-HK"
        assert qtr.report_period == "2026-03-31"
        assert qtr.period_source is PeriodSource.EXPLICIT_TITLE
        assert qtr.document_role is DocumentRole.FULL_REPORT

    def test_qtr_hk_with_default_mix(self):
        # 業績检索先行 → 先弹出 yeji 页（QTR + 判期证据），后 40000 页
        pages = [_raw("search_00700_10000_yeji_2026.raw.html"),
                 _raw("search_00700_40000_3y.raw.html")]

        def responder(method, url, **kwargs):
            return _html_response(pages.pop(0))

        adapter, session = _make_adapter([
            (PREFIX_URL_PREFIX, _jsonp_response(_PREFIX_00700_BODY)),
            (SEARCH_URL_PREFIX, responder),
        ])
        resolved = _resolve_00700(adapter)
        discovery = adapter.list_reports(
            resolved, ReportQuery(last_n=6, forms=["ANNUAL", "INTERIM", "QTR-HK"]))
        search_calls = [r for r in session.requests
                        if r[1].startswith(SEARCH_URL_PREFIX)]
        assert len(search_calls) == 2  # 两类检索都发起
        urls = [r[1] for r in search_calls]
        assert any("t1code=40000" in u for u in urls)
        assert any("t1code=10000" in u for u in urls)
        assert len(discovery.reports) == 7  # 6 + 1 QTR-HK

    def test_combined_annual_esg_subcategory_mapped_as_annual(self):
        """回归实测（2026-09-21）：合并子类别"年報 / 環境…報告" → ANNUAL。"""
        raw = _raw("search_00700_40000_3y.raw.html")
        raw = raw.replace("[年報]", "[年報 &#x2f; 環境、社會及管治資料&#x2f;報告]", 1)
        adapter, _ = _make_adapter([
            (PREFIX_URL_PREFIX, _jsonp_response(_PREFIX_00700_BODY)),
            (SEARCH_URL_PREFIX, _html_response(raw)),
        ])
        resolved = _resolve_00700(adapter)
        discovery = adapter.list_reports(resolved, ReportQuery(last_n=4))
        assert len(discovery.reports) == 6  # 合并标签不丢年报
        combined = [r for r in discovery.reports if r.title == "2025 年報"][0]
        assert combined.doc_type == "ANNUAL"
        assert combined.source_metadata["subcategory"] ==             "年報 / 環境、社會及管治資料/報告"

    def test_unknown_subcategory_warns(self):
        # 真实结构上合成值：未知子类别的 headline（契约漂移告警留痕）
        raw = _raw("search_00700_40000_3y.raw.html")
        raw = raw.replace("[中期&#x2f;半年度報告]", "[嶄新子類別]", 1)
        adapter, _ = _make_adapter([
            (PREFIX_URL_PREFIX, _jsonp_response(_PREFIX_00700_BODY)),
            (SEARCH_URL_PREFIX, _html_response(raw)),
        ])
        resolved = _resolve_00700(adapter)
        discovery = adapter.list_reports(resolved, ReportQuery(last_n=4))
        assert len(discovery.reports) == 5  # 原 6 - 1 被未知子类别替换
        assert any("未知子类别" in w for w in discovery.warnings)

    def test_budget_truncates(self):
        adapter, session = _make_adapter([
            (PREFIX_URL_PREFIX, _jsonp_response(_PREFIX_00700_BODY)),
            (SEARCH_URL_PREFIX,
             _html_response(_raw("search_00700_40000_3y.raw.html"))),
        ])
        resolved = _resolve_00700(adapter)
        discovery = adapter.list_reports(
            resolved, ReportQuery(last_n=4, max_discovery_requests=0))
        assert discovery.truncated is True
        assert discovery.exhausted is False
        assert discovery.reports == []
        assert not [r for r in session.requests  # 预算 0：不发检索请求
                    if r[1].startswith(SEARCH_URL_PREFIX)]

    def test_over_limit_splits_year_windows(self):
        # 真实结构上替换总数标记：1500 > 站点单页上限 1000 → 按年切窗。
        # RF-HK-PERIOD-EVIDENCE-001 后显式 ANNUAL/INTERIM 也发起業績检索
        # （证据采集）：两类检索面对同一超限页各自按年切窗（各 1+3 次）。
        raw = _raw("search_00700_40000_3y.raw.html").replace("共有 9 紀錄",
                                                             "共有 1500 紀錄")
        adapter, session = _make_adapter([
            (PREFIX_URL_PREFIX, _jsonp_response(_PREFIX_00700_BODY)),
            (SEARCH_URL_PREFIX, _html_response(raw)),
        ])
        resolved = _resolve_00700(adapter)
        query = ReportQuery(last_n=4, forms=["ANNUAL", "INTERIM"],
                            max_lookback_years=2,
                            max_discovery_requests=100)
        adapter.list_reports(resolved, query)
        all_urls = [r[1] for r in session.requests
                    if r[1].startswith(SEARCH_URL_PREFIX)]
        by_t1 = {code: [u for u in all_urls if f"t1code={code}" in u]
                 for code in ("10000", "40000")}
        assert len(by_t1["10000"]) == 1 + 3   # 業績检索同样整窗 + 3 年窗
        assert len(by_t1["40000"]) == 1 + 3   # 整窗 1 次 + 3 个年窗
        urls = by_t1["40000"]  # 年窗形状断言沿用 40000 检索

        def _date_of(url: str, key: str) -> date:
            raw_value = re.search(rf"{key}=(\d{{8}})", url).group(1)
            return date(int(raw_value[:4]), int(raw_value[4:6]), int(raw_value[6:]))

        for url in urls[1:]:
            span = (_date_of(url, "to") - _date_of(url, "from")).days
            assert 0 <= span <= 366  # 年窗不超过一年
        # 年窗从最新开始，覆盖到整窗起点
        assert _date_of(urls[-1], "from") == _date_of(urls[0], "from")

    def test_split_year_windows_helper(self):
        from datetime import date
        windows = _split_year_windows(date(2024, 3, 15), date(2026, 8, 20))
        assert windows[0] == (date(2026, 1, 1), date(2026, 8, 20))
        assert windows[1] == (date(2025, 1, 1), date(2025, 12, 31))
        assert windows[2] == (date(2024, 3, 15), date(2024, 12, 31))


class TestPeriodEvidence:
    """RF-HK-PERIOD-EVIDENCE-001：業績公告标题判期证据采集。"""

    def test_evidence_collected_from_yeji_fixture(self):
        adapter, _ = _make_adapter([
            (PREFIX_URL_PREFIX, _jsonp_response(_PREFIX_00700_BODY)),
            (SEARCH_URL_PREFIX,
             _html_response(_raw("search_00700_10000_yeji_2026.raw.html"))),
        ])
        resolved = _resolve_00700(adapter)
        discovery = adapter.list_reports(
            resolved, ReportQuery(last_n=4, forms=["ANNUAL", "INTERIM"]))
        evidence = discovery.period_evidence
        assert {("INTERIM", 2026): {"2026-06-30"},
                ("ANNUAL", 2025): {"2025-12-31"}} == \
            {k: set(v["periods"]) for k, v in evidence.items()}
        interim_row = evidence[("INTERIM", 2026)]["rows"][0]
        assert interim_row["source_id"].endswith("2026081200297_c.pdf")
        assert "中期業績" in interim_row["subcategory"]
        # 業績公告本身不入候选池（仅作证据）；forms 无 QTR-HK → 候选为空
        assert discovery.reports == []

    def test_evidence_titles_from_real_1810_forms(self):
        """生产 1810 实测标题形态（含空格变体与复合子类别）。"""
        from reports_fetcher.adapters.hk_hkexnews import (
            HKHkexnewsAdapter,
            _explicit_end_date,
            _hk_title_year_label,
        )

        evidence: dict = {}
        row = {
            "headline": "公告及通告 - [中期業績]",
            "title": "截至2026 年6 月30 日止三個月及六個月之業績公告",
            "file_link": "/listedco/listconews/sehk/2026/0818/"
                         "2026081801015_c.pdf",
        }
        HKHkexnewsAdapter._collect_period_evidence(row, evidence)
        assert set(evidence[("INTERIM", 2026)]["periods"]) == {"2026-06-30"}

        compound = {
            "headline": "公告及通告 - [末期業績 &#x2f; 股息或分派]",
            "title": "截至2025年12月31日止年度之全年業績公告",
            "file_link": "/listedco/listconews/sehk/2026/0324/"
                         "2026032400609_c.pdf",
        }
        HKHkexnewsAdapter._collect_period_evidence(compound, evidence)
        assert set(evidence[("ANNUAL", 2025)]["periods"]) == {"2025-12-31"}
        assert "末期業績" in evidence[("ANNUAL", 2025)]["rows"][0]["subcategory"]

        assert _hk_title_year_label("2026年中期報告") == 2026
        assert _hk_title_year_label("2025 年報") == 2025
        assert _hk_title_year_label("二零二四年年報") == 2024
        # 小米年报形态（生产实测 2026-09-26）：年度報告 变体
        assert _hk_title_year_label("2025年度報告") == 2025
        assert _hk_title_year_label("二零二五年度報告") == 2025
        assert _hk_title_year_label("2024/25 年報") is None
        assert _hk_title_year_label("季度報告") is None
        assert _explicit_end_date("截至2026 年6 月30 日止三個月") == "2026-06-30"

    def test_ambiguous_evidence_not_applied(self):
        """同期两条不同期末 → 歧义，阶段 C 不回填。"""
        from reports_fetcher.adapters.hk_hkexnews import HKHkexnewsAdapter
        from reports_fetcher.core import FetchService

        evidence: dict = {}
        HKHkexnewsAdapter._collect_period_evidence(
            {"headline": "公告及通告 - [中期業績]",
             "title": "截至二零二六年六月三十日止六個月之業績公告",
             "file_link": "/x/a_c.pdf"}, evidence)
        HKHkexnewsAdapter._collect_period_evidence(
            {"headline": "公告及通告 - [中期業績]",
             "title": "截至二零二六年七月三十一日止六個月之業績公告",
             "file_link": "/x/b_c.pdf"}, evidence)
        assert set(evidence[("INTERIM", 2026)]["periods"]) == \
            {"2026-06-30", "2026-07-31"}
        candidate = make_report(market=Market.HK, symbol="01810",
                                doc_type="INTERIM", title="2026年中期報告",
                                report_period=None,
                                period_source=PeriodSource.UNKNOWN)
        assert FetchService._apply_period_evidence([candidate], evidence) == 0
        assert candidate.report_period is None
