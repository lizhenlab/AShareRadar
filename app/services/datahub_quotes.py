from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.models.schemas import DataQuality, Kline, Quote
from app.services.data_quality import build_data_quality
from app.services.datahub_cache import _matched_quotes, _normalize_symbols, _ordered_complete_quotes, _tag_cached_quotes
from app.services.datahub_runtime import ProviderAttempt, ProviderRuntime, provider_source_name
from app.services.datahub_status import _provider_source_key
from app.utils.market_data import filter_valid_quotes
from app.utils.symbols import standard_symbol


@dataclass(frozen=True)
class ConsistencySummary:
    compared: int
    failed: int
    gaps: list[float]


class QuoteCoordinator:
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

    async def quote(self, symbol: str, use_cache: bool = True) -> Quote:
        requested = standard_symbol(symbol)
        return (await self.quotes([requested], use_cache=use_cache))[0]

    async def quotes(self, symbols: Iterable[str], use_cache: bool = True) -> list[Quote]:
        requested_symbols = _normalize_symbols(symbols)
        if not requested_symbols:
            return []
        symbol_list = list(dict.fromkeys(requested_symbols))

        collected = self._short_cache_quotes(symbol_list) if use_cache else {}
        if len(collected) == len(symbol_list):
            return _ordered_quotes(collected, requested_symbols)

        errors = await self._fill_realtime_quotes(symbol_list, collected)
        if len(collected) == len(symbol_list):
            return _ordered_quotes(collected, requested_symbols)

        self._fill_fallback_quotes(symbol_list, collected)
        if len(collected) == len(symbol_list):
            _safe_log_quote_event(self.cache, "fallback", "部分或全部实时数据源失败，缺失个股使用24小时内缓存报价")
            return _ordered_quotes(collected, requested_symbols)

        unresolved = [symbol for symbol in symbol_list if symbol not in collected]
        raise RuntimeError("实时行情未完整返回，缺失：" + "、".join(unresolved) + "；" + "；".join(errors))

    def _short_cache_quotes(self, symbol_list: list[str]) -> dict[str, Quote]:
        cached = self.cache.get_quotes(symbol_list, self.settings.quote_cache_seconds)
        cached = _tag_cached_quotes(cached, "短时缓存")
        return _quotes_by_symbol(cached)

    async def _fill_realtime_quotes(self, symbol_list: list[str], collected: dict[str, Quote]) -> list[str]:
        errors: list[str] = []
        for attempt in self.runtime.attempts(self.priority("quote"), self.providers, "quote", errors):
            remaining = [symbol for symbol in symbol_list if symbol not in collected]
            if not remaining:
                break
            await self._try_provider_quotes(attempt, remaining, collected, errors)
        return errors

    async def _try_provider_quotes(
        self,
        attempt: ProviderAttempt,
        remaining: list[str],
        collected: dict[str, Quote],
        errors: list[str],
    ) -> None:
        source = provider_source_name(attempt.provider, attempt.name)
        try:
            result = await self.runtime.timed_call(attempt.provider.quotes(remaining))  # type: ignore[attr-defined]
            quotes = filter_valid_quotes(result.value)
            matched, missing = _matched_quotes(quotes, remaining)
            if not matched:
                raise RuntimeError(f"{source} 行情缺失或字段无效：{','.join(missing)}")
            self.runtime.record_attempt_success(attempt, "quote", result.latency_ms)
            collected.update(_quotes_by_symbol(matched))
            self._save_quotes_best_effort(matched)
            if missing:
                message = f"{source} 批量行情部分缺失：{','.join(missing)}"
                errors.append(f"{attempt.name}: {message}")
                _safe_log_quote_event(self.cache, "fallback", message)
        except Exception as exc:
            self.runtime.record_attempt_failure(attempt, "quote", exc, errors)

    def _save_quotes_best_effort(self, quotes: list[Quote]) -> None:
        try:
            self.cache.save_quotes(quotes)
        except Exception:
            pass

    def _fill_fallback_quotes(self, symbol_list: list[str], collected: dict[str, Quote]) -> None:
        missing_symbols = [symbol for symbol in symbol_list if symbol not in collected]
        fallback = _tag_cached_quotes(self.cache.get_quotes(missing_symbols, max_age_seconds=60 * 60 * 24), "兜底缓存")
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
            quality_klines = (
                self.cache.get_klines(f"{quote.code}.{quote.market}", 120, self.settings.kline_cache_seconds)
                if require_kline and use_cache
                else []
            )
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

        summary = self._summarize_consistency_results(quote, await asyncio.gather(*tasks))
        return self._consistency_result(target_symbol, summary)

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
            if _provider_source_key(provider.source_name) == current_source:
                continue
            tasks.append(self.consistency_probe(index, name, provider, target_symbol))
        return tasks

    def _summarize_consistency_results(self, quote: Quote, results: list[dict[str, object]]) -> ConsistencySummary:
        gaps: list[float] = []
        compared = 0
        failed = 0
        for result in results:
            name = result["name"]
            index = int(result["index"])
            exc = result.get("error")
            if isinstance(exc, Exception):
                failed += 1
                self.runtime.record_failure(str(name), index, exc, "quote")
                continue
            self.runtime.record_success(str(name), index, float(result["latency_ms"]), "quote")
            other = result["quote"]
            compared += 1
            if quote.price > 0 and other.price > 0:
                gap_pct = abs(other.price - quote.price) / quote.price * 100
                gaps.append(gap_pct)
        return ConsistencySummary(compared=compared, failed=failed, gaps=gaps)

    def _consistency_result(self, target_symbol: str, summary: ConsistencySummary) -> tuple[str, list[str], int]:
        if summary.compared == 0:
            return "单源可用", ["备用行情源均不可用，多源一致性暂无法确认。"], 8
        if not summary.gaps:
            return "字段异常", [f"已连接 {summary.compared + 1} 个行情源，但备用源价格字段无效，需人工复核。"], 12
        max_price_gap_pct = max(summary.gaps)
        threshold = self.settings.quote_consistency_warning_pct
        source_note = f"参与校验 {summary.compared + 1} 个行情源，备用失败 {summary.failed} 个。"
        if max_price_gap_pct > threshold:
            note = f"{source_note}多源最大价格差异 {max_price_gap_pct:.2f}%，超过 {threshold:.2f}% 阈值。"
            _safe_save_monitor_event(self.cache, "warning", "quote", note, symbol=target_symbol)
            return "存在差异", [note], 18
        note = f"{source_note}多源最大价格差异 {max_price_gap_pct:.2f}%，处于可接受范围。"
        return "一致", [note], 0

    async def consistency_probe(self, index: int, name: str, provider, target_symbol: str) -> dict[str, object]:
        try:
            result = await self.runtime.timed_call(provider.quotes([target_symbol]))
            rows = result.value
            ordered = _ordered_complete_quotes(rows, [target_symbol], provider.source_name)
            return {"name": name, "index": index, "quote": ordered[0], "latency_ms": result.latency_ms}
        except Exception as exc:
            return {"name": name, "index": index, "error": exc}


def _quotes_by_symbol(quotes: Iterable[Quote]) -> dict[str, Quote]:
    return {standard_symbol(f"{quote.code}.{quote.market}"): quote for quote in quotes}


def _ordered_quotes(by_symbol: dict[str, Quote], requested_symbols: list[str]) -> list[Quote]:
    return [by_symbol[symbol] for symbol in requested_symbols]


def _safe_log_quote_event(cache: object, category: str, message: str) -> None:
    log_event = getattr(cache, "log_event", None)
    if not callable(log_event):
        return
    try:
        log_event(category, message)
    except Exception:
        pass


def _safe_save_monitor_event(cache: object, level: str, category: str, message: str, *, symbol: str) -> None:
    save_monitor_event = getattr(cache, "save_monitor_event", None)
    if not callable(save_monitor_event):
        return
    try:
        save_monitor_event(level, category, message, symbol=symbol)
    except Exception:
        pass


def _consistency_skip_result(quote: Quote, check_consistency: bool) -> tuple[str, list[str], int] | None:
    if not check_consistency:
        return "未校验", ["当前报价未做多源一致性抽检。"], 4
    if quote.fallback_used or ("缓存" in quote.source and "短时缓存" not in quote.source):
        return "未校验", ["当前报价来自较旧兜底缓存，暂不做多源一致性抽检。"], 4
    return None
