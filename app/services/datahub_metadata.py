from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import time
from typing import TypeVar

from app.models.schemas import PlateItem, StockConceptItem, StockInfo
from app.services.datahub_cache import _normalize_stock_concepts, _stock_pool_cache_is_authoritative, _stock_pool_rows_are_authoritative
from app.services.datahub_runtime import ProviderRuntime
from app.services.datahub_status import _provider_error_text
from app.services.provider_utils import ensure_positive_limit
from app.utils.symbols import normalize_symbol, standard_symbol


STOCK_POOL_FALLBACK_SECONDS = 60 * 60 * 24 * 30
T = TypeVar("T")


@dataclass(frozen=True)
class StockPoolRequest:
    keyword: str | None
    limit: int
    refresh: bool


@dataclass(frozen=True)
class ProviderAttempt:
    index: int
    name: str
    provider: object


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

    async def stock_pool(self, keyword: str | None = None, limit: int = 5000, refresh: bool = False) -> list[StockInfo]:
        ensure_positive_limit(limit)
        request = StockPoolRequest(keyword=keyword, limit=limit, refresh=refresh)
        cached = self._stock_pool_cache_result(request)
        if cached is not None:
            return cached

        errors: list[str] = []
        provider_rows = await self._stock_pool_provider_result(request, errors)
        if provider_rows is not None:
            return provider_rows

        fallback = self._stock_pool_final_fallback(request)
        if fallback is not None:
            return fallback
        raise RuntimeError("所有股票池数据源均不可用：" + "；".join(errors))

    def _stock_pool_cache_result(self, request: StockPoolRequest) -> list[StockInfo] | None:
        if request.refresh:
            return None
        cached = self.cache.get_stock_pool(self.settings.stock_pool_cache_seconds, limit=request.limit, keyword=request.keyword)
        if cached:
            return cached
        fallback = self._stock_pool_keyword_fallback(request)
        if fallback:
            return fallback
        if self._fresh_stock_pool_cache_can_confirm_empty(request.keyword):
            return cached
        return None

    def _stock_pool_keyword_fallback(self, request: StockPoolRequest) -> list[StockInfo]:
        if not request.keyword:
            return []
        fallback = self.cache.get_stock_pool(STOCK_POOL_FALLBACK_SECONDS, limit=request.limit, keyword=request.keyword)
        if fallback:
            self.cache.log_event("fallback", f"股票池新缓存未命中，使用30天内本地股票主数据：{request.keyword}")
        return fallback

    def _fresh_stock_pool_cache_can_confirm_empty(self, keyword: str | None) -> bool:
        if not keyword:
            return False
        return _stock_pool_cache_is_authoritative(
            self.cache.stats(),
            self.settings.stock_pool_cache_seconds,
            self.settings.stock_pool_authoritative_min_count,
            fresh_count=self.cache.stock_pool_count(self.settings.stock_pool_cache_seconds),
        )

    async def _stock_pool_provider_result(self, request: StockPoolRequest, errors: list[str]) -> list[StockInfo] | None:
        for attempt in self._provider_attempts("stock", errors):
            if not hasattr(attempt.provider, "stock_pool"):
                continue
            try:
                rows = await self._fetch_provider_stock_pool(attempt)
            except Exception as exc:
                errors.append(f"{attempt.name}: {exc}")
                self.runtime.record_failure(attempt.name, attempt.index, exc, "stock")
                continue
            selected = self._select_stock_pool_rows(rows, request)
            if selected is not None:
                return selected
            errors.append(f"{attempt.name}: 股票池覆盖不足，无法确认 {request.keyword}")
        return None

    async def _fetch_provider_stock_pool(self, attempt: ProviderAttempt) -> list[StockInfo]:
        started = time.perf_counter()
        rows = await self.runtime.call(attempt.provider.stock_pool())  # type: ignore[attr-defined]
        if not rows:
            raise RuntimeError(f"{_provider_source_name(attempt.provider, attempt.name)} 股票池返回为空")
        latency_ms = (time.perf_counter() - started) * 1000
        self.runtime.record_success(attempt.name, attempt.index, round(latency_ms, 2), "stock")
        self.cache.save_stock_pool(rows)
        return rows

    def _select_stock_pool_rows(self, rows: list[StockInfo], request: StockPoolRequest) -> list[StockInfo] | None:
        if not request.keyword:
            return rows[: request.limit]
        matched = _match_stock_pool_keyword(rows, request.keyword)
        if matched or _stock_pool_rows_are_authoritative(rows, self.settings.stock_pool_authoritative_min_count):
            return matched[: request.limit]
        return None

    def _stock_pool_final_fallback(self, request: StockPoolRequest) -> list[StockInfo] | None:
        fallback = self.cache.get_stock_pool(max_age_seconds=STOCK_POOL_FALLBACK_SECONDS, limit=request.limit, keyword=request.keyword)
        if fallback:
            self.cache.log_event("fallback", "股票池数据源失败，使用本地缓存股票池")
            return fallback
        if request.keyword and _stock_pool_cache_is_authoritative(
            self.cache.stats(),
            STOCK_POOL_FALLBACK_SECONDS,
            self.settings.stock_pool_authoritative_min_count,
            fresh_count=self.cache.stock_pool_count(STOCK_POOL_FALLBACK_SECONDS),
        ):
            return []
        return None

    async def stock_profile(self, symbol: str) -> StockInfo | None:
        code, market = normalize_symbol(symbol)
        target = f"{code}.{market.upper()}"
        rows = await self.stock_pool(keyword=code, limit=10, refresh=False)
        profile = _stock_profile_match(rows, target)
        local_profile = await self._local_stock_profile(target)
        return _profile_with_local_industry(profile, local_profile)

    async def _local_stock_profile(self, target: str) -> StockInfo | None:
        local_provider = self.providers.get("local")
        if local_provider is None:
            return None
        awaitable = _provider_call(local_provider, "stock_pool")
        if awaitable is None:
            return None
        try:
            local_rows = await self.runtime.call(awaitable)
        except Exception as exc:
            self.cache.log_event("fallback", f"本地个股基础资料不可用，行业兜底跳过：{target}；{_provider_error_text(exc)}")
            return None
        return _stock_profile_match(local_rows, target)

    async def plate_rank(self, limit: int = 20, refresh: bool = False) -> list[PlateItem]:
        ensure_positive_limit(limit)
        if not refresh:
            cached = self.cache.get_plate_rank(self.settings.plate_rank_cache_seconds, limit=limit)
            if cached:
                return cached

        errors: list[str] = []
        fetched = await self._metadata_provider_result(
            kind="plate",
            errors=errors,
            call=lambda provider: _provider_call(provider, "plate_rank", limit=limit),
            prepare=lambda rows: _non_empty_metadata_rows(rows, "板块排行返回为空")[:limit],
            save=lambda rows: self.cache.save_plate_rank(rows),
            record_failure=self._record_plate_failure,
        )
        if fetched is not None:
            return fetched

        fallback = self.cache.get_plate_rank(max_age_seconds=60 * 60 * 24, limit=limit)
        if fallback:
            self.cache.log_event("fallback", "板块数据源失败，使用本地缓存板块排行")
            return fallback
        raise RuntimeError("所有板块数据源均不可用：" + "；".join(errors))

    async def stock_concepts(self, symbol: str, limit: int = 8, refresh: bool = False) -> list[StockConceptItem]:
        ensure_positive_limit(limit)
        normalized = standard_symbol(symbol)
        if not refresh:
            cached = self.cache.get_stock_concepts(normalized, self.settings.stock_concept_cache_seconds, limit=limit)
            if cached:
                return cached

        errors: list[str] = []
        fetched = await self._metadata_provider_result(
            kind="concept",
            errors=errors,
            call=lambda provider: _provider_call(provider, "stock_concepts", normalized, limit=limit),
            prepare=lambda rows: _non_empty_metadata_rows(
                _normalize_stock_concepts(normalized, rows, limit),
                "概念归属返回为空",
            )[:limit],
            save=lambda rows: self.cache.save_stock_concepts(normalized, rows),
            record_failure=lambda name, index, exc: self.runtime.record_failure(name, index, exc, "concept"),
        )
        if fetched is not None:
            return fetched

        fallback = self.cache.get_stock_concepts(normalized, max_age_seconds=60 * 60 * 24 * 30, limit=limit)
        if fallback:
            self.cache.log_event("fallback", f"概念归属数据源失败，使用本地缓存概念：{normalized}")
            return fallback
        self.cache.log_event("fallback", f"概念归属不可用：{normalized}；" + "；".join(errors))
        return []

    async def _metadata_provider_result(
        self,
        *,
        kind: str,
        errors: list[str],
        call: Callable[[object], Awaitable[list[T]] | None],
        prepare: Callable[[list[T]], list[T]],
        save: Callable[[list[T]], None],
        record_failure: Callable[[str, int, Exception], None],
    ) -> list[T] | None:
        for attempt in self._provider_attempts(kind, errors):
            awaitable = call(attempt.provider)
            if awaitable is None:
                continue
            started = time.perf_counter()
            try:
                rows = prepare(await self.runtime.call(awaitable))
                latency_ms = (time.perf_counter() - started) * 1000
                self.runtime.record_success(attempt.name, attempt.index, round(latency_ms, 2), kind)
                save(rows)
                return rows
            except Exception as exc:
                errors.append(f"{attempt.name}: {exc}")
                record_failure(attempt.name, attempt.index, exc)
        return None

    def _provider_attempts(self, kind: str, errors: list[str]) -> list[ProviderAttempt]:
        attempts: list[ProviderAttempt] = []
        for index, name in self.priority(kind):
            provider = self._available_provider(kind, name, errors)
            if provider is not None:
                attempts.append(ProviderAttempt(index, name, provider))
        return attempts

    def _available_provider(self, kind: str, name: str, errors: list[str]) -> object | None:
        if self.runtime.is_cooling(name, kind):
            errors.append(f"{name}: 最近失败，短暂冷却中")
            return None
        provider = self.providers.get(name)
        if provider is None:
            errors.append(f"{name}: 数据源未注册")
            return None
        return provider

    def _record_plate_failure(self, name: str, index: int, exc: Exception) -> None:
        if name == "akshare":
            self.cache.log_event("fallback", f"AKShare板块排行不可用，继续尝试本地板块：{_provider_error_text(exc)}")
        self.runtime.record_failure(name, index, exc, "plate")


def _match_stock_pool_keyword(rows: list[StockInfo], keyword: str) -> list[StockInfo]:
    keyword_lower = keyword.lower()
    return [
        item
        for item in rows
        if keyword_lower in item.code.lower()
        or keyword_lower in item.name.lower()
        or keyword_lower in item.symbol.lower()
    ]


def _stock_profile_match(rows: list[StockInfo], target: str) -> StockInfo | None:
    return next((item for item in rows if item.symbol == target), None)


def _profile_with_local_industry(profile: StockInfo | None, local_profile: StockInfo | None) -> StockInfo | None:
    if profile and local_profile and not profile.industry:
        return profile.model_copy(update={"industry": local_profile.industry})
    return profile


def _provider_call(provider: object, method_name: str, *args, **kwargs) -> Awaitable[list[T]] | None:
    method = getattr(provider, method_name, None)
    if not callable(method):
        return None
    return method(*args, **kwargs)


def _non_empty_metadata_rows(rows: list[T], error: str) -> list[T]:
    if not rows:
        raise RuntimeError(error)
    return rows


def _provider_source_name(provider: object, fallback: str) -> str:
    source = getattr(provider, "source_name", None)
    if isinstance(source, str) and source.strip():
        return source
    return fallback
