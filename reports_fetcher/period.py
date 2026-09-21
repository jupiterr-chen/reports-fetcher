"""日期与报告期工具（DESIGN §9）。

数据质量铁律：未知即 null。解析不出的日期返回 None，
禁止用公告日、12-31 或财季惯例猜造（HK 非日历年结公司是真实场景）。
"""
from __future__ import annotations

import datetime as dt
import re

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 中文数字（含繁简与"〇"变体）；现代标题年份为逐位读法（二零二六）
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

_CN_YEAR_RE = re.compile(r"^([零〇○一二贰貳兩两三叁參四肆五伍六陆陸七柒八捌九玖\d]{4})$")


def parse_iso_date(value: str | None) -> dt.date | None:
    """严格解析 YYYY-MM-DD；任何不合法/非日历日期返回 None，绝不猜测。"""
    if not value or not isinstance(value, str):
        return None
    if not _ISO_DATE_RE.match(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def parse_iso_date_safely(value: str | None, *, what: str) -> tuple[str | None, bool]:
    """解析来源日期字段：返回 (ISO 字符串或 None, 是否合法)。

    非法输入返回 (None, False)，调用方据此附质量警告；不抛异常。
    """
    if value in (None, ""):
        return None, True  # 来源字段缺失是"未知"而非"非法"，不告警脏数据
    parsed = parse_iso_date(value)
    if parsed is None:
        return None, False
    return parsed.isoformat(), True


def chinese_year(text: str) -> int | None:
    """解析中文数字年份（逐位读法，如 二零二六 → 2026）。

    仅支持现代标题的逐位形式；不支持"一千九百"等进位读法（返回 None）。
    纯阿拉伯数字年份直接转换。
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit() and len(text) == 4:
        return int(text)
    if not _CN_YEAR_RE.match(text):
        return None
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


def chinese_small_number(text: str) -> int | None:
    """解析 1-99 的中文数字（含"十"进位），如 三→3、十→10、
    十一→11、二十一→21、三十一→31；纯数字串直接转换。

    用于 HK 业绩公告标题中的期末月/日（截至二零二六年三月三十一日止…）。
    """
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
        if len(parts) != 2 or any(ch == "十" for ch in parts):
            return None
        tens = _CN_DIGITS.get(parts[0], 1) if parts[0] else 1
        ones = _CN_DIGITS.get(parts[1], 0) if parts[1] else 0
        if tens is None or ones is None:
            return None
        value = tens * 10 + ones
        return value if 1 <= value <= 99 else None
    return None


def period_or_unknown(period: str | None) -> str:
    """文件名等展示用的报告期表示；未知用字面 unknown。"""
    return period if period else "unknown"


def today_utc() -> dt.date:
    return dt.datetime.now(dt.UTC).date()
