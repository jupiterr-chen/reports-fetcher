"""store：schema v1、原子提交、缓存复核与基础恢复（DESIGN §11 / §16）。"""
from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from reports_fetcher.models import DownloadedFile, PeriodSource
from reports_fetcher.store import (
    Store,
    StoreError,
    compute_artifact_id,
    compute_report_id,
    sanitize_component,
)
from tests.conftest import make_report


def _downloaded(store: Store, content: bytes, media_type: str = "text/html"
                ) -> DownloadedFile:
    temp = store.tmp_dir / f".tmp-test-{hashlib.sha256(content).hexdigest()[:8]}.part"
    temp.write_bytes(content)
    return DownloadedFile(temp_path=str(temp),
                          sha256=hashlib.sha256(content).hexdigest(),
                          bytes=len(content), media_type=media_type,
                          final_url="https://example/final")


def _commit(store: Store, report=None, content: bytes | None = None):
    report = report or make_report()
    content = content if content is not None else (b"<html><body>" + b"d" * 500 + b"</body></html>")
    ref = store.upsert_report(report)
    attempt = store.register_download(ref.report_id)
    outcome = store.commit_file(ref.report_id, report,
                                _downloaded(store, content), attempt)
    return report, ref, outcome


class TestSchema:
    def test_schema_v2_created_with_wal(self, tmp_path):
        store = Store(tmp_path / "archive")
        mode = store.connection().execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        tables = {r["name"] for r in store.connection().execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"schema_meta", "manifest", "artifacts", "archive_intents",
                "symbol_map", "jobs", "job_symbols", "job_items"} <= tables
        version = store.connection().execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert version["value"] == "2"

    def test_v1_database_upgraded_additively(self, tmp_path):
        root = tmp_path / "archive"
        store = Store(root)
        store.close()
        conn = sqlite3.connect(root / "archive.sqlite3")
        conn.execute("UPDATE schema_meta SET value='1' WHERE key='schema_version'")
        conn.execute("DROP TABLE jobs")
        conn.execute("DROP TABLE job_symbols")
        conn.execute("DROP TABLE job_items")
        conn.commit()
        conn.close()
        store2 = Store(root)  # v1 → v2 增量迁移（非重建）
        version = store2.connection().execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        assert version["value"] == "2"
        tables = {r["name"] for r in store2.connection().execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"jobs", "job_symbols", "job_items"} <= tables

    def test_incompatible_version_rejected(self, tmp_path):
        root = tmp_path / "archive"
        store = Store(root)
        store.close()
        conn = sqlite3.connect(root / "archive.sqlite3")
        conn.execute("UPDATE schema_meta SET value='99' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with pytest.raises(StoreError):
            Store(root)

    def test_bad_layout_rejected(self, tmp_path):
        with pytest.raises(StoreError):
            Store(tmp_path / "a", layout="spiral")


class TestUpsertAndCache:
    def test_upsert_dedup_stable_report_id(self, tmp_path):
        store = Store(tmp_path / "archive")
        report = make_report()
        ref1 = store.upsert_report(report)
        ref2 = store.upsert_report(make_report(title="updated-title.htm"))
        assert ref1.report_id == ref2.report_id
        row = store.connection().execute(
            "SELECT title, status FROM manifest WHERE report_id=?",
            (ref1.report_id,)).fetchone()
        assert row["title"] == "updated-title.htm"  # 元数据刷新生效
        assert row["status"] == "discovered"

    def test_report_id_deterministic(self):
        rid = compute_report_id("US", "AAPL", "acc/doc.htm")
        assert rid == compute_report_id("US", "AAPL", "acc/doc.htm")
        assert len(rid) == 20
        assert rid != compute_report_id("US", "MSFT", "acc/doc.htm")
        assert compute_artifact_id(rid, "deadbeef") == \
               compute_artifact_id(rid, "deadbeef")

    def test_full_commit_then_cached(self, tmp_path):
        root = tmp_path / "archive"
        store = Store(root)
        report, ref, outcome = _commit(store)
        target = root / outcome.rel_path
        assert target.is_file()
        assert "US/AAPL" in outcome.rel_path
        assert report.report_period in outcome.rel_path
        assert ref.report_id in outcome.rel_path
        assert outcome.artifact_id in outcome.rel_path
        cached = store.find_cached(ref.report_id)
        assert cached is not None
        assert cached.artifact_id == outcome.artifact_id
        # manifest 状态：done + current_artifact
        row = store.connection().execute(
            "SELECT status, current_artifact_id FROM manifest WHERE report_id=?",
            (ref.report_id,)).fetchone()
        assert row["status"] == "done"
        assert row["current_artifact_id"] == outcome.artifact_id

    def test_reopen_keeps_cache(self, tmp_path):
        root = tmp_path / "archive"
        store = Store(root)
        _report, ref, _outcome = _commit(store)
        store.close()
        store2 = Store(root)
        assert store2.find_cached(ref.report_id) is not None

    def test_corrupt_file_breaks_cache_and_manifest_fails(self, tmp_path):
        root = tmp_path / "archive"
        store = Store(root)
        _report, ref, outcome = _commit(store)
        (root / outcome.rel_path).write_bytes(b"corrupted")
        assert store.find_cached(ref.report_id) is None
        art = store.connection().execute(
            "SELECT state FROM artifacts WHERE artifact_id=?",
            (outcome.artifact_id,)).fetchone()
        assert art["state"] == "unavailable"
        mrow = store.connection().execute(
            "SELECT status, current_artifact_id FROM manifest WHERE report_id=?",
            (ref.report_id,)).fetchone()
        assert mrow["status"] == "failed"
        assert mrow["current_artifact_id"] is None

    def test_refetch_same_content_revives_artifact_id(self, tmp_path):
        root = tmp_path / "archive"
        store = Store(root)
        report, ref, outcome = _commit(store)
        content = (root / outcome.rel_path).read_bytes()
        (root / outcome.rel_path).unlink()  # 文件丢失
        assert store.find_cached(ref.report_id) is None
        # 重抓同内容：复用原 artifact_id（修复 unavailable，不违反唯一约束）
        attempt = store.register_download(ref.report_id)
        outcome2 = store.commit_file(ref.report_id, report,
                                     _downloaded(store, content), attempt)
        assert outcome2.artifact_id == outcome.artifact_id
        assert outcome2.reused is True
        assert store.find_cached(ref.report_id) is not None

    def test_new_content_creates_new_artifact(self, tmp_path):
        root = tmp_path / "archive"
        store = Store(root)
        report = make_report()
        ref = store.upsert_report(report)
        a1 = store.commit_file(ref.report_id, report,
                               _downloaded(store, b"version-one" * 50),
                               store.register_download(ref.report_id))
        a2 = store.commit_file(ref.report_id, report,
                               _downloaded(store, b"version-two" * 50),
                               store.register_download(ref.report_id))
        assert a1.artifact_id != a2.artifact_id
        rows = store.connection().execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE report_id=?",
            (ref.report_id,)).fetchone()
        assert rows["n"] == 2  # 旧版本保留
        cached = store.find_cached(ref.report_id)
        assert cached.artifact_id == a2.artifact_id  # 当前版本切换

    def test_same_sha_second_commit_reuses(self, tmp_path):
        store = Store(tmp_path / "archive")
        report = make_report()
        ref = store.upsert_report(report)
        content = b"identical" * 100
        o1 = store.commit_file(ref.report_id, report, _downloaded(store, content),
                               store.register_download(ref.report_id))
        o2 = store.commit_file(ref.report_id, report, _downloaded(store, content),
                               store.register_download(ref.report_id))
        assert o2.reused is True and o2.artifact_id == o1.artifact_id
        n = store.connection().execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE report_id=?",
            (ref.report_id,)).fetchone()["n"]
        assert n == 1

    def test_mark_failed_downloading(self, tmp_path):
        store = Store(tmp_path / "archive")
        report = make_report()
        ref = store.upsert_report(report)
        store.register_download(ref.report_id)
        store.mark_failed(ref.report_id, "download_invalid", "boom")
        row = store.connection().execute(
            "SELECT status FROM manifest WHERE report_id=?",
            (ref.report_id,)).fetchone()
        assert row["status"] == "failed"

    def test_mark_failed_does_not_degrade_done(self, tmp_path):
        store = Store(tmp_path / "archive")
        _report, ref, _outcome = _commit(store)
        store.mark_failed(ref.report_id, "download_invalid", "later failure")
        row = store.connection().execute(
            "SELECT status FROM manifest WHERE report_id=?",
            (ref.report_id,)).fetchone()
        assert row["status"] == "done"  # 已有有效版本不降级

    def test_open_artifact_path_boundary(self, tmp_path):
        root = tmp_path / "archive"
        store = Store(root)
        _report, _ref, outcome = _commit(store)
        path, row = store.open_artifact(outcome.artifact_id)
        assert path.is_file()
        assert row["state"] == "ready"


class TestRecovery:
    """基础原子性（DoD #3）：.part → 校验 → 原子改名 → 标记 的各中断点。"""

    def _root(self, tmp_path):
        return tmp_path / "archive"

    def test_crash_during_download(self, tmp_path):
        """intent=registered（无 sha）+ 遗留 .part → 重开废弃并清扫。"""
        root = self._root(tmp_path)
        store = Store(root)
        report = make_report()
        ref = store.upsert_report(report)
        store.register_download(ref.report_id)  # 下载中途被强杀
        stray = store.tmp_dir / ".tmp-orphan.part"
        stray.write_bytes(b"half a file")
        store.close()

        store2 = Store(root)  # 重跑：恢复收敛
        row = store2.connection().execute(
            "SELECT status FROM manifest WHERE report_id=?",
            (ref.report_id,)).fetchone()
        assert row["status"] == "discovered"  # 无脏 downloading
        assert not stray.exists()             # 半文件被清扫
        intent = store2.connection().execute(
            "SELECT state FROM archive_intents").fetchone()
        assert intent["state"] == "failed"
        # 重跑可正常完成
        _report2, ref2, outcome = _commit(store2, report=report)
        assert ref2.report_id == ref.report_id
        assert (root / outcome.rel_path).is_file()
        assert store2.find_cached(ref.report_id) is not None

    def test_crash_after_rename_before_mark(self, tmp_path):
        """改名后、标记 ready 前崩溃 → 重开补记 ready。"""
        root = self._root(tmp_path)
        store = Store(root)
        report = make_report()
        ref = store.upsert_report(report)
        attempt = store.register_download(ref.report_id)
        content = b"<html><body>" + b"z" * 600 + b"</body></html>"
        store._finalize_commit = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("crash after rename"))
        with pytest.raises(RuntimeError):
            store.commit_file(ref.report_id, report,
                              _downloaded(store, content), attempt)
        store.close()

        store2 = Store(root)
        assert store2.find_cached(ref.report_id) is not None  # 补记 ready
        row = store2.connection().execute(
            "SELECT status FROM manifest WHERE report_id=?",
            (ref.report_id,)).fetchone()
        assert row["status"] == "done"

    def test_crash_after_stage_before_rename(self, tmp_path):
        """staged 事务后、改名前崩溃 → 重开用完整临时文件完成提交。"""
        root = self._root(tmp_path)
        store = Store(root)
        report = make_report()
        ref = store.upsert_report(report)
        attempt = store.register_download(ref.report_id)
        content = b"<html><body>" + b"y" * 600 + b"</body></html>"
        downloaded = _downloaded(store, content)
        now = "2026-09-20T00:00:00+00:00"
        conn = store.connection()
        with conn:
            artifact_id = compute_artifact_id(ref.report_id, downloaded.sha256)
            conn.execute(
                """INSERT INTO artifacts(
                       artifact_id, report_id, sha256, bytes, media_type,
                       local_path, state, fetched_at, created_at, updated_at)
                   VALUES(?,?,?,?,?,?, 'staged', ?, ?, ?)""",
                (artifact_id, ref.report_id, downloaded.sha256,
                 downloaded.bytes, downloaded.media_type,
                 "unused-until-rename", now, now, now))
            rel = store._target_rel(report, ref.report_id, artifact_id, "html")
            conn.execute(
                """UPDATE archive_intents SET state='staged', artifact_id=?,
                       temp_path=?, target_rel_path=?, expected_sha256=?,
                       expected_bytes=?, updated_at=? WHERE attempt_id=?""",
                (artifact_id, downloaded.temp_path, rel, downloaded.sha256,
                 downloaded.bytes, now, attempt))
        store.close()

        store2 = Store(root)
        assert store2.find_cached(ref.report_id) is not None
        assert (root / rel).is_file()
        assert not Path(downloaded.temp_path).exists()

    def test_recovery_replaces_tampered_target_with_validated_temp(self, tmp_path):
        """staged + 目标被写入异样内容 → 重开以已校验临时文件收敛（不产生脏 done）。"""
        root = self._root(tmp_path)
        store = Store(root)
        report = make_report()
        ref = store.upsert_report(report)
        attempt = store.register_download(ref.report_id)
        content = b"<html><body>" + b"w" * 600 + b"</body></html>"
        downloaded = _downloaded(store, content)
        now = "2026-09-20T00:00:00+00:00"
        conn = store.connection()
        with conn:
            artifact_id = compute_artifact_id(ref.report_id, downloaded.sha256)
            rel = store._target_rel(report, ref.report_id, artifact_id, "html")
            conn.execute(
                """INSERT INTO artifacts(
                       artifact_id, report_id, sha256, bytes, media_type,
                       local_path, state, fetched_at, created_at, updated_at)
                   VALUES(?,?,?,?,?,?, 'staged', ?, ?, ?)""",
                (artifact_id, ref.report_id, downloaded.sha256,
                 downloaded.bytes, downloaded.media_type, str(root / rel),
                 now, now, now))
            conn.execute(
                """UPDATE archive_intents SET state='staged', artifact_id=?,
                       temp_path=?, target_rel_path=?, expected_sha256=?,
                       expected_bytes=?, updated_at=? WHERE attempt_id=?""",
                (artifact_id, downloaded.temp_path, rel, downloaded.sha256,
                 downloaded.bytes, now, attempt))
        # 目标路径被写入异样内容（模拟异常现场）
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"different bytes")
        store.close()

        store2 = Store(root)
        cached = store2.find_cached(ref.report_id)
        assert cached is not None               # 以已校验内容收敛，非脏 done
        assert (root / rel).read_bytes() == content


class TestPaths:
    def test_sanitize_illegal_chars(self):
        assert "<" not in sanitize_component('a<b>:c"/\\|?*d')
        assert sanitize_component("  spaced.  ") == "spaced"
        assert sanitize_component("") == "_"
        assert sanitize_component("CON") == "_CON"  # Windows 保留名
        assert sanitize_component("com1.tailing") == "_com1.tailing"
        assert len(sanitize_component("x" * 500)) == 180

    def test_layout_nested(self, tmp_path):
        store = Store(tmp_path / "archive", layout="nested")
        _report, _ref, outcome = _commit(store)
        parts = Path(outcome.rel_path).parts
        assert parts[0] == "US" and parts[1] == "AAPL" and parts[2] == "2026-06-27"

    def test_source_form_not_in_filename(self, tmp_path):
        store = Store(tmp_path / "archive")
        report = make_report(source_form="10-K/A", doc_type="10-K",
                             title="aapl-20250927x10ka.htm")
        _report, _ref, outcome = _commit(store, report=report)
        assert "10-K" in Path(outcome.rel_path).name
        assert "/" not in Path(outcome.rel_path).name  # /A 不进入文件名


class TestConcurrency:
    def test_thread_local_connections_commit(self, tmp_path):
        store = Store(tmp_path / "archive")

        def work(i: int):
            local_store = store  # 共享 Store：内部 thread-local 连接
            report = make_report(source_id=f"acc-{i}/doc{i}.htm",
                                 report_period=f"2025-0{i + 1}-27")
            ref = local_store.upsert_report(report)
            attempt = local_store.register_download(ref.report_id)
            return local_store.commit_file(
                ref.report_id, report,
                _downloaded(local_store, f"<html>v{i}</html>".encode() * 50),
                attempt).artifact_id

        with ThreadPoolExecutor(max_workers=3) as pool:
            artifact_ids = list(pool.map(work, range(6)))
        assert len(set(artifact_ids)) == 6
        n = store.connection().execute(
            "SELECT COUNT(*) AS n FROM manifest").fetchone()["n"]
        assert n == 6


class TestSymbolMap:
    def test_upsert_symbol(self, tmp_path):
        from reports_fetcher.models import Market, ResolvedSymbol
        store = Store(tmp_path / "archive")
        store.upsert_symbol(ResolvedSymbol(
            market=Market.US, symbol="AAPL", raw_inputs=["aapl", "AAPL"],
            display_name="Apple Inc.", source_issuer_id="0000320193",
            issuer_id="sec:0000320193"))
        row = store.connection().execute(
            "SELECT * FROM symbol_map WHERE market='US' AND symbol='AAPL'"
        ).fetchone()
        assert row["source_issuer_id"] == "0000320193"
        assert row["display_name"] == "Apple Inc."
        assert row["expires_at"] is not None


class TestOwnerLock:
    """进程级所有者锁（DESIGN §11.4；PHASE1_REVIEW T1：锁先于 schema/恢复）。"""

    def test_second_store_construction_rejected_without_side_effects(
            self, tmp_path):
        """持锁实例有在途意图与临时文件时，第二实例构造即被拒绝且状态不变。"""
        root = tmp_path / "archive"
        first = Store(root)
        report = make_report()
        ref = first.upsert_report(report)
        first.register_download(ref.report_id)   # 在途 registered 意图
        stray = first.tmp_dir / ".tmp-t1.part"
        stray.write_bytes(b"in-flight download")
        conn_before = sqlite3.connect(root / "archive.sqlite3")
        intent_before = conn_before.execute(
            "SELECT state FROM archive_intents").fetchone()[0]
        conn_before.close()

        with pytest.raises(StoreError) as ei:
            Store(root)                          # 第二实例：构造期即拒绝
        assert ei.value.code == "store_in_use"

        # 数据库状态与临时文件均未被第二实例触碰（T1 验收）
        conn_after = sqlite3.connect(root / "archive.sqlite3")
        intent_after = conn_after.execute(
            "SELECT state FROM archive_intents").fetchone()[0]
        conn_after.close()
        assert intent_after == intent_before == "registered"
        assert stray.read_bytes() == b"in-flight download"

        first.close()                            # 所有者退出
        fresh = Store(root)                      # 新实例可正确恢复遗留状态
        stray_after = fresh.tmp_dir / ".tmp-t1.part"
        assert not stray_after.exists()          # 恢复清扫未完成临时文件
        conn = sqlite3.connect(root / "archive.sqlite3")
        assert conn.execute("SELECT state FROM archive_intents").fetchone()[0] \
            == "failed"
        conn.close()
        fresh.close()

    def test_double_acquire_on_same_store_is_noop(self, tmp_path):
        store = Store(tmp_path / "archive")      # 构造即持锁
        store.acquire_owner_lock()               # 幂等
        store.close()

    def test_release_allows_reopen(self, tmp_path):
        root = tmp_path / "archive"
        holder = Store(root)
        holder.release_owner_lock()
        fresh = Store(root)
        fresh.close()
        holder.close()


class TestRefreshVersions:
    """refresh：重下候选并保留旧内容版本（多 artifact 并存、按 ID 读取）。"""

    def test_refresh_new_content_keeps_old_artifact(self, tmp_path):
        root = tmp_path / "archive"
        store = Store(root)
        report = make_report()
        ref = store.upsert_report(report)
        v1 = store.commit_file(ref.report_id, report,
                               _downloaded(store, b"version-one" * 60),
                               store.register_download(ref.report_id))
        # refresh 下载期间：manifest 保持 done（不降级已有有效内容）
        attempt = store.register_download(ref.report_id)
        status_during_refresh = store.connection().execute(
            "SELECT status FROM manifest WHERE report_id=?",
            (ref.report_id,)).fetchone()["status"]
        assert status_during_refresh == "done"
        v2 = store.commit_file(ref.report_id, report,
                               _downloaded(store, b"version-two" * 60),
                               attempt)
        assert v1.artifact_id != v2.artifact_id
        # 旧 artifact 仍可按 ID 读取且 checksum 一致（DoD #2）
        path1, row1 = store.open_artifact(v1.artifact_id)
        assert path1.read_bytes() == b"version-one" * 60
        assert row1["sha256"] == hashlib.sha256(b"version-one" * 60).hexdigest()
        assert row1["state"] == "ready"
        # 当前版本切换为新内容
        cached = store.find_cached(ref.report_id)
        assert cached.artifact_id == v2.artifact_id
        n = store.connection().execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE report_id=?",
            (ref.report_id,)).fetchone()["n"]
        assert n == 2  # 多版本并存

    def test_refresh_identical_content_reuses(self, tmp_path):
        store = Store(tmp_path / "archive")
        report = make_report()
        ref = store.upsert_report(report)
        content = b"same-bytes" * 80
        o1 = store.commit_file(ref.report_id, report,
                               _downloaded(store, content),
                               store.register_download(ref.report_id))
        o2 = store.commit_file(ref.report_id, report,
                               _downloaded(store, content),
                               store.register_download(ref.report_id))
        assert o2.reused is True and o2.artifact_id == o1.artifact_id
        n = store.connection().execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE report_id=?",
            (ref.report_id,)).fetchone()["n"]
        assert n == 1

    def test_refresh_failure_does_not_degrade_done(self, tmp_path):
        store = Store(tmp_path / "archive")
        report = make_report()
        ref = store.upsert_report(report)
        store.commit_file(ref.report_id, report,
                          _downloaded(store, b"good" * 100),
                          store.register_download(ref.report_id))
        # refresh 尝试失败：只记任务尝试，已有有效版本不降级
        store.register_download(ref.report_id)
        store.mark_failed(ref.report_id, "download_invalid", "refresh failed")
        row = store.connection().execute(
            "SELECT status, current_artifact_id FROM manifest WHERE report_id=?",
            (ref.report_id,)).fetchone()
        assert row["status"] == "done" and row["current_artifact_id"]
        assert store.find_cached(ref.report_id) is not None


class TestFileLossRepair:
    """丢失/损坏文件修复（DoD #1：文件被删 / 文件损坏）。"""

    def _committed(self, tmp_path):
        root = tmp_path / "archive"
        store = Store(root)
        report = make_report()
        ref = store.upsert_report(report)
        content = b"<html><body>report v1</body></html>" + b"x" * 400
        outcome = store.commit_file(ref.report_id, report,
                                    _downloaded(store, content),
                                    store.register_download(ref.report_id))
        return store, root, report, ref, content, outcome

    def test_deleted_file_repaired_with_same_artifact_id(self, tmp_path):
        store, root, report, ref, content, outcome = self._committed(tmp_path)
        (root / outcome.rel_path).unlink()
        assert store.find_cached(ref.report_id) is None  # 检出丢失并标记
        # 修复：重抓同内容 → 复用原 artifact ID（不违反 report+sha 唯一约束）
        attempt = store.register_download(ref.report_id)
        outcome2 = store.commit_file(ref.report_id, report,
                                     _downloaded(store, content), attempt)
        assert outcome2.artifact_id == outcome.artifact_id
        assert outcome2.reused is True
        path, row = store.open_artifact(outcome.artifact_id)
        assert path.read_bytes() == content and row["state"] == "ready"

    def test_corrupted_file_repaired_by_validated_temp(self, tmp_path):
        store, root, report, ref, content, outcome = self._committed(tmp_path)
        (root / outcome.rel_path).write_bytes(b"corrupted junk")
        assert store.find_cached(ref.report_id) is None  # 检出损坏并标记
        # 修复：目标现场已被判 unavailable → 以已校验临时文件覆盖
        attempt = store.register_download(ref.report_id)
        outcome2 = store.commit_file(ref.report_id, report,
                                     _downloaded(store, content), attempt)
        assert outcome2.artifact_id == outcome.artifact_id
        assert (root / outcome.rel_path).read_bytes() == content
        assert store.find_cached(ref.report_id) is not None

    def test_tampered_ready_target_refuses_overwrite(self, tmp_path):
        store, root, report, ref, content, outcome = self._committed(tmp_path)
        # 现场伪造：ready 目标被外部替换为异样内容，且未经 find_cached 检出
        # （无 unavailable 判定）→ 同名重下时拒绝覆盖（DESIGN §11.3 步骤 4）
        (root / outcome.rel_path).write_bytes(b"tampered")
        with pytest.raises(StoreError, match="拒绝覆盖"):
            store.commit_file(ref.report_id, report,
                              _downloaded(store, content),
                              store.register_download(ref.report_id))

    def test_finalize_idempotent_after_mark_crash(self, tmp_path):
        """标记后崩溃（DoD #1）：重开/再标记幂等，状态保持一致。"""
        store, root, report, ref, content, outcome = self._committed(tmp_path)
        store.close()
        for _ in range(2):  # 连续重开多次
            reopened = Store(root)
            cached = reopened.find_cached(ref.report_id)
            assert cached is not None
            reopened.close()


class TestPeriodEnrichment:
    """原文报告期富化：仅补未知、绝不覆盖可信、不触碰归档文件。"""

    @staticmethod
    def _committed(store, **overrides):
        report = make_report(**overrides)
        ref = store.upsert_report(report)
        content = (b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
                   + b"x" * 400 + b"\n%%EOF\n")
        outcome = store.commit_file(ref.report_id, report,
                                    _downloaded(store, content,
                                                media_type="application/pdf"),
                                    store.register_download(ref.report_id))
        return report, ref, outcome

    def test_enrich_fills_unknown_period(self, tmp_path):
        store = Store(tmp_path / "archive")
        _report, ref, outcome = self._committed(
            store, report_period=None, period_source=PeriodSource.UNKNOWN)
        assert store.enrich_report_period(ref.report_id, "2025-12-31") is True
        row = store.connection().execute(
            "SELECT report_period, period_source FROM manifest WHERE report_id=?",
            (ref.report_id,)).fetchone()
        assert row["report_period"] == "2025-12-31"
        assert row["period_source"] == "document"
        # 归档文件与 artifact 路径不变（不产生重复/孤儿文件）
        cached = store.find_cached(ref.report_id)
        assert cached is not None and cached.artifact_id == outcome.artifact_id

    def test_enrich_never_overwrites_trusted_period(self, tmp_path):
        store = Store(tmp_path / "archive")
        _report, ref, _outcome = self._committed(
            store, report_period="2026-06-27",
            period_source=PeriodSource.SOURCE_FIELD)
        assert store.enrich_report_period(ref.report_id, "2020-01-01") is False
        row = store.connection().execute(
            "SELECT report_period, period_source FROM manifest WHERE report_id=?",
            (ref.report_id,)).fetchone()
        assert row["report_period"] == "2026-06-27"
        assert row["period_source"] == "source_field"

    def test_enrich_rejects_invalid_date(self, tmp_path):
        store = Store(tmp_path / "archive")
        _report, ref, _outcome = self._committed(
            store, report_period=None, period_source=PeriodSource.UNKNOWN)
        assert store.enrich_report_period(ref.report_id, "2025-02-30") is False
        assert store.enrich_report_period(ref.report_id, "not-a-date") is False

    def test_upsert_preserves_trusted_when_incoming_unknown(self, tmp_path):
        store = Store(tmp_path / "archive")
        known = make_report(report_period="2025-12-31",
                            period_source=PeriodSource.DOCUMENT)
        ref1 = store.upsert_report(known)
        incoming = make_report(report_period=None,
                               period_source=PeriodSource.UNKNOWN,
                               title="refreshed title")
        ref2 = store.upsert_report(incoming)
        assert ref2.report_id == ref1.report_id
        assert ref2.report_period == "2025-12-31"
        assert ref2.period_source == "document"
        row = store.connection().execute(
            "SELECT report_period, period_source, title FROM manifest "
            "WHERE report_id=?", (ref1.report_id,)).fetchone()
        assert row["report_period"] == "2025-12-31"
        assert row["title"] == "refreshed title"  # 其他元数据仍刷新
