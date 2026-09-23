"""HK 默认季度类型与混合选择冷/热库一致性（RF-HK-QTR-DEFAULT-001 A1–A10）。

真实披露易 fixture（titlesearch.xhtml 原始 HTML）驱动真实适配器 + 真实
Store/Selection/Core 编排，不触网：PDF 为通过内容校验的合成体（尾部带
可区分标记），原文期末提取按标记映射 monkeypatch。"无季度发行人"页为
真实结构上移除 [季度業績] 行的最小变体。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import requests

import reports_fetcher.core as core_module
from reports_fetcher import document_period
from reports_fetcher.adapters.hk_hkexnews import parse_search_page
from reports_fetcher.config import Config, HttpConfig
from reports_fetcher.core import FetchService
from reports_fetcher.jobs import JobService
from reports_fetcher.models import (
    DocumentRole,
    Market,
    OUTCOME_CACHED,
    OUTCOME_DOWNLOADED,
    OUTCOME_FAILED,
    PeriodSource,
    Report,
    ReportQuery,
)
from reports_fetcher.store import Store
from reports_fetcher.symbol import normalize_symbol
from tests.conftest import FakeResponse, PDF_DOC, make_transport

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hk"
FILE_BASE = "https://www1.hkexnews.hk"

_PREFIX_00700_BODY = (
    'callback({"more":"1","stockInfo":'
    '[{"stockId":7609,"code":"00700","name":"騰訊控股"}]});\r\n')
_PREFIX_0016_BODY = (
    'callback({"more":"1","stockInfo":'
    '[{"stockId":25,"code":"00016","name":"新鴻基地產"}]});\r\n')


def _raw(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fixture_rows(name: str) -> list[dict]:
    import json
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["rows"]


def _no_quarterly_yeji_page() -> str:
    """真实 yeji 页结构上移除 [季度業績] 行（无季度发行人的最小组造）。"""
    raw = _raw("search_00700_10000_yeji_2026.raw.html")
    rows = re.findall(r"<tr[^>]*>.*?</tr>", raw, flags=re.S)
    removed = 0
    for row in rows:
        if "季度業績" in row:
            raw = raw.replace(row, "")
            removed += 1
    assert removed == 1
    raw = raw.replace("共有 3 紀錄", "共有 2 紀錄")
    count, parsed = parse_search_page(raw)
    assert count == 2 and len(parsed) == 2
    assert not any("季度業績" in (r["headline"] or "") for r in parsed)
    return raw


# 0700（日历年结）：标题 → 原文提取的明确期末日（模拟 PDF 原文内容）
_PERIODS_00700 = {
    "listedco/listconews/sehk/2026/0825/2026082500557_c.pdf":
        "2026-06-30",   # 中期報告 2026
    "listedco/listconews/sehk/2026/0409/2026040901232_c.pdf":
        "2025-12-31",   # 2025 年報
    "listedco/listconews/sehk/2025/0826/2025082600677_c.pdf":
        "2025-06-30",   # 中期報告 2025
    "listedco/listconews/sehk/2025/0408/2025040800668_c.pdf":
        "2024-12-31",   # 2024 年報
    "listedco/listconews/sehk/2024/0827/2024082701155_c.pdf":
        "2024-06-30",   # 中期報告 2024
    "listedco/listconews/sehk/2024/0408/2024040801823_c.pdf":
        "2023-12-31",   # 2023 年報
}
_QTR_LINK_00700 = "listedco/listconews/sehk/2026/0513/2026051300335_c.pdf"

# A1 四期序列所需的第二个季度候选：来自 2026-09-22 生产任务
# job_0636cd75c81f4158ab79 的真实元数据（source_id/title/期末日/公告日）。
# 该行不在共享解析 fixture 中（不修改真实抓取样本，也不编造响应结构），
# 仅在非解析层注入，用于锁定任务书 §3.1 的 2025-09-30 QTR-HK。
_QTR_LINK_00700_Q3_2025 = (
    "listedco/listconews/sehk/2025/1113/2025111300287_c.pdf")
_QTR_TITLE_00700_Q3_2025 = "截至二零二五年九月三十日止三個月及九個月業績公佈"


def _real_production_q3_2025() -> Report:
    """真实生产证据候选：0700 Q3-2025 季度業績（非解析层注入）。"""
    return Report(
        market=Market.HK, symbol="00700",
        source_id=_QTR_LINK_00700_Q3_2025,
        source_url=f"{FILE_BASE}/{_QTR_LINK_00700_Q3_2025}",
        title=_QTR_TITLE_00700_Q3_2025,
        doc_type="QTR-HK", source_form="季度業績",
        filing_date="2025-11-13",
        report_period="2025-09-30",
        period_source=PeriodSource.EXPLICIT_TITLE,
        language="zh", document_role=DocumentRole.FULL_REPORT,
        source_issuer_id="7609",
        source_metadata={"subcategory": "季度業績"},
    )


class _AugmentedHKAdapter:
    """真实 HK 适配器 + 非解析层候选注入（不改共享解析 fixture）。

    仅追加真实生产证据候选；`default_forms`/`base_forms`/`download_headers`/
    `source_group` 等全部委托给真实适配器，检索/解析仍走真实契约代码。
    """

    def __init__(self, inner, extra: list[Report]):
        self._inner = inner
        self._extra = extra

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def list_reports(self, symbol, query):
        discovery = self._inner.list_reports(symbol, query)
        forms = set(query.forms or self._inner.default_forms())
        known = {r.source_id for r in discovery.reports}
        for report in self._extra:
            if report.market is symbol.market and report.doc_type in forms \
                    and report.source_id not in known:
                discovery.reports.append(report)
                known.add(report.source_id)
        return discovery

# 0016（六月年结，非日历年）：跨年标签 + 原文提取的六月/十二月期末
_PERIODS_0016 = {
    "listedco/listconews/sehk/2026/0319/2026031900315_c.pdf":
        "2025-12-31",   # 2025/26 中期報告
    "listedco/listconews/sehk/2025/1008/2025100800799_c.pdf":
        "2025-06-30",   # 2024/25 年報
    "listedco/listconews/sehk/2025/0320/2025032000464_c.pdf":
        "2024-12-31",   # 2024/25 中期報告
    "listedco/listconews/sehk/2024/1007/2024100700636_c.pdf":
        "2024-06-30",   # 2023/24 年報
    "listedco/listconews/sehk/2024/0320/2024032000433_c.pdf":
        "2023-12-31",   # 2023/24 中期報告
    "listedco/listconews/sehk/2023/1004/2023100400816_c.pdf":
        "2023-06-30",   # 2022/23 年報
}


def _pdf_for(link: str) -> bytes:
    return PDF_DOC + b"\n% probe-marker: " + link.encode("utf-8") + b"\n"


class RoutingSession:
    """按 URL 子串路由响应（真实适配器 + 真实 Store/Core 编排用）。"""

    def __init__(self, routes: list[tuple[str, object]]):
        self.routes = list(routes)
        self.requests: list[tuple] = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        for needle, handler in self.routes:
            if needle in url:
                if isinstance(handler, Exception):
                    raise handler
                if callable(handler):
                    return handler(method, url, **kwargs)
                return handler
        raise AssertionError(f"未预期的请求: {url}")

    def mount(self, *args, **kwargs):
        pass

    def close(self):
        pass

    # -- 请求统计 ---------------------------------------------------------

    def file_gets(self) -> list[str]:
        return [url for _m, url, _kw in self.requests if "/listedco/" in url]


def _patch_extract(monkeypatch, periods: dict[str, str | None]) -> None:
    """按 PDF 尾部标记返回"原文提取"的明确期末日；无映射/None 即提取失败。"""

    def fake_extract(path):
        data = Path(path).read_bytes()
        marker = data.split(b"% probe-marker: ", 1)[-1].split(b"\n", 1)[0]
        return periods.get(marker.decode("utf-8"))

    monkeypatch.setattr(document_period, "extract_report_period", fake_extract)


def _harness(tmp_path, *, prefix_body=_PREFIX_00700_BODY,
             page_40000="search_00700_40000_3y.raw.html",
             page_10000="search_00700_10000_yeji_2026.raw.html",
             periods: dict[str, str | None] | None = None,
             failing: frozenset[str] = frozenset(),
             extract_patch=True, monkeypatch=None,
             extra_reports: list[Report] | None = None):
    """真实适配器/Store/Core + 路由 session；返回 (service, store, session)。

    extra_reports：非解析层注入的真实生产证据候选（如 0700 Q3-2025），经
    `_AugmentedHKAdapter` 追加到真实适配器候选集，不修改共享解析 fixture。
    """
    p40000 = _raw(page_40000)
    p10000 = page_10000 if isinstance(page_10000, str) and "\n" in page_10000 \
        else _raw(page_10000)
    links = set(_PERIODS_00700) | {_QTR_LINK_00700}
    if prefix_body is _PREFIX_0016_BODY:
        links = set(_PERIODS_0016)
    for extra in extra_reports or []:
        links.add(extra.source_id)
    routes: list[tuple[str, object]] = [
        ("prefix.do", FakeResponse(
            200, prefix_body.encode("utf-8"),
            headers={"Content-Type": "text/javascript"})),
        ("t1code=10000", FakeResponse(
            200, p10000.encode("utf-8"),
            headers={"Content-Type": "text/html"})),
        ("titlesearch.xhtml", FakeResponse(
            200, p40000.encode("utf-8"),
            headers={"Content-Type": "text/html"})),
    ]
    for link in sorted(links):
        if link in failing:
            routes.append((link, requests.ConnectionError("mock 网络错误")))
        else:
            body = _pdf_for(link)
            routes.append((link, FakeResponse(
                200, body,
                headers={"Content-Type": "application/pdf",
                         "Content-Length": str(len(body))})))
    store = Store(tmp_path / "archive")
    config = Config()
    config.fetch.market_workers = 1
    session = RoutingSession(routes)
    transport = make_transport(session, config=HttpConfig())
    service = FetchService(config, store, transport)
    if extra_reports:
        # 在 get_adapter 层注入：JobService 的 begin_discovery_session 会清空
        # 适配器缓存，必须保证重新构造时仍得到注入后的适配器。
        assert monkeypatch is not None
        real_get_adapter = core_module.get_adapter

        def _injected_get_adapter(market, transport_, config_):
            inner = real_get_adapter(market, transport_, config_)
            if market is Market.HK:
                return _AugmentedHKAdapter(inner, extra_reports)
            return inner

        monkeypatch.setattr(core_module, "get_adapter", _injected_get_adapter)
    if extract_patch:
        assert monkeypatch is not None
        _patch_extract(monkeypatch, periods or {})
    return service, store, session


def _rows_by_source(store, market: str = "HK", symbol: str = "00700"
                    ) -> dict[str, dict]:
    return {r["source_id"]: r for r in store.manifest_rows(market, symbol)}


def _selected(store, result) -> list[tuple[str, str | None, str]]:
    rows = _rows_by_source(store, result.market.value, result.symbol)
    return [(rows[i.source_id]["doc_type"], rows[i.source_id]["report_period"],
             rows[i.source_id]["period_source"]) for i in result.items]


class TestMixedDefaultColdStore:
    """A1/A8/A9：默认（省略 forms）下三类候选按可信报告期穿插选择。"""

    def test_default_last4_interleaves_and_archives(self, tmp_path, monkeypatch):
        # A1（HK_QUARTERLY_DEFAULT_TASK §3.1）：冷库默认 last_n=4 精确锁定
        # 2026-06-30 INTERIM、2026-03-31 QTR-HK、2025-12-31 ANNUAL、
        # 2025-09-30 QTR-HK。Q3-2025 季度候选来自真实生产证据（非解析层注入）。
        service, store, session = _harness(
            tmp_path, monkeypatch=monkeypatch, periods=_PERIODS_00700,
            extra_reports=[_real_production_q3_2025()])
        try:
            batch = service.fetch(["0700.HK"], last_n=4)
            result = batch.results[0]
            assert result.status == "ok"
            assert result.coverage["total_groups"] == 8  # 6 完整报告 + 2 季度
            # A8：t1=10000 中 [中期業績]/[末期業績] 不重复入池（否则组数 > 8）
            assert all(i.outcome == OUTCOME_DOWNLOADED for i in result.items)
            assert _selected(store, result) == [
                ("INTERIM", "2026-06-30", "document"),
                ("QTR-HK", "2026-03-31", "explicit_title"),
                ("ANNUAL", "2025-12-31", "document"),
                ("QTR-HK", "2025-09-30", "explicit_title"),
            ]
            assert result.warnings == []      # 全部已知期，无质量缺口
            assert not result.coverage["insufficient_history"]
            for item in result.items:
                assert item.local_path and Path(item.local_path).is_file()
            # 预取下载可追溯（T3）：说明性 notice，不降级状态
            assert any("预取" in n for n in result.coverage["notices"])
            # T2.4：预取过的候选最终入选不重复下载——每个 PDF URL 恰好
            # 请求一次（6 个判期预取 + 2 个 QTR 首次下载）
            file_gets = session.file_gets()
            assert len(file_gets) == 8
            assert len(set(file_gets)) == 8
            # 未入选预取候选：无孤儿临时文件、无 artifact、discovered 行带期
            assert not list((tmp_path / "archive" / ".tmp").glob("*"))
            unselected = set(_PERIODS_00700) - {
                i.source_id for i in result.items}
            for link in unselected:
                row = _rows_by_source(store)[link]
                assert row["status"] == "discovered"
                assert row["report_period"]   # 判期结果已落库（下次免预取）
            artifacts = store.connection().execute(
                "SELECT COUNT(*) n FROM artifacts").fetchone()["n"]
            assert artifacts == 4
        finally:
            service.close()

    def test_probe_bounded_per_doc_type(self, tmp_path, monkeypatch):
        """last_n=2 → 每类型至多预取 2 个（公告时间仅限定工作量）。"""
        service, store, session = _harness(tmp_path, monkeypatch=monkeypatch,
                                           periods=_PERIODS_00700)
        try:
            batch = service.fetch(["0700.HK"], last_n=2)
            result = batch.results[0]
            assert _selected(store, result) == [
                ("INTERIM", "2026-06-30", "document"),
                ("QTR-HK", "2026-03-31", "explicit_title"),
            ]
            # 预取 ANNUAL×2 + INTERIM×2 + QTR 首次下载 1 = 5 次文件请求
            assert len(session.file_gets()) == 5
        finally:
            service.close()


class TestColdHotConsistency:
    """A2：缓存重跑与冷库首跑返回相同 report_id 顺序与报告期序列。"""

    def test_rerun_all_cached_identical_sequence(self, tmp_path, monkeypatch):
        service, store, session = _harness(
            tmp_path, monkeypatch=monkeypatch, periods=_PERIODS_00700,
            extra_reports=[_real_production_q3_2025()])
        try:
            first = service.fetch(["0700.HK"], last_n=4).results[0]
            first_ids = [i.report_id for i in first.items]
            first_seq = _selected(store, first)
            # 冷库即 A1 四期序列；热库必须逐位一致（report_id 与报告期）
            assert first_seq == [
                ("INTERIM", "2026-06-30", "document"),
                ("QTR-HK", "2026-03-31", "explicit_title"),
                ("ANNUAL", "2025-12-31", "document"),
                ("QTR-HK", "2025-09-30", "explicit_title"),
            ]
            first_warnings = list(first.warnings)
            requests_before = len(session.requests)

            file_gets_before = len(session.file_gets())
            second = service.fetch(["0700.HK"], last_n=4).results[0]
            assert all(i.outcome == OUTCOME_CACHED
                       for i in second.items)
            assert [i.report_id for i in second.items] == first_ids
            assert _selected(store, second) == first_seq
            assert second.warnings == first_warnings == []
            assert second.status == "ok"
            artifacts = store.connection().execute(
                "SELECT COUNT(*) n FROM artifacts").fetchone()["n"]
            assert artifacts == 4
            # 热库：报告期全部由阶段 A 回填，不再预取/下载任何文件
            # （prefix + 两类检索属正常的重新发现，共 3 次元数据请求）
            assert len(session.requests) - requests_before == 3
            assert len(session.file_gets()) == file_gets_before
        finally:
            service.close()

    def test_preview_backfills_periods_after_archive(self, tmp_path,
                                                     monkeypatch):
        """list 预览不下载原文：冷库混合排序偏向已知期，归档后回填一致。"""
        service, store, _session = _harness(
            tmp_path, monkeypatch=monkeypatch, periods=_PERIODS_00700,
            extra_reports=[_real_production_q3_2025()])
        try:
            cold = service.preview(["0700.HK"], last_n=4)[0][0]
            assert all(item.report_period is None
                       for item in cold.items
                       if item.doc_type != "QTR-HK")
            service.fetch(["0700.HK"], last_n=4)
            hot = service.preview(["0700.HK"], last_n=4)[0][0]
            hot_seq = [(i.doc_type, i.report_period) for i in hot.items]
            # 归档后 list 预览经阶段 A 回填，同样得到 A1 四期序列
            assert hot_seq == [
                ("INTERIM", "2026-06-30"), ("QTR-HK", "2026-03-31"),
                ("ANNUAL", "2025-12-31"), ("QTR-HK", "2025-09-30")]
        finally:
            service.close()


class TestExplicitForms:
    """A3/A4：显式 forms 行为不变。"""

    def test_explicit_annual_interim_excludes_quarterly(self, tmp_path,
                                                        monkeypatch):
        service, store, session = _harness(tmp_path, monkeypatch=monkeypatch,
                                           periods=_PERIODS_00700)
        try:
            batch = service.fetch(["0700.HK"], last_n=4,
                                  forms=["ANNUAL", "INTERIM"])
            result = batch.results[0]
            seq = _selected(store, result)
            assert seq == [
                ("INTERIM", "2026-06-30", "document"),
                ("ANNUAL", "2025-12-31", "document"),
                ("INTERIM", "2025-06-30", "document"),
                ("ANNUAL", "2024-12-31", "document"),
            ]
            assert not any("t1code=10000" in url
                           for _m, url, _kw in session.requests)
        finally:
            service.close()

    def test_explicit_qtr_only(self, tmp_path, monkeypatch):
        service, store, session = _harness(tmp_path, monkeypatch=monkeypatch,
                                           periods=_PERIODS_00700)
        try:
            batch = service.fetch(["0700.HK"], last_n=1,
                                  forms=["QTR-HK"])
            result = batch.results[0]
            assert _selected(store, result) == [
                ("QTR-HK", "2026-03-31", "explicit_title")]
            assert result.status == "ok"
            # 显式 QTR-HK：只发 t1=10000 检索；无 ANNUAL/INTERIM 判期预取
            searches = [url for _m, url, _kw in session.requests
                        if "titlesearch.xhtml" in url]
            assert len(searches) == 1 and "t1code=10000" in searches[0]
            gets = session.file_gets()
            assert len(gets) == 1 and gets[0].endswith(_QTR_LINK_00700)
        finally:
            service.close()


class TestNoQuarterlyIssuer:
    """A5/A6：无季度披露的发行人正常降级，不是错误。"""

    def test_default_degrades_without_quarterly_warning(self, tmp_path,
                                                        monkeypatch):
        service, store, _session = _harness(
            tmp_path, monkeypatch=monkeypatch, periods=_PERIODS_00700,
            page_10000=_no_quarterly_yeji_page())
        try:
            batch = service.fetch(["0700.HK"], last_n=4)
            result = batch.results[0]
            assert result.status == "ok"
            seq = _selected(store, result)
            assert {doc for doc, _p, _s in seq} == {"ANNUAL", "INTERIM"}
            assert not any("QTR-HK" == doc for doc, _p, _s in seq)
            # 用更早的年报/中报填满 last_n；无"缺少季报"类错误或警告
            assert result.warnings == []
            assert result.error is None
            assert not result.coverage["insufficient_history"]
        finally:
            service.close()

    def test_explicit_qtr_only_no_results_is_no_reports(self, tmp_path,
                                                        monkeypatch):
        service, _store, _session = _harness(
            tmp_path, monkeypatch=monkeypatch, periods=_PERIODS_00700,
            page_10000=_no_quarterly_yeji_page())
        try:
            batch = service.fetch(["0700.HK"], last_n=1,
                                  forms=["QTR-HK"])
            result = batch.results[0]
            assert result.status == "empty"
            assert result.error == "no_reports"
            assert result.items == []
            # 正常空结果，不是网络/来源失败伪装
            assert not any("source" in w for w in result.warnings)
        finally:
            service.close()


class TestNonCalendarYearEnd:
    """A7/A9：0016 六月年结——不猜 12-31；能提取则 document，否则 null+unknown。"""

    def test_extraction_success_uses_document_periods(self, tmp_path,
                                                      monkeypatch):
        service, store, _session = _harness(
            tmp_path, monkeypatch=monkeypatch, periods=_PERIODS_0016,
            prefix_body=_PREFIX_0016_BODY, page_40000="search_0016_40000_3y.raw.html",
            page_10000=_no_quarterly_yeji_page())
        try:
            batch = service.fetch(["0016.HK"], last_n=4)
            result = batch.results[0]
            # 六月年结：document 期来自"原文"，跨年标签未被拼成 12-31
            assert _selected(store, result) == [
                ("INTERIM", "2025-12-31", "document"),
                ("ANNUAL", "2025-06-30", "document"),
                ("INTERIM", "2024-12-31", "document"),
                ("ANNUAL", "2024-06-30", "document"),
            ]
            assert result.warnings == []
        finally:
            service.close()

    def test_extraction_failure_keeps_null_unknown_and_notices(
            self, tmp_path, monkeypatch):
        service, store, _session = _harness(
            tmp_path, monkeypatch=monkeypatch, periods={},
            prefix_body=_PREFIX_0016_BODY, page_40000="search_0016_40000_3y.raw.html",
            page_10000=_no_quarterly_yeji_page())
        try:
            batch = service.fetch(["0016.HK"], last_n=2)
            result = batch.results[0]
            # 提取失败：保留 null + unknown；已知期倒序规则退化为公告日倒序
            seq = _selected(store, result)
            assert seq == [("INTERIM", None, "unknown"),
                           ("ANNUAL", None, "unknown")]
            assert seq[0][0] == "INTERIM"   # 2026-03-19 公告的中期在前
            # 仅选中的 2 份产生未知期警告；未选中的聚合进 notices（A9）
            assert any("报告期未知" in w for w in result.warnings)
            assert any("未选入" in n for n in result.coverage["notices"])
            assert result.status == "partial"
            # 不猜 12-31：全部为 null
            rows = store.connection().execute(
                "SELECT report_period FROM manifest WHERE market='HK'").fetchall()
            assert all(r["report_period"] is None for r in rows)
        finally:
            service.close()


class TestPrefetchRobustness:
    """A10：预取失败/中断/重启——无孤儿文件、脏 done、永久 running。"""

    def test_prefetch_failure_does_not_block_other_types(self, tmp_path,
                                                         monkeypatch):
        failing = frozenset({"listedco/listconews/sehk/2024/0408/"
                             "2024040801823_c.pdf"})   # 2023 年報（最旧）
        service, store, session = _harness(
            tmp_path, monkeypatch=monkeypatch, periods=_PERIODS_00700,
            failing=failing)
        try:
            batch = service.fetch(["0700.HK"], last_n=4)
            result = batch.results[0]
            assert result.status == "ok"
            assert _selected(store, result) == [
                ("INTERIM", "2026-06-30", "document"),
                ("QTR-HK", "2026-03-31", "explicit_title"),
                ("ANNUAL", "2025-12-31", "document"),
                ("INTERIM", "2025-06-30", "document"),
            ]
            # 预取失败可追溯但不计入 failed、不产生质量警告
            assert any("预取失败" in n for n in result.coverage["notices"])
            assert result.warnings == []
            assert not any(i.outcome == "failed" for i in result.items)
            # 失败候选：从未持久化（无 manifest 行）、无脏 done、无遗留临时文件
            assert "listedco/listconews/sehk/2024/0408/2024040801823_c.pdf"                 not in _rows_by_source(store)
            assert not list((tmp_path / "archive" / ".tmp").glob("*"))
            intents = store.connection().execute(
                "SELECT COUNT(*) n FROM archive_intents "
                "WHERE state IN ('registered','staged')").fetchone()["n"]
            assert intents == 0
        finally:
            service.close()

    def test_prefetch_failure_of_recent_candidate_is_visible_gap(
            self, tmp_path, monkeypatch):
        """近期完整报告判期预取最终失败：不得静默漏掉后仍 ok。

        已知期候选已满足 last_n 时，失败的 2025 年報（近期）会被未知期置后并
        排除。必须形成可追溯质量缺口（warning + partial），其他可用类型继续
        返回，且不产生 failed item 伪装或丢弃可用报告。
        """
        failing = frozenset({"listedco/listconews/sehk/2026/0409/"
                             "2026040901232_c.pdf"})   # 2025 年報（近期）
        service, store, session = _harness(
            tmp_path, monkeypatch=monkeypatch, periods=_PERIODS_00700,
            failing=failing)
        try:
            batch = service.fetch(["0700.HK"], last_n=4)
            result = batch.results[0]
            seq = _selected(store, result)
            # 失败候选报告期不可知：未被选入，但结果不得伪装完整成功
            assert ("ANNUAL", None, "unknown") not in seq
            assert len(result.items) == 4
            assert all(i.outcome != OUTCOME_FAILED for i in result.items)
            assert result.status == "partial"
            assert any("判期预取失败" in w and "遗漏" in w
                       for w in result.warnings)
            # 其他可用类型（季度/中报）继续返回
            assert ("QTR-HK", "2026-03-31", "explicit_title") in seq
            assert any(doc == "INTERIM" for doc, _p, _s in seq)
        finally:
            service.close()

    def test_crash_leftover_part_swept_on_restart(self, tmp_path):
        """崩溃遗留的预取 .part 由 Store 恢复清扫收敛（不登记 intent）。"""
        archive = tmp_path / "archive"
        store = Store(archive)
        store.close()
        leftover = archive / ".tmp" / ".tmp-deadbeef.part"
        leftover.write_bytes(b"%PDF-1.7 partial")
        reopened = Store(archive)
        try:
            assert not leftover.exists()
            downloading = reopened.connection().execute(
                "SELECT COUNT(*) n FROM manifest WHERE "
                "status='downloading'").fetchone()["n"]
            assert downloading == 0
        finally:
            reopened.close()

    def test_probe_deadline_skips_prefetch(self, tmp_path, monkeypatch):
        """时限已到：判期预取整体跳过（说明性 notice），不发文件请求。"""
        service, _store, session = _harness(tmp_path, monkeypatch=monkeypatch,
                                            periods=_PERIODS_00700)
        try:
            norm = normalize_symbol("0700.HK")
            adapter = service.adapter(Market.HK)
            resolved = adapter.resolve(norm)
            discovery = adapter.list_reports(resolved, ReportQuery(last_n=4))
            requests_before = len(session.requests)
            files, notes, failures = service._prepare_selection_periods(
                adapter, norm, discovery, ReportQuery(last_n=4),
                deadline_ts=time.monotonic() - 1)
            assert files == {}
            assert failures == []
            assert any("时限" in n for n in notes)
            assert len(session.requests) == requests_before
        finally:
            service.close()


class TestJobServiceMixed:
    """HTTP 任务语义：省略 forms 的生效默认值含 QTR-HK，产出 A1 序列。"""

    def test_default_job_effective_forms_and_summary(self, tmp_path,
                                                     monkeypatch):
        # A1：HTTP 任务省略 HK forms 时生效默认含 QTR-HK，产出任务书四期序列
        service, store, _session = _harness(
            tmp_path, monkeypatch=monkeypatch, periods=_PERIODS_00700,
            extra_reports=[_real_production_q3_2025()])
        config = Config()
        config.fetch.market_workers = 1
        jobs = JobService(config, store,
                          fetch_service_factory=lambda: service)
        try:
            submitted = jobs.submit("app-a", "hk-mixed-1",
                                    symbols=["0700.HK"], last_n=4,
                                    forms_by_market=None, refresh=False)
            conn = store.connection()
            effective = conn.execute(
                "SELECT effective_request_json FROM jobs WHERE job_id=?",
                (submitted.job_id,)).fetchone()
            import json as _json
            assert _json.loads(effective["effective_request_json"])[
                "forms_by_market"]["HK"] == ["ANNUAL", "INTERIM", "QTR-HK"]
            jobs.run_job(submitted.job_id)
            doc = jobs.get_job(submitted.job_id)
            assert doc["status"] == "succeeded"
            assert doc["summary"] == {"downloaded": 4, "cached": 0, "failed": 0}
            sym = doc["results"][0]
            assert sym["status"] == "succeeded"
            by_report_id = {r["report_id"]: r
                            for r in store.manifest_rows("HK", "00700")}
            seq = [(by_report_id[r]["doc_type"],
                    by_report_id[r]["report_period"])
                   for r in sym["report_ids"]]
            assert seq == [
                ("INTERIM", "2026-06-30"), ("QTR-HK", "2026-03-31"),
                ("ANNUAL", "2025-12-31"), ("QTR-HK", "2025-09-30")]
        finally:
            service.close()
