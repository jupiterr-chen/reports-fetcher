"""document_period：HK 归档 PDF 原文报告期末提取（离线 fixture，不触网）。

捕获文本 provenance 见 tests/fixtures/hk/document_period.json：captured_text 为
官方 HKEXnews 0700.HK PDF（同一 URL 已记录于 tests/fixtures/hk/pdf_check.json）
第 6 物理页的逐字摘录。PDF 提取路径用运行时生成的含可提取文本的多页最小 PDF
验证（纯 ASCII 文本，与真实扫描一致地按物理页遍历）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reports_fetcher import document_period
from reports_fetcher.document_period import (
    extract_report_period,
    parse_period_from_text,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hk"


def _fixture() -> dict:
    return json.loads((FIXTURES / "document_period.json").read_text("utf-8"))


def _multi_page_pdf(texts: list[str]) -> bytes:
    """最小多页 PDF；每页一个可被 pdfminer.six 提取的 ASCII 文本对象。"""

    def esc(value: str) -> str:
        return value.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    count = len(texts)
    page_ids = [3 + 2 * index for index in range(count)]
    font_id = 3 + 2 * count
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (f"<< /Type /Pages /Kids [{' '.join(f'{pid} 0 R' for pid in page_ids)}] "
         f"/Count {count} >>").encode(),
    ]
    for index, text in enumerate(texts):
        content = f"BT /F1 12 Tf 72 720 Td ({esc(text)}) Tj ET".encode("latin-1")
        objects.append(
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
             f"/Contents {4 + 2 * index} 0 R /Resources "
             f"<< /Font << /F1 {font_id} 0 R >> >> >>").encode())
        objects.append(b"<< /Length " + str(len(content)).encode()
                       + b" >>\nstream\n" + content + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    return bytes(out)


class TestProvenance:
    def test_captured_excerpt_is_from_official_pdf(self):
        fixture = _fixture()
        pdf_check = json.loads(
            (FIXTURES / "pdf_check.json").read_text("utf-8"))
        assert fixture["provenance"]["source_url"] == pdf_check["url"]
        assert fixture["provenance"]["captured_at"] == "2026-09-22"
        assert fixture["provenance"]["excerpt_page"] == "physical page 6"
        assert fixture["captured_text"] == (
            "本人欣然向各股東提呈我們截至二零二六年六月三十日止三個月及"
            "六個月的中期報告。")
        assert "offline recreation" not in json.dumps(fixture, ensure_ascii=False)
        assert "复刻" not in json.dumps(fixture, ensure_ascii=False)


class TestParsePeriodFromText:
    def test_captured_0700_interim_excerpt(self):
        assert parse_period_from_text(
            _fixture()["captured_text"]) == "2026-06-30"

    @pytest.mark.parametrize("key,period", [
        ("chinese_simplified", "2025-12-31"),
        ("english_year", "2025-12-31"),
        ("english_months", "2026-06-30"),
        ("english_as_at", "2025-12-31"),
    ])
    def test_explicit_end_dates(self, key, period):
        assert parse_period_from_text(
            _fixture()["synthetic_cases"][key]) == period

    def test_plain_chinese_digits_variant(self):
        assert parse_period_from_text(
            "截至 2025 年 12 月 31 日 止年度") == "2025-12-31"

    @pytest.mark.parametrize("key", ["invalid_date", "ambiguous", "no_end_date",
                                     "cross_year"])
    def test_unusable_text_stays_unknown(self, key):
        assert parse_period_from_text(
            _fixture()["synthetic_cases"][key]) is None

    @pytest.mark.parametrize("text", ["", None,
                                      "没有日期的普通段落",
                                      "截至2025年13月40日止年度"])
    def test_no_or_invalid_date(self, text):
        assert parse_period_from_text(text) is None

    def test_ambiguous_english_dates(self):
        text = ("For the year ended 31 December 2025 ... "
                "for the six months ended 30 June 2025")
        assert parse_period_from_text(text) is None


class TestPagewiseExtraction:
    def test_reaches_page_six_current_period_not_later_comparison(self, tmp_path):
        """早期页无匹配 → 第 6 页唯一本期期末 → 第 7 页为同比，不得误取。"""
        path = tmp_path / "interim.pdf"
        path.write_bytes(_multi_page_pdf([
            "Corporate information",
            "Contents",
            "Chairman's statement",
            "Management discussion and analysis",
            "Condensed consolidated statement",
            "For the six months ended 30 June 2026",
            "For the six months ended 30 June 2025",
        ]))
        assert extract_report_period(path) == "2026-06-30"

    def test_conflicting_page_is_skipped_for_next_unique_page(self, tmp_path):
        """某页含互相矛盾日期则整页不可用，继续到下一页唯一日期。"""
        path = tmp_path / "report.pdf"
        path.write_bytes(_multi_page_pdf([
            "For the year ended 31 December 2024 and as at 31 December 2025",
            "For the year ended 31 December 2026",
        ]))
        assert extract_report_period(path) == "2026-12-31"

    def test_no_explicit_period_anywhere(self, tmp_path):
        path = tmp_path / "blank.pdf"
        path.write_bytes(_multi_page_pdf([
            "page one", "page two", "page three"]))
        assert extract_report_period(path) is None

    def test_scan_is_bounded(self, tmp_path):
        path = tmp_path / "long.pdf"
        texts = [f"page {i}" for i in range(12)]
        texts.append("For the year ended 31 December 2025")  # page 13, beyond 8
        path.write_bytes(_multi_page_pdf(texts))
        assert extract_report_period(path) is None

    def test_non_pdf_is_non_fatal(self, tmp_path):
        path = tmp_path / "broken.pdf"
        path.write_bytes(b"not really a pdf at all")
        assert extract_report_period(path) is None

    def test_extraction_failure_is_non_fatal(self, tmp_path, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("extractor exploded")

        monkeypatch.setattr(document_period, "extract_page_texts", boom)
        assert extract_report_period(tmp_path / "x.pdf") is None
