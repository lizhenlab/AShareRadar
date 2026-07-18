from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, cast

from app.config import Settings
from app.models.schemas import Kline, MinuteKline, OrderBook, PlateItem, ProviderCapability, Quote, StockConceptItem, StockInfo
from app.runtime_environment import isolate_user_site_packages

isolate_user_site_packages()

from app.services.akshare_provider import AKShareProvider
from app.services.baostock_provider import BaoStockProvider
from app.services.futu_provider import FutuProvider
from app.services.local_metadata_provider import LocalIndividualStockProvider
from app.services.tushare_provider import TushareProvider
from app.services.providers import DemoMarketDataProvider, TencentMarketDataProvider


CAPABILITY_PROVIDER_ORDER = ("tencent", "akshare", "tushare", "baostock", "futu", "local", "demo")
DEFAULT_MARKET_PROVIDER_KINDS = ("quote", "kline")
PRIORITY_SETTING_BY_KIND = {
    "quote": "quote_provider_priority",
    "kline": "kline_provider_priority",
    "minute": "minute_provider_priority",
    "stock": "stock_provider_priority",
    "plate": "plate_provider_priority",
    "concept": "plate_provider_priority",
}
CAPABILITY_FIELD_BY_KIND = {
    "quote": "realtime_quote",
    "kline": "daily_kline",
    "minute": "minute_kline",
    "stock": "stock_pool",
    "plate": "plate_rank",
    "concept": "concept_board",
    "order_book": "order_book",
}
FALLBACK_PROVIDER_NOTES = {
    "tencent": "腾讯公开行情接口，适合个人研究兜底；非正式授权行情源，需结合多源校验和缓存新鲜度判断。",
}


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
        "tencent": cast(MarketProvider, TencentMarketDataProvider(timeout=settings.request_timeout_seconds)),
        "akshare": AKShareProvider(),
        "baostock": BaoStockProvider(),
        "tushare": TushareProvider(token=settings.tushare_token),
        "futu": FutuProvider(
            host=settings.futu_host,
            port=settings.futu_port,
            enabled=settings.futu_enabled,
        ),
        "local": LocalIndividualStockProvider(),
        "demo": cast(MarketProvider, DemoMarketDataProvider(enabled=settings.demo_provider_enabled)),
    }


def provider_priority(settings: Settings, providers: dict[str, MarketProvider], kind: str) -> list[tuple[int, str]]:
    configured_names = _priority_names(settings, kind)
    if not configured_names:
        return []
    names = _with_optional_demo(settings, kind, configured_names)
    enabled_names = [name for name in names if _provider_enabled_by_name(providers, name, kind)]
    return [(provider_index(settings, providers, name), name) for name in enabled_names]


def all_provider_names(settings: Settings, providers: dict[str, MarketProvider]) -> list[str]:
    names: list[str] = []
    for group in (
        settings.quote_provider_priority,
        settings.kline_provider_priority,
        settings.minute_provider_priority,
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
        return kind in DEFAULT_MARKET_PROVIDER_KINDS
    if not capability.enabled:
        return False
    field = CAPABILITY_FIELD_BY_KIND.get(kind)
    return bool(field and getattr(capability, field, False))


def provider_is_enabled(provider: MarketProvider) -> bool:
    capability = provider_capability(provider)
    return bool(capability.enabled) if capability else True


def provider_capabilities(providers: dict[str, MarketProvider]) -> list[ProviderCapability]:
    result = []
    for name in ordered_provider_names(providers):
        provider = providers.get(name)
        if provider is None:
            continue
        result.append(provider_capability_for_name(name, provider))
    return result


def provider_capability(provider: MarketProvider) -> ProviderCapability | None:
    capability = getattr(provider, "capability", None)
    if not callable(capability):
        return None
    return capability()


def provider_capability_for_name(name: str, provider: MarketProvider) -> ProviderCapability:
    capability = provider_capability(provider)
    if capability is None:
        return _fallback_capability(name, provider)
    if capability.name != name:
        return capability.model_copy(update={"name": name})
    return capability


def supported_provider_kinds(provider: MarketProvider) -> list[str]:
    capability = provider_capability(provider)
    if capability is None:
        return list(DEFAULT_MARKET_PROVIDER_KINDS)
    return [kind for kind, field in CAPABILITY_FIELD_BY_KIND.items() if getattr(capability, field, False)]


def ordered_provider_names(providers: dict[str, MarketProvider]) -> list[str]:
    ordered = [name for name in CAPABILITY_PROVIDER_ORDER if name in providers]
    ordered.extend(name for name in providers if name not in ordered)
    return ordered


def _priority_names(settings: Settings, kind: str) -> list[str]:
    setting_name = PRIORITY_SETTING_BY_KIND.get(kind)
    if setting_name is None:
        return []
    return _unique(getattr(settings, setting_name))


def _with_optional_demo(settings: Settings, kind: str, names: list[str]) -> list[str]:
    result = list(names)
    if settings.demo_provider_enabled and kind in DEFAULT_MARKET_PROVIDER_KINDS and "demo" not in result:
        result.append("demo")
    return result


def _provider_enabled_by_name(providers: dict[str, MarketProvider], name: str, kind: str) -> bool:
    provider = providers.get(name)
    return bool(provider and provider_enabled_for(provider, kind))


def _fallback_capability(name: str, provider: MarketProvider) -> ProviderCapability:
    supported = set(DEFAULT_MARKET_PROVIDER_KINDS)
    return ProviderCapability(
        name=name,
        installed=True,
        enabled=True,
        reliability_level="公开源",
        realtime_quote="quote" in supported,
        daily_kline="kline" in supported,
        note=FALLBACK_PROVIDER_NOTES.get(name, f"{getattr(provider, 'source_name', name)} 未声明能力，按基础行情源处理。"),
    )


def _unique(names: Iterable[str]) -> list[str]:
    result: list[str] = []
    for name in names:
        if name not in result:
            result.append(name)
    return result
