from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import math
from typing import TypeVar

from app.models.schemas import PlateItem, StockConceptItem, StockInfo
from app.services.datahub_cache import _normalize_stock_concepts, _stock_pool_cache_is_authoritative, _stock_pool_rows_are_authoritative
from app.services.datahub_runtime import ProviderAttempt, ProviderRuntime, provider_source_name
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
class StockPoolResolution:
    resolved: bool
    reason: str
    rows: tuple[StockInfo, ...] = ()

    @classmethod
    def hit(cls, rows: list[StockInfo], reason: str) -> "StockPoolResolution":
        return cls(resolved=True, reason=reason, rows=tuple(rows))

    @classmethod
    def miss(cls, reason: str) -> "StockPoolResolution":
        return cls(resolved=False, reason=reason)

    def list_rows(self) -> list[StockInfo]:
        return list(self.rows)


class ProviderCoverageMiss(Exception):
    """Raised when a fallback provider is healthy but has no coverage for the requested item."""


class StockPoolResolver:
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
        resolution = await self.stock_pool_resolution(keyword=keyword, limit=limit, refresh=refresh)
        return resolution.list_rows()

    async def stock_pool_resolution(self, keyword: str | None = None, limit: int = 5000, refresh: bool = False) -> StockPoolResolution:
        ensure_positive_limit(limit)
        request = StockPoolRequest(keyword=keyword, limit=limit, refresh=refresh)
        cached = self._cache_result(request)
        if cached.resolved:
            return cached

        errors: list[str] = []
        provider_rows = await self._provider_result(request, errors)
        if provider_rows.resolved:
            return provider_rows

        fallback = self._final_fallback(request)
        if fallback.resolved:
            return fallback
        raise RuntimeError("所有股票池数据源均不可用：" + "；".join(errors))

    def _cache_result(self, request: StockPoolRequest) -> StockPoolResolution:
        if request.refresh:
            return StockPoolResolution.miss("refresh-request")
        cached = self.cache.get_stock_pool(self.settings.stock_pool_cache_seconds, limit=request.limit, keyword=request.keyword)
        if cached:
            return StockPoolResolution.hit(cached, "fresh-cache")
        if self._fresh_cache_can_confirm_empty(request.keyword):
            return StockPoolResolution.hit(cached, "fresh-authoritative-empty")
        fallback = self._keyword_fallback(request)
        if fallback:
            return StockPoolResolution.hit(fallback, "keyword-fallback")
        return StockPoolResolution.miss("cache-miss")

    def _keyword_fallback(self, request: StockPoolRequest) -> list[StockInfo]:
        if not request.keyword:
            return []
        fallback = self.cache.get_stock_pool(STOCK_POOL_FALLBACK_SECONDS, limit=request.limit, keyword=request.keyword)
        if fallback:
            _safe_log_metadata_event(self.cache, "fallback", f"股票池新缓存未命中，使用30天内本地股票主数据：{request.keyword}")
        return fallback

    def _fresh_cache_can_confirm_empty(self, keyword: str | None) -> bool:
        if not keyword:
            return False
        return _stock_pool_cache_is_authoritative(
            self.cache.stats(),
            self.settings.stock_pool_cache_seconds,
            self.settings.stock_pool_authoritative_min_count,
            fresh_count=self.cache.stock_pool_count(self.settings.stock_pool_cache_seconds),
        )

    async def _provider_result(self, request: StockPoolRequest, errors: list[str]) -> StockPoolResolution:
        priority_rows = list(self.priority("stock"))
        if not priority_rows:
            errors.append(_no_provider_message("stock"))
            return StockPoolResolution.miss("provider-miss")
        for attempt in self.runtime.attempts(priority_rows, self.providers, "stock", errors):
            awaitable = _provider_call(attempt.provider, "stock_pool")
            if awaitable is None:
                errors.append(_unsupported_provider_message(attempt, "stock"))
                continue
            try:
                rows = await self._fetch_provider_stock_pool(attempt, awaitable)
            except Exception as exc:
                self.runtime.record_attempt_failure(attempt, "stock", exc, errors)
                continue
            selected = self._select_rows(rows, request)
            if selected.resolved:
                return selected
            errors.append(f"{attempt.name}: 股票池覆盖不足，无法确认 {request.keyword}")
        return StockPoolResolution.miss("provider-miss")

    async def _fetch_provider_stock_pool(
        self,
        attempt: ProviderAttempt,
        awaitable: Awaitable[list[StockInfo]],
    ) -> list[StockInfo]:
        result = await self.runtime.timed_call(awaitable)
        rows = result.value
        if not rows:
            raise RuntimeError(f"{provider_source_name(attempt.provider, attempt.name)} 股票池返回为空")
        self.runtime.record_attempt_success(attempt, "stock", result.latency_ms)
        _save_metadata_best_effort(lambda items: self.cache.save_stock_pool(items), rows)
        return rows

    def _select_rows(self, rows: list[StockInfo], request: StockPoolRequest) -> StockPoolResolution:
        if not request.keyword:
            return StockPoolResolution.hit(rows[: request.limit], "provider-full-pool")
        matched = _match_stock_pool_keyword(rows, request.keyword)
        if matched:
            return StockPoolResolution.hit(matched[: request.limit], "provider-keyword-match")
        if _stock_pool_rows_are_authoritative(rows, self.settings.stock_pool_authoritative_min_count):
            return StockPoolResolution.hit([], "provider-authoritative-empty")
        return StockPoolResolution.miss("provider-coverage-miss")

    def _final_fallback(self, request: StockPoolRequest) -> StockPoolResolution:
        fallback = self.cache.get_stock_pool(max_age_seconds=STOCK_POOL_FALLBACK_SECONDS, limit=request.limit, keyword=request.keyword)
        if fallback:
            _safe_log_metadata_event(self.cache, "fallback", "股票池数据源失败，使用本地缓存股票池")
            return StockPoolResolution.hit(fallback, "stale-fallback")
        if request.keyword and _stock_pool_cache_is_authoritative(
            self.cache.stats(),
            STOCK_POOL_FALLBACK_SECONDS,
            self.settings.stock_pool_authoritative_min_count,
            fresh_count=self.cache.stock_pool_count(STOCK_POOL_FALLBACK_SECONDS),
        ):
            return StockPoolResolution.hit([], "stale-authoritative-empty")
        return StockPoolResolution.miss("fallback-miss")

    async def stock_profile(self, symbol: str) -> StockInfo | None:
        code, market = normalize_symbol(symbol)
        target = f"{code}.{market.upper()}"
        local_profile = await self.local_stock_profile(target)
        try:
            resolution = await self.stock_pool_resolution(keyword=code, limit=10, refresh=False)
        except RuntimeError:
            if local_profile is not None:
                return local_profile
            raise
        profile = _stock_profile_match(resolution.list_rows(), target)
        return _profile_with_local_industry(
            profile,
            local_profile,
            allow_local_only=_stock_profile_resolution_allows_local_only(resolution.reason),
        )

    async def local_stock_profile(self, target: str) -> StockInfo | None:
        local_provider = self.providers.get("local")
        if local_provider is None:
            return None
        awaitable = _provider_call(local_provider, "stock_pool")
        if awaitable is None:
            return None
        try:
            local_rows = await self.runtime.call(awaitable)
        except Exception as exc:
            _safe_log_metadata_event(
                self.cache,
                "fallback",
                f"本地个股基础资料不可用，行业兜底跳过：{target}；{_provider_error_text(exc)}",
            )
            return None
        return _stock_profile_match(local_rows, target)


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

    async def stock_pool(self, keyword: str | None = None, limit: int = 5000, refresh: bool = False) -> list[StockInfo]:
        return await self.stock_pool_resolver.stock_pool(keyword=keyword, limit=limit, refresh=refresh)

    async def stock_profile(self, symbol: str) -> StockInfo | None:
        return await self.stock_pool_resolver.stock_profile(symbol)

    async def _local_stock_profile(self, target: str) -> StockInfo | None:
        return await self.stock_pool_resolver.local_stock_profile(target)

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
            prepare=lambda _attempt, rows: _prepare_plate_rows(rows, limit),
            save=lambda rows: self.cache.save_plate_rank(rows),
            record_failure=self._record_plate_failure,
        )
        if fetched is not None:
            return fetched

        fallback = self.cache.get_plate_rank(max_age_seconds=60 * 60 * 24, limit=limit)
        if fallback:
            _safe_log_metadata_event(self.cache, "fallback", "板块数据源失败，使用本地缓存板块排行")
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
            prepare=lambda attempt, rows: _prepare_concept_rows(attempt, normalized, rows, limit),
            save=lambda rows: self.cache.save_stock_concepts(normalized, rows),
            record_failure=lambda name, index, exc: self.runtime.record_failure(name, index, exc, "concept"),
        )
        if fetched is not None:
            return fetched

        fallback = self.cache.get_stock_concepts(normalized, max_age_seconds=60 * 60 * 24 * 30, limit=limit)
        if fallback:
            _safe_log_metadata_event(self.cache, "fallback", f"概念归属数据源失败，使用本地缓存概念：{normalized}")
            return fallback
        if errors:
            message = f"概念归属不可用：{normalized}；{_metadata_error_detail(errors, '本地兜底无覆盖')}"
            _safe_log_metadata_event(self.cache, "fallback", message)
            raise RuntimeError(message)
        return []

    async def _metadata_provider_result(
        self,
        *,
        kind: str,
        errors: list[str],
        call: Callable[[object], Awaitable[list[T]] | None],
        prepare: Callable[[ProviderAttempt, list[T]], list[T]],
        save: Callable[[list[T]], None],
        record_failure: Callable[[str, int, Exception], None],
    ) -> list[T] | None:
        priority_rows = list(self.priority(kind))
        if not priority_rows:
            errors.append(_no_provider_message(kind))
            return None
        for attempt in self.runtime.attempts(priority_rows, self.providers, kind, errors):
            awaitable = call(attempt.provider)
            if awaitable is None:
                errors.append(_unsupported_provider_message(attempt, kind))
                continue
            result = None
            try:
                result = await self.runtime.timed_call(awaitable)
                rows = prepare(attempt, result.value)
                self.runtime.record_attempt_success(attempt, kind, result.latency_ms)
                _save_metadata_best_effort(save, rows)
                return rows
            except ProviderCoverageMiss as exc:
                if result is None:
                    self.runtime.record_attempt_failure(attempt, kind, exc, errors, record_failure=record_failure)
                    continue
                self.runtime.record_attempt_success(attempt, kind, result.latency_ms)
                continue
            except Exception as exc:
                self.runtime.record_attempt_failure(attempt, kind, exc, errors, record_failure=record_failure)
        return None

    def _record_plate_failure(self, name: str, index: int, exc: Exception) -> None:
        if name == "akshare":
            _safe_log_metadata_event(self.cache, "fallback", f"AKShare板块排行不可用，继续尝试本地板块：{_provider_error_text(exc)}")
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


def _unsupported_provider_message(attempt: ProviderAttempt, kind: str) -> str:
    source = provider_source_name(attempt.provider, attempt.name)
    return f"{attempt.name}: {source} 不支持{_metadata_kind_label(kind)}能力"


def _metadata_kind_label(kind: str) -> str:
    return {"stock": "股票池", "plate": "板块", "concept": "概念"}.get(kind, kind)


def _no_provider_message(kind: str) -> str:
    return f"{_metadata_kind_label(kind)}未配置可用数据源"


def _stock_profile_match(rows: list[StockInfo], target: str) -> StockInfo | None:
    return next((item for item in rows if item.symbol == target), None)


def _profile_with_local_industry(
    profile: StockInfo | None,
    local_profile: StockInfo | None,
    *,
    allow_local_only: bool = False,
) -> StockInfo | None:
    if profile is None and allow_local_only:
        return local_profile
    if profile and local_profile and not profile.industry:
        return profile.model_copy(update={"industry": local_profile.industry})
    return profile


def _stock_profile_resolution_allows_local_only(reason: str) -> bool:
    return reason not in {
        "fresh-authoritative-empty",
        "provider-authoritative-empty",
        "stale-authoritative-empty",
    }


def _provider_call(provider: object, method_name: str, *args, **kwargs) -> Awaitable[list[T]] | None:
    method = getattr(provider, method_name, None)
    if not callable(method):
        return None
    return method(*args, **kwargs)


def _non_empty_metadata_rows(rows: list[T], error: str) -> list[T]:
    if not rows:
        raise RuntimeError(error)
    return rows


def _prepare_plate_rows(rows: list[PlateItem], limit: int) -> list[PlateItem]:
    _non_empty_metadata_rows(rows, "板块排行返回为空")
    return _non_empty_metadata_rows(_clean_plate_rows(rows), "板块排行字段无效")[:limit]


def _clean_plate_rows(rows: list[PlateItem]) -> list[PlateItem]:
    cleaned: list[PlateItem] = []
    for row in rows or []:
        rank = _positive_rank(row.rank)
        name = _required_text(row.name)
        change_pct = _finite_float(row.change_pct)
        source = _required_text(row.source)
        updated_at = _required_text(row.updated_at)
        if rank is None or change_pct is None or not all((name, source, updated_at)):
            continue
        cleaned.append(
            row.model_copy(
                update={
                    "rank": rank,
                    "name": name,
                    "change_pct": change_pct,
                    "amount": _optional_non_negative_float(row.amount),
                    "turnover_rate": _optional_non_negative_float(row.turnover_rate),
                    "leading_stock": _optional_text(row.leading_stock),
                    "leading_stock_change_pct": _optional_finite_float(row.leading_stock_change_pct),
                    "source": source,
                    "updated_at": updated_at,
                }
            )
        )
    return cleaned


def _save_metadata_best_effort(save: Callable[[list[T]], None], rows: list[T]) -> None:
    try:
        save(rows)
    except Exception:
        pass


def _safe_log_metadata_event(cache: object, category: str, message: str) -> None:
    log_event = getattr(cache, "log_event", None)
    if not callable(log_event):
        return
    try:
        log_event(category, message)
    except Exception:
        pass


def _prepare_concept_rows(
    attempt: ProviderAttempt,
    normalized: str,
    rows: list[StockConceptItem],
    limit: int,
) -> list[StockConceptItem]:
    normalized_rows = _clean_stock_concept_rows(_normalize_stock_concepts(normalized, rows, limit))
    if normalized_rows:
        return normalized_rows[:limit]
    if attempt.name == "local":
        raise ProviderCoverageMiss
    raise RuntimeError("概念归属返回为空")


def _clean_stock_concept_rows(rows: list[StockConceptItem]) -> list[StockConceptItem]:
    cleaned: list[StockConceptItem] = []
    seen_names: set[str] = set()
    for row in rows or []:
        rank = _positive_rank(row.rank)
        name = _required_text(row.name)
        change_pct = _finite_float(row.change_pct)
        source = _required_text(row.source)
        updated_at = _required_text(row.updated_at)
        if rank is None or change_pct is None or not all((name, source, updated_at)) or name in seen_names:
            continue
        seen_names.add(name)
        cleaned.append(
            row.model_copy(
                update={
                    "rank": rank,
                    "name": name,
                    "change_pct": change_pct,
                    "amount": _optional_non_negative_float(row.amount),
                    "turnover_rate": _optional_non_negative_float(row.turnover_rate),
                    "leading_stock": _optional_text(row.leading_stock),
                    "leading_stock_change_pct": _optional_finite_float(row.leading_stock_change_pct),
                    "match_reason": _required_text(row.match_reason) or "概念成分匹配",
                    "source": source,
                    "updated_at": updated_at,
                }
            )
        )
    return cleaned


def _positive_rank(value: object) -> int | None:
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return None
    return rank if rank > 0 else None


def _finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_finite_float(value: object) -> float | None:
    if value is None:
        return None
    return _finite_float(value)


def _optional_non_negative_float(value: object) -> float | None:
    number = _optional_finite_float(value)
    return number if number is not None and number >= 0 else None


def _required_text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    text = _required_text(value)
    return text or None


def _metadata_error_detail(errors: list[str], fallback: str) -> str:
    return "；".join(errors) if errors else fallback
