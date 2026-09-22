"""从已归档原文（HK 年报/中期报告 PDF）提取明确报告期末（period_source=document）。

数据质量铁律（AGENTS "未知即 null"）：只接受原文写出的**完整、明确**日期：
- 中文："截至 <年> 年 <月> 月 <日> 日 止"（繁简/中文数字均可）；
- 英文："for the year/six months/... ended <date>"、"year ended <date>"、
  "as at <date>"、"as of <date>"。

校验为真实日历日期。按页扫描一个有界的早期页窗口：返回**第一个**只含唯一
明确期末日的页；某页没有明确日期则继续；某页出现多个互相矛盾的明确日期则视为
不可用并继续。整份文件都不可用时返回 None（非致命，报告期保持 null + unknown）。
绝不按标题年份、公告日或财季惯例猜测（HK 非日历年结公司是真实场景）。

提取器使用 pdfminer.six（MIT 许可），已在官方 0700.HK 中期报告 PDF 上验证；
不使用 AGPL 依赖（如 PyMuPDF）。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

logger = logging.getLogger("reports_fetcher.document_period")

try:  # pdfminer.six 为运行/测试依赖；缺失时降级为"不富化"，绝不阻断抓取
    from pdfminer.high_level import extract_pages as _extract_pages
    from pdfminer.layout import LTTextContainer as _LTTextContainer
except ImportError:  # pragma: no cover - 仅在精简安装组出现
    _extract_pages = None
    _LTTextContainer = None

# 早期页窗口：官方 0700.HK 中期报告在第 6 物理页出现"截至…止"明确期末日。
# 有界且足够覆盖封面/目录后的主席报告，避免落入正文同比表格。
_MAX_PAGES = 8

_CN_YEAR_CHARS = "零〇○一二贰貳兩两三叁參四肆五伍六陆陸七柒八捌九玖"
_CN_SMALL_CHARS = "一二三四五六七八九十"

_CN_DIGITS = {
    "零": 0, "〇": 0, "○": 0,
    "一": 1, "壹": 1,
    "二": 2, "貳": 2, "贰": 2, "兩": 2, "两": 2,
    "三": 3, "叁": 3, "參": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陆": 6, "陸": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}

# 截至 二〇二五年十二月三十一日 止（要求出现"止"，明确为期末日）
_CN_END_RE = re.compile(
    r"截至\s*(?P<y>[" + _CN_YEAR_CHARS + r"\d]{4})\s*年"
    r"\s*(?P<m>[" + _CN_SMALL_CHARS + r"\d]{1,3})\s*月"
    r"\s*(?P<d>[" + _CN_SMALL_CHARS + r"\d]{1,3})\s*日\s*止")

_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_EN_END_RE = re.compile(
    r"(?:"
    r"for the\s+(?:financial\s+)?"
    r"(?:year|six\s+months|three\s+months|nine\s+months|period|quarter)\s+ended"
    r"|(?:financial\s+)?year\s+ended"
    r"|period\s+ended"
    r"|as\s+at"
    r"|as\s+of"
    r")\s*:?\s*"
    r"(?:(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m1>[A-Za-z]+),?\s+(?P<y1>\d{4})"
    r"|(?P<m2>[A-Za-z]+)\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y2>\d{4}))",
    re.IGNORECASE)


def _cn_year(text: str) -> int | None:
    text = (text or "").strip()
    if text.isdigit() and len(text) == 4:
        return int(text)
    digits: list[int] = []
    for ch in text:
        if ch.isdigit():
            digits.append(int(ch))
        elif ch in _CN_DIGITS:
            digits.append(_CN_DIGITS[ch])
        else:
            return None
    if len(digits) != 4 or digits[0] == 0:
        return None
    return digits[0] * 1000 + digits[1] * 100 + digits[2] * 10 + digits[3]


def _cn_small(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        value = int(text)
        return value if 1 <= value <= 99 else None
    if text == "十":
        return 10
    if len(text) == 1:
        value = _CN_DIGITS.get(text)
        return value if value is not None and value >= 1 else None
    if "十" in text:
        parts = text.split("十")
        if len(parts) != 2:
            return None
        tens = _CN_DIGITS.get(parts[0], 1) if parts[0] else 1
        ones = _CN_DIGITS.get(parts[1], 0) if parts[1] else 0
        if tens is None or ones is None:
            return None
        value = tens * 10 + ones
        return value if 1 <= value <= 99 else None
    return None


def _iso_or_none(year: int | None, month: int | None,
                 day: int | None) -> str | None:
    if not year or not month or not day:
        return None
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_period_from_text(text: str) -> str | None:
    """从文本提取**唯一**明确期末日；无匹配、非法或互相矛盾时返回 None。

    只返回单一明确日期，绝不从多个矛盾日期中挑选；这样调用方可以按页扫描，
    跳过无日期或自相矛盾的页面。
    """
    if not text:
        return None
    found: set[str] = set()

    for match in _CN_END_RE.finditer(text):
        found.add(_iso_or_none(_cn_year(match.group("y")),
                               _cn_small(match.group("m")),
                               _cn_small(match.group("d"))) or "")
    for match in _EN_END_RE.finditer(text):
        if match.group("m1") is not None:
            month = _EN_MONTHS.get(match.group("m1").lower())
            found.add(_iso_or_none(int(match.group("y1")), month,
                                   int(match.group("d1"))) or "")
        else:
            month = _EN_MONTHS.get(match.group("m2").lower())
            found.add(_iso_or_none(int(match.group("y2")), month,
                                   int(match.group("d2"))) or "")

    found.discard("")
    if len(found) == 1:
        return next(iter(found))
    return None  # 无匹配、非法或出现多个矛盾日期（歧义）→ 视为不可用


def extract_page_texts(path: Path, *, max_pages: int = _MAX_PAGES
                       ) -> list[str]:
    """按物理页返回前 max_pages 页文本；任何失败返回已取得部分（非致命）。"""
    if _extract_pages is None:  # pragma: no cover
        logger.warning("pdfminer.six 不可用，跳过原文报告期提取")
        return []
    texts: list[str] = []
    try:
        for index, layout in enumerate(_extract_pages(str(path))):
            if index >= max_pages:
                break
            try:
                chunks = [element.get_text() for element in layout
                          if _LTTextContainer is not None
                          and isinstance(element, _LTTextContainer)]
                texts.append("".join(chunks))
            except Exception as e:  # noqa: BLE001 - 单页失败不阻断其余页
                logger.debug("PDF 单页文本提取失败 %s#%d: %s",
                             Path(path).name, index + 1, e)
                texts.append("")
    except Exception as e:  # noqa: BLE001 - 损坏/受保护/加密 PDF 不致命
        logger.warning("PDF 文本提取失败（跳过富化）%s: %s", Path(path).name, e)
    return texts


def extract_report_period(pdf_path: Path) -> str | None:
    """逐页扫描有界早期窗口，返回第一个唯一明确期末日；否则 None（非致命）。

    某页无明确日期（或自相矛盾）则继续下一页；不在多个矛盾日期间做选择，
    也不按标题年份/公告日/财历惯例推断。
    """
    try:
        for page_text in extract_page_texts(Path(pdf_path)):
            period = parse_period_from_text(page_text)
            if period:
                return period
    except Exception as e:  # noqa: BLE001 - 提取绝不阻断抓取
        logger.warning("原文报告期提取异常（跳过富化）: %s", e)
    return None
