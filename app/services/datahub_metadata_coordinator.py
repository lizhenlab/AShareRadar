from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from app.models.schemas import PlateItem, StockConceptItem, StockInfo
from app.services.datahub_metadata_mapping import _prepare_concept_rows, _prepare_plate_rows
from app.services.datahub_metadata_provider import (
    _metadata_error_detail,
    _no_provider_message,
    _provider_call,
    _required_metadata_call,
    _safe_log_metadata_event_async,
    _save_metadata_best_effort,
    _unsupported_provider_message,
)
from app.services.datahub_metadata_stock_pool import (
    StockPoolResolution,
    StockPoolResolver,
)
from app.services.datahub_runtime import (
    ProviderAttempt,
    ProviderCallBusyError,
    ProviderCoverageMiss,
    ProviderRuntime,
    run_cache_io,
)
from app.services.datahub_status import _provider_error_text
from app.services.provider_errors import (
    is_provider_coverage_miss,
    sanitize_provider_error,
)
from app.services.provider_utils import ensure_positive_limit
from app.utils.symbols import standard_symbol


T = TypeVar("T")
STATIC_LOCAL_METADATA_SOURCE = "本地个股基础数据"


@dataclass(frozen=True)
class PlateRankResult:
    """板块排行的来源元数据；公开列表 API 保持兼容。"""

    rows: list[PlateItem]
    used_fallback_cache: bool = False


@dataclass(frozen=True)
class StockConceptResult:
    rows: list[StockConceptItem]
    used_fallback_cache: bool = False


class MetadataCoordinator:
    def __init__(
        self,
        *,
        settings,
        cache,
        providers: dict,
        runtime: ProviderRuntime,
        priority: Callable[[str], list[tuple[int, str]]],
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.providers = providers
        self.runtime = runtime
        self.priority = priority
        self.stock_pool_resolver = StockPoolResolver(
            settings=settings,
            cache=cache,
            providers=providers,
            runtime=runtime,
            priority=priority,
        )

    async def stock_pool(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets: Iterable[str] | None = None,
        minimum_market_counts: Mapping[str, int] | None = None,
    ) -> list[StockInfo]:
        return await self.stock_pool_resolver.stock_pool(
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
        return await self.stock_pool_resolver.stock_pool_resolution(
            keyword=keyword,
            limit=limit,
            refresh=refresh,
            required_markets=required_markets,
            minimum_market_counts=minimum_market_counts,
        )

    async def stock_profile(self, symbol: str) -> StockInfo | None:
        return await self.stock_pool_resolver.stock_profile(symbol)

    async def _local_stock_profile(self, target: str) -> StockInfo | None:
        return await self.stock_pool_resolver.local_stock_profile(target)

    async def plate_rank(self, limit: int = 20, refresh: bool = False) -> list[PlateItem]:
        return (await self.plate_rank_result(limit=limit, refresh=refresh)).rows

    async def plate_rank_result(self, limit: int = 20, refresh: bool = False) -> PlateRankResult:
        ensure_positive_limit(limit)
        if not refresh:
            cached = await run_cache_io(
                self.cache.get_plate_rank,
                self.settings.plate_rank_cache_seconds,
                limit=limit,
            )
            cached = _without_static_local_metadata(cached)
            if cached:
                return PlateRankResult(rows=cached)

        errors: list[str] = []
        fetched = await self._metadata_provider_result(
            kind="plate",
            method_name="plate_rank",
            errors=errors,
            call=lambda provider: _provider_call(provider, "plate_rank", limit=limit),
            prepare=lambda _attempt, rows: _prepare_plate_rows(rows, limit),
            save=lambda rows: self.cache.save_plate_rank(rows),
            before_failure=self._log_plate_failure,
            request_key=("plate_rank", limit),
        )
        if fetched is not None:
            return PlateRankResult(rows=fetched)

        fallback = await run_cache_io(
            self.cache.get_plate_rank,
            max_age_seconds=60 * 60 * 24,
            limit=limit,
        )
        fallback = _without_static_local_metadata(fallback)
        if fallback:
            await _safe_log_metadata_event_async(
                self.cache,
                "fallback",
                "板块数据源失败，使用本地缓存板块排行",
            )
            return PlateRankResult(
                rows=[item.model_copy(update={"fallback_used": True}) for item in fallback],
                used_fallback_cache=True,
            )
        raise RuntimeError("所有板块数据源均不可用：" + "；".join(errors))

    async def stock_concepts(
        self,
        symbol: str,
        limit: int = 8,
        refresh: bool = False,
    ) -> list[StockConceptItem]:
        return (await self.stock_concepts_result(symbol, limit=limit, refresh=refresh)).rows

    async def stock_concepts_result(
        self,
        symbol: str,
        limit: int = 8,
        refresh: bool = False,
    ) -> StockConceptResult:
        ensure_positive_limit(limit)
        normalized = standard_symbol(symbol)
        if not refresh:
            cached = await run_cache_io(
                self.cache.get_stock_concepts,
                normalized,
                self.settings.stock_concept_cache_seconds,
                limit=limit,
            )
            cached = _without_static_local_metadata(cached)
            if cached:
                return StockConceptResult(rows=cached)

        errors: list[str] = []
        fetched = await self._metadata_provider_result(
            kind="concept",
            method_name="stock_concepts",
            errors=errors,
            call=lambda provider: _provider_call(
                provider,
                "stock_concepts",
                normalized,
                limit=limit,
            ),
            prepare=lambda attempt, rows: _prepare_concept_rows(attempt, normalized, rows, limit),
            save=lambda rows: self.cache.save_stock_concepts(normalized, rows),
            request_key=("stock_concepts", normalized, limit),
        )
        if fetched is not None:
            return StockConceptResult(rows=fetched)

        fallback = await run_cache_io(
            self.cache.get_stock_concepts,
            normalized,
            max_age_seconds=60 * 60 * 24 * 30,
            limit=limit,
        )
        fallback = _without_static_local_metadata(fallback)
        if fallback:
            await _safe_log_metadata_event_async(
                self.cache,
                "fallback",
                f"概念归属数据源失败，使用本地缓存概念：{normalized}",
            )
            return StockConceptResult(
                rows=[item.model_copy(update={"fallback_used": True}) for item in fallback],
                used_fallback_cache=True,
            )
        if errors:
            message = f"概念归属不可用：{normalized}；{_metadata_error_detail(errors, '本地兜底无覆盖')}"
            await _safe_log_metadata_event_async(self.cache, "fallback", message)
            raise RuntimeError(message)
        return StockConceptResult(rows=[])

    async def _metadata_provider_result(
        self,
        *,
        kind: str,
        method_name: str,
        errors: list[str],
        call: Callable[[object], Awaitable[list[T]] | None],
        prepare: Callable[[ProviderAttempt, list[T]], list[T]],
        save: Callable[[list[T]], None],
        before_failure: Callable[[ProviderAttempt, Exception], Awaitable[None]] | None = None,
        request_key: Hashable,
    ) -> list[T] | None:
        priority_rows = list(self.priority(kind))
        if not priority_rows:
            errors.append(_no_provider_message(kind))
            return None
        for attempt in self.runtime.attempts(priority_rows, self.providers, kind, errors):
            if attempt.name == "local" and kind in {"plate", "concept"}:
                errors.append(f"local: 本地静态资料不提供{kind}实时涨跌幅")
                continue
            if not callable(getattr(attempt.provider, method_name, None)):
                errors.append(_unsupported_provider_message(attempt, kind))
                continue
            result = None
            try:
                result = await self.runtime.timed_provider_call(
                    attempt.name,
                    kind,
                    lambda: _required_metadata_call(call, attempt.provider, kind),
                    request_key=request_key,
                )
                rows = prepare(attempt, result.value)
                await self.runtime.record_attempt_success_async(attempt, kind, result.latency_ms)
                await _save_metadata_best_effort(save, rows)
                return rows
            except ProviderCoverageMiss as exc:
                if result is None:
                    await self._record_attempt_failure(attempt, kind, exc, errors, before_failure)
                    continue
                await self.runtime.record_attempt_success_async(attempt, kind, result.latency_ms)
                continue
            except Exception as exc:
                await self._record_attempt_failure(attempt, kind, exc, errors, before_failure)
        return None

    async def _record_attempt_failure(
        self,
        attempt: ProviderAttempt,
        kind: str,
        exc: Exception,
        errors: list[str],
        before_failure: Callable[[ProviderAttempt, Exception], Awaitable[None]] | None,
    ) -> None:
        errors.append(f"{attempt.name}: {sanitize_provider_error(_provider_error_text(exc))}")
        if is_provider_coverage_miss(exc) or isinstance(exc, ProviderCallBusyError):
            return
        if before_failure is not None:
            await before_failure(attempt, exc)
        await self.runtime.record_failure_async(attempt.name, attempt.index, exc, kind)

    async def _log_plate_failure(self, attempt: ProviderAttempt, exc: Exception) -> None:
        if attempt.name == "akshare":
            await _safe_log_metadata_event_async(
                self.cache,
                "fallback",
                "AKShare板块排行不可用，继续尝试其他实时板块源：" f"{sanitize_provider_error(_provider_error_text(exc))}",
            )


def _without_static_local_metadata(rows: list[T]) -> list[T]:
    return [item for item in rows if str(getattr(item, "source", "")).strip() != STATIC_LOCAL_METADATA_SOURCE]
