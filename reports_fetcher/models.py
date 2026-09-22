"""统一数据模型与稳定错误码（DESIGN §3/§4）。

数据库与 API 可采用不同结构，但必须与本模块无损映射。
可未知字段统一用 None，不混用空字符串、0 或占位日期。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Market(str, Enum):
    CN = "CN"
    HK = "HK"
    US = "US"


class PeriodSource(str, Enum):
    """报告期可信级别（REQUIREMENTS §3）。

    DOCUMENT = 从已归档原文（HK PDF）中提取到明确期末日；仅在报告期仍未知
    时启用，且绝不覆盖既有的可信非 unknown 报告期。
    """

    SOURCE_FIELD = "source_field"
    EXPLICIT_TITLE = "explicit_title"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


class DocumentRole(str, Enum):
    """文档完整性角色；不明确的完整性不能伪装 full_report。"""

    FULL_REPORT = "full_report"
    AMENDMENT_FULL = "amendment_full"
    NOTICE = "notice"
    SUMMARY = "summary"
    UNKNOWN = "unknown"


class ReportStatus(str, Enum):
    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"


class ArtifactState(str, Enum):
    STAGED = "staged"
    READY = "ready"
    UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------- 错误体系

class ReportsFetcherError(Exception):
    """所有业务异常的基类；code 是对外的稳定错误码（DESIGN §4）。"""

    code = "internal_error"

    def __init__(self, message: str = "", *, code: str | None = None,
                 detail: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 展示辅助
        if self.detail:
            return f"{super().__str__()} ({self.detail})"
        return super().__str__()


class ConfigError(ReportsFetcherError):
    code = "config_invalid"


class UaNotConfiguredError(ConfigError):
    """SEC 合规要求真实 User-Agent（NFR-5）：未配置时 US 功能明确报错。"""

    code = "ua_not_configured"


class SymbolError(ReportsFetcherError):
    code = "invalid_symbol"


class UnsupportedMarketSegmentError(SymbolError):
    """北交所等未支持市场段，不静默落到其他市场查询。"""

    code = "unsupported_market_segment"


class UnsupportedMarketError(ReportsFetcherError):
    """市场整体尚未提供适配器（I1 仅有 US；CN/HK 在 I2/I3 落地）。"""

    code = "unsupported_market"


class UnsupportedFormError(ReportsFetcherError):
    code = "unsupported_form"


class ResolveError(ReportsFetcherError):
    code = "symbol_not_found"


class AmbiguousSymbolError(ResolveError):
    code = "ambiguous_symbol"


class DataSourceError(ReportsFetcherError):
    """来源侧错误（网络/风控/契约变化），不能伪装成空结果。"""

    code = "source_unavailable"


class SourceUnavailableError(DataSourceError):
    code = "source_unavailable"


class SourceRateLimitedError(DataSourceError):
    code = "source_rate_limited"


class SourceContractChangedError(DataSourceError):
    code = "source_contract_changed"


class UnexpectedStatusError(DataSourceError):
    """非 2xx 且不重试的响应；由调用方（适配器）解释语义。"""

    code = "source_unavailable"

    def __init__(self, message: str = "", *, status: int = 0,
                 content_type: str = "", body_head: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.content_type = content_type
        self.body_head = body_head


class DownloadError(ReportsFetcherError):
    code = "download_invalid"


class DownloadInvalidError(DownloadError):
    code = "download_invalid"


class DownloadTooLargeError(DownloadError):
    code = "download_too_large"


class StoreError(ReportsFetcherError):
    code = "store_unavailable"


class FileNotAvailableError(StoreError):
    code = "file_not_available"


# ---------------------------------------------------------------- 数据契约

@dataclass
class NormalizedSymbol:
    """本地规范化结果：只识别市场，不判定有效性（FR-1）。"""

    market: Market
    symbol: str                 # 规范化后的代码（HK 补零 5 位、US 大写等）
    raw: str                    # 原始输入
    exchange_hint: str | None = None  # 显式交易所信息（SH/SZ 等），仅供 resolve 参考


@dataclass
class ResolvedSymbol:
    market: Market
    symbol: str
    raw_inputs: list[str] = field(default_factory=list)
    display_name: str | None = None
    exchange: str | None = None
    source_issuer_id: str | None = None   # orgId / stockId / 补零 CIK
    issuer_id: str | None = None          # 带来源命名空间，如 "sec:CIK0000320193"


@dataclass
class Report:
    """候选报告的统一字段契约（数据库与 API 必须无损映射）。"""

    market: Market
    symbol: str
    source_id: str              # 去重键组成：SEC 为 accessionNumber/primaryDocument
    source_url: str
    title: str
    doc_type: str               # 基础类型（10-Q/10-K/20-F/Q1/H1/Q3/FY/ANNUAL/INTERIM）
    source_form: str            # 来源原始类型（如 10-K/A）
    filing_date: str | None     # ISO 日期或 None
    report_period: str | None   # ISO 期末日或 None；未知即 None，禁止猜测
    period_source: PeriodSource
    language: str
    document_role: DocumentRole
    is_amendment: bool = False
    revision_of: str | None = None      # 可确认时的基础版本 source_id
    source_issuer_id: str | None = None
    source_metadata: dict = field(default_factory=dict)

    def acceptance_key(self) -> str:
        """组内版本排序用的来源接受时间线索（缺失时退化为空串）。"""
        return str(self.source_metadata.get("acceptanceDateTime") or "")


@dataclass
class ReportQuery:
    last_n: int = 4
    forms: list[str] | None = None       # None → 使用市场默认
    languages: list[str] | None = None   # None → 使用市场默认偏好
    max_lookback_years: int = 10
    max_discovery_requests: int = 100


@dataclass
class DiscoveryResult:
    reports: list[Report]                # 通过类型筛选的全部规范化候选
    requested_count: int
    selected_count: int = 0              # 统一选择函数回填
    searched_from: str | None = None
    searched_to: str | None = None
    exhausted: bool = True               # 声明检索窗口是否穷尽
    truncated: bool = False              # 预算耗尽必须显式上报
    warnings: list[str] = field(default_factory=list)


@dataclass
class DownloadedFile:
    """已校验的临时文件；仅代表下载成功，不代表归档成功。"""

    temp_path: str
    sha256: str
    bytes: int
    media_type: str              # 已验证类型：application/pdf | text/html
    final_url: str


# ---------------------------------------------------------------- 抓取结果

OUTCOME_DOWNLOADED = "downloaded"
OUTCOME_CACHED = "cached"
OUTCOME_FAILED = "failed"


@dataclass
class FetchItem:
    report_id: str
    source_id: str
    outcome: str                 # downloaded / cached / failed
    error: str | None = None     # 稳定错误码
    detail: str | None = None
    local_path: str | None = None


@dataclass
class FetchResult:
    market: Market
    symbol: str
    status: str                  # ok / partial / failed / empty
    items: list[FetchItem] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)   # 影响完整性的质量警告
    notices: list[str] = field(default_factory=list)    # 说明性信息（正常截取、语言选择），不降级状态
    error: str | None = None     # 证券级失败的稳定错误码
    display_name: str | None = None

    @property
    def has_usable(self) -> bool:
        return any(i.outcome != OUTCOME_FAILED for i in self.items)


@dataclass
class SymbolPreviewItem:
    """list 命令的预览条目（不下载原文）。"""

    source_id: str
    report_id: str | None
    doc_type: str
    report_period: str | None
    filing_date: str | None
    title: str
    source_url: str
    archived: bool               # manifest 中已有 done 记录


@dataclass
class SymbolPreview:
    market: Market
    symbol: str
    status: str                  # ok / empty / failed
    display_name: str | None = None
    items: list[SymbolPreviewItem] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
