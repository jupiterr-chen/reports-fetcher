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
from tests.conftest import FakeResponse, make_config, make_transport

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
    @pytest.mark.parametrize("title,period", [
        ("中期報告 2026", "2026-06-30"),
        ("2025 年報", "2025-12-31"),
        ("二零二四年年報", "2024-12-31"),
        ("中期報告 二零二五年", "2025-06-30"),
        # 匯豐形態（I3 e2e 实测 2026-09-21）：年份在前的中期業績報告
        ("2026年中期業績報告(附僱員股份計劃)", "2026-06-30"),
        ("2024年中期業績報告", "2024-06-30"),
        # 匯豐年報及賬目形態：(\d{4})年報 前缀命中
        ("2016年報及賬目(附僱員股份計劃)", "2016-12-31"),
    ])
    def test_single_year_labels(self, title, period):
        parsed, source, warning = parse_hk_title_period(title)
        assert parsed == period
        assert source is PeriodSource.EXPLICIT_TITLE
        assert warning is None

    @pytest.mark.parametrize("title", [
        "2024/25 年報", "2025/26 中期報告", "2024/25年中期業績報告",
    ])
    def test_cross_year_labels_null_with_warning(self, title):
        parsed, source, warning = parse_hk_title_period(title)
        assert parsed is None
        assert source is PeriodSource.UNKNOWN
        assert warning and "不猜测" in warning

    @pytest.mark.parametrize("title,period", [
        ("截至二零二六年三月三十一日止三個月業績公佈", "2026-03-31"),
        ("截至二零二五年十二月三十一日止年度全年業績公佈", "2025-12-31"),
        ("截至二零二六年六月三十日止三個月及六個月業績公佈", "2026-06-30"),
    ])
    def test_explicit_end_dates(self, title, period):
        parsed, source, warning = parse_hk_title_period(title)
        assert parsed == period
        assert source is PeriodSource.EXPLICIT_TITLE

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
        # 单年标签 → 日历年结期末（DESIGN §7）
        by_title = {r.title: r for r in discovery.reports}
        assert by_title["中期報告 2026"].report_period == "2026-06-30"
        assert by_title["2025 年報"].report_period == "2025-12-31"
        # 發放時間 DD/MM/YYYY → Asia/Hong_Kong 公告日
        assert by_title["中期報告 2026"].filing_date == "2026-08-25"
        assert by_title["中期報告 2026"].source_metadata["subcategory"] == "中期/半年度報告"
        assert by_title["中期報告 2026"].source_url == (
            "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0825/"
            "2026082500557_c.pdf")
        # 默认类型不触发 t1=10000 检索（prefix.do 的 resolve 请求除外）
        search_calls = [r for r in session.requests
                        if r[1].startswith(SEARCH_URL_PREFIX)]
        assert len(search_calls) == 1
        assert "t1code=40000" in search_calls[0][1]
        assert re.search(r"from=\d{8}&to=\d{8}", search_calls[0][1])

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
        assert [(r.doc_type, r.report_period) for r in selection.selected] == [
            ("INTERIM", "2026-06-30"), ("ANNUAL", "2025-12-31"),
            ("INTERIM", "2025-06-30"), ("ANNUAL", "2024-12-31")]

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
        assert any("跨年标签" in w for w in discovery.warnings)
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
        pages = [_raw("search_00700_40000_3y.raw.html"),
                 _raw("search_00700_10000_yeji_2026.raw.html")]

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
        # 真实结构上替换总数标记：1500 > 站点单页上限 1000 → 按年切窗
        raw = _raw("search_00700_40000_3y.raw.html").replace("共有 9 紀錄",
                                                             "共有 1500 紀錄")
        adapter, session = _make_adapter([
            (PREFIX_URL_PREFIX, _jsonp_response(_PREFIX_00700_BODY)),
            (SEARCH_URL_PREFIX, _html_response(raw)),
        ])
        resolved = _resolve_00700(adapter)
        query = ReportQuery(last_n=4, max_lookback_years=2,
                            max_discovery_requests=100)
        adapter.list_reports(resolved, query)
        urls = [r[1] for r in session.requests
                if r[1].startswith(SEARCH_URL_PREFIX)]
        assert len(urls) == 1 + 3  # 整窗 1 次 + 3 个年窗（2026/2025/2024 部分）

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
