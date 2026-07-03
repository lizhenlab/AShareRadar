from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable

from app.config import get_settings
from app.models.schemas import (
    DataQuality,
    DataSourcePlan,
    DataStatus,
    Kline,
    MinuteKline,
    OrderBook,
    ProviderCapability,
    ProviderCapabilityStatus,
    ProviderDecision,
    ProviderStatus,
    Quote,
    StockInfo,
)
from app.services.cache import SQLiteCache
from app.services.datahub_metadata import MetadataCoordinator
from app.services.datahub_klines import KlineCoordinator
from app.services.datahub_orderbook import OrderBookCoordinator
from app.services.datahub_status import (
    _provider_error_text,
    _provider_source_key,
)
from app.services.datahub_source_plan import SourcePlanBuilder
from app.services.datahub_quotes import QuoteCoordinator
from app.services.datahub_runtime import ProviderRuntime
from app.services.workbench_context import WorkbenchContextCache
from app.services.provider_registry import (
    all_provider_names,
    build_providers,
    provider_capabilities,
    provider_enabled_for,
    provider_index,
    provider_is_enabled,
    provider_priority,
    supported_provider_kinds,
)


__all__ = ["DataHub", "_provider_error_text", "_provider_source_key"]


@dataclass(frozen=True)
class DataHubCoordinators:
    quote: QuoteCoordinator
    kline: KlineCoordinator
    metadata: MetadataCoordinator
    order_book: OrderBookCoordinator
    source_plan: SourcePlanBuilder


def _build_coordinators(datahub: DataHub, runtime: ProviderRuntime) -> DataHubCoordinators:
    return DataHubCoordinators(
        quote=QuoteCoordinator(
            settings=datahub.settings,
            cache=datahub.cache,
            providers=datahub.providers,
            runtime=runtime,
            priority=lambda kind: datahub._priority(kind),
        ),
        kline=KlineCoordinator(
            settings=datahub.settings,
            cache=datahub.cache,
            providers=datahub.providers,
            runtime=runtime,
            priority=lambda kind: datahub._priority(kind),
        ),
        metadata=MetadataCoordinator(
            settings=datahub.settings,
            cache=datahub.cache,
            providers=datahub.providers,
            runtime=runtime,
            priority=lambda kind: datahub._priority(kind),
        ),
        source_plan=SourcePlanBuilder(
            provider_names=lambda: datahub._all_provider_names(),
            priority=lambda kind: datahub._priority(kind),
            provider_index=lambda name: datahub._provider_index(name),
            is_cooling=lambda name, kind: datahub._provider_is_cooling(name, kind),
        ),
        order_book=OrderBookCoordinator(
            providers=datahub.providers,
            runtime=runtime,
            provider_index=lambda name: datahub._provider_index(name),
        ),
    )


class DataHub:
    def __init__(self, cache: SQLiteCache | None = None) -> None:
        self.settings = get_settings()
        self.cache = cache or SQLiteCache()
        self.workbench_contexts = WorkbenchContextCache()
        self.providers = build_providers(self.settings)
        self._provider_runtime = ProviderRuntime(self.cache, self.settings)
        coordinators = _build_coordinators(self, self._provider_runtime)
        self._quote_coordinator = coordinators.quote
        self._kline_coordinator = coordinators.kline
        self._metadata_coordinator = coordinators.metadata
        self._order_book_coordinator = coordinators.order_book
        self._source_plan_builder = coordinators.source_plan
        self._sync_provider_enabled_flags()

    async def quote(self, symbol: str, use_cache: bool = True) -> Quote:
        return await self._quote_coordinator.quote(symbol, use_cache=use_cache)

    async def quotes(self, symbols: Iterable[str], use_cache: bool = True) -> list[Quote]:
        return await self._quote_coordinator.quotes(symbols, use_cache=use_cache)

    async def quote_with_quality(
        self,
        symbol: str,
        use_cache: bool = True,
        check_consistency: bool = True,
    ) -> tuple[Quote, DataQuality]:
        return await self._quote_coordinator.quote_with_quality(symbol, use_cache=use_cache, check_consistency=check_consistency)

    async def assess_quote_quality(
        self,
        quote: Quote,
        klines: list[Kline] | None = None,
        use_cache: bool = True,
        require_kline: bool = True,
        check_consistency: bool = True,
    ) -> DataQuality:
        return await self._quote_coordinator.assess_quote_quality(
            quote,
            klines=klines,
            use_cache=use_cache,
            require_kline=require_kline,
            check_consistency=check_consistency,
        )

    async def kline(self, symbol: str, limit: int = 120, use_cache: bool = True) -> list[Kline]:
        return await self._kline_coordinator.kline(symbol, limit=limit, use_cache=use_cache)

    async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120, use_cache: bool = True) -> list[MinuteKline]:
        return await self._kline_coordinator.minute_kline(symbol, interval=interval, limit=limit, use_cache=use_cache)

    async def stock_pool(self, keyword: str | None = None, limit: int = 5000, refresh: bool = False) -> list[StockInfo]:
        return await self._metadata_coordinator.stock_pool(keyword=keyword, limit=limit, refresh=refresh)

    async def stock_profile(self, symbol: str) -> StockInfo | None:
        return await self._metadata_coordinator.stock_profile(symbol)

    async def plate_rank(self, limit: int = 20, refresh: bool = False):
        return await self._metadata_coordinator.plate_rank(limit=limit, refresh=refresh)

    async def stock_concepts(self, symbol: str, limit: int = 8, refresh: bool = False):
        return await self._metadata_coordinator.stock_concepts(symbol, limit=limit, refresh=refresh)

    async def order_book(self, symbol: str) -> OrderBook:
        return await self._order_book_coordinator.order_book(symbol)

    async def futu_ping(self) -> dict[str, object]:
        return await self._order_book_coordinator.futu_ping()

    async def warmup(self, symbols: list[str]) -> None:
        await asyncio.gather(
            self.quotes(symbols, use_cache=False),
            *(self.kline(symbol, 120, use_cache=False) for symbol in symbols),
        )

    def status(self) -> DataStatus:
        self._sync_provider_enabled_flags()
        providers = self.cache.provider_statuses()
        capability_statuses = self.cache.provider_capability_statuses()
        capabilities = self.capabilities()
        return DataStatus(
            providers=providers,
            cache=self.cache.stats(),
            capabilities=capabilities,
            capability_statuses=capability_statuses,
            source_plan=self._source_plan(providers, capabilities, capability_statuses),
        )

    def capabilities(self) -> list[ProviderCapability]:
        return provider_capabilities(self.providers)

    def _source_plan(
        self,
        providers: list[ProviderStatus],
        capabilities: list[ProviderCapability],
        capability_statuses: list[ProviderCapabilityStatus] | None = None,
    ) -> DataSourcePlan:
        return self._source_plan_builder.build(providers, capabilities, capability_statuses)

    def _provider_decision(
        self,
        name: str,
        status: ProviderStatus | None,
        capability: ProviderCapability | None,
        quote_names: list[str],
        kline_names: list[str],
        minute_names: list[str],
        capability_statuses: dict[tuple[str, str], ProviderCapabilityStatus],
    ) -> ProviderDecision:
        return self._source_plan_builder.provider_decision(
            name,
            status,
            capability,
            quote_names,
            kline_names,
            minute_names,
            capability_statuses,
        )

    def _priority(self, kind: str) -> list[tuple[int, str]]:
        return provider_priority(self.settings, self.providers, kind)

    async def _call(self, awaitable):
        return await self._provider_runtime.call(awaitable)

    def _provider_is_cooling(self, name: str, kind: str = "general") -> bool:
        return self._provider_runtime.is_cooling(name, kind)

    def _record_provider_success(self, name: str, index: int, latency_ms: float, kind: str) -> None:
        self._provider_runtime.record_success(name, index, latency_ms, kind)

    def _record_provider_failure(self, name: str, index: int, exc: Exception, kind: str) -> None:
        self._provider_runtime.record_failure(name, index, exc, kind)

    def _clear_provider_cooldown(self, name: str, kind: str = "general") -> None:
        self._provider_runtime.clear_cooldown(name, kind)

    def _all_provider_names(self) -> list[str]:
        return all_provider_names(self.settings, self.providers)

    def _provider_index(self, name: str) -> int:
        return provider_index(self.settings, self.providers, name)

    def _sync_provider_enabled_flags(self) -> None:
        for name in self._all_provider_names():
            provider = self.providers[name]
            priority = self._provider_index(name)
            self.cache.ensure_provider(name, priority, enabled=provider_is_enabled(provider))
            for kind in supported_provider_kinds(provider):
                self.cache.ensure_provider_capability(name, kind, priority, enabled=provider_enabled_for(provider, kind))

    async def _quote_consistency(self, quote: Quote, check_consistency: bool = True) -> tuple[str, list[str], int]:
        return await self._quote_coordinator.consistency(quote, check_consistency=check_consistency)

    async def _quote_consistency_probe(self, index: int, name: str, provider, target_symbol: str) -> dict[str, object]:
        return await self._quote_coordinator.consistency_probe(index, name, provider, target_symbol)
