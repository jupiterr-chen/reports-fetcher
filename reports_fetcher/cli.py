"""CLI 入口（DESIGN §12，FR-7）：fetch / list / version（serve 在 I5 提供）。

退出码（DESIGN §4）：
- 0：成功且无警告/缺口；或纯空查询（no_reports）正常结束；
- 1：有可用结果且存在警告/缺口；
- 2：全无结果且有执行错误；参数/配置错误。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from reports_fetcher import __version__
from reports_fetcher.config import Config, load_config
from reports_fetcher.core import BatchResult, FetchService
from reports_fetcher.models import (
    OUTCOME_CACHED,
    OUTCOME_DOWNLOADED,
    OUTCOME_FAILED,
    ReportsFetcherError,
)

logger = logging.getLogger("reports_fetcher.cli")

_OUTCOME_LABELS = {
    OUTCOME_DOWNLOADED: "downloaded",
    OUTCOME_CACHED: "cached",
    OUTCOME_FAILED: "failed",
}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None, metavar="PATH",
                        help="配置文件路径（默认读取 ./config.toml）")
    common.add_argument("--verbose", action="store_true",
                        help="输出 DEBUG 诊断日志（凭据仍脱敏）")

    parser = argparse.ArgumentParser(
        prog="reports-fetcher",
        description="A股/港股/美股定期财报原文获取与归档（I1：US 先行）")
    parser.add_argument("--config", default=None, metavar="PATH")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_p = sub.add_parser("fetch", parents=[common],
                             help="抓取并归档最新 N 份定期报告")
    fetch_p.add_argument("symbols", nargs="*", default=[], metavar="SYMBOL")
    fetch_p.add_argument("--file", default=None, metavar="PATH",
                         help="从文件读取代码（每行一个，# 注释）")
    fetch_p.add_argument("--last", type=int, default=None, metavar="N",
                         help="最新 N 个逻辑报告组（默认 4，范围 1-20）")
    fetch_p.add_argument("--forms", default=None, metavar="A,B",
                         help="基础类型白名单（单市场任务；如 10-K,10-Q）")
    fetch_p.add_argument("--out", default=None, metavar="PATH",
                         help="归档根目录（默认取配置）")
    fetch_p.add_argument("--layout", choices=["flat", "nested"], default=None,
                         help="归档布局（默认取配置）")
    fetch_p.add_argument("--market-workers", type=int, default=None, metavar="N",
                         help="下载并行度 1-3（不提升同源限速预算）")
    fetch_p.add_argument("--refresh", action="store_true",
                         help="重下候选并保留旧内容版本（默认跳过已有）")

    list_p = sub.add_parser("list", parents=[common],
                            help="联网预览元数据与覆盖信息（不下载原文）")
    list_p.add_argument("symbols", nargs="*", default=[], metavar="SYMBOL")
    list_p.add_argument("--file", default=None, metavar="PATH")
    list_p.add_argument("--last", type=int, default=None, metavar="N")
    list_p.add_argument("--forms", default=None, metavar="A,B")

    sub.add_parser("version", parents=[common], help="输出版本号")
    return parser


def _setup_logging(config: Config, verbose: bool) -> None:
    level = "DEBUG" if verbose else config.log.level
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if config.log.file:
        handlers.append(logging.FileHandler(config.log.file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True)


def _read_symbols(args: argparse.Namespace) -> list[str]:
    symbols = [s for s in (args.symbols or []) if s and s.strip()]
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            raise ConfigError(f"--file 文件不存在: {args.file}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                symbols.append(line)
    if not symbols:
        raise ConfigError("未提供任何证券代码（位置参数或 --file 二选一）")
    return symbols


def _parse_forms(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    forms = [f.strip().upper() for f in raw.split(",") if f.strip()]
    return forms or None


def _validate_args(args: argparse.Namespace) -> None:
    if getattr(args, "last", None) is not None and not (1 <= args.last <= 20):
        raise ConfigError("--last 范围为 1-20", detail=f"last={args.last}")
    if getattr(args, "market_workers", None) is not None \
            and not (1 <= args.market_workers <= 3):
        raise ConfigError("--market-workers 范围为 1-3")


def _print_coverage(coverage: dict) -> None:
    if not coverage:
        return
    window = ""
    if coverage.get("searched_from"):
        window = f"，检索窗口 {coverage['searched_from']}..{coverage['searched_to']}"
    exhausted = "已穷尽" if coverage.get("exhausted") else "未穷尽"
    truncated = "，预算截断" if coverage.get("truncated") else ""
    print(f"  覆盖: {coverage.get('selected')}/{coverage.get('requested')} 个逻辑报告组"
          f"（共发现 {coverage.get('total_groups')}）{exhausted}{truncated}{window}")


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"  警告: {warning}")


def _cmd_fetch(service: FetchService, args: argparse.Namespace) -> int:
    symbols = _read_symbols(args)
    batch: BatchResult = service.fetch(
        symbols, last_n=args.last, forms=_parse_forms(args.forms),
        refresh=bool(getattr(args, "refresh", False)))
    for raw, error in batch.invalid:
        print(f"[invalid_symbol] {raw}: {error}")
    for result in batch.results:
        header = f"{result.market.value} {result.symbol}"
        if result.display_name:
            header += f"（{result.display_name}）"
        print(header)
        _print_coverage(result.coverage)
        for item in result.items:
            label = _OUTCOME_LABELS.get(item.outcome, item.outcome)
            if item.outcome == OUTCOME_FAILED:
                print(f"  [{label}] {item.source_id}: {item.error} {item.detail or ''}")
            else:
                note = f"（{item.detail}）" if item.detail else ""
                print(f"  [{label}] {item.source_id}{note} -> {item.local_path}")
        if result.status == "empty":
            print(f"  无匹配报告（no_reports）：{result.error}")
        if result.status == "failed":
            print(f"  失败: {result.error}")
        _print_warnings(result.warnings)

    # 退出码：有可用结果且存在警告/缺口 → 1；全无结果且有执行错误 → 2
    usable = any(r.status in ("ok", "partial") for r in batch.results)
    gaps = bool(batch.invalid) or any(
        r.status in ("partial", "failed") or r.warnings for r in batch.results)
    if not usable and gaps:
        return 2
    if gaps:
        return 1
    return 0


def _cmd_list(service: FetchService, args: argparse.Namespace) -> int:
    symbols = _read_symbols(args)
    previews, invalid = service.preview(
        symbols, last_n=args.last, forms=_parse_forms(args.forms))
    for raw, error in invalid:
        print(f"[invalid_symbol] {raw}: {error}")
    for preview in previews:
        header = f"{preview.market.value} {preview.symbol}"
        if preview.display_name:
            header += f"（{preview.display_name}）"
        print(header)
        _print_coverage(preview.coverage)
        for item in preview.items:
            mark = "已归档" if item.archived else "未归档"
            period = item.report_period or "unknown"
            print(f"  [{mark}] {period} {item.doc_type} {item.title} "
                  f"({item.filing_date or '未知公告日'}) {item.source_url}")
        if preview.status == "empty":
            print(f"  无匹配报告（no_reports）")
        if preview.status == "failed":
            print(f"  失败: {preview.error}")
        _print_warnings(preview.warnings)
    print("说明：list 已联网预览并更新本地 symbol_map 缓存（默认 7 天过期）；"
          "未下载任何原文。")
    usable = any(p.status == "ok" for p in previews)
    gaps = bool(invalid) or any(
        p.status == "failed" or p.warnings for p in previews)
    if not usable and gaps:
        return 2
    if gaps:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _validate_args(args)
        config = load_config(args.config)
        if args.command == "fetch":
            if args.out is not None:
                config.general.out_dir = args.out
            if args.layout is not None:
                config.general.layout = args.layout
            if args.market_workers is not None:
                config.fetch.market_workers = args.market_workers
        _setup_logging(config, args.verbose)
        if args.command == "version":
            print(f"reports-fetcher {__version__}")
            return 0
        service = FetchService.from_config(config)
        try:
            if args.command in ("fetch", "list"):
                # 归档根目录单写者锁（list 也写 symbol_map，DESIGN §11.4/§12）
                service.store.acquire_owner_lock()
            if args.command == "fetch":
                return _cmd_fetch(service, args)
            if args.command == "list":
                return _cmd_list(service, args)
        finally:
            service.close()
        parser.error(f"未知命令: {args.command}")  # pragma: no cover
    except ReportsFetcherError as e:
        print(f"错误 [{getattr(e, 'code', 'internal_error')}]: {e}",
              file=sys.stderr)
        return 2
    return 2  # pragma: no cover
