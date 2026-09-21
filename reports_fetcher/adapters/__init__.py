"""适配器注册表：市场 → 适配器。I3 起三市场齐备。"""
from __future__ import annotations

from reports_fetcher.config import Config
from reports_fetcher.downloader import Transport
from reports_fetcher.models import Market, UnsupportedMarketError
from reports_fetcher.adapters.base import BaseMarketAdapter
from reports_fetcher.adapters.cn_cninfo import CNCninfoAdapter
from reports_fetcher.adapters.hk_hkexnews import HKHkexnewsAdapter
from reports_fetcher.adapters.us_edgar import USEdgarAdapter

_ADAPTER_FACTORIES = {
    Market.US: USEdgarAdapter,
    Market.CN: CNCninfoAdapter,
    Market.HK: HKHkexnewsAdapter,
}

_PLANNED_ITERATIONS: dict = {}


def get_adapter(market: Market, transport: Transport,
                config: Config) -> BaseMarketAdapter:
    factory = _ADAPTER_FACTORIES.get(market)
    if factory is None:
        planned = _PLANNED_ITERATIONS.get(market, "后续迭代")
        raise UnsupportedMarketError(
            f"{market.value} 市场适配器尚未提供（计划 {planned} 落地）",
            detail=f"market={market.value}")
    return factory(transport, config)


def supported_markets() -> set[Market]:
    return set(_ADAPTER_FACTORIES)
