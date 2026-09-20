"""US / SEC EDGAR 适配器（DESIGN §8，I0 已验证契约 2026-09-20）。

要点：
- ticker→CIK 映射来自 company_tickers.json（类股为连字符格式，精确匹配，
  不做点号转换；GOOG/GOOGL 同 CIK 不同 ticker 属正常形态）；
- submissions recent 并行数组 zip 前必须校验等长；不存在 CIK → 404 + XML
  错误体，按 symbol_not_found 处理；
- filings.files 按需读取（recent 不足 last_n 时）；历史文件顶层即并行数组，
  老申报 primaryDocument 为空串 → 跳过并警告（I1 不做 index.json 兜底）；
- reportDate 为权威期末（Apple 财年 9 月止：10-K reportDate=2025-09-27），
  不可假设日历季度；缺失/非法 → null + period_source=unknown + 警告；
- 修订申报（10-K/A 等）归入基础类型并保留 source_form/is_amendment。
"""
from __future__ import annotations

import logging
from typing import ClassVar

from reports_fetcher.adapters.base import BaseMarketAdapter
from reports_fetcher.downloader import Transport, expected_kind_for_url
from reports_fetcher.models import (
    DiscoveryResult,
    DocumentRole,
    Market,
    NormalizedSymbol,
    PeriodSource,
    Report,
    ReportQuery,
    ResolveError,
    ResolvedSymbol,
    SourceContractChangedError,
    UnexpectedStatusError,
    UaNotConfiguredError,
)
from reports_fetcher.period import parse_iso_date_safely, today_utc

logger = logging.getLogger("reports_fetcher.us")

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"
_SEC_GROUP = "sec"

_FORM_TO_BASE: dict[str, tuple[str, bool]] = {
    "10-Q": ("10-Q", False),
    "10-Q/A": ("10-Q", True),
    "10-K": ("10-K", False),
    "10-K/A": ("10-K", True),
    "20-F": ("20-F", False),
    "20-F/A": ("20-F", True),
}

_ROW_KEYS = ("accessionNumber", "filingDate", "reportDate", "form",
             "primaryDocument", "acceptanceDateTime")


def _require_ua(transport: Transport) -> dict[str, str]:
    """SEC 合规（NFR-5）：未配置真实 UA 时 US 功能明确报错。"""
    ua = transport.config.user_agent
    if not ua:
        raise UaNotConfiguredError(
            "SEC 要求真实联系邮箱 User-Agent：请在配置 [http] user_agent 填写"
            "（或设置环境变量 SEC_UA_EMAIL）后再使用 US 功能")
    return {"User-Agent": ua}


def _iter_parallel_rows(arrays: dict, *, source: str):
    """SEC 并行数组 → 行迭代；zip 前校验等长（DESIGN §8.2）。"""
    if not isinstance(arrays, dict) or "form" not in arrays:
        raise SourceContractChangedError(
            "并行数组结构缺失", detail=f"source={source}")
    lengths = {k: len(v) for k, v in arrays.items() if isinstance(v, list)}
    if len(set(lengths.values())) > 1:
        raise SourceContractChangedError(
            "并行数组长度不一致，拒绝 zip",
            detail=f"source={source} lengths={lengths}")
    count = lengths.get("form", 0)
    for key in _ROW_KEYS:
        if key in arrays and key not in lengths:
            raise SourceContractChangedError(
                f"字段 {key} 不是数组", detail=f"source={source}")
    for i in range(count):
        yield {k: arrays[k][i] for k in _ROW_KEYS if k in arrays}


class USEdgarAdapter(BaseMarketAdapter):
    market = Market.US
    base_forms: ClassVar[set[str]] = {"10-Q", "10-K", "20-F"}
    default_language_preference: ClassVar[list[str]] = ["en"]
    source_group: ClassVar[str] = "sec"

    def __init__(self, transport: Transport, config) -> None:
        super().__init__(transport, config)
        self._ticker_map: dict[str, tuple[str, str]] | None = None
        self._submissions_cache: dict[str, dict] = {}

    # ------------------------------------------------------------ resolve

    def _load_ticker_map(self) -> dict[str, tuple[str, str]]:
        if self._ticker_map is None:
            headers = _require_ua(self.transport)
            data = self.transport.get_json(_TICKER_MAP_URL, group=_SEC_GROUP,
                                           headers=headers)
            if not isinstance(data, dict):
                raise SourceContractChangedError(
                    "company_tickers.json 结构异常（期望对象）")
            mapping: dict[str, tuple[str, str]] = {}
            for entry in data.values():
                try:
                    ticker = str(entry["ticker"]).strip().upper()
                    mapping[ticker] = (str(entry["cik_str"]), str(entry.get("title", "")))
                except (KeyError, TypeError) as e:
                    raise SourceContractChangedError(
                        f"company_tickers 条目缺字段: {e}") from e
            self._ticker_map = mapping
        return self._ticker_map

    def _load_submissions(self, cik_padded: str) -> dict:
        """submissions JSON；404（XML 错误体）按 symbol_not_found 处理。"""
        if cik_padded in self._submissions_cache:
            return self._submissions_cache[cik_padded]
        headers = _require_ua(self.transport)
        url = _SUBMISSIONS_URL.format(cik=cik_padded)
        try:
            data = self.transport.get_json(url, group=_SEC_GROUP, headers=headers)
        except UnexpectedStatusError as e:
            if e.status == 404:
                raise ResolveError(
                    "CIK 无 submissions 记录（代码不存在或已注销）",
                    detail=f"cik={cik_padded}") from e
            raise
        if not isinstance(data, dict) or "filings" not in data:
            raise SourceContractChangedError(
                "submissions 结构异常（缺少 filings）", detail=f"url={url}")
        self._submissions_cache[cik_padded] = data
        return data

    def resolve(self, normalized: NormalizedSymbol) -> ResolvedSymbol:
        ticker_map = self._load_ticker_map()
        hit = ticker_map.get(normalized.symbol)
        if hit is None:
            raise ResolveError(
                "ticker 未在 SEC 官方映射中精确命中",
                detail=f"ticker={normalized.symbol}")
        cik_str, _title = hit
        cik_int = int(cik_str)
        cik_padded = f"{cik_int:010d}"
        sub = self._load_submissions(cik_padded)
        exchanges = sub.get("exchanges") or []
        return ResolvedSymbol(
            market=Market.US,
            symbol=normalized.symbol,
            raw_inputs=[normalized.raw],
            display_name=sub.get("name"),
            exchange=str(exchanges[0]) if exchanges else None,
            source_issuer_id=cik_padded,
            issuer_id=f"sec:{cik_padded}",
        )

    # ------------------------------------------------------------ list

    def list_reports(self, symbol: ResolvedSymbol,
                     query: ReportQuery) -> DiscoveryResult:
        cik_padded = symbol.source_issuer_id
        assert cik_padded is not None
        cik_int = int(cik_padded)
        sub = self._load_submissions(cik_padded)
        filings = sub["filings"]
        recent = filings.get("recent")
        if recent is None:
            raise SourceContractChangedError(
                "submissions 缺少 filings.recent", detail=f"cik={cik_padded}")

        forms = set(query.forms or self.default_forms())
        valid_forms = {f for f in forms if f in self.base_forms}
        if not valid_forms:
            valid_forms = set(self.default_forms())

        result = DiscoveryResult(reports=[], requested_count=query.last_n)
        candidates: list[Report] = []
        requests_used = 1  # submissions 本身计一次发现请求

        for row in _iter_parallel_rows(recent, source="recent"):
            self._append_candidate(row, cik_int, symbol, valid_forms,
                                   candidates, result)

        # 按需扩窗：recent 内逻辑报告组不足 last_n 时读 filings.files
        # 历史文件（默认场景 recent 覆盖，不触历史）
        if self._group_count(candidates) < query.last_n:
            files = filings.get("files") or []
            cutoff = today_utc().replace(
                year=today_utc().year - query.max_lookback_years).isoformat()
            stopped_early = False
            for file_meta in sorted(files, key=lambda f: f.get("filingTo", ""),
                                    reverse=True):
                if requests_used >= query.max_discovery_requests:
                    result.truncated = True
                    result.exhausted = False  # 预算耗尽不得宣称窗口穷尽
                    result.warnings.append(
                        f"发现请求预算耗尽（{query.max_discovery_requests} 次），"
                        "结果可能不完整")
                    stopped_early = True
                    break
                if str(file_meta.get("filingTo", "")) < cutoff:
                    break  # 整段历史文件早于回溯窗口：声明窗口已完成
                headers = _require_ua(self.transport)
                url = f"https://data.sec.gov/submissions/{file_meta.get('name', '')}"
                try:
                    hist = self.transport.get_json(url, group=_SEC_GROUP,
                                                   headers=headers)
                except UnexpectedStatusError as e:
                    result.warnings.append(
                        f"历史文件读取失败 HTTP {e.status}: {file_meta.get('name')}")
                    continue
                requests_used += 1
                if not isinstance(hist, dict) or "form" not in hist:
                    raise SourceContractChangedError(
                        "历史文件顶层应为并行数组（DESIGN §8.3）",
                        detail=f"file={file_meta.get('name')}")
                for row in _iter_parallel_rows(hist, source=str(file_meta.get("name"))):
                    self._append_candidate(row, cik_int, symbol, valid_forms,
                                           candidates, result)
                if self._group_count(candidates) >= query.last_n:
                    stopped_early = True  # N 已满足即停，更早历史未检索
                    break
            result.exhausted = not stopped_early
        else:
            result.exhausted = False  # recent 已满足 N，更早历史未检索

        filing_dates = [r.filing_date for r in candidates if r.filing_date]
        if filing_dates:
            result.searched_from = min(filing_dates)
            result.searched_to = max(filing_dates)
        self._link_amendments(candidates)
        result.reports = candidates
        return result

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _group_count(candidates: list[Report]) -> int:
        keys = {(r.report_period or f"~{r.source_id}", r.doc_type)
                for r in candidates}
        return len(keys)

    def _append_candidate(self, row: dict, cik_int: int, symbol: ResolvedSymbol,
                          valid_forms: set[str], candidates: list[Report],
                          result: DiscoveryResult) -> None:
        form = str(row.get("form") or "")
        mapped = _FORM_TO_BASE.get(form)
        if mapped is None:
            return
        doc_type, is_amendment = mapped
        if doc_type not in valid_forms:
            return
        accession = str(row.get("accessionNumber") or "")
        primary_doc = str(row.get("primaryDocument") or "")
        if not accession:
            result.warnings.append(f"候选行缺少 accessionNumber，跳过: form={form}")
            return
        if not primary_doc:
            # 老申报（1990 年代）primaryDocument 为空串（fixture 留证）；
            # I1 跳过并警告，不做 index.json 兜底
            result.warnings.append(
                f"历史申报 primaryDocument 为空，跳过: {accession} form={form}")
            return
        source_id = f"{accession}/{primary_doc}"
        report_period, period_ok = parse_iso_date_safely(
            row.get("reportDate"), what="reportDate")
        filing_date, filing_ok = parse_iso_date_safely(
            row.get("filingDate"), what="filingDate")
        if report_period is None:
            # 数据质量铁律：报告期解析不出 → unknown + 警告（缺失与非法都计入）
            reason = "缺失" if not row.get("reportDate") else "非法"
            result.warnings.append(
                f"reportDate {reason}，报告期置空（不猜测）: {source_id} "
                f"reportDate={row.get('reportDate')!r}")
        if not filing_ok:
            result.warnings.append(
                f"filingDate 非法: {source_id} filingDate={row.get('filingDate')!r}")
        acc_nodash = accession.replace("-", "")
        source_url = _ARCHIVE_URL.format(cik_int=cik_int, acc_nodash=acc_nodash,
                                         doc=primary_doc)
        metadata = {"accessionNumber": accession, "form": form}
        acceptance = row.get("acceptanceDateTime")
        if acceptance:
            metadata["acceptanceDateTime"] = str(acceptance)
        size = row.get("size")
        if size not in (None, ""):
            metadata["size"] = size
        candidates.append(Report(
            market=Market.US,
            symbol=symbol.symbol,
            source_id=source_id,
            source_url=source_url,
            title=primary_doc,
            doc_type=doc_type,
            source_form=form,
            filing_date=filing_date,
            report_period=report_period,
            period_source=(PeriodSource.SOURCE_FIELD
                           if report_period else PeriodSource.UNKNOWN),
            language="en",
            document_role=(DocumentRole.AMENDMENT_FULL if is_amendment
                           else DocumentRole.FULL_REPORT),
            is_amendment=is_amendment,
            source_issuer_id=symbol.source_issuer_id,
            source_metadata=metadata,
        ))

    @staticmethod
    def _link_amendments(candidates: list[Report]) -> None:
        """/A 修订关联：同组（doc_type + report_period）内存在更早基础版本时链接。"""
        bases: dict[tuple[str, str | None], Report] = {}
        for report in sorted(candidates, key=lambda r: (r.filing_date or "",
                                                        r.source_id)):
            if not report.is_amendment:
                bases[(report.doc_type, report.report_period)] = report
        for report in candidates:
            if report.is_amendment:
                base = bases.get((report.doc_type, report.report_period))
                if base is not None and base.source_id != report.source_id:
                    report.revision_of = base.source_id

    def download_headers(self, report) -> dict[str, str]:
        # SEC 全部域名（含 Archives 文件下载）都要求真实 UA（NFR-5）
        return _require_ua(self.transport)
