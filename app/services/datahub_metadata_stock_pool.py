from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import math

from app.models.schemas import StockInfo
from app.services.datahub_cache import _stock_pool_rows_are_authoritative
from app.services.datahub_metadata_mapping import (
    _match_stock_pool_keyword,
    _profile_with_local_industry,
    _stock_profile_match,
    _stock_profile_resolution_allows_local_only,
)
from app.services.datahub_metadata_provider import (
    _no_provider_message,
    _required_provider_call,
    _safe_log_metadata_event,
    _safe_log_metadata_event_async,
    _save_metadata_best_effort,
    _unsupported_provider_message,
)
from app.services.datahub_runtime import (
    ProviderAttempt,
    ProviderRuntime,
    TimedProviderCall,
    provider_source_name,
    run_cache_io,
    run_cache_io_best_effort,
)
from app.services.datahub_status import _provider_error_text
from app.services.provider_errors import ProviderProtocolError, sanitize_provider_error
from app.services.provider_utils import ensure_positive_limit
from app.utils.stock_pool import normalize_stock_pool_rows
from app.utils.symbols import normalize_symbol


STOCK_POOL_FALLBACK_SECONDS = 60 * 60 * 24 * 30
STOCK_POOL_MARKETS = frozenset({"SH", "SZ", "BJ"})
STOCK_POOL_BASELINE_COMPARISON_MIN_COUNT = 100
STOCK_POOL_MIN_BASELINE_RETAIN_RATIO = 0.90


def _ensure_optional_positive_limit(limit: int | None) -> None:
    if limit is not None:
        ensure_positive_limit(limit)


def _limit_stock_pool_rows(rows: list[StockInfo], limit: int | None) -> list[StockInfo]:
    return rows if limit is None else rows[:limit]


def _normalize_required_markets(markets: Iterable[str] | None) -> frozenset[str]:
    normalized = frozenset(str(market).strip().upper() for market in (markets or ()) if str(market).strip())
    unsupported = normalized - STOCK_POOL_MARKETS
    if unsupported:
        raise ValueError(f"股票池包含不支持的市场要求：{','.join(sorted(unsupported))}")
    return normalized


def _stock_pool_market_counts(rows: Iterable[StockInfo]) -> Counter[str]:
    return Counter(item.market for item in normalize_stock_pool_rows(rows))


def _stock_pool_markets(rows: Iterable[StockInfo]) -> set[str]:
    return set(_stock_pool_market_counts(rows))


def _stock_pool_covers_markets(rows: Iterable[StockInfo], required_markets: frozenset[str]) -> bool:
    return not required_markets or required_markets.issubset(_stock_pool_markets(rows))


def _normalize_market_minimums(
    values: Mapping[str, int] | None,
) -> tuple[tuple[str, int], ...]:
    normalized: dict[str, int] = {}
    for raw_market, raw_count in (values or {}).items():
        market = str(raw_market).strip().upper()
        if market not in STOCK_POOL_MARKETS:
            raise ValueError(f"股票池包含不支持的市场数量要求：{market or raw_market}")
        if isinstance(raw_count, bool) or int(raw_count) <= 0:
            raise ValueError(f"股票池市场最低数量必须为正整数：{market}")
        normalized[market] = int(raw_count)
    return tuple(sorted(normalized.items()))


def _configured_market_minimums(settings: object) -> tuple[tuple[str, int], ...]:
    return _normalize_market_minimums(
        {
            "SH": int(getattr(settings, "market_scan_min_sh_count", 1)),
            "SZ": int(getattr(settings, "market_scan_min_sz_count", 1)),
            "BJ": int(getattr(settings, "market_scan_min_bj_count", 1)),
        }
    )


def _stock_pool_meets_market_minimums(
    rows: Iterable[StockInfo],
    minimums: tuple[tuple[str, int], ...],
) -> bool:
    if not minimums:
        return True
    counts = _stock_pool_market_counts(rows)
    return all(counts[market] >= minimum for market, minimum in minimums)


def _stock_pool_covers_request(rows: list[StockInfo], request: "StockPoolRequest") -> bool:
    return _stock_pool_covers_markets(rows, request.required_markets) and _stock_pool_meets_market_minimums(
        rows,
        request.minimum_market_counts,
    )


def _stock_pool_shrinkage_diagnostic(
    rows: list[StockInfo],
    cached: list[StockInfo],
    *,
    authoritative_min_count: int,
    minimum_market_counts: tuple[tuple[str, int], ...],
) -> str | None:
    baseline_counts = _stock_pool_market_counts(cached)
    baseline_total = sum(baseline_counts.values())
    comparison_floor = max(
        STOCK_POOL_BASELINE_COMPARISON_MIN_COUNT,
        authoritative_min_count,
    )
    if baseline_total < comparison_floor or not _is_full_stock_pool_snapshot(
        cached,
        authoritative_min_count,
        minimum_market_counts,
    ):
        return None

    candidate_counts = _stock_pool_market_counts(rows)
    comparisons = (
        ("总量", sum(candidate_counts.values()), baseline_total),
        *((market, candidate_counts[market], baseline_counts[market]) for market in sorted(STOCK_POOL_MARKETS)),
    )
    shortfalls = [
        f"{label} {candidate}/{baseline}"
        for label, candidate, baseline in comparisons
        if baseline > 0 and candidate < math.ceil(baseline * STOCK_POOL_MIN_BASELINE_RETAIN_RATIO)
    ]
    if not shortfalls:
        return None
    return "股票池相对最近权威快照异常缩水：" + "，".join(shortfalls)


def _is_full_stock_pool_snapshot(
    rows: list[StockInfo],
    minimum_count: int,
    minimum_market_counts: tuple[tuple[str, int], ...],
) -> bool:
    normalized_rows = normalize_stock_pool_rows(rows)
    return (
        _stock_pool_rows_are_authoritative(normalized_rows, minimum_count)
        and _stock_pool_covers_markets(normalized_rows, STOCK_POOL_MARKETS)
        and _stock_pool_meets_market_minimums(normalized_rows, minimum_market_counts)
    )


def _is_authoritative_query_snapshot(rows: list[StockInfo], minimum_count: int) -> bool:
    normalized_rows = normalize_stock_pool_rows(rows)
    return _stock_pool_rows_are_authoritative(normalized_rows, minimum_count) and _stock_pool_covers_markets(
        normalized_rows,
        STOCK_POOL_MARKETS,
    )


def _merge_cached_stock_fields(rows: list[StockInfo], cached: list[StockInfo]) -> list[StockInfo]:
    normalized_rows = normalize_stock_pool_rows(rows)
    cached_by_symbol = {item.symbol: item for item in normalize_stock_pool_rows(cached)}
    merged: list[StockInfo] = []
    for item in normalized_rows:
        previous = cached_by_symbol.get(item.symbol)
        if previous is None:
            merged.append(item)
            continue
        merged.append(
            item.model_copy(
                update={
                    "industry": item.industry or previous.industry,
                    "list_date": item.list_date or previous.list_date,
                }
            )
        )
    return normalize_stock_pool_rows(merged)


@dataclass(frozen=True)
class StockPoolRequest:
    keyword: str | None
    limit: int | None
    refresh: bool
    required_markets: frozenset[str] = frozenset()
    minimum_market_counts: tuple[tuple[str, int], ...] = ()


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

    async def stock_pool(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets: Iterable[str] | None = None,
        minimum_market_counts: Mapping[str, int] | None = None,
    ) -> list[StockInfo]:
        _ensure_optional_positive_limit(limit)
        resolution = await self.stock_pool_resolution(
            keyword=keyword,
            limit=limit,
            refresh=refresh,
            required_markets=required_markets,
            minimum_market_counts=minimum_market_counts,
        )
        return resolution.list_rows()

    async def stock_pool_resolution(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets: Iterable[str] | None = None,
        minimum_market_counts: Mapping[str, int] | None = None,
    ) -> StockPoolResolution:
        _ensure_optional_positive_limit(limit)
        normalized_minimums = _normalize_market_minimums(minimum_market_counts)
        request = StockPoolRequest(
            keyword=keyword,
            limit=limit,
            refresh=refresh,
            required_markets=(_normalize_required_markets(required_markets) | frozenset(market for market, _minimum in normalized_minimums)),
            minimum_market_counts=normalized_minimums,
        )
        cached = await run_cache_io(self._cache_result, request)
        if cached.resolved:
            return cached

        errors: list[str] = []
        provider_rows = await self._provider_result(request, errors)
        if provider_rows.resolved:
            return provider_rows

        fallback = await run_cache_io(self._final_fallback, request)
        if fallback.resolved:
            return fallback
        raise RuntimeError("所有股票池数据源均不可用：" + "；".join(errors))

    def _cache_result(self, request: StockPoolRequest) -> StockPoolResolution:
        if request.refresh:
            return StockPoolResolution.miss("refresh-request")
        cached = self.cache.get_stock_pool(
            self.settings.stock_pool_cache_seconds,
            limit=request.limit,
            keyword=request.keyword,
        )
        if cached and _stock_pool_covers_request(cached, request):
            return StockPoolResolution.hit(cached, "fresh-cache")
        if self._fresh_cache_can_confirm_empty(request):
            return StockPoolResolution.hit(cached, "fresh-authoritative-empty")
        fallback = self._keyword_fallback(request)
        if fallback:
            return StockPoolResolution.hit(fallback, "keyword-fallback")
        return StockPoolResolution.miss("cache-miss")

    def _keyword_fallback(self, request: StockPoolRequest) -> list[StockInfo]:
        if not request.keyword:
            return []
        fallback = self.cache.get_stock_pool(
            STOCK_POOL_FALLBACK_SECONDS,
            limit=request.limit,
            keyword=request.keyword,
        )
        if fallback:
            _safe_log_metadata_event(
                self.cache,
                "fallback",
                f"股票池新缓存未命中，使用30天内本地股票主数据：{request.keyword}",
            )
        return fallback

    def _fresh_cache_can_confirm_empty(self, request: StockPoolRequest) -> bool:
        if not request.keyword or request.required_markets or request.minimum_market_counts:
            return False
        full_cache = self.cache.get_stock_pool(
            self.settings.stock_pool_cache_seconds,
            limit=None,
            keyword=None,
        )
        return _is_authoritative_query_snapshot(
            full_cache,
            self.settings.stock_pool_authoritative_min_count,
        )

    async def _provider_result(self, request: StockPoolRequest, errors: list[str]) -> StockPoolResolution:
        priority_rows = list(self.priority("stock"))
        if not priority_rows:
            errors.append(_no_provider_message("stock"))
            return StockPoolResolution.miss("provider-miss")
        for attempt in self.runtime.attempts(priority_rows, self.providers, "stock", errors):
            if not callable(getattr(attempt.provider, "stock_pool", None)):
                errors.append(_unsupported_provider_message(attempt, "stock"))
                continue
            try:
                rows = await self._fetch_provider_stock_pool(attempt)
            except Exception as exc:
                await self.runtime.record_attempt_failure_async(attempt, "stock", exc, errors)
                continue
            selected = self._select_rows(rows, request)
            if selected.resolved:
                await self._save_provider_stock_pool(rows)
                return selected
            self._append_coverage_error(attempt.name, rows, request, errors)
        return StockPoolResolution.miss("provider-miss")

    @staticmethod
    def _append_coverage_error(
        provider_name: str,
        rows: list[StockInfo],
        request: StockPoolRequest,
        errors: list[str],
    ) -> None:
        counts = _stock_pool_market_counts(rows)
        missing_markets = sorted(request.required_markets - set(counts))
        if missing_markets:
            errors.append(f"{provider_name}: 股票池缺少市场 {','.join(missing_markets)}")
            return
        if request.minimum_market_counts:
            shortages = [f"{market} {counts[market]}/{minimum}" for market, minimum in request.minimum_market_counts if counts[market] < minimum]
            errors.append(f"{provider_name}: 股票池分市场覆盖不足 {','.join(shortages)}")
            return
        errors.append(f"{provider_name}: 股票池覆盖不足，无法确认 {request.keyword}")

    async def _save_provider_stock_pool(self, rows: list[StockInfo]) -> None:
        if _is_full_stock_pool_snapshot(
            rows,
            self.settings.stock_pool_authoritative_min_count,
            _configured_market_minimums(self.settings),
        ):
            await _save_metadata_best_effort(lambda items: self.cache.replace_stock_pool(items), rows)
            return
        await _save_metadata_best_effort(lambda items: self.cache.save_stock_pool(items), rows)

    async def _fetch_provider_stock_pool(self, attempt: ProviderAttempt) -> list[StockInfo]:
        result: TimedProviderCall[list[StockInfo]] = await self.runtime.timed_provider_call(
            attempt.name,
            "stock",
            lambda: _required_provider_call(attempt.provider, "stock_pool", "股票池"),
            request_key=("stock_pool",),
            timeout_seconds=self.settings.stock_pool_provider_timeout_seconds,
        )
        provider_rows = result.value
        if not provider_rows:
            raise RuntimeError(f"{provider_source_name(attempt.provider, attempt.name)} 股票池返回为空")
        rows = normalize_stock_pool_rows(provider_rows)
        if not rows:
            raise ProviderProtocolError(f"{provider_source_name(attempt.provider, attempt.name)} 股票池不包含可持久化股票")
        cached = await run_cache_io_best_effort(
            self.cache.get_stock_pool,
            STOCK_POOL_FALLBACK_SECONDS,
            limit=None,
            keyword=None,
        )
        shrinkage = _stock_pool_shrinkage_diagnostic(
            rows,
            cached or [],
            authoritative_min_count=self.settings.stock_pool_authoritative_min_count,
            minimum_market_counts=_configured_market_minimums(self.settings),
        )
        if shrinkage:
            raise ProviderProtocolError(shrinkage)
        rows = _merge_cached_stock_fields(rows, cached or [])
        await self.runtime.record_attempt_success_async(attempt, "stock", result.latency_ms)
        return rows

    def _select_rows(self, rows: list[StockInfo], request: StockPoolRequest) -> StockPoolResolution:
        if not _stock_pool_covers_request(rows, request):
            return StockPoolResolution.miss("provider-required-market-miss")
        if not request.keyword:
            reason = (
                "provider-full-pool"
                if _is_full_stock_pool_snapshot(
                    rows,
                    self.settings.stock_pool_authoritative_min_count,
                    _configured_market_minimums(self.settings),
                )
                else "provider-partial-pool"
            )
            return StockPoolResolution.hit(_limit_stock_pool_rows(rows, request.limit), reason)
        matched = _match_stock_pool_keyword(rows, request.keyword)
        if matched:
            return StockPoolResolution.hit(
                _limit_stock_pool_rows(matched, request.limit),
                "provider-keyword-match",
            )
        if _is_authoritative_query_snapshot(
            rows,
            self.settings.stock_pool_authoritative_min_count,
        ):
            return StockPoolResolution.hit([], "provider-authoritative-empty")
        return StockPoolResolution.miss("provider-coverage-miss")

    def _final_fallback(self, request: StockPoolRequest) -> StockPoolResolution:
        max_age_seconds = self.settings.stock_pool_cache_seconds if request.required_markets else STOCK_POOL_FALLBACK_SECONDS
        fallback = self.cache.get_stock_pool(
            max_age_seconds=max_age_seconds,
            limit=request.limit,
            keyword=request.keyword,
        )
        if fallback and _stock_pool_covers_request(fallback, request):
            _safe_log_metadata_event(self.cache, "fallback", "股票池数据源失败，使用本地缓存股票池")
            return StockPoolResolution.hit(fallback, "stale-fallback")
        full_fallback = self.cache.get_stock_pool(
            max_age_seconds=STOCK_POOL_FALLBACK_SECONDS,
            limit=None,
            keyword=None,
        )
        if request.keyword and _is_authoritative_query_snapshot(
            full_fallback,
            self.settings.stock_pool_authoritative_min_count,
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
        if local_provider is None or not callable(getattr(local_provider, "stock_pool", None)):
            return None
        try:
            local_rows: list[StockInfo] = await self.runtime.call_provider(
                "local",
                "stock",
                lambda: _required_provider_call(local_provider, "stock_pool", "股票池"),
                request_key=("stock_pool",),
            )
        except Exception as exc:
            await _safe_log_metadata_event_async(
                self.cache,
                "fallback",
                f"本地个股基础资料不可用，行业兜底跳过：{target}；" f"{sanitize_provider_error(_provider_error_text(exc))}",
            )
            return None
        return _stock_profile_match(local_rows, target)
