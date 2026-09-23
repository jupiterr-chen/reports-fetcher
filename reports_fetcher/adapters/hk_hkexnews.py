"""HK / 披露易适配器（DESIGN §7，I0 已验证契约 2026-09-20）。

契约要点（v1.3，旧 titleSearcherJson.do 已 404）：
- resolve：GET prefix.do（callback=callback&lang=ZH&type=A&name=<code>&market=SEHK），
  JSONP 剥壳只剥已知包装、绝不 eval；返回五位 code 与 stockId，按 code 精确
  匹配；不存在代码返回空 stockInfo（fixture 留证）；
- list：GET titlesearch.xhtml 深链 + 服务端渲染 HTML；有效日期参数是
  from/to（YYYYMMDD），fromDate/toDate 被忽略（参数矩阵实测）；
- 结果行：<tr> 含發放時間（DD/MM/YYYY HH:MM，Asia/Hong_Kong）、股份代號
  （可能含人民币柜台第二代码，只取主代码）、headline 文本、PDF 链接与大小；
- 子類別（方括号文本，实体反转义后）是 doc_type / 排除规则的权威来源：
  [年報]→ANNUAL、[中期/半年度報告]→INTERIM、[環境、社會及管治資料/報告]→
  排除（与财报同在 t1=40000，不可只按类别判定）；
- t1=10000 + title=業績 可召回 [季度業績] 公告 → QTR-HK；v1.0.3 起纳入
  HK 默认类型（RF-HK-QTR-DEFAULT-001，发行人无季度材料时正常跳过），
  显式 forms 仍可只取 ANNUAL/INTERIM 或只取 QTR-HK；
- 单页返回窗口内全部结果；站点显示上限 1000 条，超限按年切窗（不做
  load-more 模拟）；
- 免 Cookie/免预热；偶发 TLS 握手重置由传输层显式重试兜底；
- 标题期末规则（PHASE1_REVIEW T5 后）：仅"明确期末日"（截至…止，
  如业绩公告标题）→ explicit_title；单年标签（2025 年報 / 中期報告 2026）
  与跨年标签（2024/25 年報）都只有年份语义、无期末日证据 →
  null + unknown + 警告（不按 12-31/06-30 拼接）。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
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
from reports_fetcher.period import chinese_small_number, chinese_year

logger = logging.getLogger("reports_fetcher.hk")

_HK_TZ = ZoneInfo("Asia/Hong_Kong")

_PREFIX_URL = "https://www1.hkexnews.hk/search/prefix.do"
_SEARCH_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml"
_FILE_BASE = "https://www1.hkexnews.hk"
_GROUP = "hkex"

_SITE_ROW_LIMIT = 1000  # 站点显示上限（页面配置 ViewMoreRecords，I0 实测）

# 子類別（实体反转义后）→ doc_type；权威来源，优于标题正则（DESIGN §7）
_SUBCATEGORY_MAP: dict[str, tuple[str, DocumentRole]] = {
    "年報": ("ANNUAL", DocumentRole.FULL_REPORT),
    "中期/半年度報告": ("INTERIM", DocumentRole.FULL_REPORT),
    "季度業績": ("QTR-HK", DocumentRole.FULL_REPORT),
}
# t1=40000 内的已知非财报子类别（排除，不警告）
_KNOWN_EXCLUDED = {"環境、社會及管治資料/報告"}

_CN_YEAR_CHARS = "零〇○一二贰貳兩两三叁參四肆五伍六陆陸七柒八捌九玖"
_CN_SMALL_CHARS = "一二三四五六七八九十"

# 标题期末解析（市场规则放适配器，DESIGN §9）
_HK_EXPLICIT_DATE_RE = re.compile(
    r"截至\s*(?P<y>[" + _CN_YEAR_CHARS + r"\d]{4})\s*年"
    r"\s*(?P<m>[" + _CN_SMALL_CHARS + r"\d]{1,3})\s*月"
    r"\s*(?P<d>[" + _CN_SMALL_CHARS + r"\d]{1,3})\s*日")
# 跨年标签：任何 YYYY/NN 斜杠年份（2024/25 年報、2025/26 中期報告、
# 2024/25年中期業績報告 等）都意味着非日历年结，日历期末不可得
_HK_CROSS_YEAR_RE = re.compile(r"\d{4}\s*/\s*\d{2,4}")
_HK_ANNUAL_YEAR_RE = re.compile(r"(\d{4})\s*年報")
_HK_INTERIM_YEAR_RE = re.compile(r"中期報告\s*[:：]?\s*(\d{4})")
# 年份在前的中期变体（匯豐形態：2026年中期業績報告，I3 e2e 实测 2026-09-21）
_HK_INTERIM_YEAR_BEFORE_RE = re.compile(
    r"(\d{4})\s*年\s*中期(?:業績)?報告")
# 中文数字年份自带"年"字（二零二四年 + 年報 → 二零二四年年報）
_HK_CN_ANNUAL_YEAR_RE = re.compile(
    r"([" + _CN_YEAR_CHARS + r"]{4})\s*年\s*年報")
_HK_CN_INTERIM_YEAR_RE = re.compile(
    r"中期報告\s*[:：]?\s*([" + _CN_YEAR_CHARS + r"]{4})\s*年?")

_LABEL_PREFIX_RE = re.compile(
    r"^(?:發放時間|股份代號|股份簡稱|文件)\s*[:：]\s*")
_RECORD_COUNT_RE = re.compile(r"共有\s*(\d+)\s*紀錄")
_RELEASE_TIME_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})")


class SearchPageParser(HTMLParser):
    """解析 titlesearch.xhtml 服务端渲染结果行（fixture 为解析基准）。

    convert_charrefs=True 保证 &#x2f; 等实体先反转义再匹配（DESIGN §7）。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._in_tbody = False
        self._row: dict | None = None
        self._td_class: str | None = None
        self._cell_text: list[str] = []
        self._headline_depth = 0
        self._headline_text: list[str] = []
        self._doc_link_depth = 0
        self._in_a = False
        self._a_href: str | None = None
        self._a_text: list[str] = []
        self._in_filesize = False
        self._filesize_text: list[str] = []

    # -- 标签事件 ----------------------------------------------------

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if tag == "tbody":
            self._in_tbody = True
        elif tag == "tr" and self._in_tbody:
            self._row = {"cells": {}, "headline": None, "file_link": None,
                         "title": None, "file_size": None}
        elif tag == "td" and self._row is not None:
            self._td_class = " ".join(classes)
            self._cell_text = []
        elif tag == "br":
            if self._headline_depth:
                self._headline_text.append("\n")
            elif self._td_class is not None:
                self._cell_text.append("\n")
        elif tag == "div":
            if "headline" in classes and self._row is not None:
                self._headline_depth += 1
            elif "doc-link" in classes and self._row is not None:
                self._doc_link_depth += 1
        elif tag == "a" and self._doc_link_depth and self._row is not None:
            self._in_a = True
            self._a_href = attr.get("href")
            self._a_text = []
        elif tag == "span" and "attachment_filesize" in classes \
                and self._row is not None:
            self._in_filesize = True
            self._filesize_text = []

    def handle_endtag(self, tag):
        if tag == "tbody":
            self._in_tbody = False
        elif tag == "tr" and self._row is not None:
            self._finish_row()
        elif tag == "td":
            if self._row is not None and self._td_class:
                text = "".join(self._cell_text)
                self._row["cells"][self._td_class] = text.strip()
            self._td_class = None
            self._cell_text = []
        elif tag == "div":
            if self._headline_depth:
                self._headline_depth -= 1
                if self._headline_depth == 0 and self._row is not None:
                    self._row["headline"] = "".join(
                        self._headline_text).strip()
                    self._headline_text = []
            elif self._doc_link_depth:
                self._doc_link_depth -= 1
        elif tag == "a" and self._in_a:
            self._in_a = False
            if self._row is not None:
                self._row["file_link"] = self._a_href
                self._row["title"] = "".join(self._a_text).strip()
        elif tag == "span" and self._in_filesize:
            self._in_filesize = False
            if self._row is not None:
                self._row["file_size"] = "".join(
                    self._filesize_text).strip() or None

    def handle_data(self, data):
        if self._in_filesize:
            self._filesize_text.append(data)
        elif self._in_a:
            self._a_text.append(data)
        elif self._headline_depth:
            self._headline_text.append(data)
        elif self._td_class is not None:
            self._cell_text.append(data)

    # -- 行归一 ------------------------------------------------------

    def _finish_row(self) -> None:
        row = self._row
        self._row = None
        self._td_class = None
        if not row or not row.get("file_link"):
            return
        normalized = {
            "release_datetime": _strip_label(
                row["cells"].get(_cell_class(row, ("release-time",)), "")),
            "stock_code": _first_line(_strip_label(
                row["cells"].get(_cell_class(row, ("stock-short-code",)), ""))),
            "stock_name": _first_line(_strip_label(
                row["cells"].get(_cell_class(row, ("stock-short-name",)), ""))),
            "headline": row.get("headline") or "",
            "title": row.get("title") or "",
            "file_link": row["file_link"],
            "file_size": row.get("file_size"),
        }
        self.rows.append(normalized)


def _cell_class(row: dict, keywords: tuple[str, ...]) -> str | None:
    for cls in row["cells"]:
        if any(keyword in cls for keyword in keywords):
            return cls
    return None


def _strip_label(text: str) -> str:
    return _LABEL_PREFIX_RE.sub("", text.strip())


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def parse_search_page(html_text: str) -> tuple[int | None, list[dict]]:
    """解析深链检索页：返回 (record_count, rows)。"""
    count_match = _RECORD_COUNT_RE.search(html_text)
    record_count = int(count_match.group(1)) if count_match else None
    parser = SearchPageParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as e:  # noqa: BLE001 - 结构异常即契约变化
        raise SourceContractChangedError(
            f"检索页 HTML 解析失败: {e}") from e
    if record_count is None:
        raise SourceContractChangedError(
            "检索页缺少「共有 N 紀錄」总数标记（契约变化或错误页）")
    # 防御：桌面/移动重复表按链接去重
    deduped: list[dict] = []
    seen: set[str] = set()
    for row in parser.rows:
        if row["file_link"] not in seen:
            seen.add(row["file_link"])
            deduped.append(row)
    return record_count, deduped


def strip_jsonp(text: str, callback: str = "callback") -> str:
    """只剥已知 JSONP 包装（callback( ... ); 形态），绝不 eval（DESIGN §7）。"""
    text = text.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    prefix = f"{callback}("
    if not text.startswith(prefix) or not text.endswith(")"):
        raise SourceContractChangedError(
            "JSONP 包装不符合已知形式（callback( ... )），拒绝解析")
    return text[len(prefix):-1]


def parse_hk_title_period(title: str) -> tuple[str | None, PeriodSource,
                                               str | None]:
    """HK 标题 → (报告期, period_source, 警告|None)。

    PHASE1_REVIEW T5：仅"明确期末日"（截至…止）可设期；单年标签与跨年
    标签都只包含年份语义、没有期末日证据 → null + unknown + 警告，
    不按 12-31/06-30 财季惯例拼接（AGENTS 数据质量铁律 / DESIGN §9）。
    """
    explicit = _HK_EXPLICIT_DATE_RE.search(title)
    if explicit:
        year = chinese_year(explicit.group("y"))
        month = chinese_small_number(explicit.group("m"))
        day = chinese_small_number(explicit.group("d"))
        if year and month and day:
            try:
                return (date(year, month, day).isoformat(),
                        PeriodSource.EXPLICIT_TITLE, None)
            except ValueError:
                pass  # 非日历日期 → 视为不可解析
    if _HK_CROSS_YEAR_RE.search(title):
        # 跨年标签（如 2024/25 年報）：非日历年结公司，日历期末不可得
        return (None, PeriodSource.UNKNOWN,
                "跨年标签（非日历年结公司），日历期末不可得，不猜测")
    for pattern in (_HK_ANNUAL_YEAR_RE, _HK_INTERIM_YEAR_RE,
                    _HK_INTERIM_YEAR_BEFORE_RE, _HK_CN_ANNUAL_YEAR_RE,
                    _HK_CN_INTERIM_YEAR_RE):
        if pattern.search(title):
            return (None, PeriodSource.UNKNOWN,
                    "仅有年份标签、无期末日证据，不猜测")
    return (None, PeriodSource.UNKNOWN, "标题无可确认的期末日")


def _release_datetime(value: str) -> datetime | None:
    match = _RELEASE_TIME_RE.search(value or "")
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group(1)} {match.group(2)}", "%d/%m/%Y %H:%M"
        ).replace(tzinfo=_HK_TZ)
    except ValueError:
        return None


def _split_year_windows(from_date: date, to_date: date) -> list[tuple[date, date]]:
    """超站点单页上限时按年切窗（newest→oldest），不做 load-more 模拟。"""
    windows: list[tuple[date, date]] = []
    window_to = to_date
    while window_to >= from_date:
        year_start = window_to.replace(month=1, day=1)
        window_from = max(from_date, year_start)
        windows.append((window_from, window_to))
        window_to = window_from - timedelta(days=1)
    return windows


class HKHkexnewsAdapter(BaseMarketAdapter):
    market = Market.HK
    base_forms: ClassVar[set[str]] = {"ANNUAL", "INTERIM", "QTR-HK"}
    default_language_preference: ClassVar[list[str]] = ["zh", "en"]
    source_group: ClassVar[str] = "hkex"

    # ------------------------------------------------------------ resolve

    def resolve(self, normalized: NormalizedSymbol) -> ResolvedSymbol:
        params = {"callback": "callback", "lang": "ZH", "type": "A",
                  "name": normalized.symbol, "market": "SEHK"}
        url = f"{_PREFIX_URL}?{urllib.parse.urlencode(params)}"
        response = self.transport.request("GET", url, group=_GROUP)
        body = (response.content or b"").decode("utf-8", errors="strict")
        try:
            payload = json.loads(strip_jsonp(body))
        except (json.JSONDecodeError, SourceContractChangedError) as e:
            raise SourceContractChangedError(
                f"prefix.do 响应无法解析: {e}", detail=f"body_head={body[:120]!r}"
            ) from e
        if not isinstance(payload, dict) or "stockInfo" not in payload:
            raise SourceContractChangedError(
                "prefix.do 结构异常（缺少 stockInfo）")
        matches = [row for row in payload["stockInfo"] or []
                   if str(row.get("code", "")) == normalized.symbol]
        if not matches:
            raise ResolveError("prefix.do 无精确命中（代码不存在）",
                               detail=f"code={normalized.symbol}")
        if len(matches) > 1:
            raise AmbiguousSymbolError(
                "prefix.do 多条精确命中", detail=f"code={normalized.symbol}")
        row = matches[0]
        stock_id = row.get("stockId")
        if stock_id in (None, ""):
            raise SourceContractChangedError(
                "prefix.do 命中行缺少 stockId", detail=f"code={normalized.symbol}")
        return ResolvedSymbol(
            market=Market.HK,
            symbol=normalized.symbol,
            raw_inputs=[normalized.raw],
            display_name=row.get("name"),
            exchange="SEHK",
            source_issuer_id=str(stock_id),
            issuer_id=f"hkex:{stock_id}",
        )

    # ------------------------------------------------------------ list

    def list_reports(self, symbol: ResolvedSymbol,
                     query: ReportQuery) -> DiscoveryResult:
        result = DiscoveryResult(reports=[], requested_count=query.last_n)
        forms = set(query.forms or self.default_forms())
        valid_forms = {f for f in forms if f in self.base_forms} or set(self.default_forms())
        candidates: list[Report] = []
        used_requests = 0
        truncated = False

        today = datetime.now(_HK_TZ).date()
        try:
            from_date = today.replace(year=today.year - query.max_lookback_years)
        except ValueError:  # 2月29日
            from_date = today.replace(year=today.year - query.max_lookback_years,
                                      day=28)

        # 检索 1：t1=40000（財務報表/ESG）→ ANNUAL / INTERIM
        if valid_forms & {"ANNUAL", "INTERIM"}:
            used_requests, truncated = self._search_windows(
                symbol, from_date, today, t1_code="40000", title="",
                keep_doc_types={"ANNUAL", "INTERIM"},
                valid_forms=valid_forms, candidates=candidates,
                warnings=result.warnings, budget=query.max_discovery_requests,
                used_requests=used_requests)

        # 检索 2：t1=10000 + title=業績 → QTR-HK（v1.0.3 起默认启用；
        # [中期業績]/[末期業績] 跳过，不与 INTERIM/ANNUAL 正文重复）
        if "QTR-HK" in valid_forms:
            used_requests, truncated = self._search_windows(
                symbol, from_date, today, t1_code="10000", title="業績",
                keep_doc_types={"QTR-HK"},
                valid_forms=valid_forms, candidates=candidates,
                warnings=result.warnings, budget=query.max_discovery_requests,
                used_requests=used_requests)

        dates = [r.filing_date for r in candidates if r.filing_date]
        if dates:
            result.searched_from = min(dates)
            result.searched_to = max(dates)
        result.reports = candidates
        result.exhausted = not truncated
        result.truncated = truncated
        return result

    def _search_windows(self, symbol: ResolvedSymbol, from_date: date,
                        to_date: date, *, t1_code: str, title: str,
                        keep_doc_types: set[str],
                        valid_forms: set[str], candidates: list[Report],
                        warnings: list[str], budget: int,
                        used_requests: int) -> tuple[int, bool]:
        """深链检索：整窗查询；超站点单页上限时按年切窗。返回 (请求数, truncated)。"""
        windows: list[tuple[date, date]] = [(from_date, to_date)]
        truncated = False
        while windows:
            win_from, win_to = windows.pop(0)
            if used_requests >= budget:
                truncated = True
                warnings.append(
                    f"发现请求预算耗尽（{budget} 次），结果可能不完整")
                break
            params = {
                "lang": "ZH", "category": "0", "market": "SEHK",
                "searchType": "1", "documentType": "-1",
                "t1code": t1_code, "t2Gcode": "-2", "t2code": "-2",
                "stockId": symbol.source_issuer_id or "",
                "title": title,
                "from": win_from.strftime("%Y%m%d"),
                "to": win_to.strftime("%Y%m%d"),
            }
            url = f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}"
            response = self.transport.request("GET", url, group=_GROUP)
            used_requests += 1
            try:
                html_text = (response.content or b"").decode(
                    "utf-8", errors="strict")
            except UnicodeDecodeError as e:
                raise SourceContractChangedError(
                    f"检索页非 UTF-8: {e}") from e
            record_count, rows = parse_search_page(html_text)
            if record_count > _SITE_ROW_LIMIT and (win_to - win_from).days > 365:
                # 超单页上限：丢弃整窗结果，按年切窗重查（newest→oldest）
                windows = _split_year_windows(win_from, win_to) + windows
                continue
            for row in rows:
                self._append_candidate(row, symbol, keep_doc_types,
                                       valid_forms, candidates, warnings,
                                       source_search=t1_code)
        return used_requests, truncated

    # ------------------------------------------------------------ 候选构建

    def _append_candidate(self, row: dict, symbol: ResolvedSymbol,
                          keep_doc_types: set[str],
                          valid_forms: set[str], candidates: list[Report],
                          warnings: list[str], *, source_search: str) -> None:
        headline = row.get("headline") or ""
        sub_match = re.search(r"\[([^\]]+)\]", headline)
        subcategory = (sub_match.group(1).strip() if sub_match else "")
        if subcategory in _KNOWN_EXCLUDED:
            return  # ESG 报告等已知非财报类，与财报同在 t1=40000（DESIGN §7）
        mapped = _SUBCATEGORY_MAP.get(subcategory)
        if mapped is None:
            # 回归实测（2026-09-21，PHASE1_REVIEW 修复期）：旧年份存在合并
            # 子类别"年報 / 環境、社會及管治資料/報告"——按包含关系判定，
            # 纯 ESG 子类别已在上方精确排除，不会落入这两条包含规则。
            if "年報" in subcategory:
                mapped = ("ANNUAL", DocumentRole.FULL_REPORT)
            elif "中期報告" in subcategory or "半年度報告" in subcategory:
                mapped = ("INTERIM", DocumentRole.FULL_REPORT)
        if mapped is None:
            # t1=10000 检索中的中期/末期業績公告属已知形态（内容与
            # INTERIM/ANNUAL 报告重复），静默跳过；其余未知子类别告警留痕
            if source_search == "10000":
                return
            warnings.append(f"未知子类别，跳过: [{subcategory}] "
                            f"{row.get('title')!r}")
            return
        doc_type, role = mapped
        if doc_type not in valid_forms:
            return
        if doc_type not in keep_doc_types:
            return
        file_link = row.get("file_link") or ""
        if not file_link:
            warnings.append(f"行缺少文件链接，跳过: {row.get('title')!r}")
            return
        source_id = file_link.lstrip("/")
        source_url = urllib.parse.urljoin(_FILE_BASE, source_id)
        title = (row.get("title") or "").strip()
        released = _release_datetime(row.get("release_datetime") or "")
        filing_date = released.date().isoformat() if released else None
        if filing_date is None:
            warnings.append(f"發放時間无法解析: {title!r} "
                            f"raw={row.get('release_datetime')!r}")
        period, period_source, period_warning = parse_hk_title_period(title)
        language = "en" if re.search(r"_e\.pdf$", file_link) else "zh"
        metadata: dict = {"subcategory": subcategory}
        if period_warning:
            # 候选级期末告警随报告携带，不进入 discovery.warnings：
            # 仅选中的报告才可能对外告警（core 在原文富化后重建），
            # 未选中的候选最多在 coverage.notices 聚合一次（避免 last_n=1
            # 被其他未选中行的未知期警告污染）。
            metadata["period_warning"] = period_warning
        if row.get("release_datetime"):
            metadata["release_datetime"] = row["release_datetime"]  # 原值保留
        if row.get("file_size"):
            metadata["file_size"] = row["file_size"]
        candidates.append(Report(
            market=Market.HK,
            symbol=symbol.symbol,
            source_id=source_id,
            source_url=source_url,
            title=title or source_id.rsplit("/", 1)[-1],
            doc_type=doc_type,
            source_form=subcategory,
            filing_date=filing_date,
            report_period=period,
            period_source=period_source,
            language=language,
            document_role=role,
            source_issuer_id=symbol.source_issuer_id,
            source_metadata=metadata,
        ))
