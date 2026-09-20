"""CN / 巨潮资讯适配器（DESIGN §6，I0 已验证契约 2026-09-20）。

要点：
- resolve：POST topSearch/query，按 code 精确匹配读 orgId（gssh0/gssz0 前缀
  证实但不依赖）；不存在的代码返回 200 + 空数组（fixture 留证），不是错误；
- 首页预热一次收集 Cookie（Cookie 是否强制未消融，保守保留；失败容忍）；
- list：POST hisAnnouncement/query，column 统一 szse（沪市两值返回一致，
  I0 实测），category 限定定期报告四类，seDate 窗口 + pageNum/hasMore 分页；
- 标题解析：公司名前缀可有可无（600519 带前缀 / 000001 不带，fixture 留证）；
  季度标题"第一季度/一季度"两种变体并存；含年份 → report_period 按类型
  映射期末（A 股均为日历年结），period_source=explicit_title；无年份 →
  period=None + unknown + 警告（不猜测）；
- document_role：摘要 / 更正·补充公告 / 更新后 / 原稿（含英文版=语言 en）；
- announcementTime 毫秒时间戳 → Asia/Shanghai 公告日期，原值保留在
  source_metadata；
- 文件：static.cninfo.com.cn + adjunctUrl（实测 magic %PDF-）；
  source_id 为规范化 adjunctUrl。
"""
from __future__ import annotations

import logging
import math
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

from reports_fetcher.adapters.base import BaseMarketAdapter
from reports_fetcher.downloader import Transport
from reports_fetcher.models import (
    AmbiguousSymbolError,
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
)

logger = logging.getLogger("reports_fetcher.cn")

_CN_TZ = ZoneInfo("Asia/Shanghai")

# I0 探测使用的请求头（缺 UA/Referer 裸请求 403，见 SOURCE_VERIFICATION §1）
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
_REFERER = ("http://www.cninfo.com.cn/new/commonUrl/pageOfSearch"
            "?url=disclosure/list/notice")

_INDEX_URL = "http://www.cninfo.com.cn/new/index"
_TOPSEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"
_HISANN_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
_FILE_BASE = "http://static.cninfo.com.cn/"

# 定期报告四类（年度/半年度/一季度/三季度）
_CATEGORY = ("category_ndbg_szsh;category_bndbg_szsh;"
             "category_yjdbg_szsh;category_sjdbg_szsh")
_GROUP = "cninfo"
_PAGE_SIZE = 30

# 标题 → (doc_type, 期末月日)；A 股均为日历年结，"2026年半年度报告" 的
# 明确语义即 2026-06-30 期末（explicit_title），非财季惯例猜测。
# 顺序敏感：半年度必须先于年度匹配（子串包含）。
_CN_PERIOD_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"(\d{4})年半年度报告"), "H1", "06-30"),
    (re.compile(r"(\d{4})年第?一季度报告"), "Q1", "03-31"),
    (re.compile(r"(\d{4})年第?三季度报告"), "Q3", "09-30"),
    (re.compile(r"(\d{4})年年度报告"), "FY", "12-31"),
]
# 无年份但类型可识别 → 报告期未知（DoD：保留 null + 警告，不猜测）
_CN_TYPE_ONLY: list[tuple[str, str]] = [
    ("半年度报告", "H1"), ("一季度报告", "Q1"), ("第一季度报告", "Q1"),
    ("三季度报告", "Q3"), ("第三季度报告", "Q3"), ("年度报告", "FY"),
]


@dataclass
class CnTitleParse:
    doc_type: str | None
    report_period: str | None
    language: str
    document_role: DocumentRole
    notes: list[str] = field(default_factory=list)


def parse_cn_title(title: str) -> CnTitleParse:
    """解析巨潮公告标题；解析不出的维度保持 None（未知即 null）。"""
    language = "en" if "英文版" in title else "zh"
    if "摘要" in title:
        role = DocumentRole.SUMMARY
    elif "更正" in title or "补充公告" in title:
        role = DocumentRole.NOTICE
    elif "更新后" in title:
        role = DocumentRole.AMENDMENT_FULL
    else:
        role = DocumentRole.FULL_REPORT

    for pattern, doc_type, month_day in _CN_PERIOD_PATTERNS:
        match = pattern.search(title)
        if match:
            return CnTitleParse(doc_type, f"{match.group(1)}-{month_day}",
                                language, role)
    for keyword, doc_type in _CN_TYPE_ONLY:
        if keyword in title:
            return CnTitleParse(doc_type, None, language, role,
                                ["标题无年份，报告期置空（不猜测）"])
    return CnTitleParse(None, None, language, role,
                        ["标题无法识别定期报告类型，跳过"])


def _shift_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 2月29日 → 2月28日
        return day.replace(year=day.year + years, day=28)


def _announcement_date(ms) -> str | None:
    """毫秒时间戳 → Asia/Shanghai 公告日期（DESIGN §6）。"""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=_CN_TZ).date().isoformat()


class CNCninfoAdapter(BaseMarketAdapter):
    market = Market.CN
    base_forms: ClassVar[set[str]] = {"Q1", "H1", "Q3", "FY"}
    default_language_preference: ClassVar[list[str]] = ["zh", "en"]
    source_group: ClassVar[str] = "cninfo"

    def __init__(self, transport: Transport, config) -> None:
        super().__init__(transport, config)
        self._warmed_up = False

    # ------------------------------------------------------------ 请求头/预热

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": _UA,
            "Referer": _REFERER,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }

    def download_headers(self, report) -> dict[str, str]:
        return self._headers()

    def _ensure_warmup(self) -> None:
        """首页预热一次收集 Cookie；失败容忍（I0：Cookie 是否强制未消融）。"""
        if self._warmed_up:
            return
        self._warmed_up = True
        try:
            self.transport.request("GET", _INDEX_URL, group=_GROUP,
                                   headers=self._headers())
        except Exception as e:  # noqa: BLE001 - 预热失败继续直连（探测同策略）
            logger.warning("巨潮首页预热失败（继续尝试直连）: %s", e)

    # ------------------------------------------------------------ resolve

    def resolve(self, normalized: NormalizedSymbol) -> ResolvedSymbol:
        self._ensure_warmup()
        data = self.transport.post_form_json(
            _TOPSEARCH_URL, group=_GROUP,
            data={"keyWord": normalized.symbol, "maxNum": 10},
            headers=self._headers())
        if not isinstance(data, list):
            raise SourceContractChangedError(
                "topSearch 响应结构异常（期望数组）",
                detail=f"type={type(data).__name__}")
        matches = [row for row in data
                   if str(row.get("code", "")) == normalized.symbol]
        if not matches:
            raise ResolveError(
                "topSearch 无精确命中（代码不存在）",
                detail=f"code={normalized.symbol}")
        if len(matches) > 1:
            raise AmbiguousSymbolError(
                "topSearch 多条精确命中，无法确定主体",
                detail=f"code={normalized.symbol} rows={len(matches)}")
        row = matches[0]
        org_id = row.get("orgId")
        if not org_id:
            raise SourceContractChangedError(
                "topSearch 命中行缺少 orgId", detail=f"code={normalized.symbol}")
        return ResolvedSymbol(
            market=Market.CN,
            symbol=normalized.symbol,
            raw_inputs=[normalized.raw],
            display_name=row.get("zwjc"),
            exchange=normalized.exchange_hint,
            source_issuer_id=str(org_id),
            issuer_id=f"cninfo:{org_id}",
        )

    # ------------------------------------------------------------ list

    def list_reports(self, symbol: ResolvedSymbol,
                     query: ReportQuery) -> DiscoveryResult:
        result = DiscoveryResult(reports=[], requested_count=query.last_n)
        forms = set(query.forms or self.default_forms())
        valid_forms = {f for f in forms if f in self.base_forms} or set(self.default_forms())
        candidates: list[Report] = []

        today = datetime.now(_CN_TZ).date()
        # 分窗策略（DESIGN §5）：先小窗（覆盖 last_n 所需年数 + 1 年余量），
        # 窗口内分页必须穷尽；组数不足再有界扩窗至回溯上限（各一次）。
        stage_years = min(
            query.max_lookback_years,
            max(1, math.ceil(query.last_n / 4) + 1))
        windows = [stage_years]
        if stage_years < query.max_lookback_years:
            windows.append(query.max_lookback_years)

        used_requests = 0
        truncated = False
        exhausted = False
        for window_index, years in enumerate(windows):
            from_date = _shift_years(today, -years)
            se_date = f"{from_date.isoformat()}~{today.isoformat()}"
            page = 1
            while True:
                if used_requests >= query.max_discovery_requests:
                    truncated = True
                    exhausted = False  # 预算耗尽不得宣称窗口穷尽
                    result.warnings.append(
                        f"发现请求预算耗尽（{query.max_discovery_requests} 次），"
                        "结果可能不完整")
                    break
                payload = self.transport.post_form_json(
                    _HISANN_URL, group=_GROUP,
                    data=self._list_form(symbol, se_date, page),
                    headers=self._headers())
                used_requests += 1
                announcements = self._validate_list_payload(payload)
                for row in announcements:
                    self._append_candidate(row, symbol, valid_forms,
                                           candidates, result.warnings)
                has_more = bool(payload.get("hasMore"))
                if not has_more:
                    # 窗口分页穷尽（DESIGN §5：不能看到 N 条就停）
                    exhausted = True
                    break
                page += 1
            if truncated:
                break
            if self._group_count(candidates) >= query.last_n:
                # 末段窗口已穷尽才算 exhausted；小窗满足即停则更早历史未检索
                exhausted = window_index == len(windows) - 1
                break

        self._link_amendments(candidates)
        dates = [r.filing_date for r in candidates if r.filing_date]
        if dates:
            result.searched_from = min(dates)
            result.searched_to = max(dates)
        result.reports = candidates
        result.exhausted = exhausted
        result.truncated = truncated
        return result

    def _list_form(self, symbol: ResolvedSymbol, se_date: str,
                   page: int) -> dict:
        # 与 I0 探测一致的表单（tools/probe/probe_sources.py）
        return {
            "pageNum": page, "pageSize": _PAGE_SIZE, "column": "szse",
            "tabName": "fulltext", "plate": "",
            "stock": f"{symbol.symbol},{symbol.source_issuer_id}",
            "searchkey": "", "secid": "", "category": _CATEGORY, "trade": "",
            "seDate": se_date, "sortName": "", "sortType": "",
            "isHLtitle": "false",
        }

    @staticmethod
    def _validate_list_payload(payload) -> list[dict]:
        if not isinstance(payload, dict) or "announcements" not in payload \
                or "hasMore" not in payload:
            raise SourceContractChangedError(
                "hisAnnouncement 响应结构异常（期望 announcements/hasMore）")
        rows = payload.get("announcements")
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise SourceContractChangedError(
                "announcements 字段异常（期望数组或 null）")
        return rows

    # ------------------------------------------------------------ 候选构建

    def _append_candidate(self, row: dict, symbol: ResolvedSymbol,
                          valid_forms: set[str], candidates: list[Report],
                          warnings: list[str]) -> None:
        title = str(row.get("announcementTitle") or "")
        adjunct = str(row.get("adjunctUrl") or "")
        if not title:
            warnings.append("公告行缺少 announcementTitle，跳过")
            return
        parse = parse_cn_title(title)
        if parse.doc_type is None or parse.doc_type not in valid_forms:
            if parse.doc_type is None:
                warnings.append(f"{parse.notes[0]}: {title!r}")
            return
        if not adjunct:
            warnings.append(f"公告行缺少 adjunctUrl，跳过: {title!r}")
            return
        source_id = adjunct.lstrip("/")
        source_url = urllib.parse.urljoin(_FILE_BASE, source_id)
        announcement_time = row.get("announcementTime")
        filing_date = _announcement_date(announcement_time)
        if filing_date is None:
            warnings.append(f"公告时间缺失或非法: {title!r}")
        for note in parse.notes:
            warnings.append(f"{note}: {title!r}")
        metadata: dict = {}
        if row.get("announcementId"):
            metadata["announcementId"] = str(row["announcementId"])
        if announcement_time is not None:
            metadata["announcementTime"] = announcement_time  # 原值保留
        if row.get("adjunctSize") is not None:
            metadata["adjunctSize"] = row.get("adjunctSize")
        if row.get("associateAnnouncement"):
            metadata["associateAnnouncement"] = row.get("associateAnnouncement")
        candidates.append(Report(
            market=Market.CN,
            symbol=symbol.symbol,
            source_id=source_id,
            source_url=source_url,
            title=title,
            doc_type=parse.doc_type,
            source_form=parse.doc_type,
            filing_date=filing_date,
            report_period=parse.report_period,
            period_source=(PeriodSource.EXPLICIT_TITLE
                           if parse.report_period else PeriodSource.UNKNOWN),
            language=parse.language,
            document_role=parse.document_role,
            is_amendment=parse.document_role is DocumentRole.AMENDMENT_FULL,
            source_issuer_id=symbol.source_issuer_id,
            source_metadata=metadata,
        ))

    @staticmethod
    def _group_count(candidates: list[Report]) -> int:
        return len({(r.report_period or f"~{r.source_id}", r.doc_type)
                    for r in candidates
                    if r.document_role is not DocumentRole.SUMMARY})

    @staticmethod
    def _link_amendments(candidates: list[Report]) -> None:
        """更新后全文 → 同组更早的原稿 source_id（可确认时）。"""
        bases: dict[tuple[str, str | None], Report] = {}
        for report in sorted(candidates, key=lambda r: (r.filing_date or "",
                                                        r.source_id)):
            if report.document_role is DocumentRole.FULL_REPORT:
                bases[(report.doc_type, report.report_period)] = report
        for report in candidates:
            if report.is_amendment:
                base = bases.get((report.doc_type, report.report_period))
                if base is not None and base.source_id != report.source_id:
                    report.revision_of = base.source_id
