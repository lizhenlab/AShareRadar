from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
import math
import time
from typing import TypeVar

from app.models.schemas import Kline, MinuteKline
from app.services.datahub_cache import _kline_cache_is_fresh, _normalize_minute_interval, _tag_klines, _tag_minute_klines
from app.services.datahub_runtime import ProviderRuntime
from app.services.provider_utils import ensure_positive_limit
from app.utils.market_data import filter_valid_klines, filter_valid_minute_klines
from app.utils.symbols import normalize_symbol


T = TypeVar("T", Kline, MinuteKline)

MAX_DAILY_KLINE_LIMIT = 10_000
DEFAULT_MAX_MINUTE_KLINE_LIMIT = 20_000


class KlineCoordinator:
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

    async def kline(self, symbol: str, limit: int = 120, use_cache: bool = True) -> list[Kline]:
        ensure_positive_limit(limit)
        limit = _bounded_limit(limit, getattr(self.settings, "max_kline_rows", MAX_DAILY_KLINE_LIMIT), MAX_DAILY_KLINE_LIMIT)
        normalize_symbol(symbol)
        if use_cache:
            cached = self.cache.get_klines(symbol, limit, self.settings.kline_cache_seconds)
            if len(cached) >= limit and _kline_cache_is_fresh(cached):
                return cached[-limit:]

        errors: list[str] = []
        fetched = await self._fetch_from_priority(
            kind="kline",
            errors=errors,
            fetch=lambda provider: _kline_call(provider, symbol, limit),
            prepare=lambda rows, source: _tag_klines(
                _non_empty_rows(_latest_daily_klines(rows, limit), "K线返回为空"),
                source,
                from_cache=False,
            ),
            save=lambda rows, source: self.cache.save_klines(symbol, rows, source),
        )
        if fetched is not None:
            return fetched

        fallback = self.cache.get_klines(symbol, limit, max_age_seconds=60 * 60 * 24 * 30)
        if fallback:
            self.cache.log_event("fallback", f"所有K线数据源失败，使用缓存K线：{symbol}")
            return _tag_klines(fallback, None, from_cache=True, fallback_used=True)
        raise RuntimeError("所有K线数据源均不可用：" + "；".join(errors))

    async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120, use_cache: bool = True) -> list[MinuteKline]:
        ensure_positive_limit(limit)
        limit = _bounded_limit(
            limit,
            getattr(self.settings, "max_minute_kline_rows", DEFAULT_MAX_MINUTE_KLINE_LIMIT),
            DEFAULT_MAX_MINUTE_KLINE_LIMIT,
        )
        normalize_symbol(symbol)
        normalized_interval = _normalize_minute_interval(interval)
        if use_cache:
            cached = self.cache.get_minute_klines(symbol, normalized_interval, limit, self.settings.minute_kline_cache_seconds)
            if len(cached) >= limit:
                return cached[-limit:]

        errors: list[str] = []
        fetched = await self._fetch_from_priority(
            kind="minute",
            errors=errors,
            fetch=lambda provider: _minute_kline_call(provider, symbol, normalized_interval, limit),
            prepare=lambda rows, source: _tag_minute_klines(
                _non_empty_rows(_latest_minute_klines(rows, limit), "分钟K线返回为空"),
                source,
                normalized_interval,
                from_cache=False,
            ),
            save=lambda rows, source: self.cache.save_minute_klines(symbol, normalized_interval, rows, source),
        )
        if fetched is not None:
            return fetched

        fallback = self.cache.get_minute_klines(symbol, normalized_interval, limit, max_age_seconds=60 * 60 * 6)
        if fallback:
            self.cache.log_event("fallback", f"所有分钟K线数据源失败，使用缓存分钟K线：{symbol}")
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
    ) -> list[T] | None:
        for index, name in self.priority(kind):
            if self.runtime.is_cooling(name, kind):
                errors.append(f"{name}: 最近失败，短暂冷却中")
                continue
            provider = self.providers.get(name)
            if provider is None:
                exc = RuntimeError("数据源未注册")
                errors.append(f"{name}: {exc}")
                self.runtime.record_failure(name, index, exc, kind)
                continue
            source = _provider_source_name(provider, name)
            started = time.perf_counter()
            try:
                awaitable = fetch(provider)
                if awaitable is None:
                    raise RuntimeError(f"数据源不支持{_kind_label(kind)}能力")
                rows = prepare(await self.runtime.call(awaitable), source)
                latency_ms = (time.perf_counter() - started) * 1000
                self.runtime.record_success(name, index, round(latency_ms, 2), kind)
                save(rows, source)
                return rows
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                self.runtime.record_failure(name, index, exc, kind)
        return None


def _non_empty_rows(rows: list[T], error: str) -> list[T]:
    if not rows:
        raise RuntimeError(error)
    return rows


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
        parsed = parsed.astimezone().replace(tzinfo=None)
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


def _kind_label(kind: str) -> str:
    return {"kline": "日K", "minute": "分钟K"}.get(kind, kind)


def _provider_source_name(provider: object, fallback: str) -> str:
    source = getattr(provider, "source_name", None)
    if isinstance(source, str) and source.strip():
        return source
    return fallback
