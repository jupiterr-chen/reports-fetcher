"""市场适配器契约（DESIGN §5）。

适配器负责：resolve（源站精确匹配）与 list_reports（获取足够候选并规范化）。
分组、版本选择、排序和截断由统一选择函数（reports_fetcher.selection）负责。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from reports_fetcher.config import Config
from reports_fetcher.downloader import Transport
from reports_fetcher.models import (
    DiscoveryResult,
    Market,
    NormalizedSymbol,
    ReportQuery,
    ResolvedSymbol,
)


class BaseMarketAdapter(ABC):
    market: ClassVar[Market]
    base_forms: ClassVar[set[str]]
    default_language_preference: ClassVar[list[str]]
    source_group: ClassVar[str]  # 传输层来源组名（限速预算合并查询与文件域名）

    def __init__(self, transport: Transport, config: Config) -> None:
        self.transport = transport
        self.config = config

    @abstractmethod
    def resolve(self, normalized: NormalizedSymbol) -> ResolvedSymbol:
        """源站精确匹配确认市场/证券类型与代码；不接受搜索结果模糊命中。"""

    @abstractmethod
    def list_reports(self, symbol: ResolvedSymbol,
                     query: ReportQuery) -> DiscoveryResult:
        """按声明检索窗口获取候选并规范化为统一 Report 契约。"""

    def download_headers(self, report) -> dict[str, str]:
        """文件下载所需请求头（如 SEC 的 User-Agent）；默认空。"""
        return {}

    def default_forms(self) -> list[str]:
        market_forms = self.config.fetch.default_forms.get(self.market.value)
        return list(market_forms) if market_forms else sorted(self.base_forms)
