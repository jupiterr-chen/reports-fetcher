"""FetchService：核心链路编排（DESIGN §14）。

规范化/分组 → 逐市场串行 resolve/list → 统一选择 → upsert 报告 →
校验缓存/下载（有界并行）→ Store 归档 → 汇总。

失败隔离（FR-6）：单文件失败继续该证券其他文件；单证券失败继续整批。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from reports_fetcher.adapters import get_adapter
from reports_fetcher.adapters.base import BaseMarketAdapter
from reports_fetcher.config import Config
from reports_fetcher.downloader import Transport, expected_kind_for_url
from reports_fetcher.models import (
    FetchItem,
    FetchResult,
    Market,
    NormalizedSymbol,
    OUTCOME_CACHED,
    OUTCOME_DOWNLOADED,
    OUTCOME_FAILED,
    Report,
    ReportQuery,
    ReportsFetcherError,
    StoreError,
    SymbolError,
    SymbolPreview,
    SymbolPreviewItem,
    UaNotConfiguredError,
)
from reports_fetcher.selection import SelectionResult, select_reports
from reports_fetcher.store import Store
from reports_fetcher.symbol import batch_normalize

logger = logging.getLogger("reports_fetcher.core")


@dataclass
class BatchResult:
    results: list[FetchResult] = field(default_factory=list)
    invalid: list[tuple[str, SymbolError]] = field(default_factory=list)


@dataclass
class _DownloadTask:
    result: FetchResult
    item: FetchItem
    report: Report
    group: str
    headers: dict[str, str]


class FetchService:
    def __init__(self, config: Config, store: Store, transport: Transport) -> None:
        self.config = config
        self.store = store
        self.transport = transport
        self._adapters: dict[Market, BaseMarketAdapter] = {}

    @classmethod
    def from_config(cls, config: Config, *, store: Store | None = None,
                    transport: Transport | None = None) -> "FetchService":
        store = store or Store(Path(config.general.out_dir),
                               layout=config.general.layout)
        transport = transport or Transport(config.http)
        return cls(config, store, transport)

    def close(self) -> None:
        self.store.close()

    def adapter(self, market: Market) -> BaseMarketAdapter:
        if market not in self._adapters:
            self._adapters[market] = get_adapter(market, self.transport,
                                                 self.config)
        return self._adapters[market]

    # ------------------------------------------------------------ fetch

    def fetch(self, raw_symbols: list[str], *, last_n: int | None = None,
              forms: list[str] | None = None,
              refresh: bool = False) -> BatchResult:
        """抓取并归档。refresh=True 时重下候选并保留旧内容版本（多 artifact
        并存、按 ID 读取，DESIGN §12）；默认仅跳过已有。"""
        ordered, _aliases, invalid = batch_normalize(raw_symbols)
        batch = BatchResult(invalid=invalid)
        if not ordered:
            return batch

        markets = {norm.market for norm in ordered}
        if Market.US in markets and not self.transport.config.user_agent:
            # NFR-5：SEC UA 未配置时 US 功能明确报错（联网前拦截）
            raise UaNotConfiguredError(
                "SEC 要求真实联系邮箱 User-Agent：请在配置文件 [http] user_agent "
                "填写（或设置环境变量 SEC_UA_EMAIL）后再使用 US 功能")

        query_base = dict(
            last_n=last_n or self.config.fetch.last_n,
            max_lookback_years=self.config.fetch.max_lookback_years,
            max_discovery_requests=self.config.fetch.max_discovery_requests_per_symbol,
        )
        pending: list[_DownloadTask] = []
        for norm in ordered:
            result, selection = self._discover(norm, query_base, forms)
            batch.results.append(result)
            for report in selection.selected:
                item = self._prepare_archive(report, result, refresh=refresh)
                if item is None or item.outcome == OUTCOME_CACHED:
                    continue
                adapter = self.adapter(norm.market)
                pending.append(_DownloadTask(
                    result=result, item=item, report=report,
                    group=adapter.source_group,
                    headers=adapter.download_headers(report)))

        workers = max(1, min(self.config.fetch.market_workers, 3))
        if pending:
            if workers == 1:
                for task in pending:
                    self._run_download(task)
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    list(pool.map(self._run_download, pending))
        for result in batch.results:
            self._finalize_status(result)
        return batch

    # ------------------------------------------------------------ list 预览

    def preview(self, raw_symbols: list[str], *, last_n: int | None = None,
                forms: list[str] | None = None) -> tuple[list[SymbolPreview],
                                                         list[tuple[str, SymbolError]]]:
        """联网预览元数据与覆盖信息，不下载原文；会更新 symbol_map（缓存副作用）。"""
        ordered, _aliases, invalid = batch_normalize(raw_symbols)
        markets = {norm.market for norm in ordered}
        if Market.US in markets and not self.transport.config.user_agent:
            raise UaNotConfiguredError(
                "SEC 要求真实联系邮箱 User-Agent：请在配置文件 [http] user_agent "
                "填写（或设置环境变量 SEC_UA_EMAIL）后再使用 US 功能")
        query_base = dict(
            last_n=last_n or self.config.fetch.last_n,
            max_lookback_years=self.config.fetch.max_lookback_years,
            max_discovery_requests=self.config.fetch.max_discovery_requests_per_symbol,
        )
        previews: list[SymbolPreview] = []
        for norm in ordered:
            previews.append(self._preview_symbol(norm, query_base, forms))
        return previews, invalid

    def _preview_symbol(self, norm: NormalizedSymbol, query_base: dict,
                        forms: list[str] | None) -> SymbolPreview:
        warnings: list[str] = []
        try:
            adapter = self.adapter(norm.market)
            resolved = adapter.resolve(norm)
            self.store.upsert_symbol(resolved)
        except ReportsFetcherError as e:
            return SymbolPreview(market=norm.market, symbol=norm.symbol,
                                 status="failed", error=e.code,
                                 warnings=[str(e)])
        query = ReportQuery(
            forms=self._resolve_forms(adapter, forms, norm, warnings),
            **query_base)
        try:
            discovery = adapter.list_reports(resolved, query)
        except ReportsFetcherError as e:
            return SymbolPreview(market=norm.market, symbol=norm.symbol,
                                 status="failed", error=e.code)
        warnings.extend(discovery.warnings)
        selection = select_reports(discovery.reports, query,
                                   language_preference=adapter.default_language_preference)
        warnings.extend(selection.warnings)
        archived = self.store.archived_source_ids(norm.market.value, norm.symbol)
        items = [SymbolPreviewItem(
            source_id=r.source_id,
            report_id=archived.get(r.source_id),
            doc_type=r.doc_type,
            report_period=r.report_period,
            filing_date=r.filing_date,
            title=r.title,
            source_url=r.source_url,
            archived=r.source_id in archived,
        ) for r in selection.selected]
        return SymbolPreview(
            market=norm.market, symbol=norm.symbol,
            status="ok" if items else "empty",
            display_name=resolved.display_name, items=items,
            coverage={
                "requested": query.last_n,
                "selected": selection.selected_count,
                "total_groups": selection.total_groups,
                "exhausted": discovery.exhausted,
                "truncated": discovery.truncated,
                "searched_from": discovery.searched_from,
                "searched_to": discovery.searched_to,
            },
            warnings=warnings,
            error=None if items else "no_reports")

    # ------------------------------------------------------------ 内部

    def _discover(self, norm: NormalizedSymbol, query_base: dict,
                  forms: list[str] | None
                  ) -> tuple[FetchResult, SelectionResult]:
        """阶段 1（串行）：resolve → list → 统一选择。"""
        warnings: list[str] = []
        try:
            adapter = self.adapter(norm.market)
        except ReportsFetcherError as e:
            logger.warning("市场不可用 %s: %s", norm.symbol, e)
            return (FetchResult(market=norm.market, symbol=norm.symbol,
                                status="failed", error=e.code,
                                warnings=[str(e)]), SelectionResult())
        try:
            resolved = adapter.resolve(norm)
            self.store.upsert_symbol(resolved)
        except ReportsFetcherError as e:
            logger.warning("resolve 失败 %s: %s", norm.symbol, e)
            return (FetchResult(market=norm.market, symbol=norm.symbol,
                                status="failed", error=e.code), SelectionResult())
        query = ReportQuery(forms=self._resolve_forms(adapter, forms, norm,
                                                      warnings),
                            **query_base)
        try:
            discovery = adapter.list_reports(resolved, query)
        except ReportsFetcherError as e:
            logger.warning("list_reports 失败 %s: %s", norm.symbol, e)
            return (FetchResult(market=norm.market, symbol=norm.symbol,
                                status="failed", error=e.code), SelectionResult())
        warnings.extend(discovery.warnings)
        selection = select_reports(discovery.reports, query,
                                   language_preference=adapter.default_language_preference)
        warnings.extend(selection.warnings)
        coverage = {
            "requested": query.last_n,
            "selected": selection.selected_count,
            "total_groups": selection.total_groups,
            "exhausted": discovery.exhausted,
            "truncated": discovery.truncated,
            "searched_from": discovery.searched_from,
            "searched_to": discovery.searched_to,
        }
        empty = FetchResult(market=norm.market, symbol=norm.symbol,
                            status="empty", error="no_reports",
                            coverage=coverage, warnings=warnings,
                            display_name=resolved.display_name)
        ok = FetchResult(market=norm.market, symbol=norm.symbol,
                         status="ok", coverage=coverage, warnings=warnings,
                         display_name=resolved.display_name)
        return (ok if selection.selected else empty, selection)

    def _resolve_forms(self, adapter: BaseMarketAdapter, forms: list[str] | None,
                       norm: NormalizedSymbol, warnings: list[str]) -> list[str] | None:
        """forms 生效点：用户显式指定时校验 ⊆ 市场基础类型，否则市场默认。"""
        if not forms:
            return adapter.default_forms()
        unknown = [f for f in forms if f not in adapter.base_forms]
        if unknown:
            warnings.append(
                f"忽略不支持的基础类型 {unknown}（{norm.market.value} 支持 "
                f"{sorted(adapter.base_forms)}），使用市场默认")
            return adapter.default_forms()
        return forms

    def _prepare_archive(self, report: Report, result: FetchResult, *,
                         refresh: bool = False) -> FetchItem | None:
        """upsert 候选报告得到稳定 report_id；缓存命中（sha256 复核）则跳过。

        refresh 模式不做缓存短路：重下候选，内容变化产生新 artifact、
        旧版本保留（DESIGN §12）。
        """
        try:
            ref = self.store.upsert_report(report)
            cached = None if refresh else self.store.find_cached(ref.report_id)
        except StoreError as e:
            logger.error("store 写入失败 %s: %s", report.source_id, e)
            result.items.append(FetchItem(
                report_id="", source_id=report.source_id,
                outcome=OUTCOME_FAILED, error=e.code, detail=str(e)))
            return None
        if cached is not None:
            logger.info("缓存命中 %s -> %s", report.source_id, cached.local_path)
            result.items.append(FetchItem(
                report_id=ref.report_id, source_id=report.source_id,
                outcome=OUTCOME_CACHED, local_path=str(cached.local_path)))
            return None
        item = FetchItem(report_id=ref.report_id, source_id=report.source_id,
                         outcome=OUTCOME_FAILED)  # 占位：下载结果回填
        result.items.append(item)
        return item

    def _run_download(self, task: _DownloadTask) -> None:
        """阶段 2：注册 → 下载（已校验临时文件）→ Store 原子提交。"""
        report = task.report
        logger.info("开始下载 %s %s (report=%s)", task.result.symbol,
                    report.source_id, task.item.report_id)
        try:
            attempt_id = self.store.register_download(task.item.report_id)
            downloaded = self.transport.download(
                report.source_url, group=task.group,
                dest_dir=self.store.tmp_dir,
                expected_kind=expected_kind_for_url(report.source_url),
                headers=task.headers or None)
            outcome = self.store.commit_file(task.item.report_id, report,
                                             downloaded, attempt_id)
            task.item.outcome = OUTCOME_DOWNLOADED
            task.item.local_path = str(self.store.root / outcome.rel_path)
            if outcome.reused:
                task.item.detail = "内容未变化，复用既有内容版本"
            logger.info("归档完成 %s -> %s%s", report.source_id, outcome.rel_path,
                        "（复用既有内容版本）" if outcome.reused else "")
        except Exception as e:
            code = getattr(e, "code", "internal_error")
            detail = str(e)
            try:
                self.store.mark_failed(task.item.report_id, code, detail)
            except StoreError as se:  # pragma: no cover
                logger.error("mark_failed 失败: %s", se)
            task.item.outcome = OUTCOME_FAILED
            task.item.error = code
            task.item.detail = detail[:300]
            logger.warning("下载失败 %s: %s %s", report.source_id, code, detail)

    @staticmethod
    def _finalize_status(result: FetchResult) -> None:
        if result.status in ("empty", "failed") or not result.items:
            return
        all_ok = all(i.outcome != OUTCOME_FAILED for i in result.items)
        result.status = "ok" if all_ok and not result.warnings else "partial"
