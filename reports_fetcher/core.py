"""FetchService：核心链路编排（DESIGN §14）。

规范化/分组 → 逐市场串行 resolve/list → 统一选择 → upsert 报告 →
校验缓存/下载（有界并行）→ Store 归档 → 汇总。

失败隔离（FR-6）：单文件失败继续该证券其他文件；单证券失败继续整批。
CLI 用单市场 forms；HTTP 任务用 forms_by_market（HTTP_API §3）。
deadline_ts（单调时钟）供任务执行时限：超时后剩余证券不再触网、
待下载项直接标 job_deadline_exceeded（HTTP_API §4）。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from reports_fetcher.adapters import get_adapter
from reports_fetcher.adapters.base import BaseMarketAdapter
from reports_fetcher.config import Config
from reports_fetcher import document_period
from reports_fetcher.downloader import Transport, expected_kind_for_url
from reports_fetcher.models import (
    FetchItem,
    FetchResult,
    Market,
    NormalizedSymbol,
    OUTCOME_CACHED,
    OUTCOME_DOWNLOADED,
    OUTCOME_FAILED,
    PeriodSource,
    Report,
    ReportQuery,
    ReportsFetcherError,
    StoreError,
    SymbolError,
    SymbolPreview,
    SymbolPreviewItem,
    UaNotConfiguredError,
)
from reports_fetcher.selection import (
    PERIOD_UNKNOWN_HINT,
    SelectionResult,
    select_reports,
)
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

    def begin_discovery_session(self) -> None:
        """开始新的发现会话（PHASE1_REVIEW T2）。

        适配器内 的 resolve/list 元数据缓存（ticker 映射、submissions）
        以"发现会话"为生命周期：会话内共享（同批不重复请求），会话间
        重新获取。CLI 一次运行为一个会话；HTTP 每个任务开始时开启新会话，
        因此常驻服务能看到新发布的申报，refresh 不被旧列表阻挡。
        传输层与限速器保持共享，不因会话重建。
        """
        self._adapters = {}

    # ------------------------------------------------------------ fetch

    def fetch(self, raw_symbols: list[str], *, last_n: int | None = None,
              forms: list[str] | None = None,
              forms_by_market: dict[str, list[str]] | None = None,
              refresh: bool = False,
              deadline_ts: float | None = None) -> BatchResult:
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
        selections: dict[int, SelectionResult] = {}
        for norm in ordered:
            if deadline_ts is not None and time.monotonic() > deadline_ts:
                batch.results.append(FetchResult(
                    market=norm.market, symbol=norm.symbol,
                    status="failed", error="job_deadline_exceeded"))
                continue
            market_forms = (forms_by_market.get(norm.market.value)
                            if forms_by_market is not None else forms)
            result, selection = self._discover(norm, query_base, market_forms)
            batch.results.append(result)
            selections[id(result)] = selection
            for report in selection.selected:
                item = self._prepare_archive(report, result, refresh=refresh)
                if item is None or item.outcome == OUTCOME_CACHED:
                    continue
                adapter = self.adapter(norm.market)
                pending.append(_DownloadTask(
                    result=result, item=item, report=report,
                    group=adapter.source_group,
                    headers=adapter.download_headers(report)))

        if deadline_ts is not None and time.monotonic() > deadline_ts:
            for task in pending:
                task.item.outcome = OUTCOME_FAILED
                task.item.error = "job_deadline_exceeded"
                try:
                    self.store.register_download(task.item.report_id)
                    self.store.mark_failed(task.item.report_id,
                                           "job_deadline_exceeded",
                                           "任务执行时限已到，未下载")
                except StoreError:  # pragma: no cover
                    pass
            pending = []

        workers = max(1, min(self.config.fetch.market_workers, 3))
        if pending:
            if workers == 1:
                for task in pending:
                    self._run_download(task)
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    list(pool.map(self._run_download, pending))
        for result in batch.results:
            selection = selections.get(id(result))
            if selection is not None:
                # 原文富化可能已补全选中的报告期：在最终状态前重建期末警告，
                # 避免"已富化成功却仍保留陈旧 unknown 警告并降级"。
                self._apply_period_warnings(result, selection)
            self._finalize_status(result)
        return batch

    # ------------------------------------------------------------ list 预览

    def preview(self, raw_symbols: list[str], *, last_n: int | None = None,
                forms: list[str] | None = None,
                forms_by_market: dict[str, list[str]] | None = None
                ) -> tuple[list[SymbolPreview], list[tuple[str, SymbolError]]]:
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
            market_forms = (forms_by_market.get(norm.market.value)
                            if forms_by_market is not None else forms)
            previews.append(self._preview_symbol(norm, query_base, market_forms))
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
                "insufficient_history": bool(selection.selected)
                and selection.selected_count < query.last_n,
                "notices": list(selection.notices),
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
        notices = list(selection.notices)
        # 未选中的候选期末未知只聚合一次（PHASE1_REVIEW 后续：last_n=1
        # 不得被其他未选中行的逐条未知期警告污染）。
        selected_ids = {r.source_id for r in selection.selected}
        unselected_unknown = [
            r for r in discovery.reports
            if r.report_period is None and r.source_id not in selected_ids]
        if unselected_unknown:
            notices.append(
                f"另有 {len(unselected_unknown)} 个候选报告期未知且未选入 "
                f"last_n（未逐条告警）")
        # PHASE1_REVIEW T6：区分"影响完整性的质量警告"（warnings）与
        # "说明性信息"（notices：正常 last_n 截取、偏好语言正常选择）；
        # 不足 N 份但取到文件 → insufficient_history 缺口。
        coverage = {
            "requested": query.last_n,
            "selected": selection.selected_count,
            "total_groups": selection.total_groups,
            "exhausted": discovery.exhausted,
            "truncated": discovery.truncated,
            "searched_from": discovery.searched_from,
            "searched_to": discovery.searched_to,
            "insufficient_history": bool(selection.selected)
            and selection.selected_count < query.last_n,
            "notices": notices,
        }
        empty = FetchResult(market=norm.market, symbol=norm.symbol,
                            status="empty", error="no_reports",
                            coverage=coverage, warnings=warnings,
                            notices=notices,
                            display_name=resolved.display_name)
        ok = FetchResult(market=norm.market, symbol=norm.symbol,
                         status="ok", coverage=coverage, warnings=warnings,
                         notices=notices,
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
            self._sync_report_period(report, ref)
            cached = None if refresh else self.store.find_cached(ref.report_id)
        except StoreError as e:
            logger.error("store 写入失败 %s: %s", report.source_id, e)
            result.items.append(FetchItem(
                report_id="", source_id=report.source_id,
                outcome=OUTCOME_FAILED, error=e.code, detail=str(e)))
            return None
        if cached is not None:
            logger.info("缓存命中 %s -> %s", report.source_id, cached.local_path)
            # 缓存命中同样富化（已归档的 HK PDF 可在后续抓取时补齐 manifest，
            # 不重下）；文件内容与路径不变，不产生重复/孤儿归档。
            self._maybe_enrich_period(report, ref.report_id,
                                      cached.local_path, cached.media_type)
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
            # 归档提交完成后才富化（已校验本地 PDF 存在）：只改 manifest
            # 元数据，不触碰 artifact/路径与原子提交/恢复语义。
            self._maybe_enrich_period(report, task.item.report_id,
                                      task.item.local_path,
                                      downloaded.media_type)
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
    def _sync_report_period(report: Report, ref) -> None:
        """库中生效报告期回填内存对象（未知不覆盖可信，保证警告不陈旧）。"""
        if report.report_period is None and ref.report_period:
            report.report_period = ref.report_period
            try:
                report.period_source = PeriodSource(ref.period_source)
            except ValueError:  # pragma: no cover - 未知来源串
                report.period_source = PeriodSource.UNKNOWN

    def _maybe_enrich_period(self, report: Report, report_id: str,
                             local_path: str, media_type: str) -> bool:
        """HK PDF 年報/中期：报告期未知时从已校验原文提取明确期末日。

        仅在本地 PDF 已通过原子提交（或缓存复核）后调用；只更新 manifest
        元数据，绝不触碰归档文件/路径。失败非致命，保持 null + unknown。
        """
        if report.market is not Market.HK \
                or report.doc_type not in ("ANNUAL", "INTERIM"):
            return False
        if report.report_period is not None \
                or report.period_source is not PeriodSource.UNKNOWN:
            return False
        if media_type != "application/pdf":
            return False
        try:
            period = document_period.extract_report_period(Path(local_path))
        except Exception as e:  # noqa: BLE001 - 提取失败不得影响抓取
            logger.warning("原文报告期提取异常 %s: %s", report.source_id, e)
            return False
        if not period:
            return False
        try:
            updated = self.store.enrich_report_period(report_id, period)
        except StoreError as e:
            logger.warning("报告期富化写入失败 %s: %s", report.source_id, e)
            return False
        if updated:
            report.report_period = period
            report.period_source = PeriodSource.DOCUMENT
            logger.info("原文富化报告期 %s -> %s", report.source_id, period)
        return updated

    @staticmethod
    def _apply_period_warnings(result: FetchResult,
                               selection: SelectionResult) -> None:
        """原文富化后重建期末警告：仅对选中且仍未知的报告告警。

        清除 selection 产生的陈旧 unknown 集总警告，避免已成功富化的报告
        仍被当作 partial 缺口。
        """
        result.warnings = [w for w in result.warnings
                           if PERIOD_UNKNOWN_HINT not in w]
        usable_sources = {i.source_id for i in result.items
                          if i.outcome != OUTCOME_FAILED}
        unknown = [r for r in selection.selected
                   if r.report_period is None
                   and r.source_id in usable_sources]
        if unknown:
            result.warnings.append(
                f"{len(unknown)} 份报告{PERIOD_UNKNOWN_HINT}")

    @staticmethod
    def _finalize_status(result: FetchResult) -> None:
        """按实际可用文件与质量缺口汇总状态（PHASE1_REVIEW T6）。

        - 全部文件失败且有执行错误 → failed（无可用报告）；
        - 至少一个可用文件 + 存在失败/质量警告/预算截断/历史不足 → partial；
        - 正常满足 N（含正常截取与语言选择说明）→ ok。
        说明性 notices 不参与降级。
        """
        if result.status in ("empty", "failed") or not result.items:
            return
        usable = [i for i in result.items if i.outcome != OUTCOME_FAILED]
        if not usable:
            result.status = "failed"   # 选了报告但一份文件都没拿到
            return
        gaps = any(i.outcome == OUTCOME_FAILED for i in result.items)
        gaps = gaps or bool(result.warnings)
        gaps = gaps or bool(result.coverage.get("truncated"))
        gaps = gaps or bool(result.coverage.get("insufficient_history"))
        result.status = "partial" if gaps else "ok"
