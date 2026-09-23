"""FetchService：核心链路编排（DESIGN §14）。

规范化/分组 → 逐市场串行 resolve/list → 报告期两阶段准备 → 统一选择 →
upsert 报告 → 校验缓存/下载（有界并行）→ Store 归档 → 汇总。

混合选择两阶段（RF-HK-QTR-DEFAULT-001 T2，冷库/热库一致性的关键）：
选择发生在归档与原文富化之前，而 HK 年报/中报标题经常只有年份（发现阶段
report_period=null）。若直接把已知期的 QTR-HK 与未知期的 ANNUAL/INTERIM
混合排序，last_n 会被季度公告占满。因此在最终选择前：
1) 阶段 A（所有市场）：按 (market, symbol, source_id) 从 Store 只读回填
   库内可信报告期（热库一致性的来源）；
2) 阶段 B（仅 HK ANNUAL/INTERIM 仍未知者）：已归档候选从本地已校验 PDF
   提取期末日；未归档候选按公告时间每类型预选至多 last_n 个，下载到已
   校验临时文件后提取（公告时间只限定工作量，绝不写成报告期）。提取成功
   即写入候选（period_source=document）并持久化，未入选候选同样落库，
   避免下次运行重复预取；临时文件若最终入选则复用提交（不重复下载），
   未入选则在任务结束前清理。
QTR-HK 的期末日始终来自标题明确日期（explicit_title）；未知即 null 铁律
不受影响——提取失败/歧义的候选保留 null + unknown 置后。
判期最终失败的近期完整报告不得静默漏掉：未知期在统一选择中被置后，已知期
满足 last_n 时会被排除；凡失败候选公告时间不早于最旧入选报告者，产生可追溯
质量警告并使证券 partial（其他可用类型继续返回），不得伪装完整成功。

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
    DownloadedFile,
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
    prefetched: DownloadedFile | None = None   # 预取已下载的校验临时文件，复用提交


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
        # 预取产生的校验临时文件：入选则复用提交（改名离开 .tmp），
        # 未入选/缓存短路则在 finally 统一清理——不得遗留孤儿临时文件。
        prefetched_files: list[DownloadedFile] = []
        try:
            for norm in ordered:
                if deadline_ts is not None and time.monotonic() > deadline_ts:
                    batch.results.append(FetchResult(
                        market=norm.market, symbol=norm.symbol,
                        status="failed", error="job_deadline_exceeded"))
                    continue
                market_forms = (forms_by_market.get(norm.market.value)
                                if forms_by_market is not None else forms)
                result, selection, prefetched = self._discover(
                    norm, query_base, market_forms, deadline_ts=deadline_ts)
                batch.results.append(result)
                selections[id(result)] = selection
                prefetched_files.extend(prefetched.values())
                for report in selection.selected:
                    item = self._prepare_archive(report, result, refresh=refresh)
                    if item is None or item.outcome == OUTCOME_CACHED:
                        continue
                    adapter = self.adapter(norm.market)
                    pending.append(_DownloadTask(
                        result=result, item=item, report=report,
                        group=adapter.source_group,
                        headers=adapter.download_headers(report),
                        prefetched=prefetched.get(report.source_id)))

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
        finally:
            self._cleanup_prefetched(prefetched_files)
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
        # list 不下载原文：仅做阶段 A 库内回填（预取/本地提取只属于 fetch）。
        # 冷库下预览的混合排序仍可能偏向已知期候选，属已文档化的预览边界。
        self._backfill_periods_from_store(norm, discovery.reports)
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
                  forms: list[str] | None, *, deadline_ts: float | None = None
                  ) -> tuple[FetchResult, SelectionResult, dict[str, DownloadedFile]]:
        """阶段 1（串行）：resolve → list → 报告期两阶段准备 → 统一选择。

        返回 (结果, 选择, 预取临时文件 by source_id)。"""
        warnings: list[str] = []
        try:
            adapter = self.adapter(norm.market)
        except ReportsFetcherError as e:
            logger.warning("市场不可用 %s: %s", norm.symbol, e)
            return (FetchResult(market=norm.market, symbol=norm.symbol,
                                status="failed", error=e.code,
                                warnings=[str(e)]), SelectionResult(), {})
        try:
            resolved = adapter.resolve(norm)
            self.store.upsert_symbol(resolved)
        except ReportsFetcherError as e:
            logger.warning("resolve 失败 %s: %s", norm.symbol, e)
            return (FetchResult(market=norm.market, symbol=norm.symbol,
                                status="failed", error=e.code),
                    SelectionResult(), {})
        query = ReportQuery(forms=self._resolve_forms(adapter, forms, norm,
                                                      warnings),
                            **query_base)
        try:
            discovery = adapter.list_reports(resolved, query)
        except ReportsFetcherError as e:
            logger.warning("list_reports 失败 %s: %s", norm.symbol, e)
            return (FetchResult(market=norm.market, symbol=norm.symbol,
                                status="failed", error=e.code),
                    SelectionResult(), {})
        warnings.extend(discovery.warnings)
        # 两阶段准备（T2）：选择前使混合候选具备可比较的可信报告期。
        prefetched, stage_notices, probe_failures = self._prepare_selection_periods(
            adapter, norm, discovery, query, deadline_ts=deadline_ts)
        selection = select_reports(discovery.reports, query,
                                   language_preference=adapter.default_language_preference)
        warnings.extend(selection.warnings)
        notices = list(selection.notices)
        notices.extend(stage_notices)   # 预取可追溯性（T3）：说明性信息，不降级
        # 未选中的候选期末未知只聚合一次（PHASE1_REVIEW 后续：last_n=1
        # 不得被其他未选中行的逐条未知期警告污染）。
        selected_ids = {r.source_id for r in selection.selected}
        # 判期预取失败的近期完整报告不得静默漏掉：未知期候选在统一选择中被
        # 置后，已知期若已满足 last_n 即被排除。以公告时间作保守代理判定
        # "可能挤掉真实入选者"的失败候选 → 可追溯质量缺口（partial），
        # 不伪装完整成功（其他可用类型继续返回）。
        omitted = self._omitted_probe_failures(probe_failures, selection)
        if omitted:
            warnings.append(
                f"{len(omitted)} 份近期完整报告（ANNUAL/INTERIM）判期预取失败，"
                f"报告期不可知，可能遗漏最新 {query.last_n} 份报告"
                f"（按公告时间无法排除其在最新 N 内）："
                f"{[r.source_id for r in omitted]}；其他可用类型已继续返回")
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
        return (ok if selection.selected else empty, selection, prefetched)

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

    # ------------------------------------------------- 混合选择两阶段（T2）

    # 阶段 B 只处理 HK 这两类：QTR-HK 期末来自标题明确日期（explicit_title），
    # US/CN 报告期来自来源字段/标题年份解析，均不依赖原文判期。
    _PROBE_DOC_TYPES = ("ANNUAL", "INTERIM")

    def _backfill_periods_from_store(self, norm: NormalizedSymbol,
                                     reports: list[Report]) -> dict[str, dict]:
        """阶段 A：按 (market, symbol, source_id) 只读回填库内可信报告期。

        未知不覆盖：仅当候选报告期未知且库内为可信（period_source != unknown）
        时回填内存对象——热库（含富化/预取落库结果）与冷库由此得到一致的
        选择输入。不写库、不产生副作用；返回库内行供阶段 B 复用。
        """
        try:
            archived = self.store.archived_periods(norm.market.value,
                                                   norm.symbol)
        except StoreError as e:  # pragma: no cover - 只读查询失败不阻断选择
            logger.warning("archived_periods 读取失败 %s: %s", norm.symbol, e)
            return {}
        for report in reports:
            row = archived.get(report.source_id)
            if row is None or report.report_period is not None:
                continue
            if row["report_period"] \
                    and row["period_source"] != PeriodSource.UNKNOWN.value:
                report.report_period = row["report_period"]
                try:
                    report.period_source = PeriodSource(row["period_source"])
                except ValueError:  # pragma: no cover - 未知来源串
                    report.period_source = PeriodSource.UNKNOWN
                report.source_metadata.pop("period_warning", None)
        return archived

    def _prepare_selection_periods(
            self, adapter: BaseMarketAdapter, norm: NormalizedSymbol,
            discovery, query: ReportQuery, *,
            deadline_ts: float | None = None
    ) -> tuple[dict[str, DownloadedFile], list[str], list[Report]]:
        """选择前的报告期准备：阶段 A 回填 + 阶段 B HK 有界判期预取。

        返回 (预取临时文件 by source_id, 说明性 notices, 判期失败候选)。
        公告时间只用于限定工作量（每类型至多 last_n 个近期候选），绝不写成
        报告期；提取失败/歧义非致命，候选保留 null + unknown。第三个返回值
        汇集预取后报告期仍不可知的候选（下载最终失败或已校验原文提取不到
        明确期末日），供上层判断是否构成"可能遗漏最新完整报告"的质量缺口。
        """
        archived = self._backfill_periods_from_store(norm, discovery.reports)
        if norm.market is not Market.HK:
            return {}, [], []
        unknown = [r for r in discovery.reports
                   if r.doc_type in self._PROBE_DOC_TYPES
                   and r.report_period is None]
        if not unknown:
            return {}, [], []

        # 每种类型按公告时间倒序预选至多 last_n 个（公告时间 ≠ 报告期）
        by_type: dict[str, list[Report]] = {}
        for report in unknown:
            by_type.setdefault(report.doc_type, []).append(report)
        probe_pool: list[Report] = []
        for members in by_type.values():
            members.sort(key=lambda r: (r.filing_date or "", r.source_id),
                         reverse=True)
            probe_pool.extend(members[:query.last_n])

        prefetched: dict[str, DownloadedFile] = {}
        probe_failures: list[Report] = []
        notes: list[str] = []
        local_extracted = failures = 0
        deadline_skipped = False
        for report in probe_pool:
            if deadline_ts is not None and time.monotonic() > deadline_ts:
                deadline_skipped = True
                continue
            row = archived.get(report.source_id)
            if row is not None and row.get("archived_path"):
                # 已归档：从本地已校验 PDF 提取，不触网
                period = self._extract_probe_period(
                    report, Path(row["archived_path"]),
                    row.get("media_type") or "")
                if period:
                    local_extracted += 1
                    self._apply_probe_period(report, period)
                    try:
                        self.store.enrich_report_period(
                            row["report_id"], period)
                    except StoreError as e:
                        logger.warning("报告期富化写入失败 %s: %s",
                                       report.source_id, e)
                else:
                    probe_failures.append(report)
                continue
            try:
                downloaded = self.transport.download(
                    report.source_url, group=adapter.source_group,
                    dest_dir=self.store.tmp_dir,
                    expected_kind=expected_kind_for_url(report.source_url),
                    headers=adapter.download_headers(report) or None)
            except Exception as e:  # noqa: BLE001 - 预取失败不阻止其他候选
                failures += 1
                probe_failures.append(report)
                logger.warning("候选预取失败 %s: %s", report.source_id, e)
                continue
            prefetched[report.source_id] = downloaded
            period = self._extract_probe_period(
                report, Path(downloaded.temp_path), downloaded.media_type)
            if period:
                self._apply_probe_period(report, period)
                # 未入选候选同样持久化可信期（discovered 行），下次运行
                # 阶段 A 直接回填，不再重复预取下载。
                try:
                    self.store.upsert_report(report)
                except StoreError as e:
                    logger.warning("候选报告期落库失败 %s: %s",
                                   report.source_id, e)
            else:
                probe_failures.append(report)
        if prefetched:
            notes.append(
                f"为混合选择预取 {len(prefetched)} 个 HK 候选用于报告期判定"
                f"（每类型至多 last_n={query.last_n} 个，按公告时间限定工作量；"
                f"未入选候选不计入 downloaded/cached，临时文件任务结束前清理）")
        if local_extracted:
            notes.append(
                f"从 {local_extracted} 个已归档 HK 原文提取报告期"
                f"（period_source=document，不重复下载）")
        if failures:
            notes.append(
                f"{failures} 个候选预取失败（候选保留未知期参与选择，"
                f"不影响其他类型；非最终文件失败，不计入 failed 统计）")
        if deadline_skipped:
            notes.append("任务时限临近，部分候选未做判期预取")
        return prefetched, notes, probe_failures

    @staticmethod
    def _omitted_probe_failures(failures: list[Report],
                                selection: SelectionResult) -> list[Report]:
        """判期失败且可能挤掉真实入选者的近期候选（可追溯质量缺口）。

        失败候选报告期未知，在统一选择中被置后；当已知期候选已满足 last_n
        时会被静默排除。报告期既不可知，则以公告时间作保守代理：不早于最旧
        入选报告公告时间的失败候选无法被证明不在最新 N 内，视为可能遗漏。
        已入选的失败候选由选择层未知期警告覆盖，不在此重复计入。
        """
        if not failures or not selection.selected:
            return []
        selected_ids = {r.source_id for r in selection.selected}
        selected_dates = [r.filing_date for r in selection.selected
                          if r.filing_date]
        oldest_selected = min(selected_dates) if selected_dates else None
        return [r for r in failures
                if r.source_id not in selected_ids
                and r.filing_date
                and (oldest_selected is None
                     or r.filing_date >= oldest_selected)]

    @staticmethod
    def _extract_probe_period(report: Report, path: Path,
                              media_type: str) -> str | None:
        """从已校验 PDF（预取临时文件或已归档文件）提取明确期末日。

        数据质量铁律：只接受原文写出的完整明确日期；失败/歧义返回 None。
        """
        if media_type != "application/pdf":
            return None
        try:
            return document_period.extract_report_period(path)
        except Exception as e:  # noqa: BLE001 - 提取失败不影响抓取
            logger.warning("候选报告期提取异常 %s: %s", report.source_id, e)
            return None

    @staticmethod
    def _apply_probe_period(report: Report, period: str) -> None:
        """预取判期结果写回内存候选（选择与后续 upsert 使用）。"""
        report.report_period = period
        report.period_source = PeriodSource.DOCUMENT
        report.source_metadata.pop("period_warning", None)

    @staticmethod
    def _cleanup_prefetched(files: list[DownloadedFile]) -> None:
        """删除未被提交消费的预取临时文件（入选者已由 Store 原子改名）。

        幂等：commit_file 改名/复用后 temp 已不存在，unlink 缺失即无操作。
        崩溃遗留的 .tmp/*.part 由 Store 下次构造时的恢复清扫收敛（A10）。
        """
        for downloaded in files:
            try:
                Path(downloaded.temp_path).unlink(missing_ok=True)
            except OSError as e:  # pragma: no cover
                logger.warning("预取临时文件清理失败 %s: %s",
                               downloaded.temp_path, e)

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
        """阶段 2：注册 → 下载（已校验临时文件）→ Store 原子提交。

        判期预取已下载过的候选直接复用其临时文件，不重复下载（T2.4）。
        """
        report = task.report
        logger.info("开始下载 %s %s (report=%s)", task.result.symbol,
                    report.source_id, task.item.report_id)
        try:
            attempt_id = self.store.register_download(task.item.report_id)
            downloaded = task.prefetched or self.transport.download(
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
