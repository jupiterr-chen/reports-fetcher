"""配置加载（DESIGN §13）：CLI 显式参数 > 环境变量 > 配置文件 > 默认值。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from reports_fetcher import __version__
from reports_fetcher.models import ConfigError

DEFAULT_CONFIG_FILENAME = "config.toml"
_MAX_ROOT_PATH_LEN = 120  # 归档根目录绝对路径长度预算（Windows MAX_PATH 安全余量）


@dataclass
class HttpConfig:
    user_agent: str = ""
    max_file_bytes: int = 209_715_200          # 200 MiB
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 120.0
    file_deadline_seconds: float = 600.0
    max_retries: int = 3
    source_intervals: dict[str, float] = field(
        default_factory=lambda: {"sec": 0.13, "cninfo": 0.5, "hkex": 0.3})


@dataclass
class FetchConfig:
    last_n: int = 4
    market_workers: int = 3
    max_lookback_years: int = 10
    max_discovery_requests_per_symbol: int = 100
    default_forms: dict[str, list[str]] = field(default_factory=lambda: {
        "CN": ["Q1", "H1", "Q3", "FY"],
        "HK": ["ANNUAL", "INTERIM"],
        "US": ["10-Q", "10-K", "20-F"],
    })


@dataclass
class LogConfig:
    level: str = "INFO"
    file: str = ""


@dataclass
class GeneralConfig:
    out_dir: str = "./reports"
    layout: str = "flat"


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    log: LogConfig = field(default_factory=LogConfig)


def _merge_into(target: dict, source: dict) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_into(target[key], value)
        else:
            target[key] = value


def _coerce(data: dict) -> Config:
    try:
        cfg = Config()
        g = data.get("general", {})
        cfg.general.out_dir = str(g.get("out_dir", cfg.general.out_dir))
        cfg.general.layout = str(g.get("layout", cfg.general.layout))
        h = data.get("http", {})
        cfg.http.user_agent = str(h.get("user_agent", "")).strip()
        cfg.http.max_file_bytes = int(h.get("max_file_bytes", cfg.http.max_file_bytes))
        cfg.http.connect_timeout_seconds = float(
            h.get("connect_timeout_seconds", cfg.http.connect_timeout_seconds))
        cfg.http.read_timeout_seconds = float(
            h.get("read_timeout_seconds", cfg.http.read_timeout_seconds))
        cfg.http.file_deadline_seconds = float(
            h.get("file_deadline_seconds", cfg.http.file_deadline_seconds))
        cfg.http.max_retries = int(h.get("max_retries", cfg.http.max_retries))
        cfg.http.source_intervals = {
            **cfg.http.source_intervals,
            **{k: float(v) for k, v in h.get("source_intervals", {}).items()},
        }
        f = data.get("fetch", {})
        cfg.fetch.last_n = int(f.get("last_n", cfg.fetch.last_n))
        cfg.fetch.market_workers = int(f.get("market_workers", cfg.fetch.market_workers))
        cfg.fetch.max_lookback_years = int(
            f.get("max_lookback_years", cfg.fetch.max_lookback_years))
        cfg.fetch.max_discovery_requests_per_symbol = int(
            f.get("max_discovery_requests_per_symbol",
                  cfg.fetch.max_discovery_requests_per_symbol))
        cfg.fetch.default_forms = {
            **cfg.fetch.default_forms,
            **{k: [str(x) for x in v] for k, v in f.get("default_forms", {}).items()},
        }
        lg = data.get("log", {})
        cfg.log.level = str(lg.get("level", cfg.log.level)).upper()
        cfg.log.file = str(lg.get("file", cfg.log.file))
        return cfg
    except (TypeError, ValueError) as e:
        raise ConfigError(f"配置字段类型不合法: {e}") from e


def _validate(cfg: Config) -> None:
    if cfg.general.layout not in ("flat", "nested"):
        raise ConfigError("general.layout 仅支持 flat / nested",
                          detail=f"layout={cfg.general.layout}")
    if not cfg.general.out_dir:
        raise ConfigError("general.out_dir 不能为空")
    if len(str(Path(cfg.general.out_dir).resolve())) > _MAX_ROOT_PATH_LEN:
        raise ConfigError(
            f"归档根目录绝对路径过长（>{_MAX_ROOT_PATH_LEN} 字符），"
            "路径不足以容纳受控文件名，请改用更短的 out_dir",
            detail=f"out_dir={cfg.general.out_dir}")
    if not (1 <= cfg.fetch.last_n <= 20):
        raise ConfigError("fetch.last_n 范围为 1-20", detail=f"last_n={cfg.fetch.last_n}")
    if not (1 <= cfg.fetch.market_workers <= 3):
        raise ConfigError("fetch.market_workers 范围为 1-3")
    if cfg.http.max_retries < 0 or cfg.http.max_retries > 5:
        raise ConfigError("http.max_retries 范围为 0-5")
    if cfg.http.max_file_bytes <= 0:
        raise ConfigError("http.max_file_bytes 必须为正")
    for name in ("connect_timeout_seconds", "read_timeout_seconds",
                 "file_deadline_seconds"):
        if getattr(cfg.http, name) <= 0:
            raise ConfigError(f"http.{name} 必须为正")
    for group, interval in cfg.http.source_intervals.items():
        if interval < 0:
            raise ConfigError("http.source_intervals 不能为负",
                              detail=f"group={group}")
    if cfg.log.level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("log.level 仅支持 DEBUG/INFO/WARNING/ERROR")
    for market, forms in cfg.fetch.default_forms.items():
        if market not in ("CN", "HK", "US"):
            raise ConfigError("fetch.default_forms 键仅支持 CN/HK/US",
                              detail=f"market={market}")
        if not forms:
            raise ConfigError("fetch.default_forms 不能为空列表",
                              detail=f"market={market}")


def _apply_env(cfg: Config) -> None:
    """敏感项经环境变量注入（DESIGN §13）。配置文件已显式给出 UA 时优先。"""
    email = os.environ.get("SEC_UA_EMAIL", "").strip()
    if email and not cfg.http.user_agent:
        cfg.http.user_agent = f"reports-fetcher/{__version__} ({email})"


def load_config(path: str | None = None) -> Config:
    """加载配置。显式指定 --config 而文件缺失时明确报错（不静默用默认值）。"""
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"配置文件不存在: {path}")
        data: dict = {}
        if p.stat().st_size > 0:
            try:
                data = tomllib.loads(p.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f"配置文件解析失败: {e}") from e
    else:
        p = Path(DEFAULT_CONFIG_FILENAME)
        data = {}
        if p.is_file() and p.stat().st_size > 0:
            try:
                data = tomllib.loads(p.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f"配置文件解析失败: {e}") from e

    cfg = _coerce(data)
    _apply_env(cfg)
    _validate(cfg)
    return cfg
