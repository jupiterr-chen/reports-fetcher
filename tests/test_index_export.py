"""平面索引导出（INDEX.csv/INDEX.md）：快速定位文档的机器/人读索引。"""
from __future__ import annotations

import csv
from pathlib import Path

from reports_fetcher.index_export import CSV_NAME, MD_NAME, export_index
from reports_fetcher.models import Market, PeriodSource, ResolvedSymbol
from reports_fetcher.store import Store
from tests.conftest import make_report


def _downloaded(store: Store, content: bytes):
    import hashlib
    temp = store.tmp_dir / f".tmp-{hashlib.sha256(content).hexdigest()[:8]}.part"
    temp.write_bytes(content)
    from reports_fetcher.models import DownloadedFile
    return DownloadedFile(temp_path=str(temp),
                          sha256=hashlib.sha256(content).hexdigest(),
                          bytes=len(content), media_type="application/pdf",
                          final_url="https://x/f")


def _seed(tmp_path, *, second_version=False):
    root = tmp_path / "archive"
    store = Store(root)
    store.upsert_symbol(ResolvedSymbol(
        market=Market.HK, symbol="01810", raw_inputs=["1810.HK"],
        display_name="小米集團－Ｗ", source_issuer_id="190371"))
    r1 = make_report(market=Market.HK, symbol="01810", doc_type="INTERIM",
                     title="2026年中期報告", source_id="hk/a.pdf",
                     report_period="2026-06-30", filing_date="2026-09-23",
                     period_source=PeriodSource.ANNOUNCEMENT_TITLE)
    r2 = make_report(market=Market.HK, symbol="01810", doc_type="ANNUAL",
                     title="2025年度報告", source_id="hk/b.pdf",
                     report_period=None, filing_date="2026-04-28",
                     period_source=PeriodSource.UNKNOWN)
    for r in (r1, r2):
        ref = store.upsert_report(r)
        store.commit_file(ref.report_id, r, _downloaded(store, str(r).encode()),
                          store.register_download(ref.report_id))
    if second_version:
        ref = store.upsert_report(r1)
        store.commit_file(ref.report_id, r1,
                          _downloaded(store, b"version-two" * 40),
                          store.register_download(ref.report_id))
    return root, store


class TestExportIndex:
    def test_csv_and_md_contents(self, tmp_path):
        root, _ = _seed(tmp_path)
        stats = export_index(root)
        assert stats == {"reports": 2, "artifacts": 2}
        with open(root / CSV_NAME, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 2
        interim = next(r for r in rows if r["doc_type"] == "INTERIM")
        assert interim["report_period"] == "2026-06-30"
        assert interim["display_name"] == "小米集團－Ｗ"
        assert interim["is_current"] == "1"
        assert interim["path"].startswith("HK/01810/")
        assert interim["path"].endswith(".pdf")
        annual = next(r for r in rows if r["doc_type"] == "ANNUAL")
        assert annual["report_period"] == "unknown"
        assert annual["period_source"] == "unknown"

        md = (root / MD_NAME).read_text(encoding="utf-8")
        assert "## HK / 01810（小米集團－Ｗ）" in md
        assert "2026-06-30" in md and "中报(INTERIM)" in md
        assert "unknown" in md
        assert "自动生成" in md

    def test_multiple_versions_is_current_flag(self, tmp_path):
        root, _ = _seed(tmp_path, second_version=True)
        stats = export_index(root)
        assert stats["artifacts"] == 3
        with open(root / CSV_NAME, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        interim_rows = [r for r in rows if r["doc_type"] == "INTERIM"]
        assert len(interim_rows) == 2
        assert sum(int(r["is_current"]) for r in interim_rows) == 1
        current = next(r for r in interim_rows if r["is_current"] == "1")
        assert current["sha256"] != next(
            r for r in interim_rows if r["is_current"] == "0")["sha256"]
        # MD 只展示当前版（INTERIM 只一行表格记录）
        md = (root / MD_NAME).read_text(encoding="utf-8")
        interim_lines = [ln for ln in md.splitlines()
                         if ln.startswith("| 2026-06-30 |")]
        assert len(interim_lines) == 1

    def test_empty_archive_noop(self, tmp_path):
        (tmp_path / "archive").mkdir()
        assert export_index(tmp_path / "archive") == {"reports": 0,
                                                      "artifacts": 0}
        assert not (tmp_path / "archive" / CSV_NAME).exists()


class TestWiring:
    def test_job_completion_refreshes_index(self, tmp_path):
        """任务终态后自动刷新平面索引（jobs._refresh_flat_index）。"""
        from tests.test_jobs import _harness, _submit

        jobs, store, *_ = _harness(tmp_path)
        _submit(jobs)
        jobs.run_job(jobs.claim_next())
        assert (store.root / CSV_NAME).is_file()
        assert (store.root / MD_NAME).is_file()
        with open(store.root / CSV_NAME, encoding="utf-8-sig") as fh:
            assert len(list(csv.DictReader(fh))) == 4
