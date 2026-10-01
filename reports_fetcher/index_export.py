"""平面索引导出：INDEX.csv（机器/Excel）+ INDEX.md（人读），落在归档根目录。

目的（docs/ARCHIVE_LAYOUT.md §6 的补充）：知识库工具与人工都能**不写 SQL**
快速定位文档。由服务在每次任务终态后 best-effort 刷新（jobs.run_job /
cli fetch 收尾），也可独立运行：

    python -m reports_fetcher.index_export [归档根目录]

- INDEX.csv：全部 ready 内容版本，UTF-8 **带 BOM**（Excel 直接打开中文不乱码）；
  含 is_current 列——只取当前版按该列过滤=1。
- INDEX.md：按 市场/代码 分组、仅当前版，含公司简称（symbol_map），
  供 Ctrl+F 快速人肉定位。
- 读库用只读连接（WAL 下与写入方并发安全）；导出失败不影响抓取任务。
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CSV_NAME = "INDEX.csv"
MD_NAME = "INDEX.md"

_CSV_COLUMNS = [
    "market", "symbol", "display_name", "doc_type", "report_period",
    "period_source", "filing_date", "language", "title", "report_id",
    "artifact_id", "is_current", "sha256", "bytes", "media_type",
    "path", "fetched_at",
]

_DOC_TYPE_LABELS = {
    "Q1": "一季报", "H1": "半年报", "Q3": "三季报", "FY": "年报",
    "ANNUAL": "年报", "INTERIM": "中报", "QTR-HK": "季度业绩",
    "10-Q": "季报", "10-K": "年报", "20-F": "年报(20-F)",
}


def _relative_path(local_path: str, market: str) -> str:
    """容器内绝对路径 → 归档内相对路径（按 /{market}/ 段定位，布局见 §1）。"""
    marker = f"/{market}/"
    idx = local_path.replace("\\", "/").rfind(marker)
    if idx < 0:  # pragma: no cover - 防御：异常路径原样返回
        return local_path
    return local_path[idx + 1:]


def export_index(root: Path) -> dict:
    """生成/覆盖 INDEX.csv 与 INDEX.md；返回统计。root 为归档根目录。"""
    root = Path(root)
    db_path = root / "archive.sqlite3"
    if not db_path.is_file():
        return {"reports": 0, "artifacts": 0}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT m.market, m.symbol, m.doc_type, m.report_period,
                      m.period_source, m.filing_date, m.language, m.title,
                      m.report_id, a.artifact_id, a.sha256, a.bytes,
                      a.media_type, a.local_path, a.fetched_at,
                      CASE WHEN a.artifact_id = m.current_artifact_id
                           THEN 1 ELSE 0 END AS is_current,
                      s.display_name
               FROM artifacts a
               JOIN manifest m ON m.report_id = a.report_id
               LEFT JOIN symbol_map s
                 ON s.market = m.market AND s.symbol = m.symbol
               WHERE a.state = 'ready' AND m.status = 'done'
               ORDER BY m.market, m.symbol, m.report_period DESC,
                        is_current DESC, a.fetched_at DESC"""
        ).fetchall()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        n_reports = len({r["report_id"] for r in rows})
        n_artifacts = len(rows)

        # ---- INDEX.csv（全版本，Excel 友好）----
        csv_path = root / CSV_NAME
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(_CSV_COLUMNS)
            for r in rows:
                writer.writerow([
                    r["market"], r["symbol"], r["display_name"] or "",
                    r["doc_type"], r["report_period"] or "unknown",
                    r["period_source"], r["filing_date"] or "",
                    r["language"] or "", r["title"] or "",
                    r["report_id"], r["artifact_id"], r["is_current"],
                    r["sha256"], r["bytes"], r["media_type"],
                    _relative_path(r["local_path"], r["market"]),
                    r["fetched_at"],
                ])

        # ---- INDEX.md（当前版，按 市场/代码 分组）----
        md_path = root / MD_NAME
        lines = [
            "# 财报归档索引（自动生成，勿手工编辑）",
            "",
            f"生成时间：{now} ｜ 报告 {n_reports} 份"
            f" ｜ 文件版本 {n_artifacts} 个（当前版以 is_current=1 为准，"
            f"全版本见 {CSV_NAME}；字段说明见 README.md）",
            "",
        ]
        current = [r for r in rows if r["is_current"]]
        by_symbol: dict[tuple, list] = {}
        for r in current:
            by_symbol.setdefault((r["market"], r["symbol"], r["display_name"] or ""),
                                 []).append(r)
        for (market, symbol, name), members in by_symbol.items():
            header = f"## {market} / {symbol}"
            if name:
                header += f"（{name}）"
            lines += [header, "",
                      "| 报告期 | 类型 | 公告日 | 语言 | 标题 | 文件 |",
                      "|---|---|---|---|---|---|"]
            for r in members:
                period = r["report_period"] or "unknown"
                dtype = _DOC_TYPE_LABELS.get(r["doc_type"], r["doc_type"])
                title = (r["title"] or "").replace("|", "\\|")
                fname = _relative_path(r["local_path"], market).split("/", 2)[-1]
                filed = r["filing_date"] or "?"
                lang = r["language"] or "?"
                lines.append(
                    f"| {period} | {dtype}({r['doc_type']}) | {filed} "
                    f"| {lang} | {title} | {fname} |")
            lines.append("")
        md_path.write_text("\n".join(lines), encoding="utf-8")
        return {"reports": n_reports, "artifacts": n_artifacts}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    import sys
    root = Path(argv[0]) if argv else Path(".")
    stats = export_index(root)
    print(f"{CSV_NAME}/{MD_NAME} -> {root} : {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
