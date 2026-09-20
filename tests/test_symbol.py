"""symbol：市场识别与规范化（DESIGN §2 / §16）。"""
from __future__ import annotations

import pytest

from reports_fetcher.models import Market, SymbolError, UnsupportedMarketSegmentError
from reports_fetcher.symbol import batch_normalize, normalize_symbol


class TestCN:
    @pytest.mark.parametrize("raw", ["600519", "sh600519", "SH600519",
                                     "600519.SH", "600519.SS", "600519.sh"])
    def test_shanghai_aliases(self, raw):
        norm = normalize_symbol(raw)
        assert norm.market is Market.CN
        assert norm.symbol == "600519"
        assert norm.raw == raw

    @pytest.mark.parametrize("raw", ["000001.SZ", "sz000001"])
    def test_shenzhen_aliases(self, raw):
        norm = normalize_symbol(raw)
        assert norm.market is Market.CN
        assert norm.symbol == "000001"

    def test_exchange_hint_preserved(self):
        assert normalize_symbol("600519.SH").exchange_hint == "SSE"
        assert normalize_symbol("600519.SS").exchange_hint == "SSE"
        assert normalize_symbol("000001.SZ").exchange_hint == "SZSE"
        assert normalize_symbol("sz000001").exchange_hint == "SZSE"
        assert normalize_symbol("600519").exchange_hint is None

    def test_plain_6_digits_is_cn(self):
        assert normalize_symbol("300750").market is Market.CN


class TestHK:
    @pytest.mark.parametrize("raw,expected", [
        ("0700.HK", "00700"), ("0700:HK", "00700"), ("0700.hk", "00700"),
        ("700", "00700"), ("00700", "00700"), ("16", "00016"), ("0016", "00016"),
    ])
    def test_aliases_padded_to_5(self, raw, expected):
        norm = normalize_symbol(raw)
        assert norm.market is Market.HK
        assert norm.symbol == expected


class TestUS:
    @pytest.mark.parametrize("raw,expected", [
        ("AAPL", "AAPL"), ("aapl", "AAPL"), ("Aapl", "AAPL"),
        ("BRK-B", "BRK-B"), ("BF-B", "BF-B"), ("GOOGL", "GOOGL"),
    ])
    def test_uppercase_and_class_shares(self, raw, expected):
        norm = normalize_symbol(raw)
        assert norm.market is Market.US
        assert norm.symbol == expected

    def test_dots_preserved_without_conversion(self):
        # 点号别名仅在精确映射明确时转换（resolve 阶段），规范化不改写
        assert normalize_symbol("BRK.B").symbol == "BRK.B"


class TestInvalid:
    @pytest.mark.parametrize("raw", ["", "   ", "600519.XX", "AAPL.SH",
                                     "600519.HK", "1234567", "600519/1",
                                     "60 0519", ".HK", "AAPL\\x", "600519..SS"])
    def test_invalid_symbols(self, raw):
        with pytest.raises(SymbolError):
            normalize_symbol(raw)

    @pytest.mark.parametrize("raw", ["bj430047", "BJ430047", "430047.BJ"])
    def test_bse_unsupported(self, raw):
        with pytest.raises(UnsupportedMarketSegmentError):
            normalize_symbol(raw)

    def test_overlong_input_rejected(self):
        with pytest.raises(SymbolError):
            normalize_symbol("A" * 20)

    def test_digits_with_hyphen_not_us(self):
        with pytest.raises(SymbolError):
            normalize_symbol("123-4")


class TestBatch:
    def test_dedup_keeps_order_and_aliases(self):
        ordered, aliases, invalid = batch_normalize(
            ["AAPL", "aapl", "MSFT", "0700.HK", "700"])
        assert [(n.market.value, n.symbol) for n in ordered] == [
            ("US", "AAPL"), ("US", "MSFT"), ("HK", "00700")]
        assert aliases["AAPL"] == ["AAPL", "aapl"]
        assert aliases["00700"] == ["0700.HK", "700"]
        assert invalid == []

    def test_invalid_isolated(self):
        ordered, _aliases, invalid = batch_normalize(["AAPL", "%%%"])
        assert len(ordered) == 1
        assert len(invalid) == 1
        assert invalid[0][0] == "%%%"
