"""period：日期校验与中文数字（DESIGN §9）。"""
from __future__ import annotations

import datetime as dt

import pytest

from reports_fetcher.period import (
    chinese_year,
    parse_iso_date,
    parse_iso_date_safely,
    period_or_unknown,
)


class TestParseIsoDate:
    def test_valid(self):
        assert parse_iso_date("2025-09-27") == dt.date(2025, 9, 27)
        assert parse_iso_date("2024-02-29") == dt.date(2024, 2, 29)

    @pytest.mark.parametrize("value", [
        None, "", "2025-9-27", "20250927", "2025/09/27", "2025-13-01",
        "2025-02-30", "2025-00-10", "2025-09-27 ", "x2025-09-27",
    ])
    def test_invalid_returns_none(self, value):
        assert parse_iso_date(value) is None

    def test_apple_fiscal_year_september(self):
        # reportDate 权威性：Apple 财年 9 月止（fixture 留证）
        assert parse_iso_date("2025-09-27") == dt.date(2025, 9, 27)


class TestParseIsoDateSafely:
    def test_missing_is_unknown_not_dirty(self):
        assert parse_iso_date_safely("", what="reportDate") == (None, True)
        assert parse_iso_date_safely(None, what="reportDate") == (None, True)

    def test_invalid_flagged(self):
        assert parse_iso_date_safely("2025-02-30", what="reportDate") == (None, False)
        assert parse_iso_date_safely("2025-9-27", what="filingDate") == (None, False)

    def test_valid(self):
        assert parse_iso_date_safely("2026-06-27", what="reportDate") == \
               ("2026-06-27", True)


class TestChineseYear:
    @pytest.mark.parametrize("text,expected", [
        ("二零二六", 2026), ("二〇二六", 2026), ("二○二六", 2026),
        ("一九九九", 1999), ("贰零贰伍", 2025), ("兩零二四", 2024),
        ("2026", 2026),
    ])
    def test_digit_wise_years(self, text, expected):
        assert chinese_year(text) == expected

    @pytest.mark.parametrize("text", [
        "", "十", "一百", "二零二", "二零二六六", "零二零二六", "abc",
    ])
    def test_unsupported_returns_none(self, text):
        assert chinese_year(text) is None


class TestPeriodOrUnknown:
    def test_unknown_placeholder(self):
        assert period_or_unknown(None) == "unknown"
        assert period_or_unknown("") == "unknown"
        assert period_or_unknown("2025-09-27") == "2025-09-27"
