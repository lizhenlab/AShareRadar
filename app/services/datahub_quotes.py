from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
import math

from app.models.schemas import DataQuality, Kline, Quote
from app.services.data_quality import build_data_quality
from app.services.data_quality_time import parse_quote_time, quote_cache_lookup_seconds, quote_event_time_error
from app.services.datahub_cache import _matched_quotes, _normalize_symbols, _tag_cached_quotes
from app.services.datahub_runtime import (
    ProviderAttempt,
    ProviderRuntime,
    provider_source_name,
    run_cache_io,
    run_cache_io_best_effort,
)
from app.services.datahub_status import _provider_source_key
from app.services.provider_errors import (
    ProviderCoverageMiss,
    ProviderProtocolError,
    is_provider_coverage_miss,
)
from app.services.trading_calendar import is_trading_day
from app.utils.market_data import filter_valid_quotes
from app.utils.symbols import standard_symbol


CONSISTENCY_MAX_TIMESTAMP_SKEW_SECONDS = 15 * 60


@dataclass(frozen=True)
class ConsistencySummary:
    compared: int
    failed: int
    gaps: list[float]
    coverage_missed: int = 0
    stale: int = 0


class QuoteCoordinator:
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
        self._now = now or _quote_now

    async def quote(self, symbol: str, use_cache: bool = True) -> Quote:
        requested = standard_symbol(symbol)
        return (await self.quotes([requested], use_cache=use_cache))[0]

    async def quotes(self, symbols: Iterable[str], use_cache: bool = True) -> list[Quote]:
        requested_symbols = _normalize_symbols(symbols)
        if not requested_symbols:
            return []
        symbol_list = list(dict.fromkeys(requested_symbols))
        current = self._now()

        collected = await self._short_cache_quotes(symbol_list, current) if use_cache else {}
        if len(collected) == len(symbol_list):
            return _ordered_quotes(collected, requested_symbols)

        errors = await self._fill_realtime_quotes(symbol_list, collected, current)
        if len(collected) == len(symbol_list):
            return _ordered_quotes(collected, requested_symbols)

        await self._fill_fallback_quotes(symbol_list, collected, current)
        if len(collected) == len(symbol_list):
            await _safe_log_quote_event(self.cache, "fallback", "部分或全部实时数据源失败或无覆盖，缺失个股使用最近有效交易时刻缓存报价")
            return _ordered_quotes(collected, requested_symbols)

        unresolved = [symbol for symbol in symbol_list if symbol not in collected]
        raise RuntimeError("实时行情未完整返回，缺失：" + "、".join(unresolved) + "；" + "；".join(errors))

    async def _short_cache_quotes(self, symbol_list: list[str], current: datetime) -> dict[str, Quote]:
        cached = await run_cache_io(self.cache.get_quotes, symbol_list, self.settings.quote_cache_seconds)
        cached = _tag_cached_quotes(_quotes_with_valid_event_time(cached, current), "短时缓存")
        return _quotes_by_symbol(cached)

    async def _fill_realtime_quotes(
        self,
        symbol_list: list[str],
        collected: dict[str, Quote],
        current: datetime,
    ) -> list[str]:
        errors: list[str] = []
        for attempt in self.runtime.attempts(self.priority("quote"), self.providers, "quote", errors):
            remaining = [symbol for symbol in symbol_list if symbol not in collected]
            if not remaining:
                break
            await self._try_provider_quotes(attempt, remaining, collected, errors, current)
        return errors

    async def _try_provider_quotes(
        self,
        attempt: ProviderAttempt,
        remaining: list[str],
        collected: dict[str, Quote],
        errors: list[str],
        current: datetime,
    ) -> None:
        source = provider_source_name(attempt.provider, attempt.name)
        try:
            result = await self.runtime.timed_provider_call(
                attempt.name,
                "quote",
                lambda: attempt.provider.quotes(remaining),
                request_key=_quote_request_key(remaining),
            )
            raw_quotes = result.value
            if not isinstance(raw_quotes, list):
                raise ProviderProtocolError(f"{source} 行情返回结构异常")
            if not raw_quotes:
                await self.runtime.record_attempt_success_async(attempt, "quote", result.latency_ms)
                raise ProviderCoverageMiss(f"{source} 未覆盖请求股票：{','.join(remaining)}")
            quotes = filter_valid_quotes(raw_quotes)
            matched, missing = _matched_quotes(quotes, remaining)
            if not matched:
                raise ProviderProtocolError(f"{source} 行情缺失或字段无效：{','.join(missing)}")
            event_time_error = _provider_event_time_error(source, matched, current)
            if event_time_error:
                raise ProviderProtocolError(event_time_error)
            await self.runtime.record_attempt_success_async(attempt, "quote", result.latency_ms)
            collected.update(_quotes_by_symbol(matched))
            await self._save_quotes_best_effort(matched, current)
            if missing:
                message = f"{source} 批量行情部分缺失：{','.join(missing)}"
                errors.append(f"{attempt.name}: {message}")
                await _safe_log_quote_event(self.cache, "fallback", message)
        except Exception as exc:
            await self.runtime.record_attempt_failure_async(attempt, "quote", exc, errors)

    async def _save_quotes_best_effort(self, quotes: list[Quote], current: datetime) -> None:
        valid_quotes = _quotes_with_valid_event_time(quotes, current)
        if valid_quotes:
            await run_cache_io_best_effort(self.cache.save_quotes, valid_quotes)

    async def _fill_fallback_quotes(
        self,
        symbol_list: list[str],
        collected: dict[str, Quote],
        current: datetime,
    ) -> None:
        missing_symbols = [symbol for symbol in symbol_list if symbol not in collected]
        fallback_rows = await run_cache_io(
            self.cache.get_quotes,
            missing_symbols,
            max_age_seconds=quote_cache_lookup_seconds(current),
        )
        fallback = _tag_cached_quotes(_quotes_with_valid_event_time(fallback_rows, current), "兜底缓存")
        collected.update(_quotes_by_symbol(fallback))

    async def quote_with_quality(
        self,
        symbol: str,
        use_cache: bool = True,
        check_consistency: bool = True,
    ) -> tuple[Quote, DataQuality]:
        quote = await self.quote(symbol, use_cache=use_cache)
        quality = await self.assess_quote_quality(quote, use_cache=use_cache, check_consistency=check_consistency)
        return quote, quality

    async def assess_quote_quality(
        self,
        quote: Quote,
        klines: list[Kline] | None = None,
        use_cache: bool = True,
        require_kline: bool = True,
        check_consistency: bool = True,
    ) -> DataQuality:
        quality_klines = klines
        if quality_klines is None:
            if require_kline and use_cache:
                quality_klines = await run_cache_io(
                    self.cache.get_klines,
                    f"{quote.code}.{quote.market}",
                    120,
                    self.settings.kline_cache_seconds,
                )
            else:
                quality_klines = []
        consistency_level, notes, penalty = await self.consistency(quote, check_consistency=check_consistency)
        return build_data_quality(
            quote,
            quality_klines,
            consistency_level=consistency_level,
            consistency_notes=notes,
            consistency_penalty=penalty,
            require_kline=require_kline,
        )

    async def consistency(self, quote: Quote, check_consistency: bool = True) -> tuple[str, list[str], int]:
        skipped = _consistency_skip_result(quote, check_consistency)
        if skipped:
            return skipped
        target_symbol = f"{quote.code}.{quote.market}"
        current_source = _provider_source_key(quote.source)
        tasks = self._consistency_tasks(current_source, target_symbol)
        if not tasks:
            return "单源可用", ["当前只有主行情源可用，多源一致性暂无法确认。"], 8

        summary = await self._summarize_consistency_results(quote, await asyncio.gather(*tasks))
        return await self._consistency_result(target_symbol, summary)

    def _consistency_tasks(self, current_source: str, target_symbol: str) -> list:
        tasks = []
        for index, name in self.priority("quote"):
            if name in {"demo"}:
                continue
            if self.runtime.is_cooling(name, "quote"):
                continue
            provider = self.providers.get(name)
            if provider is None:
                continue
            if _provider_source_key(provider_source_name(provider, name)) == current_source:
                continue
            tasks.append(self.consistency_probe(index, name, provider, target_symbol))
        return tasks

    async def _summarize_consistency_results(
        self,
        quote: Quote,
        results: list[dict[str, object]],
    ) -> ConsistencySummary:
        gaps: list[float] = []
        counts = {"compared": 0, "failed": 0, "coverage_missed": 0, "stale": 0}
        for result in results:
            outcome, gap_pct = await self._consistency_observation(quote, result)
            counts[outcome] += 1
            if gap_pct is not None:
                gaps.append(gap_pct)
        return ConsistencySummary(
            compared=counts["compared"],
            failed=counts["failed"],
            gaps=gaps,
            coverage_missed=counts["coverage_missed"],
            stale=counts["stale"],
        )

    async def _consistency_observation(
        self,
        primary: Quote,
        result: dict[str, object],
    ) -> tuple[str, float | None]:
        name = str(result["name"])
        index = result["index"]
        assert isinstance(index, int)
        exc = result.get("error")
        if isinstance(exc, Exception):
            if is_provider_coverage_miss(exc):
                latency_ms = result.get("latency_ms")
                if isinstance(latency_ms, int | float):
                    await self.runtime.record_success_async(name, index, float(latency_ms), "quote")
                return "coverage_missed", None
            await self.runtime.record_failure_async(name, index, exc, "quote")
            return "failed", None

        other = result["quote"]
        assert isinstance(other, Quote)
        event_time_error = quote_event_time_error(other.timestamp, now=self._now())
        freshness_error = event_time_error or _consistency_freshness_error(primary, other)
        if freshness_error:
            if event_time_error:
                source = provider_source_name(self.providers.get(name), name)
                freshness_error = f"{source} {event_time_error}"
            await self.runtime.record_failure_async(
                name,
                index,
                ProviderProtocolError(freshness_error),
                "quote",
            )
            return "stale", None

        latency_ms = result["latency_ms"]
        assert isinstance(latency_ms, int | float)
        await self.runtime.record_success_async(name, index, float(latency_ms), "quote")
        return "compared", _quote_price_gap_pct(primary, other)

    async def _consistency_result(
        self,
        target_symbol: str,
        summary: ConsistencySummary,
    ) -> tuple[str, list[str], int]:
        if summary.compared == 0:
            if summary.stale:
                return (
                    "字段异常",
                    [f"备用行情源有 {summary.stale} 个报价交易日或时间不一致，未参与价格比较。"],
                    12,
                )
            if summary.coverage_missed and not summary.failed:
                return (
                    "单源可用",
                    [f"备用行情源均未覆盖该股票（{summary.coverage_missed} 个），多源一致性暂无法确认。"],
                    8,
                )
            return "单源可用", ["备用行情源均不可用，多源一致性暂无法确认。"], 8
        if not summary.gaps:
            return "字段异常", [f"已连接 {summary.compared + 1} 个行情源，但备用源价格字段无效，需人工复核。"], 12
        max_price_gap_pct = max(summary.gaps)
        threshold = self.settings.quote_consistency_warning_pct
        source_note = _consistency_source_note(summary)
        if max_price_gap_pct > threshold:
            note = f"{source_note}多源最大价格差异 {max_price_gap_pct:.2f}%，超过 {threshold:.2f}% 阈值。"
            await _safe_save_monitor_event(self.cache, "warning", "quote", note, symbol=target_symbol)
            return "存在差异", [note], 18
        note = f"{source_note}多源最大价格差异 {max_price_gap_pct:.2f}%，处于可接受范围。"
        return "一致", [note], 4 if summary.stale else 0

    async def consistency_probe(self, index: int, name: str, provider, target_symbol: str) -> dict[str, object]:
        result = None
        try:
            result = await self.runtime.timed_provider_call(
                name,
                "quote",
                lambda: provider.quotes([target_symbol]),
                request_key=_quote_request_key([target_symbol]),
            )
            rows = result.value
            source = provider_source_name(provider, name)
            if not isinstance(rows, list):
                raise ProviderProtocolError(f"{source} 行情返回结构异常")
            if not rows:
                raise ProviderCoverageMiss(f"{source} 未覆盖请求股票：{target_symbol}")
            matched, missing = _matched_quotes(filter_valid_quotes(rows), [target_symbol])
            if not matched:
                raise ProviderProtocolError(f"{source} 行情缺失或字段无效：{','.join(missing)}")
            return {"name": name, "index": index, "quote": matched[0], "latency_ms": result.latency_ms}
        except Exception as exc:
            payload: dict[str, object] = {"name": name, "index": index, "error": exc}
            if result is not None:
                payload["latency_ms"] = result.latency_ms
            return payload


def _quotes_by_symbol(quotes: Iterable[Quote]) -> dict[str, Quote]:
    return {standard_symbol(f"{quote.code}.{quote.market}"): quote for quote in quotes}


def _quote_request_key(symbols: Iterable[str]) -> tuple[str, tuple[str, ...]]:
    return "quotes", tuple(sorted(set(symbols)))


def _ordered_quotes(by_symbol: dict[str, Quote], requested_symbols: list[str]) -> list[Quote]:
    return [by_symbol[symbol] for symbol in requested_symbols]


def _quote_now() -> datetime:
    return datetime.now()


def _quotes_with_valid_event_time(quotes: Iterable[Quote], current: datetime) -> list[Quote]:
    return [quote for quote in quotes if quote_event_time_error(quote.timestamp, now=current) is None]


def _provider_event_time_error(source: str, quotes: Iterable[Quote], current: datetime) -> str | None:
    errors: list[str] = []
    for quote in quotes:
        error = quote_event_time_error(quote.timestamp, now=current)
        if error:
            errors.append(f"{standard_symbol(f'{quote.code}.{quote.market}')}: {error}")
    if not errors:
        return None
    return f"{source} 行情事件时间异常：" + "；".join(errors)


def _consistency_freshness_error(primary: Quote, secondary: Quote) -> str | None:
    primary_time = parse_quote_time(primary.timestamp)
    secondary_time = parse_quote_time(secondary.timestamp)
    if primary_time is None:
        return "主行情报价时间无法识别，二级报价无法校验"
    if secondary_time is None:
        return "二级行情报价时间无法识别"
    if not is_trading_day(primary_time.date()) or not is_trading_day(secondary_time.date()):
        return "主行情或二级行情报价日期不是交易日"
    if primary_time.date() != secondary_time.date():
        return f"二级行情交易日 {secondary_time.date().isoformat()} 与主行情交易日 " f"{primary_time.date().isoformat()} 不一致"
    skew_seconds = abs((secondary_time - primary_time).total_seconds())
    if skew_seconds > CONSISTENCY_MAX_TIMESTAMP_SKEW_SECONDS:
        return f"二级行情与主行情报价时间相差约 {max(1, int(skew_seconds // 60))} 分钟"
    return None


def _quote_price_gap_pct(primary: Quote, secondary: Quote) -> float | None:
    if primary.price <= 0 or secondary.price <= 0:
        return None
    if not math.isfinite(primary.price) or not math.isfinite(secondary.price):
        return None
    return abs(secondary.price - primary.price) / primary.price * 100


def _consistency_source_note(summary: ConsistencySummary) -> str:
    attempted = 1 + summary.compared + summary.failed + summary.coverage_missed + summary.stale
    details = [f"参与校验 {attempted} 个行情源"]
    if summary.failed:
        details.append(f"备用失败 {summary.failed} 个")
    if summary.coverage_missed:
        details.append(f"备用无覆盖 {summary.coverage_missed} 个")
    if summary.stale:
        details.append(f"备用时效异常 {summary.stale} 个")
    return "，".join(details) + "。"


async def _safe_log_quote_event(cache: object, category: str, message: str) -> None:
    log_event = getattr(cache, "log_event", None)
    if not callable(log_event):
        return
    await run_cache_io_best_effort(log_event, category, message)


async def _safe_save_monitor_event(cache: object, level: str, category: str, message: str, *, symbol: str) -> None:
    save_monitor_event = getattr(cache, "save_monitor_event", None)
    if not callable(save_monitor_event):
        return
    await run_cache_io_best_effort(save_monitor_event, level, category, message, symbol=symbol)


def _consistency_skip_result(quote: Quote, check_consistency: bool) -> tuple[str, list[str], int] | None:
    if not check_consistency:
        return "未校验", ["当前报价未做多源一致性抽检。"], 4
    if quote.fallback_used or ("缓存" in quote.source and "短时缓存" not in quote.source):
        return "未校验", ["当前报价来自较旧兜底缓存，暂不做多源一致性抽检。"], 4
    return None
