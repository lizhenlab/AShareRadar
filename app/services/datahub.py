from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from collections.abc import Iterable, Mapping
import logging
import math

from app.config import Settings
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
from app.services.cache import SQLiteCache, resolve_cache_settings
from app.services.datahub_metadata import MetadataCoordinator, StockPoolResolution
from app.services.datahub_klines import KlineCoordinator
from app.services.datahub_orderbook import OrderBookCoordinator
from app.services.datahub_status import (
    _provider_error_text,
    _provider_source_key,
)
from app.services.datahub_source_plan import SourcePlanBuilder
from app.services.datahub_quotes import QuoteCoordinator
from app.services.datahub_runtime import PROVIDER_SHUTDOWN_TIMEOUT_SECONDS, ProviderRuntime
from app.services.workbench_context import WorkbenchContextCache
from app.services.datahub_status_service import DataStatusService
from app.services.provider_registry import (
    all_provider_names,
    build_providers,
    provider_index,
    provider_priority,
)


__all__ = ["DataHub", "_provider_error_text", "_provider_source_key"]


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataHubCoordinators:
    quote: QuoteCoordinator
    kline: KlineCoordinator
    metadata: MetadataCoordinator
    order_book: OrderBookCoordinator
    source_plan: SourcePlanBuilder
    status: DataStatusService


def _build_coordinators(datahub: DataHub, runtime: ProviderRuntime) -> DataHubCoordinators:
    source_plan = SourcePlanBuilder(
        provider_names=lambda: datahub._all_provider_names(),
        priority=lambda kind: datahub._priority(kind),
        provider_index=lambda name: datahub._provider_index(name),
        is_cooling=lambda name, kind: datahub._provider_is_cooling(name, kind),
    )
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
        source_plan=source_plan,
        status=DataStatusService(
            cache=datahub.cache,
            providers=datahub.providers,
            provider_names=lambda: datahub._all_provider_names(),
            provider_index=lambda name: datahub._provider_index(name),
            source_plan_builder=source_plan,
        ),
        order_book=OrderBookCoordinator(
            providers=datahub.providers,
            runtime=runtime,
            provider_index=lambda name: datahub._provider_index(name),
        ),
    )


class DataHub:
    def __init__(
        self,
        cache: SQLiteCache | None = None,
        *,
        settings: Settings | None = None,
        workbench_contexts: WorkbenchContextCache | None = None,
    ) -> None:
        self.settings = resolve_cache_settings(cache, settings)
        self.cache = cache if cache is not None else SQLiteCache(settings=self.settings)
        self.workbench_contexts = workbench_contexts if workbench_contexts is not None else WorkbenchContextCache()
        self.providers = build_providers(self.settings)
        self._provider_runtime = ProviderRuntime(self.cache, self.settings)
        self._providers_closed = False
        self._closed_providers: list[object] = []
        self._provider_close_task: asyncio.Task[bool] | None = None
        coordinators = _build_coordinators(self, self._provider_runtime)
        self._quote_coordinator = coordinators.quote
        self._kline_coordinator = coordinators.kline
        self._metadata_coordinator = coordinators.metadata
        self._order_book_coordinator = coordinators.order_book
        self._source_plan_builder = coordinators.source_plan
        self._status_service = coordinators.status
        self._sync_provider_enabled_flags()

    async def quote(self, symbol: str, use_cache: bool = True) -> Quote:
        return await self._quote_coordinator.quote(symbol, use_cache=use_cache)

    async def quotes(self, symbols: Iterable[str], use_cache: bool = True) -> list[Quote]:
        return await self._quote_coordinator.quotes(symbols, use_cache=use_cache)

    async def partial_quotes(self, symbols: Iterable[str], use_cache: bool = True) -> list[Quote]:
        return await self._quote_coordinator.partial_quotes(symbols, use_cache=use_cache)

    async def partial_quotes_with_errors(
        self,
        symbols: Iterable[str],
        use_cache: bool = True,
    ) -> tuple[list[Quote], tuple[str, ...]]:
        return await self._quote_coordinator.partial_quotes_with_errors(symbols, use_cache=use_cache)

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

    async def kline(
        self,
        symbol: str,
        limit: int = 120,
        use_cache: bool = True,
        *,
        allow_stale: bool = False,
        require_provider_response: bool = False,
    ) -> list[Kline]:
        return await self._kline_coordinator.kline(
            symbol,
            limit=limit,
            use_cache=use_cache,
            allow_stale=allow_stale,
            require_provider_response=require_provider_response,
        )

    def provider_chain_state(self, kind: str):
        return self._provider_runtime.chain_state(self._priority(kind), self.providers, kind)

    async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120, use_cache: bool = True) -> list[MinuteKline]:
        return await self._kline_coordinator.minute_kline(symbol, interval=interval, limit=limit, use_cache=use_cache)

    async def stock_pool(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets: Iterable[str] | None = None,
        minimum_market_counts: Mapping[str, int] | None = None,
    ) -> list[StockInfo]:
        return await self._metadata_coordinator.stock_pool(
            keyword=keyword,
            limit=limit,
            refresh=refresh,
            required_markets=required_markets,
            minimum_market_counts=minimum_market_counts,
        )

    async def stock_pool_resolution(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets: Iterable[str] | None = None,
        minimum_market_counts: Mapping[str, int] | None = None,
    ) -> StockPoolResolution:
        return await self._metadata_coordinator.stock_pool_resolution(
            keyword=keyword,
            limit=limit,
            refresh=refresh,
            required_markets=required_markets,
            minimum_market_counts=minimum_market_counts,
        )

    async def stock_profile(self, symbol: str) -> StockInfo | None:
        return await self._metadata_coordinator.stock_profile(symbol)

    async def plate_rank(self, limit: int = 20, refresh: bool = False):
        return await self._metadata_coordinator.plate_rank(limit=limit, refresh=refresh)

    async def plate_rank_result(self, limit: int = 20, refresh: bool = False):
        return await self._metadata_coordinator.plate_rank_result(limit=limit, refresh=refresh)

    async def stock_concepts(self, symbol: str, limit: int = 8, refresh: bool = False):
        return await self._metadata_coordinator.stock_concepts(symbol, limit=limit, refresh=refresh)

    async def stock_concepts_result(self, symbol: str, limit: int = 8, refresh: bool = False):
        return await self._metadata_coordinator.stock_concepts_result(symbol, limit=limit, refresh=refresh)

    async def order_book(self, symbol: str) -> OrderBook:
        return await self._order_book_coordinator.order_book(symbol)

    async def futu_ping(self) -> dict[str, object]:
        return await self._order_book_coordinator.futu_ping()

    async def warmup(self, symbols: list[str]) -> None:
        await asyncio.gather(
            self.quotes(symbols, use_cache=False),
            *(self.kline(symbol, 120, use_cache=False) for symbol in symbols),
        )

    async def aclose(self, timeout: float = PROVIDER_SHUTDOWN_TIMEOUT_SECONDS) -> bool:
        close_timeout = _bounded_close_timeout(timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + close_timeout
        if self._providers_closed:
            return True
        close_task = self._get_or_create_provider_close_task()
        remaining = max(0.0, deadline - loop.time())
        return await self._wait_for_provider_close(close_task, timeout=remaining)

    def _get_or_create_provider_close_task(self) -> asyncio.Task[bool]:
        task = self._provider_close_task
        if task is not None and not task.done():
            return task
        task = asyncio.create_task(
            self._close_providers_after_runtime_quiesces(),
            name="datahub-provider-close",
        )
        task.add_done_callback(_consume_provider_close_exception)
        self._provider_close_task = task
        return task

    async def _close_providers_after_runtime_quiesces(self) -> bool:
        while not await self._provider_runtime.aclose(timeout=PROVIDER_SHUTDOWN_TIMEOUT_SECONDS):
            pass
        pending = [
            provider
            for provider in self.providers.values()
            if not self._provider_was_closed(provider)
        ]
        pending = _unique_by_identity(pending)
        results = await asyncio.gather(
            *(self._close_provider_once(provider) for provider in pending),
            return_exceptions=True,
        )
        fatal_errors: list[BaseException] = []
        for result in results:
            if result is True:
                continue
            if isinstance(result, asyncio.CancelledError):
                fatal_errors.append(result)
            elif isinstance(result, Exception):
                logger.warning("DataHub provider shutdown failed: %s", type(result).__name__)
            elif isinstance(result, BaseException):
                fatal_errors.append(result)
            else:
                logger.warning("DataHub provider shutdown reported incomplete")

        self._providers_closed = all(self._provider_was_closed(provider) for provider in self.providers.values())
        if len(fatal_errors) == 1:
            raise fatal_errors[0]
        if fatal_errors:
            raise BaseExceptionGroup("DataHub provider shutdown failed", fatal_errors)
        return self._providers_closed

    async def _close_provider_once(self, provider: object) -> bool:
        closed = await _close_provider(provider)
        if closed:
            self._closed_providers.append(provider)
        return closed

    def _provider_was_closed(self, provider: object) -> bool:
        return any(closed is provider for closed in self._closed_providers)

    async def _wait_for_provider_close(self, task: asyncio.Task[bool], *, timeout: float) -> bool:
        if task.done():
            return task.result()
        if timeout <= 0:
            return False
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            return self._providers_closed

    def status(self) -> DataStatus:
        return self._status_service.status()

    def capabilities(self) -> list[ProviderCapability]:
        return self._status_service.capabilities()

    def _source_plan(
        self,
        providers: list[ProviderStatus],
        capabilities: list[ProviderCapability],
        capability_statuses: list[ProviderCapabilityStatus] | None = None,
    ) -> DataSourcePlan:
        return self._status_service.source_plan(providers, capabilities, capability_statuses)

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
        return self._status_service.provider_decision(
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
        self._status_service.sync_provider_enabled_flags()

    async def _quote_consistency(self, quote: Quote, check_consistency: bool = True) -> tuple[str, list[str], int]:
        return await self._quote_coordinator.consistency(quote, check_consistency=check_consistency)

    async def _quote_consistency_probe(self, index: int, name: str, provider, target_symbol: str) -> dict[str, object]:
        return await self._quote_coordinator.consistency_probe(index, name, provider, target_symbol)


async def _close_provider(provider: object) -> bool:
    close = getattr(provider, "aclose", None)
    if not callable(close):
        return True
    result = close()
    if inspect.isawaitable(result):
        result = await result
    return result is not False


def _unique_by_identity(values: Iterable[object]) -> list[object]:
    unique: list[object] = []
    for value in values:
        if not any(existing is value for existing in unique):
            unique.append(value)
    return unique


def _bounded_close_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return 0.0
    return timeout if math.isfinite(timeout) and timeout >= 0 else 0.0


def _consume_provider_close_exception(task: asyncio.Task[bool]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass
