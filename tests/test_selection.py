"""selection：统一选择函数（DESIGN §5，FR-3）。"""
from __future__ import annotations

import pytest

from reports_fetcher.models import DocumentRole, Market, PeriodSource, ReportQuery
from reports_fetcher.selection import select_reports
from tests.conftest import make_report


def _query(**kw) -> ReportQuery:
    defaults = dict(last_n=4)
    defaults.update(kw)
    return ReportQuery(**defaults)


class TestGrouping:
    def test_same_period_different_types_not_merged(self):
        candidates = [
            make_report(doc_type="10-Q", report_period="2025-09-30",
                        source_id="a/q.htm"),
            make_report(doc_type="10-K", report_period="2025-09-30",
                        source_id="b/k.htm"),
        ]
        result = select_reports(candidates, _query())
        assert result.selected_count == 2
        assert {r.doc_type for r in result.selected} == {"10-Q", "10-K"}

    def test_same_type_different_periods_distinct_groups(self):
        candidates = [
            make_report(report_period="2026-06-27", source_id="a.htm"),
            make_report(report_period="2026-03-28", source_id="b.htm"),
        ]
        result = select_reports(candidates, _query())
        assert result.selected_count == 2

    def test_unknown_periods_are_independent_groups(self):
        candidates = [
            make_report(report_period=None, period_source=PeriodSource.UNKNOWN,
                        source_id="x.htm"),
            make_report(report_period=None, period_source=PeriodSource.UNKNOWN,
                        source_id="y.htm"),
        ]
        result = select_reports(candidates, _query())
        assert result.selected_count == 2


class TestVersionSelection:
    def test_amendment_supersedes_original_in_group(self):
        base = make_report(doc_type="10-K", report_period="2025-09-27",
                           source_id="0000320193-25-000079/aapl-20250927.htm",
                           filing_date="2025-10-31")
        amend = make_report(doc_type="10-K", report_period="2025-09-27",
                            source_id="0000320193-25-000099/aapl-20250927x10ka.htm",
                            filing_date="2025-12-15",
                            source_form="10-K/A", is_amendment=True,
                            document_role=DocumentRole.AMENDMENT_FULL)
        result = select_reports([base, amend], _query())
        assert result.selected_count == 1
        assert result.selected[0].source_id == amend.source_id

    def test_notice_only_group_dropped_with_warning(self):
        notice = make_report(report_period="2025-09-27",
                             source_id="n/notice.htm",
                             document_role=DocumentRole.NOTICE)
        result = select_reports([notice], _query())
        assert result.selected_count == 0
        assert any("未获得全文" in w for w in result.warnings)

    def test_notice_alongside_full_keeps_full_with_warning(self):
        full = make_report(report_period="2025-09-27", source_id="f/full.htm",
                           filing_date="2025-10-31")
        notice = make_report(report_period="2025-09-27",
                             source_id="n/notice.htm", filing_date="2025-12-01",
                             document_role=DocumentRole.NOTICE)
        result = select_reports([full, notice], _query())
        assert result.selected_count == 1
        assert result.selected[0].source_id == "f/full.htm"
        # 仅有修订通知时仍可选择原全文，但必须警告未合并修订（DESIGN §5）
        assert any("未合并" in w for w in result.warnings)

    def test_unverified_amendment_keeps_original(self):
        """PHASE1_REVIEW T4：无完整性证据的修订（role=unknown）不替代原全文。"""
        full = make_report(doc_type="10-K", report_period="2025-09-27",
                           source_id="acc/base.htm", filing_date="2025-10-31")
        amend = make_report(doc_type="10-K", report_period="2025-09-27",
                            source_id="acc/amend.htm", filing_date="2025-12-15",
                            source_form="10-K/A", is_amendment=True,
                            document_role=DocumentRole.UNKNOWN)
        result = select_reports([full, amend], _query())
        assert result.selected_count == 1
        assert result.selected[0].source_id == "acc/base.htm"
        assert any("未合并" in w for w in result.warnings)

    def test_summary_never_selected_over_full(self):
        summary = make_report(report_period="2026-06-27",
                              source_id="s/summary.htm", filing_date="2026-08-01",
                              document_role=DocumentRole.SUMMARY)
        full = make_report(report_period="2026-06-27", source_id="f/full.htm",
                           filing_date="2026-07-31")
        result = select_reports([summary, full], _query())
        assert result.selected[0].source_id == "f/full.htm"


class TestOrdering:
    def test_known_periods_descending(self):
        candidates = [
            make_report(report_period="2024-06-29", source_id="c.htm"),
            make_report(report_period="2026-06-27", source_id="a.htm"),
            make_report(report_period="2025-06-28", source_id="b.htm"),
        ]
        result = select_reports(candidates, _query())
        assert [r.source_id for r in result.selected] == ["a.htm", "b.htm", "c.htm"]

    def test_unknown_period_sorted_last_with_warning(self):
        known = make_report(report_period="2024-06-29", source_id="old.htm")
        unknown = make_report(report_period=None,
                              period_source=PeriodSource.UNKNOWN,
                              source_id="unk.htm", filing_date="2026-08-01")
        result = select_reports([unknown, known], _query())
        assert [r.source_id for r in result.selected] == ["old.htm", "unk.htm"]
        assert any("报告期未知" in w for w in result.warnings)

    def test_truncation_to_last_n(self):
        candidates = [
            make_report(report_period=f"202{i}-06-27", source_id=f"{i}.htm")
            for i in range(6)
        ]
        result = select_reports(candidates, _query(last_n=2))
        assert result.selected_count == 2
        assert [r.source_id for r in result.selected] == ["5.htm", "4.htm"]
        assert result.total_groups == 6
        # 正常截取是说明性信息（PHASE1_REVIEW T6），不进质量警告
        assert any("截断" in n for n in result.notices)
        assert not any("截断" in w for w in result.warnings)


class TestFormsFilter:
    def test_forms_actually_filter(self):
        candidates = [
            make_report(doc_type="10-Q", report_period="2026-06-27",
                        source_id="q.htm"),
            make_report(doc_type="10-K", report_period="2025-09-27",
                        source_id="k.htm"),
        ]
        result = select_reports(candidates, _query(forms=["10-K"]))
        assert result.selected_count == 1
        assert result.selected[0].doc_type == "10-K"


class TestLanguage:
    def test_language_preference(self):
        zh = make_report(report_period="2026-06-27", source_id="zh.htm",
                         language="zh")
        en = make_report(report_period="2026-06-27", source_id="en.htm",
                         language="en", filing_date="2026-08-01")
        result = select_reports([en, zh], _query(),
                                language_preference=["zh", "en"])
        assert result.selected[0].language == "zh"
        # 偏好语言可用时的正常选择：说明性信息（PHASE1_REVIEW T6）
        assert any("多语言" in n for n in result.notices)
        assert not any("多语言" in w for w in result.warnings)

    def test_language_fallback_keeps_only_full(self):
        en = make_report(report_period="2026-06-27", source_id="en.htm",
                         language="en")
        result = select_reports([en], _query(), language_preference=["zh"])
        assert result.selected[0].language == "en"
        assert any("回退" in w for w in result.warnings)


class TestEmpty:
    def test_no_candidates(self):
        result = select_reports([], _query())
        assert result.selected == []
        assert result.selected_count == 0
