"""Store：归档与索引的唯一提交方（DESIGN §11，FR-4/FR-5/FR-6）。

铁律：
- 下载器只产出已校验临时文件；Store 唯一负责"落盘原子改名、后标记 done"；
- source_id 去重键 (market, symbol, source_id)，report_id 首次持久化时分配并绑定；
- 缓存命中以 SHA-256 复核，同大小损坏不得当作命中；
- 文件系统与 SQLite 无共同事务：archive_intents 记录提交时序，
  重启时按 intent 收敛；
- 一个归档根目录一个进程级所有者锁（flock，跨容器互斥已在主力环境实测；
  进程死亡自动释放，强杀后重跑不被阻塞）。
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import socket
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from reports_fetcher.models import (
    ArtifactState,
    DownloadedFile,
    FileNotAvailableError,
    Report,
    ReportStatus,
    ResolvedSymbol,
    StoreError,
)
from reports_fetcher.period import period_or_unknown

SCHEMA_VERSION = 2  # v2 = v1 + jobs/job_symbols/job_items（I5，增量迁移）

_LAYOUTS = ("flat", "nested")
_MAX_TITLE_LEN = 60
_MAX_FILENAME_LEN = 180
_MAX_PATH_LEN = 235  # Windows MAX_PATH 安全预算
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manifest (
    report_id     TEXT PRIMARY KEY,
    market        TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    source_url    TEXT,
    title         TEXT,
    doc_type      TEXT NOT NULL,
    source_form   TEXT,
    filing_date   TEXT,
    report_period TEXT,
    period_source TEXT NOT NULL,
    language      TEXT,
    document_role TEXT,
    is_amendment  INTEGER NOT NULL DEFAULT 0,
    revision_of   TEXT,
    source_issuer_id TEXT,
    source_metadata_json TEXT,
    status        TEXT NOT NULL DEFAULT 'discovered'
                  CHECK (status IN ('discovered','downloading','done','failed')),
    current_artifact_id TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (market, symbol, source_id)
);
CREATE INDEX IF NOT EXISTS idx_manifest_symbol ON manifest(market, symbol);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    report_id   TEXT NOT NULL REFERENCES manifest(report_id),
    sha256      TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    media_type  TEXT NOT NULL,
    local_path  TEXT NOT NULL,
    source_url  TEXT,
    final_url   TEXT,
    fetched_at  TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('staged','ready','unavailable')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (report_id, sha256)
);

CREATE TABLE IF NOT EXISTS archive_intents (
    attempt_id       TEXT PRIMARY KEY,
    report_id        TEXT NOT NULL,
    artifact_id      TEXT,
    temp_path        TEXT,
    target_rel_path  TEXT,
    expected_sha256  TEXT,
    expected_bytes   INTEGER,
    state            TEXT NOT NULL
                     CHECK (state IN ('registered','staged','committed','failed')),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbol_map (
    market            TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    source_issuer_id  TEXT,
    display_name      TEXT,
    exchange          TEXT,
    raw_inputs_json   TEXT,
    updated_at        TEXT NOT NULL,
    expires_at        TEXT,
    PRIMARY KEY (market, symbol)
);

-- ---- schema v2（I5）：持久化任务 ----
CREATE TABLE IF NOT EXISTS jobs (
    job_id                TEXT PRIMARY KEY,
    client_id             TEXT NOT NULL,
    idempotency_key       TEXT NOT NULL,
    request_hash          TEXT NOT NULL,
    effective_request_json TEXT NOT NULL,
    status                TEXT NOT NULL
                          CHECK (status IN ('queued','running',
                                            'succeeded','partial','failed')),
    attempt               INTEGER NOT NULL DEFAULT 0,
    deadline              TEXT,
    started_at            TEXT,
    submitted_at          TEXT NOT NULL,
    finished_at           TEXT,
    summary_json          TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (client_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS job_symbols (
    job_id          TEXT NOT NULL REFERENCES jobs(job_id),
    market          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('succeeded','partial','failed','no_reports')),
    coverage_json   TEXT,
    warnings_json   TEXT,
    error_code      TEXT,
    display_name    TEXT,
    PRIMARY KEY (job_id, market, symbol)
);

CREATE TABLE IF NOT EXISTS job_items (
    job_id       TEXT NOT NULL,
    report_id    TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    outcome      TEXT NOT NULL
                 CHECK (outcome IN ('downloaded','cached','failed')),
    artifact_id  TEXT,
    error_code   TEXT,
    error_detail TEXT,
    PRIMARY KEY (job_id, report_id)
);
"""


def _utcnow() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def compute_report_id(market: str, symbol: str, source_id: str) -> str:
    """受控不透明标识：固定 20 hex 字符，由来源去重键派生（跨次运行稳定）。"""
    return hashlib.sha256(
        f"{market}|{symbol}|{source_id}".encode("utf-8")).hexdigest()[:20]


def compute_artifact_id(report_id: str, sha256: str) -> str:
    """内容版本标识：同一报告同内容重试/重抓得到同一 ID（利于恢复复用）。"""
    return hashlib.sha256(
        f"{report_id}|{sha256}".encode("utf-8")).hexdigest()[:20]


def sanitize_component(name: str, max_len: int = _MAX_FILENAME_LEN) -> str:
    """清理 Windows/Linux 非法字符、控制字符、保留设备名、首尾空白/点号。"""
    cleaned = _ILLEGAL_CHARS_RE.sub("_", name)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        cleaned = "_"
    stem = cleaned.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        cleaned = "_" + cleaned
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .") or "_"
    return cleaned


@dataclass
class ReportRef:
    report_id: str
    status: str


@dataclass
class CachedHit:
    artifact_id: str
    local_path: Path
    sha256: str
    bytes: int


@dataclass
class CommitOutcome:
    artifact_id: str
    rel_path: str
    reused: bool   # 命中既有 ready/unavailable 同内容版本而非新写入


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


class Store:
    """SQLite 索引 + 归档目录。每线程独立连接、短事务；库启用 WAL。"""

    def __init__(self, root: Path, *, layout: str = "flat") -> None:
        if layout not in _LAYOUTS:
            raise StoreError("layout 仅支持 flat / nested", detail=f"layout={layout}")
        self.root = Path(root).resolve()
        self.layout = layout
        self.root.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = self.root / ".tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "archive.sqlite3"
        self._local = threading.local()
        self._lock_fh = None
        # 所有者锁必须先于任何共享状态修改（PHASE1_REVIEW T1）：schema
        # 初始化与崩溃恢复都只在持锁后执行，第二实例被拒绝时库与临时
        # 文件保持原状；构造失败不留下半初始化状态。
        self.acquire_owner_lock()
        self._init_schema()
        self._recover()

    # ------------------------------------------------------------- 连接

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
        self.release_owner_lock()

    # ------------------------------------------------------------- 进程锁

    def acquire_owner_lock(self) -> None:
        """归档根目录进程级所有者锁（DESIGN §11.4，PHASE1_REVIEW T1）。

        flock 非阻塞独占；构造 Store 即持锁，schema/恢复均在锁内。
        第二写实例在构造期即报 store_in_use，且此前不触碰数据库与
        临时文件。持锁进程死亡时由内核自动释放（强杀后重跑不被阻塞）；
        跨容器互斥已在主力环境（Windows Docker Desktop bind mount）实测。
        """
        if self._lock_fh is not None:
            return
        lock_path = self.root / ".lock"
        fh = open(lock_path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            raise StoreError(
                "归档根目录正被其他实例使用（单写者保护）",
                code="store_in_use",
                detail=f"lock={lock_path}: {e}") from e
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{socket.gethostname()} pid={os.getpid()} "
                     f"acquired={_utcnow()}\n")
            fh.flush()
        except OSError:  # pragma: no cover - 锁信息写入失败不影响锁语义
            pass
        self._lock_fh = fh

    def release_owner_lock(self) -> None:
        if self._lock_fh is None:
            return
        try:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover
            pass
        self._lock_fh.close()
        self._lock_fh = None

    def _init_schema(self) -> None:
        conn = self.connection()
        with conn:
            # DDL 全部 IF NOT EXISTS：v1 库增量获得 v2 任务表（非重建）
            conn.executescript(_SCHEMA_SQL)
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),))
            elif row["value"] == str(SCHEMA_VERSION):
                pass
            elif row["value"] == "1":
                # v1 → v2：jobs 三表已由上方 DDL 增量创建，仅推进版本号
                conn.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),))
            else:
                raise StoreError(
                    "数据库 schema_version 不兼容，拒绝静默重建",
                    detail=f"db={self.db_path} version={row['value']} "
                           f"expected<={SCHEMA_VERSION}")

    # ------------------------------------------------------------- 恢复

    def _recover(self) -> list[str]:
        """基础原子性收敛（I1）：处理下载中崩溃遗留的 intent 与临时文件。

        - intent=staged 且目标文件已就位/校验一致 → 补记 ready（改名后崩溃）；
        - intent=staged 且临时文件完整 → 完成改名并标记（改名前崩溃）；
        - intent=registered（下载未完成）→ 标记 failed，manifest 回 discovered；
        - 清扫 .tmp 下未被任何在途 intent 引用的 *.part 遗留文件。
        """
        notes: list[str] = []
        conn = self.connection()
        rows = conn.execute(
            "SELECT * FROM archive_intents WHERE state IN ('registered','staged') "
            "ORDER BY created_at").fetchall()
        for row in rows:
            if row["state"] == "staged":
                target = self.root / row["target_rel_path"]
                temp = Path(row["temp_path"]) if row["temp_path"] else None
                expected_sha = row["expected_sha256"]
                if target.is_file() and expected_sha \
                        and _hash_file(target) == expected_sha:
                    self._finalize_commit(row["report_id"], row["artifact_id"],
                                          row["attempt_id"], row["target_rel_path"])
                    notes.append(f"恢复：目标已就位，补记 ready {row['report_id']}")
                elif temp and temp.is_file() and expected_sha \
                        and _hash_file(temp) == expected_sha:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temp.replace(target)
                    self._finalize_commit(row["report_id"], row["artifact_id"],
                                          row["attempt_id"], row["target_rel_path"])
                    notes.append(f"恢复：临时文件完整，完成提交 {row['report_id']}")
                else:
                    self._abandon_intent(row["attempt_id"], row["report_id"])
                    notes.append(f"恢复：废弃未完成下载 {row['report_id']}")
            else:  # registered：下载从未完成（无 expected sha）
                self._abandon_intent(row["attempt_id"], row["report_id"])
                notes.append(f"恢复：废弃在途下载 {row['report_id']}")
        self._sweep_tmp()
        return notes

    def _abandon_intent(self, attempt_id: str, report_id: str) -> None:
        conn = self.connection()
        with conn:
            conn.execute(
                "UPDATE archive_intents SET state='failed', updated_at=? "
                "WHERE attempt_id=?", (_utcnow(), attempt_id))
            conn.execute(
                "UPDATE manifest SET status='discovered', updated_at=? "
                "WHERE report_id=? AND status='downloading' AND "
                "current_artifact_id IS NULL",
                (_utcnow(), report_id))

    def _sweep_tmp(self) -> None:
        """删除 .tmp 下未被在途 intent 引用的 *.part（未完成文件重新下载）。"""
        conn = self.connection()
        live = {
            r["temp_path"] for r in conn.execute(
                "SELECT temp_path FROM archive_intents "
                "WHERE state IN ('registered','staged') AND temp_path IS NOT NULL")}
        for part in self.tmp_dir.glob("*.part"):
            if str(part) not in live:
                part.unlink(missing_ok=True)

    # ------------------------------------------------------------- 元数据

    def upsert_report(self, report: Report) -> ReportRef:
        """候选报告首次持久化分配稳定 report_id；重试返回原 ID（幂等）。"""
        report_id = compute_report_id(report.market.value, report.symbol,
                                      report.source_id)
        now = _utcnow()
        conn = self.connection()
        with conn:
            existing = conn.execute(
                "SELECT report_id, status FROM manifest "
                "WHERE market=? AND symbol=? AND source_id=?",
                (report.market.value, report.symbol, report.source_id)).fetchone()
            if existing is None:
                # 19 个绑定参数：status 为字面量 'discovered'
                conn.execute(
                    """INSERT INTO manifest(
                        report_id, market, symbol, source_id, source_url, title,
                        doc_type, source_form, filing_date, report_period,
                        period_source, language, document_role, is_amendment,
                        revision_of, source_issuer_id, source_metadata_json,
                        status, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                              'discovered',?,?)""",
                    (report_id, report.market.value, report.symbol,
                     report.source_id, report.source_url, report.title,
                     report.doc_type, report.source_form, report.filing_date,
                     report.report_period, report.period_source.value,
                     report.language, report.document_role.value,
                     1 if report.is_amendment else 0, report.revision_of,
                     report.source_issuer_id,
                     json.dumps(report.source_metadata, ensure_ascii=False),
                     now, now))
                return ReportRef(report_id, ReportStatus.DISCOVERED.value)
            conn.execute(
                """UPDATE manifest SET
                     source_url=?, title=?, doc_type=?, source_form=?,
                     filing_date=?, report_period=?, period_source=?, language=?,
                     document_role=?, is_amendment=?, revision_of=?,
                     source_issuer_id=?, source_metadata_json=?, updated_at=?
                   WHERE report_id=?""",
                (report.source_url, report.title, report.doc_type,
                 report.source_form, report.filing_date, report.report_period,
                 report.period_source.value, report.language,
                 report.document_role.value,
                 1 if report.is_amendment else 0, report.revision_of,
                 report.source_issuer_id,
                 json.dumps(report.source_metadata, ensure_ascii=False),
                 now, existing["report_id"]))
            return ReportRef(existing["report_id"], existing["status"])

    def find_cached(self, report_id: str) -> CachedHit | None:
        """缓存命中判定：done + ready + 磁盘文件 SHA-256 复核通过。

        文件缺失/损坏时标记 artifact unavailable、manifest 转 failed 并清空
        current_artifact_id（不得跳过丢失文件，按恢复规则重新抓取）。
        """
        conn = self.connection()
        row = conn.execute(
            """SELECT a.artifact_id, a.local_path, a.bytes, a.sha256
               FROM manifest m JOIN artifacts a
                 ON a.artifact_id = m.current_artifact_id
               WHERE m.report_id=? AND m.status='done' AND a.state='ready'""",
            (report_id,)).fetchone()
        if row is None:
            return None
        path = Path(row["local_path"])
        if not path.is_file() or path.stat().st_size != row["bytes"] \
                or _hash_file(path) != row["sha256"]:
            self._mark_artifact_unavailable(row["artifact_id"], report_id)
            return None
        return CachedHit(row["artifact_id"], path, row["sha256"], row["bytes"])

    def _mark_artifact_unavailable(self, artifact_id: str, report_id: str) -> None:
        conn = self.connection()
        with conn:
            conn.execute(
                "UPDATE artifacts SET state='unavailable', updated_at=? "
                "WHERE artifact_id=? AND state='ready'",
                (_utcnow(), artifact_id))
            other_ready = conn.execute(
                "SELECT COUNT(*) AS n FROM artifacts "
                "WHERE report_id=? AND state='ready' AND artifact_id!=?",
                (report_id, artifact_id)).fetchone()["n"]
            if not other_ready:
                conn.execute(
                    "UPDATE manifest SET status='failed', current_artifact_id=NULL, "
                    "updated_at=? WHERE report_id=?",
                    (_utcnow(), report_id))

    # ------------------------------------------------------------- 下载事务

    def register_download(self, report_id: str) -> str:
        """登记下载尝试：创建 registered intent。

        首次归档/修复 → manifest 标 downloading；刷新已有有效版本
        （refresh，current artifact ready）→ manifest 保持 done，
        失败只记任务尝试、不降级已有有效内容（DESIGN §11.3 步骤 2）。
        """
        attempt_id = uuid.uuid4().hex
        now = _utcnow()
        conn = self.connection()
        with conn:
            conn.execute(
                """UPDATE manifest SET
                       status=CASE WHEN EXISTS(
                           SELECT 1 FROM artifacts a
                           WHERE a.artifact_id = manifest.current_artifact_id
                             AND a.state='ready')
                       THEN 'done' ELSE 'downloading' END,
                       updated_at=?
                   WHERE report_id=? AND status IN ('discovered','failed','done')""",
                (now, report_id))
            conn.execute(
                """INSERT INTO archive_intents(
                       attempt_id, report_id, state, created_at, updated_at)
                   VALUES(?,?, 'registered', ?, ?)""",
                (attempt_id, report_id, now, now))
        return attempt_id

    def commit_file(self, report_id: str, report: Report,
                    downloaded: DownloadedFile, attempt_id: str) -> CommitOutcome:
        """唯一归档提交方：临时文件 → 短事务登记 → 原子改名 → 短事务标记。

        - 同 report+sha 已有版本（ready/unavailable）→ 复用该 artifact
          （unavailable 同时恢复 ready，复用其 ID 修复原文件）；
        - 目标路径含 report_id/artifact_id，不因标题碰撞覆盖不同内容。
        """
        sha = downloaded.sha256
        artifact_id = compute_artifact_id(report_id, sha)
        temp = Path(downloaded.temp_path)
        if not temp.is_file():
            raise StoreError("临时文件不存在，无法提交",
                             detail=f"temp={downloaded.temp_path}")
        ext = "pdf" if downloaded.media_type == "application/pdf" else "html"
        rel_path = self._target_rel(report, report_id, artifact_id, ext)
        target = self.root / rel_path
        now = _utcnow()
        conn = self.connection()

        # 短事务 1：登记 staged artifact 与完整 intent
        with conn:
            existing = conn.execute(
                "SELECT artifact_id, state FROM artifacts "
                "WHERE report_id=? AND sha256=?", (report_id, sha)).fetchone()
            reused = existing is not None
            prior_state = existing["state"] if existing is not None else None
            if existing is None:
                conn.execute(
                    """INSERT INTO artifacts(
                           artifact_id, report_id, sha256, bytes, media_type,
                           local_path, source_url, final_url, fetched_at,
                           state, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,'staged',?,?)""",
                    (artifact_id, report_id, sha, downloaded.bytes,
                     downloaded.media_type, str(target), report.source_url,
                     downloaded.final_url, now, now, now))
            else:
                artifact_id = existing["artifact_id"]
                conn.execute(
                    """UPDATE artifacts SET
                           bytes=?, media_type=?, local_path=?, source_url=?,
                           final_url=?, fetched_at=?, state='staged', updated_at=?
                       WHERE artifact_id=?""",
                    (downloaded.bytes, downloaded.media_type, str(target),
                     report.source_url, downloaded.final_url, now, now,
                     artifact_id))
            conn.execute(
                """UPDATE archive_intents SET
                       artifact_id=?, temp_path=?, target_rel_path=?,
                       expected_sha256=?, expected_bytes=?, state='staged',
                       updated_at=?
                   WHERE attempt_id=?""",
                (artifact_id, str(temp), rel_path, sha, downloaded.bytes, now,
                 attempt_id))

        # 原子改名（同卷 .tmp → 目标）：不覆盖合法在库的不同内容
        # （artifact_id 由内容派生，同名异容仅可能来自外部篡改或损坏现场）
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _hash_file(target) == sha:
                temp.unlink(missing_ok=True)
            elif prior_state in ("unavailable", "staged", None):
                # 修复路径：目标属已判损坏/未完成现场，以已校验临时文件覆盖
                temp.replace(target)
            else:
                raise StoreError(
                    "目标路径已存在且内容不同，拒绝覆盖（ready 版本被外部改动？）",
                    detail=f"target={rel_path} prior_state={prior_state}")
        else:
            temp.replace(target)

        self._finalize_commit(report_id, artifact_id, attempt_id, rel_path)
        return CommitOutcome(artifact_id, rel_path, reused)

    def _finalize_commit(self, report_id: str, artifact_id: str,
                         attempt_id: str, rel_path: str) -> None:
        now = _utcnow()
        conn = self.connection()
        with conn:
            # local_path 统一在此回写：恢复路径（改名后/改名前崩溃）同样收敛
            conn.execute(
                """UPDATE artifacts SET state='ready', local_path=?, updated_at=?
                   WHERE artifact_id=?""",
                (str(self.root / rel_path), now, artifact_id))
            conn.execute(
                "UPDATE archive_intents SET state='committed', updated_at=? "
                "WHERE attempt_id=?", (now, attempt_id))
            conn.execute(
                """UPDATE manifest SET status='done', current_artifact_id=?,
                       updated_at=? WHERE report_id=?""",
                (artifact_id, now, report_id))

    def mark_failed(self, report_id: str, code: str, detail: str | None = None) -> None:
        """单文件失败：仅当无其他有效版本时置 failed；不得降级已有 done 内容。"""
        conn = self.connection()
        now = _utcnow()
        with conn:
            row = conn.execute(
                "SELECT current_artifact_id FROM manifest WHERE report_id=?",
                (report_id,)).fetchone()
            if row is None:
                return
            if row["current_artifact_id"]:
                ready = conn.execute(
                    """SELECT COUNT(*) AS n FROM artifacts
                       WHERE artifact_id=? AND state='ready'""",
                    (row["current_artifact_id"],)).fetchone()["n"]
                if ready:
                    return  # 已有有效版本，失败只记日志不动状态
            conn.execute(
                "UPDATE manifest SET status='failed', updated_at=? "
                "WHERE report_id=? AND status='downloading'",
                (now, report_id))
            conn.execute(
                """UPDATE archive_intents SET state='failed', updated_at=?
                   WHERE report_id=? AND state IN ('registered','staged')""",
                (now, report_id))
            meta = json.loads(
                (conn.execute("SELECT source_metadata_json FROM manifest "
                              "WHERE report_id=?",
                              (report_id,)).fetchone() or {"source_metadata_json": "{}"}
                 )["source_metadata_json"] or "{}")
            meta["last_error"] = {"code": code, "detail": detail, "at": now}
            conn.execute(
                "UPDATE manifest SET source_metadata_json=?, updated_at=? "
                "WHERE report_id=?",
                (json.dumps(meta, ensure_ascii=False), now, report_id))

    # ------------------------------------------------------------- 路径

    def _target_rel(self, report: Report, report_id: str, artifact_id: str,
                    ext: str) -> str:
        """构建归档相对路径；ID 始终参与文件名（不靠标题唯一）。"""
        market = sanitize_component(report.market.value, 8)
        symbol = sanitize_component(report.symbol, 16)
        title = sanitize_component(report.title, _MAX_TITLE_LEN)
        period = sanitize_component(period_or_unknown(report.report_period), 10)
        doc_type = sanitize_component(report.doc_type, 16)
        ids = f"{report_id}__{artifact_id}"
        if self.layout == "flat":
            name = f"{period}__{doc_type}__{title}__{ids}.{ext}"
            rel = f"{market}/{symbol}/{name}"
        else:
            name = f"{doc_type}__{title}__{ids}.{ext}"
            rel = f"{market}/{symbol}/{period}/{name}"
        if len(str(self.root / rel)) > _MAX_PATH_LEN:
            # 先收紧标题再试一次；仍超限则拒绝（out 配置过长，见 config 校验）
            title = sanitize_component(report.title, 20)
            if self.layout == "flat":
                name = f"{period}__{doc_type}__{title}__{ids}.{ext}"
                rel = f"{market}/{symbol}/{name}"
            else:
                name = f"{doc_type}__{title}__{ids}.{ext}"
                rel = f"{market}/{symbol}/{period}/{name}"
            if len(str(self.root / rel)) > _MAX_PATH_LEN:
                raise StoreError(
                    "目标路径超出长度预算，拒绝提交", detail=f"rel={rel}")
        return rel

    # ------------------------------------------------------------- 查询

    def archived_source_ids(self, market: str, symbol: str) -> dict[str, str]:
        conn = self.connection()
        return {
            r["source_id"]: r["report_id"]
            for r in conn.execute(
                "SELECT source_id, report_id FROM manifest "
                "WHERE market=? AND symbol=? AND status='done'",
                (market, symbol))}

    def manifest_rows(self, market: str | None = None,
                      symbol: str | None = None) -> list[dict]:
        sql = ("SELECT report_id, market, symbol, source_id, title, doc_type, "
               "source_form, filing_date, report_period, period_source, "
               "language, document_role, is_amendment, status, "
               "current_artifact_id FROM manifest")
        conditions, params = [], []
        if market:
            conditions.append("market=?")
            params.append(market)
        if symbol:
            conditions.append("symbol=?")
            params.append(symbol)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY market, symbol, report_period DESC"
        conn = self.connection()
        return [dict(r) for r in conn.execute(sql, params)]

    def artifact_row(self, artifact_id: str) -> dict | None:
        conn = self.connection()
        row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        return dict(row) if row else None

    def open_artifact(self, artifact_id: str) -> tuple[Path, dict]:
        """按 ID 读取已归档文件（路径边界：仅限本归档根目录内）。"""
        row = self.artifact_row(artifact_id)
        if row is None or row["state"] != ArtifactState.READY.value:
            raise FileNotAvailableError("artifact 不存在或不可用",
                                        detail=f"artifact_id={artifact_id}")
        path = Path(row["local_path"]).resolve()
        root = self.root.resolve()
        if root not in path.parents:
            raise FileNotAvailableError("路径越界拒绝读取",
                                        detail=f"artifact_id={artifact_id}")
        if not path.is_file() or _hash_file(path) != row["sha256"]:
            raise FileNotAvailableError("文件缺失或校验不一致",
                                        detail=f"artifact_id={artifact_id}")
        return path, row

    # ------------------------------------------------------------- symbol_map

    def upsert_symbol(self, resolved: ResolvedSymbol, *, ttl_days: int = 7) -> None:
        now = dt.datetime.now(dt.UTC)
        expires = now + dt.timedelta(days=ttl_days)
        conn = self.connection()
        with conn:
            conn.execute(
                """INSERT INTO symbol_map(
                       market, symbol, source_issuer_id, display_name, exchange,
                       raw_inputs_json, updated_at, expires_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(market, symbol) DO UPDATE SET
                       source_issuer_id=excluded.source_issuer_id,
                       display_name=excluded.display_name,
                       exchange=excluded.exchange,
                       raw_inputs_json=excluded.raw_inputs_json,
                       updated_at=excluded.updated_at,
                       expires_at=excluded.expires_at""",
                (resolved.market.value, resolved.symbol, resolved.source_issuer_id,
                 resolved.display_name, resolved.exchange,
                 json.dumps(resolved.raw_inputs, ensure_ascii=False),
                 now.isoformat(timespec="seconds"),
                 expires.isoformat(timespec="seconds")))
