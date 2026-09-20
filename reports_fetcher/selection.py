"""统一选择函数（DESIGN §5，FR-3）："最新 N 份" = 最新 N 个逻辑报告组。

选择流程：来源类型筛选 → 排除已明确的摘要/非财报 → 识别完整修订版 →
逻辑报告分组 → 按语言选择 → 组内选最新可确认完整版本 → 已知报告期倒序、
未知期置后 → 截断 last_n。

分组键 (market, symbol, report_period, doc_type, statement_scope)；
报告期未知时每个 source_id 独立成组，不把空日期条目合成一份。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from reports_fetcher.models import (
    DocumentRole,
    Report,
    ReportQuery,
)

_FULL_ROLES = (DocumentRole.FULL_REPORT, DocumentRole.AMENDMENT_FULL)


@dataclass
class SelectionResult:
    selected: list[Report] = field(default_factory=list)
    total_groups: int = 0
    selected_count: int = 0
    warnings: list[str] = field(default_factory=list)


def _group_key(report: Report) -> tuple:
    """statement_scope 一期未知（None），字段保留给 CN/HK 规则。"""
    if report.report_period is None:
        # 未知期：独立成组，不与任何条目合并（DESIGN §5）
        return (report.market.value, report.symbol,
                f"~unknown:{report.source_id}", report.doc_type, None)
    return (report.market.value, report.symbol, report.report_period,
            report.doc_type, None)


def _version_sort_key(report: Report) -> tuple:
    """组内版本排序：公告时间倒序；同期偏好完整修订版；来源接受时间为准。"""
    return (
        report.filing_date or "",
        1 if report.is_amendment else 0,
        report.acceptance_key(),
        report.source_id,
    )


def _group_order_key(group_reports: list[Report]) -> tuple:
    """组间排序（配合 reverse=True）：已知报告期倒序在前；未知期置后。"""
    best = max(group_reports, key=_version_sort_key)
    return (
        1 if best.report_period is not None else 0,
        best.report_period or "",
        best.filing_date or "",
        best.source_id,
    )


def select_reports(candidates: list[Report], query: ReportQuery,
                   *, language_preference: list[str] | None = None
                   ) -> SelectionResult:
    """纯函数：不触网、不改库；市场特定规则由候选的元数据承载。"""
    result = SelectionResult()
    forms = query.forms

    # 1) 来源类型筛选（forms 实际生效，不能只存在于方法签名）
    pool = [r for r in candidates if forms is None or r.doc_type in forms]

    # 2) 分组（含未知期独立成组）
    groups: dict[tuple, list[Report]] = {}
    for report in pool:
        groups.setdefault(_group_key(report), []).append(report)

    selected_groups: list[list[Report]] = []
    for key, members in groups.items():
        # 3) 排除摘要/通知：无法证明为全文的不计作成功全文
        full_versions = [r for r in members if r.document_role in _FULL_ROLES]
        if not full_versions:
            non_full = [r for r in members
                        if r.document_role in (DocumentRole.NOTICE,
                                               DocumentRole.SUMMARY)]
            if non_full:
                result.warnings.append(
                    f"报告组 {key[2]}/{key[3]} 仅有摘要或修订通知，"
                    f"未获得全文（跳过: {[r.source_id for r in non_full]}）")
            continue
        # 仅有修订通知时仍可选择原全文，但必须警告未合并修订（DESIGN §5）
        unmerged = [r for r in members if r.document_role is DocumentRole.NOTICE]
        if unmerged:
            result.warnings.append(
                f"报告组 {key[2]}/{key[3]} 存在更正/修订通知未合并"
                f"（{[r.source_id for r in unmerged]}），分析时注意版本时效")
        # 4) 语言选择：优先偏好语言；不因语言丢弃唯一全文（回退 + 警告）
        chosen = _select_language(full_versions, language_preference, key, result)
        # 5) 组内版本选择：最新可确认完整版本（公告时间倒序）
        selected_groups.append(chosen)

    # 6) 排序：已知期倒序在前、未知期置后；截断 last_n
    selected_groups.sort(key=_group_order_key, reverse=True)
    for members in selected_groups:
        if len(result.selected) >= query.last_n:
            break
        # 组内版本选择：最新可确认完整版本（公告时间倒序，同期偏修订版）
        result.selected.append(max(members, key=_version_sort_key))
    result.total_groups = len(selected_groups)
    result.selected_count = len(result.selected)

    unknown = [r for r in result.selected if r.report_period is None]
    if unknown:
        result.warnings.append(
            f"{len(unknown)} 份报告报告期未知（period_source=unknown），已置于结果末尾")
    if result.total_groups > query.last_n:
        result.warnings.append(
            f"共发现 {result.total_groups} 个逻辑报告组，按 --last {query.last_n} 截断")
    return result


def _select_language(full_versions: list[Report],
                     preference: list[str] | None, key: tuple,
                     result: SelectionResult) -> list[Report]:
    """语言偏好内的一组候选；唯一语言时直接通过（不全局排除英文）。"""
    if not preference:
        return full_versions
    languages = {r.language for r in full_versions}
    if len(languages) <= 1:
        if languages and next(iter(languages)) not in preference:
            result.warnings.append(
                f"报告组 {key[2]}/{key[3]} 无偏好语言版本，回退使用 "
                f"{next(iter(languages))}")
        return full_versions
    for lang in preference:
        subset = [r for r in full_versions if r.language == lang]
        if subset:
            skipped = sorted(languages - {lang})
            result.warnings.append(
                f"报告组 {key[2]}/{key[3]} 存在多语言版本，选择 {lang}"
                f"（跳过 {skipped}）")
            return subset
    return full_versions
