from __future__ import annotations

from typing import Iterable, Protocol

from app.config import Settings
from app.models.schemas import Kline, MinuteKline, OrderBook, PlateItem, ProviderCapability, Quote, StockConceptItem, StockInfo
from app.services.optional_providers import (
    AKShareProvider,
    BaoStockProvider,
    FutuProvider,
    LocalIndividualStockProvider,
    TushareProvider,
)
from app.services.providers import DemoMarketDataProvider, TencentMarketDataProvider


CAPABILITY_PROVIDER_ORDER = ("tencent", "akshare", "tushare", "baostock", "futu", "local", "demo")


class MarketProvider(Protocol):
    source_name: str

    async def quote(self, symbol: str) -> Quote:
        ...

    async def quotes(self, symbols: Iterable[str]) -> list[Quote]:
        ...

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
        ...

    async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
        ...


class StockPoolProvider(Protocol):
    source_name: str

    async def stock_pool(self) -> list[StockInfo]:
        ...


class PlateProvider(Protocol):
    source_name: str

    async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
        ...


class ConceptProvider(Protocol):
    source_name: str

    async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
        ...


class OrderBookProvider(Protocol):
    source_name: str

    async def order_book(self, symbol: str) -> OrderBook:
        ...


class CapabilityProvider(Protocol):
    def capability(self) -> ProviderCapability:
        ...


def build_providers(settings: Settings) -> dict[str, MarketProvider]:
    return {
        "tencent": TencentMarketDataProvider(),
        "akshare": AKShareProvider(),
        "baostock": BaoStockProvider(),
        "tushare": TushareProvider(),
        "futu": FutuProvider(
            host=settings.futu_host,
            port=settings.futu_port,
            enabled=settings.futu_enabled,
        ),
        "local": LocalIndividualStockProvider(),
        "demo": DemoMarketDataProvider(enabled=settings.demo_provider_enabled),
    }


def provider_priority(settings: Settings, providers: dict[str, MarketProvider], kind: str) -> list[tuple[int, str]]:
    mapping = {
        "quote": settings.quote_provider_priority,
        "kline": settings.kline_provider_priority,
        "minute": settings.minute_provider_priority,
        "stock": settings.stock_provider_priority,
        "plate": settings.plate_provider_priority,
        "concept": settings.plate_provider_priority,
    }
    configured_names = list(mapping[kind])
    if settings.demo_provider_enabled and kind in {"quote", "kline"} and "demo" not in configured_names:
        configured_names.append("demo")
    names = [name for name in configured_names if name in providers and provider_enabled_for(providers[name], kind)]
    return [(provider_index(settings, providers, name), name) for name in names]


def all_provider_names(settings: Settings, providers: dict[str, MarketProvider]) -> list[str]:
    names = []
    for group in (
        settings.quote_provider_priority,
        settings.kline_provider_priority,
        settings.stock_provider_priority,
        settings.plate_provider_priority,
        ("futu",),
        ("demo",),
    ):
        for name in group:
            if name in providers and name not in names:
                names.append(name)
    return names


def provider_index(settings: Settings, providers: dict[str, MarketProvider], name: str) -> int:
    try:
        return all_provider_names(settings, providers).index(name) + 1
    except ValueError:
        return 99


def provider_enabled_for(provider: MarketProvider, kind: str) -> bool:
    capability = provider_capability(provider)
    if capability is None:
        return True
    if not capability.enabled:
        return False
    if kind == "quote":
        return capability.realtime_quote
    if kind == "kline":
        return capability.daily_kline
    if kind == "minute":
        return capability.minute_kline
    if kind == "stock":
        return capability.stock_pool
    if kind == "plate":
        return capability.plate_rank
    if kind == "concept":
        return capability.concept_board
    if kind == "order_book":
        return capability.order_book
    return True


def provider_is_enabled(provider: MarketProvider) -> bool:
    capability = provider_capability(provider)
    return bool(capability.enabled) if capability else True


def provider_capabilities(providers: dict[str, MarketProvider]) -> list[ProviderCapability]:
    result = []
    for name in CAPABILITY_PROVIDER_ORDER:
        provider = providers.get(name)
        if provider is None:
            continue
        capability = provider_capability(provider)
        if capability:
            result.append(capability)
        else:
            result.append(
                ProviderCapability(
                    name="tencent",
                    installed=True,
                    enabled=True,
                    reliability_level="公开源",
                    realtime_quote=True,
                    daily_kline=True,
                    note="腾讯公开行情接口，适合个人研究兜底；非正式授权行情源，需结合多源校验和缓存新鲜度判断。",
                )
            )
    return result


def provider_capability(provider: MarketProvider) -> ProviderCapability | None:
    capability = getattr(provider, "capability", None)
    if not callable(capability):
        return None
    return capability()
