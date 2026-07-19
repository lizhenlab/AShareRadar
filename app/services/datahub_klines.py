from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from datetime import datetime
import math
from typing import TypeVar

from app.models.market import (
    DAILY_KLINE_CONTRACT_VERSION,
    DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,
    KlineAdjustmentMode,
    UNKNOWN_KLINE_DATA_VERSION,
)
from app.models.schemas import Kline, MinuteKline
from app.services.datahub_cache import (
    _kline_cache_is_fresh,
    _minute_kline_cache_is_fresh,
    _normalize_minute_interval,
    _tag_klines,
    _tag_minute_klines,
)
from app.services.datahub_runtime import (
    ProviderRuntime,
    provider_source_name,
    run_cache_io,
    run_cache_io_best_effort,
)
from app.services.provider_errors import ProviderCoverageMiss, ProviderProtocolError
from app.services.provider_utils import ensure_positive_limit
from app.utils.market_data import filter_valid_klines, filter_valid_minute_klines
from app.utils.market_time import market_local_naive, market_now_naive
from app.utils.symbols import normalize_symbol


T = TypeVar("T", Kline, MinuteKline)

MAX_DAILY_KLINE_LIMIT = 10_000
DEFAULT_MAX_MINUTE_KLINE_LIMIT = 20_000
DAILY_KLINE_PRESERVATION_MAX_AGE_SECONDS = 10**9
DAILY_KLINE_INCREMENTAL_REFRESH_ROWS = 40
DAILY_KLINE_INCREMENTAL_MIN_OVERLAP_ROWS = 5
DailyKlineContractKey = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class _DailyProviderExhaustion:
    contract: DailyKlineContractKey
    row_count: int
    requested_limit: int
    provider_chain: tuple[str, ...]


@dataclass(frozen=True)
class _DailyFetchOutcome:
    rows: list[Kline] | None
    all_providers_short: bool = False
    source: str | None = None


class KlineCoordinator:
    def __init__(
        self,
        *,
        settings,
        cache,
        providers: dict,
        runtime: ProviderRuntime,
        priority: Callable[[str], list[tuple[int, str]]],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.providers = providers
        self.runtime = runtime
        self.priority = priority
        self._now = now or _kline_now
        self._daily_provider_exhaustion: dict[str, _DailyProviderExhaustion] = {}

    async def kline(self, symbol: str, limit: int = 120, use_cache: bool = True) -> list[Kline]:
        ensure_positive_limit(limit)
        limit = _bounded_limit(limit, getattr(self.settings, "max_kline_rows", MAX_DAILY_KLINE_LIMIT), MAX_DAILY_KLINE_LIMIT)
        normalized_symbol = _normalized_symbol_key(symbol)
        current = self._now()
        priority_rows = self.priority("kline")
        provider_chain = _provider_chain_key(priority_rows)
        if use_cache:
            cached = await self._fresh_daily_cache(
                symbol,
                limit=limit,
                current=current,
                normalized_symbol=normalized_symbol,
                provider_chain=provider_chain,
            )
            if cached is not None:
                return cached

        preserved = await self._load_compatible_daily_cache(
            symbol,
            DAILY_KLINE_PRESERVATION_MAX_AGE_SECONDS,
        )
        fetch_limit = max(limit, len(preserved))
        errors: list[str] = []
        if use_cache and len(preserved) >= limit:
            incremental = await self._incremental_daily_refresh(
                symbol=symbol,
                normalized_symbol=normalized_symbol,
                preserved=preserved,
                priority_rows=priority_rows,
                current=current,
                errors=errors,
            )
            if incremental is not None:
                return incremental[-limit:]

        fetched = await self._full_daily_refresh(
            symbol=symbol,
            normalized_symbol=normalized_symbol,
            fetch_limit=fetch_limit,
            priority_rows=priority_rows,
            provider_chain=provider_chain,
            current=current,
            errors=errors,
        )
        if fetched is not None:
            return fetched[-limit:]

        fallback = await self._daily_fallback(symbol, limit)
        if fallback is not None:
            return fallback
        raise RuntimeError("所有K线数据源均不可用：" + "；".join(errors))

    async def _fresh_daily_cache(
        self,
        symbol: str,
        *,
        limit: int,
        current: datetime,
        normalized_symbol: str,
        provider_chain: tuple[str, ...],
    ) -> list[Kline] | None:
        cached = await self._load_compatible_daily_cache(
            symbol,
            self.settings.kline_cache_seconds,
        )
        if not cached or not _daily_cache_has_requested_coverage(
            cached,
            limit,
            known_exhaustion=self._daily_provider_exhaustion.get(normalized_symbol),
            provider_chain=provider_chain,
        ):
            return None
        return cached[-limit:] if _kline_cache_is_fresh(cached, now=current) else None

    async def _full_daily_refresh(
        self,
        *,
        symbol: str,
        normalized_symbol: str,
        fetch_limit: int,
        priority_rows: list[tuple[int, str]],
        provider_chain: tuple[str, ...],
        current: datetime,
        errors: list[str],
    ) -> list[Kline] | None:
        outcome = await self._fetch_daily_from_priority(
            priority_rows=priority_rows,
            errors=errors,
            fetch=lambda provider: _kline_call(provider, symbol, fetch_limit),
            prepare=lambda rows, source: _prepare_daily_klines(rows, source, symbol, fetch_limit, current),
            save=lambda rows, source: self.cache.save_klines(symbol, rows, source),
            requested_limit=fetch_limit,
            request_key=(normalized_symbol, fetch_limit, DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE),
        )
        fetched = outcome.rows
        if fetched is not None:
            self._remember_daily_provider_coverage(
                normalized_symbol,
                fetched,
                fetch_limit,
                provider_chain,
                exhausted=outcome.all_providers_short,
            )
        return fetched

    async def _incremental_daily_refresh(
        self,
        *,
        symbol: str,
        normalized_symbol: str,
        preserved: list[Kline],
        priority_rows: list[tuple[int, str]],
        current: datetime,
        errors: list[str],
    ) -> list[Kline] | None:
        refresh_limit = min(DAILY_KLINE_INCREMENTAL_REFRESH_ROWS, len(preserved))
        outcome = await self._fetch_daily_from_priority(
            priority_rows=priority_rows,
            errors=errors,
            fetch=lambda provider: _kline_call(provider, symbol, refresh_limit),
            prepare=lambda rows, source: _prepare_daily_klines(
                rows,
                source,
                symbol,
                refresh_limit,
                current,
            ),
            save=lambda _rows, _source: None,
            requested_limit=refresh_limit,
            request_key=(
                normalized_symbol,
                "incremental",
                refresh_limit,
                DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,
            ),
        )
        if outcome.rows is None:
            return None
        merged = _merge_incremental_daily_klines(
            preserved,
            outcome.rows,
            target_count=len(preserved),
        )
        if merged is None:
            await _safe_log_kline_event(
                self.cache,
                "kline_incremental_rebase",
                f"日K增量契约或 OHLCV 重叠校验未通过，改为全量刷新：{symbol}",
            )
            return None
        await _save_rows_best_effort(
            lambda rows, source: self.cache.save_klines(symbol, rows, source),
            merged,
            outcome.source or merged[-1].source or "日K增量刷新",
        )
        return merged

    async def _daily_fallback(self, symbol: str, limit: int) -> list[Kline] | None:
        fallback = await run_cache_io(
            self.cache.get_klines,
            symbol,
            limit,
            max_age_seconds=60 * 60 * 24 * 30,
        )
        fallback = _compatible_daily_klines(
            fallback,
            expected_adjustment_mode=DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,
        )
        if not fallback:
            return None
        await _safe_log_kline_event(self.cache, "fallback", f"K线数据源失败或无覆盖，使用缓存K线：{symbol}")
        return _tag_klines(fallback, None, from_cache=True, fallback_used=True)

    async def _load_compatible_daily_cache(self, symbol: str, max_age_seconds: int) -> list[Kline]:
        cached = await run_cache_io_best_effort(
            self.cache.get_klines,
            symbol,
            MAX_DAILY_KLINE_LIMIT,
            max_age_seconds,
        )
        return _compatible_daily_klines(
            cached or [],
            expected_adjustment_mode=DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,
        )

    def _remember_daily_provider_coverage(
        self,
        normalized_symbol: str,
        rows: list[Kline],
        requested_limit: int,
        provider_chain: tuple[str, ...],
        *,
        exhausted: bool,
    ) -> None:
        self._daily_provider_exhaustion.pop(normalized_symbol, None)
        if not exhausted or len(rows) >= requested_limit:
            return
        contract = _daily_contract_key(rows)
        if contract is not None:
            self._daily_provider_exhaustion[normalized_symbol] = _DailyProviderExhaustion(
                contract=contract,
                row_count=len(rows),
                requested_limit=requested_limit,
                provider_chain=provider_chain,
            )

    async def _fetch_daily_from_priority(
        self,
        *,
        priority_rows: list[tuple[int, str]],
        errors: list[str],
        fetch: Callable[[object], Awaitable[list[Kline]] | None],
        prepare: Callable[[list[Kline], str], list[Kline]],
        save: Callable[[list[Kline], str], None],
        requested_limit: int,
        request_key: Hashable,
    ) -> _DailyFetchOutcome:
        candidates: list[tuple[list[Kline], str]] = []
        attempted_count = 0
        short_response_count = 0
        for attempt in self.runtime.attempts(priority_rows, self.providers, "kline", errors):
            fallback_attempt = attempted_count > 0 or bool(errors)
            attempted_count += 1
            source = provider_source_name(attempt.provider, attempt.name)
            result = None
            try:
                result = await self.runtime.timed_provider_call(
                    attempt.name,
                    "kline",
                    lambda: _run_provider_fetch(fetch, attempt.provider, "kline"),
                    request_key=request_key,
                )
                rows = prepare(result.value, source)
                if fallback_attempt:
                    rows = _tag_klines(
                        rows,
                        source,
                        from_cache=False,
                        fallback_used=True,
                    )
                await self.runtime.record_attempt_success_async(attempt, "kline", result.latency_ms)
                if len(rows) >= requested_limit:
                    await _save_rows_best_effort(save, rows, source)
                    return _DailyFetchOutcome(rows=rows, source=source)
                candidates.append((rows, source))
                short_response_count += 1
            except asyncio.CancelledError:
                raise
            except ProviderCoverageMiss as exc:
                if result is not None:
                    await self.runtime.record_attempt_success_async(attempt, "kline", result.latency_ms)
                await self.runtime.record_attempt_failure_async(attempt, "kline", exc, errors)
                short_response_count += 1
            except Exception as exc:
                await self.runtime.record_attempt_failure_async(attempt, "kline", exc, errors)

        if not candidates:
            return _DailyFetchOutcome(rows=None)
        rows, source = max(candidates, key=lambda candidate: len(candidate[0]))
        await _save_rows_best_effort(save, rows, source)
        all_providers_short = attempted_count == len(priority_rows) and short_response_count == attempted_count
        return _DailyFetchOutcome(
            rows=rows,
            all_providers_short=all_providers_short,
            source=source,
        )

    async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120, use_cache: bool = True) -> list[MinuteKline]:
        ensure_positive_limit(limit)
        limit = _bounded_limit(
            limit,
            getattr(self.settings, "max_minute_kline_rows", DEFAULT_MAX_MINUTE_KLINE_LIMIT),
            DEFAULT_MAX_MINUTE_KLINE_LIMIT,
        )
        normalized_symbol = _normalized_symbol_key(symbol)
        normalized_interval = _normalize_minute_interval(interval)
        current = self._now()
        if use_cache:
            cached = await run_cache_io(
                self.cache.get_minute_klines,
                symbol,
                normalized_interval,
                limit,
                self.settings.minute_kline_cache_seconds,
            )
            if cached and _minute_kline_cache_is_fresh(cached, normalized_interval, now=current):
                return cached[-limit:]

        errors: list[str] = []
        fetched = await self._fetch_from_priority(
            kind="minute",
            errors=errors,
            fetch=lambda provider: _minute_kline_call(provider, symbol, normalized_interval, limit),
            prepare=lambda rows, source: _prepare_minute_klines(
                rows,
                source,
                symbol,
                normalized_interval,
                limit,
                current,
            ),
            save=lambda rows, source: self.cache.save_minute_klines(symbol, normalized_interval, rows, source),
            mark_fallback=lambda rows, source: _tag_minute_klines(
                rows,
                source,
                normalized_interval,
                from_cache=False,
                fallback_used=True,
            ),
            request_key=(normalized_symbol, normalized_interval, limit),
        )
        if fetched is not None:
            return fetched

        fallback = await run_cache_io(
            self.cache.get_minute_klines,
            symbol,
            normalized_interval,
            limit,
            max_age_seconds=60 * 60 * 6,
        )
        if fallback:
            await _safe_log_kline_event(self.cache, "fallback", f"分钟K线数据源失败或无覆盖，使用缓存分钟K线：{symbol}")
            return _tag_minute_klines(fallback, None, normalized_interval, from_cache=True, fallback_used=True)
        raise RuntimeError("所有分钟K线数据源均不可用：" + "；".join(errors))

    async def _fetch_from_priority(
        self,
        *,
        kind: str,
        errors: list[str],
        fetch: Callable[[object], Awaitable[list[T]] | None],
        prepare: Callable[[list[T], str], list[T]],
        save: Callable[[list[T], str], None],
        mark_fallback: Callable[[list[T], str], list[T]],
        request_key: Hashable,
    ) -> list[T] | None:
        attempted_count = 0
        for attempt in self.runtime.attempts(self.priority(kind), self.providers, kind, errors):
            fallback_attempt = attempted_count > 0 or bool(errors)
            attempted_count += 1
            source = provider_source_name(attempt.provider, attempt.name)
            result = None
            try:
                result = await self.runtime.timed_provider_call(
                    attempt.name,
                    kind,
                    lambda: _run_provider_fetch(fetch, attempt.provider, kind),
                    request_key=request_key,
                )
                rows = prepare(result.value, source)
                if fallback_attempt:
                    rows = mark_fallback(rows, source)
                await self.runtime.record_attempt_success_async(attempt, kind, result.latency_ms)
                await _save_rows_best_effort(save, rows, source)
                return rows
            except asyncio.CancelledError:
                raise
            except ProviderCoverageMiss as exc:
                if result is not None:
                    await self.runtime.record_attempt_success_async(attempt, kind, result.latency_ms)
                await self.runtime.record_attempt_failure_async(attempt, kind, exc, errors)
            except Exception as exc:
                await self.runtime.record_attempt_failure_async(attempt, kind, exc, errors)
        return None


async def _run_provider_fetch(
    fetch: Callable[[object], Awaitable[list[T]] | None],
    provider: object,
    kind: str,
) -> list[T]:
    awaitable = fetch(provider)
    if awaitable is None:
        raise RuntimeError(f"数据源不支持{_kind_label(kind)}能力")
    return await awaitable


def _non_empty_rows(rows: list[T], error: str) -> list[T]:
    if not rows:
        raise RuntimeError(error)
    return rows


def _prepare_daily_klines(
    rows: list[Kline],
    source: str,
    symbol: str,
    limit: int,
    current: datetime,
) -> list[Kline]:
    if not isinstance(rows, list):
        raise ProviderProtocolError(f"{source} 日K返回结构异常")
    if not rows:
        raise ProviderCoverageMiss(f"{source} 日K未覆盖请求股票：{symbol}")
    cleaned = _latest_daily_klines(rows, limit)
    if not cleaned:
        raise ProviderProtocolError("K线返回为空")
    tagged = _tag_klines(cleaned, source, from_cache=False)
    _validate_daily_kline_contract(
        tagged,
        expected_adjustment_mode=DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,
    )
    if not _kline_cache_is_fresh(tagged, now=current):
        raise ProviderProtocolError(f"{source} 日K业务时间无效或已过期：{tagged[-1].date}")
    return tagged


def _prepare_minute_klines(
    rows: list[MinuteKline],
    source: str,
    symbol: str,
    interval: str,
    limit: int,
    current: datetime,
) -> list[MinuteKline]:
    if not isinstance(rows, list):
        raise ProviderProtocolError(f"{source} 分钟K线返回结构异常")
    if not rows:
        raise ProviderCoverageMiss(f"{source} 分钟K线未覆盖请求股票：{symbol}")
    cleaned = _latest_minute_klines(rows, limit)
    if not cleaned:
        raise ProviderProtocolError("分钟K线返回为空")
    if not _minute_kline_cache_is_fresh(cleaned, interval, now=current):
        raise ProviderProtocolError(f"{source} 分钟K线业务时间无效或已过期：{cleaned[-1].timestamp}")
    return _tag_minute_klines(cleaned, source, interval, from_cache=False)


async def _save_rows_best_effort(save: Callable[[list[T], str], None], rows: list[T], source: str) -> None:
    await run_cache_io_best_effort(save, rows, source)


async def _safe_log_kline_event(cache: object, category: str, message: str) -> None:
    log_event = getattr(cache, "log_event", None)
    if not callable(log_event):
        return
    await run_cache_io_best_effort(log_event, category, message)


def _kline_call(provider: object, symbol: str, limit: int) -> Awaitable[list[Kline]] | None:
    kline = getattr(provider, "kline", None)
    if not callable(kline):
        return None
    return kline(symbol, limit)


def _minute_kline_call(provider: object, symbol: str, interval: str, limit: int) -> Awaitable[list[MinuteKline]] | None:
    minute_kline = getattr(provider, "minute_kline", None)
    if not callable(minute_kline):
        return None
    return minute_kline(symbol, interval, limit)


def _latest_daily_klines(rows: list[Kline], limit: int) -> list[Kline]:
    return _latest_rows(filter_valid_klines(rows or []), limit, key=lambda row: row.date)


def _compatible_daily_klines(
    rows: list[Kline],
    *,
    expected_adjustment_mode: KlineAdjustmentMode,
) -> list[Kline]:
    if not rows:
        return []
    try:
        _validate_daily_kline_contract(
            rows,
            expected_adjustment_mode=expected_adjustment_mode,
            allow_revision_chain=True,
        )
    except ProviderProtocolError:
        return []
    return rows


def _daily_cache_has_requested_coverage(
    rows: list[Kline],
    requested_limit: int,
    *,
    known_exhaustion: _DailyProviderExhaustion | None,
    provider_chain: tuple[str, ...],
) -> bool:
    if len(rows) >= requested_limit:
        return True
    if known_exhaustion is None:
        return False
    return (
        known_exhaustion.provider_chain == provider_chain
        and requested_limit <= known_exhaustion.requested_limit
        and len(rows) == known_exhaustion.row_count
        and _daily_contract_key(rows) == known_exhaustion.contract
    )


def _daily_contract_key(rows: list[Kline]) -> DailyKlineContractKey | None:
    if not rows:
        return None
    item = rows[-1]
    return (
        item.adjustment_mode,
        str(item.as_of or "").strip(),
        item.data_version.strip(),
        item.contract_version.strip(),
        str(item.source or "").strip(),
    )


def _merge_incremental_daily_klines(
    preserved: list[Kline],
    refresh: list[Kline],
    *,
    target_count: int,
) -> list[Kline] | None:
    if not preserved or not refresh or target_count <= 0:
        return None
    if not _incremental_daily_contracts_are_compatible(preserved, refresh):
        return None
    preserved_by_date = {item.date: item for item in preserved}
    refresh_by_date = {item.date: item for item in refresh}
    overlap_dates = sorted(preserved_by_date.keys() & refresh_by_date.keys())
    required_overlap = min(
        DAILY_KLINE_INCREMENTAL_MIN_OVERLAP_ROWS,
        len(preserved),
        len(refresh),
    )
    if (
        len(overlap_dates) < required_overlap
        or preserved[-1].date not in refresh_by_date
        or any(not _same_adjusted_ohlcv(preserved_by_date[date], refresh_by_date[date]) for date in overlap_dates)
    ):
        return None

    combined = {**preserved_by_date, **refresh_by_date}
    return [combined[date] for date in sorted(combined)][-target_count:]


def _incremental_daily_contracts_are_compatible(
    preserved: list[Kline],
    refresh: list[Kline],
) -> bool:
    try:
        _validate_daily_kline_contract(
            preserved,
            expected_adjustment_mode=DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,
            allow_revision_chain=True,
        )
        _validate_daily_kline_contract(
            refresh,
            expected_adjustment_mode=DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,
        )
    except ProviderProtocolError:
        return False
    if _daily_sources(preserved) != _daily_sources(refresh):
        return False
    preserved_as_of = max(_contract_as_of_key(item.as_of) for item in preserved)
    refresh_as_of = max(_contract_as_of_key(item.as_of) for item in refresh)
    return refresh_as_of >= preserved_as_of


def _same_adjusted_ohlcv(left: Kline, right: Kline) -> bool:
    return all(
        _same_finite_number(left_value, right_value, rel_tol=rel_tol, abs_tol=abs_tol)
        for left_value, right_value, rel_tol, abs_tol in (
            (left.open, right.open, 1e-5, 1e-4),
            (left.close, right.close, 1e-5, 1e-4),
            (left.high, right.high, 1e-5, 1e-4),
            (left.low, right.low, 1e-5, 1e-4),
            (left.volume, right.volume, 1e-6, 1e-3),
        )
    )


def _same_finite_number(
    left: object,
    right: object,
    *,
    rel_tol: float,
    abs_tol: float,
) -> bool:
    try:
        left_value = float(left)
        right_value = float(right)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(left_value)
        and math.isfinite(right_value)
        and math.isclose(left_value, right_value, rel_tol=rel_tol, abs_tol=abs_tol)
    )


def _provider_chain_key(priority_rows: list[tuple[int, str]]) -> tuple[str, ...]:
    return tuple(name for _index, name in priority_rows)


def _validate_daily_kline_contract(
    rows: list[Kline],
    *,
    expected_adjustment_mode: KlineAdjustmentMode,
    allow_revision_chain: bool = False,
) -> None:
    _require_adjustment_mode(rows, expected_adjustment_mode)
    _require_uniform_contract_value(_daily_sources(rows), "日K序列 source 不一致或缺失")
    _require_contract_version(rows)
    if allow_revision_chain:
        _require_compatible_revision_chain(rows)
        return
    _require_uniform_contract_value(
        {str(item.as_of or "").strip() for item in rows},
        "日K序列 as_of 不一致或缺失",
    )
    _require_uniform_contract_value(
        {item.data_version.strip() for item in rows},
        "日K序列 data_version 不一致或缺失",
        rejected={UNKNOWN_KLINE_DATA_VERSION},
    )
    _require_snapshot_as_of_covers_rows(rows)


def _require_adjustment_mode(rows: list[Kline], expected: KlineAdjustmentMode) -> None:
    values = {item.adjustment_mode for item in rows}
    if values == {expected}:
        return
    modes = ",".join(sorted(values)) or "empty"
    raise ProviderProtocolError(f"日K复权契约不兼容：期望 {expected}，实际 {modes}")


def _require_uniform_contract_value(
    values: set[str],
    error: str,
    *,
    rejected: set[str] | None = None,
) -> None:
    if len(values) == 1 and "" not in values and not values.intersection(rejected or set()):
        return
    raise ProviderProtocolError(error)


def _require_contract_version(rows: list[Kline]) -> None:
    versions = {item.contract_version.strip() for item in rows}
    if versions == {DAILY_KLINE_CONTRACT_VERSION}:
        return
    label = ",".join(sorted(versions)) or "empty"
    raise ProviderProtocolError(f"日K contract_version 不兼容：{label}")


def _require_compatible_revision_chain(rows: list[Kline]) -> None:
    revisions: list[tuple[datetime, datetime, str]] = []
    for item in rows:
        row_date = _sort_key(item.date)
        as_of = _contract_as_of_key(item.as_of)
        data_version = item.data_version.strip()
        if row_date is None or as_of is None or as_of < row_date:
            raise ProviderProtocolError("日K序列 as_of 无法证明覆盖对应行情日期")
        if not data_version or data_version == UNKNOWN_KLINE_DATA_VERSION:
            raise ProviderProtocolError("日K序列 data_version 不一致或缺失")
        revisions.append((row_date, as_of, data_version))
    revisions.sort(key=lambda item: item[0])
    if any(current[1] < previous[1] for previous, current in zip(revisions, revisions[1:])):
        raise ProviderProtocolError("日K序列 revision 链随行情日期倒退")


def _require_snapshot_as_of_covers_rows(rows: list[Kline]) -> None:
    as_of = _contract_as_of_key(rows[0].as_of)
    for item in rows:
        row_date = _sort_key(item.date)
        if row_date is None or as_of < row_date:
            raise ProviderProtocolError("日K序列 as_of 无法证明覆盖对应行情日期")


def _daily_sources(rows: list[Kline]) -> set[str]:
    return {str(item.source or "").strip() for item in rows}


def _contract_as_of_key(value: object) -> datetime:
    parsed = _sort_key(value)
    if parsed is None:
        raise ProviderProtocolError("日K序列 as_of 无法解析")
    return parsed


def _latest_minute_klines(rows: list[MinuteKline], limit: int) -> list[MinuteKline]:
    return _latest_rows(filter_valid_minute_klines(rows or []), limit, key=lambda row: row.timestamp)


def _latest_rows(rows: list[T], limit: int, *, key: Callable[[T], object]) -> list[T]:
    indexed = [item for item in ((_sort_key(key(row)), row) for row in rows) if item[0] is not None]
    indexed.sort(key=lambda item: item[0])
    return [row for _sort_value, row in indexed[-limit:]]


def _sort_key(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = market_local_naive(parsed)
    return parsed


def _bounded_limit(limit: int, max_limit: object, default: int) -> int:
    return min(limit, _positive_int_or_default(max_limit, default))


def _positive_int_or_default(value: object, default: int) -> int:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric) or numeric <= 0:
        return default
    return max(1, int(numeric))


def _normalized_symbol_key(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    return f"{code}.{market.upper()}"


def _kline_now() -> datetime:
    return market_now_naive()


def _kind_label(kind: str) -> str:
    return {"kline": "日K", "minute": "分钟K"}.get(kind, kind)
