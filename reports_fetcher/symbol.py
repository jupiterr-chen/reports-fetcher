"""证券输入规范化与市场识别（DESIGN §2，FR-1）。

本地规则只做识别不做有效性判定：有效性由源站 resolve 精确匹配确认。
北交所显式返回 unsupported_market_segment，不落到沪深查询。
"""
from __future__ import annotations

import re

from reports_fetcher.models import (
    Market,
    NormalizedSymbol,
    SymbolError,
    UnsupportedMarketSegmentError,
)

_MAX_RAW_LEN = 16

# 仅允许字母/数字与有限分隔符（. : -），杜绝路径字符与控制字符
_ALLOWED_CHARS = re.compile(r"^[A-Za-z0-9.:\-]+$")

_CN_SUFFIX_EXCHANGE = {"SH": "SSE", "SS": "SSE", "SZ": "SZSE"}
_CN_PREFIX_EXCHANGE = {"sh": "SSE", "sz": "SZSE"}

_CN_SUFFIX_RE = re.compile(r"^(\d{6})\.(SH|SS|SZ|BJ)$", re.IGNORECASE)
_CN_PREFIX_RE = re.compile(r"^(sh|sz|bj)(\d{6})$", re.IGNORECASE)
_HK_SUFFIX_RE = re.compile(r"^(\d{1,5})[.:](HK)$", re.IGNORECASE)
_US_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]*$")

# 已知市场后缀集合：命中即视为"意图使用后缀形式"，主体不符则为冲突
_KNOWN_SUFFIXES = {"SH", "SS", "SZ", "BJ", "HK"}


def normalize_symbol(raw: str) -> NormalizedSymbol:
    raw = (raw or "").strip()
    if not raw:
        raise SymbolError("证券代码为空", detail=f"raw={raw!r}")
    if len(raw) > _MAX_RAW_LEN:
        raise SymbolError("证券代码过长", detail=f"raw={raw!r}")
    if not _ALLOWED_CHARS.match(raw):
        raise SymbolError("证券代码含非法字符", detail=f"raw={raw!r}")

    # 显式后缀：CN 交易所 / 北交所
    m = _CN_SUFFIX_RE.match(raw)
    if m:
        body, suffix = m.group(1), m.group(2).upper()
        if suffix == "BJ":
            raise UnsupportedMarketSegmentError(
                "北交所暂不支持", detail=f"raw={raw!r}")
        return NormalizedSymbol(Market.CN, body, raw,
                                exchange_hint=_CN_SUFFIX_EXCHANGE[suffix])

    # 显式前缀：sh/sz/bj + 6 位数字
    m = _CN_PREFIX_RE.match(raw)
    if m:
        prefix, body = m.group(1).lower(), m.group(2)
        if prefix == "bj":
            raise UnsupportedMarketSegmentError(
                "北交所暂不支持", detail=f"raw={raw!r}")
        return NormalizedSymbol(Market.CN, body, raw,
                                exchange_hint=_CN_PREFIX_EXCHANGE[prefix])

    # HK 后缀（0700.HK / 0700:HK）
    m = _HK_SUFFIX_RE.match(raw)
    if m:
        return NormalizedSymbol(Market.HK, m.group(1).zfill(5), raw)

    # 显式市场后缀但主体不匹配（AAPL.SH、600519.HK、.HK 等）：
    # 合法后缀形态已被上方正则处理，落到这里即显式市场冲突
    if ("." in raw or ":" in raw):
        _body, _sep, suffix = raw.rpartition(".") if "." in raw else raw.rpartition(":")
        if suffix.upper() in _KNOWN_SUFFIXES:
            raise SymbolError("显式市场后缀与代码主体不匹配",
                              detail=f"raw={raw!r}")

    # 纯数字：6 位 → CN 候选；1-5 位 → HK（补零 5 位）；更长 → 非法
    if raw.isdigit():
        if len(raw) == 6:
            return NormalizedSymbol(Market.CN, raw, raw)
        if len(raw) <= 5:
            return NormalizedSymbol(Market.HK, raw.zfill(5), raw)
        raise SymbolError("纯数字代码长度无法识别（CN 为 6 位，HK 至多 5 位）",
                          detail=f"raw={raw!r}")

    # 字母类 → US ticker（首字符为字母；保留连字符/点号类股别名形式）
    upper = raw.upper()
    if _US_TICKER_RE.match(upper):
        return NormalizedSymbol(Market.US, upper, raw)

    raise SymbolError("证券代码无法识别", detail=f"raw={raw!r}")


def batch_normalize(
    raws: list[str],
) -> tuple[list[NormalizedSymbol], dict[str, list[str]], list[tuple[str, SymbolError]]]:
    """批量规范化：按 (market, symbol) 去重，保留输入顺序与所有原始别名。

    返回 (去重后的规范化列表, {规范化代码: 原始别名列表}, [(非法原始输入, 异常)])；
    单条非法输入不阻断批次，由调用方决定如何报告（FR-1/FR-6）。
    """
    ordered: list[NormalizedSymbol] = []
    seen: dict[tuple[str, str], NormalizedSymbol] = {}
    invalid: list[tuple[str, SymbolError]] = []
    for raw in raws:
        try:
            norm = normalize_symbol(raw)
        except SymbolError as e:
            invalid.append((raw, e))
            continue
        key = (norm.market.value, norm.symbol)
        if key not in seen:
            seen[key] = norm
            ordered.append(norm)
    aliases: dict[str, list[str]] = {}
    for raw in raws:
        try:
            norm = normalize_symbol(raw)
        except SymbolError:
            continue
        aliases.setdefault(norm.symbol, [])
        if raw not in aliases[norm.symbol]:
            aliases[norm.symbol].append(raw)
    return ordered, aliases, invalid
