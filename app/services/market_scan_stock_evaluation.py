from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, datetime

from app.models.market import Kline, Quote
from app.models.market_scan import MarketScanMode, MarketScanResultItem, MarketScanResultWrite
from app.services.datahub_runtime import ProviderCallBusyError
from app.services.market_scan_completion import short_scan_error
from app.services.market_scan_contracts import (
    MarketScanDataHubProtocol,
    MarketScanKlinePrefetchProtocol,
)
from app.services.market_scan_pressure import MarketScanPressureController
from app.services.market_scan_scoring import score_market_scan_item
from app.services.market_scan_validation import (
    failed_scan_result_for_exception,
    missing_quote_result,
)
from app.utils.provider_errors import ProviderChainUnavailable


class MarketScanStockEvaluator:
    """Fetch and score one stock while preserving batch-level outage semantics."""

    def __init__(
        self,
        datahub: MarketScanDataHubProtocol,
        pressure: MarketScanPressureController,
        work_duration_ms: Counter[str],
        *,
        sensitive_values: Iterable[object],
        monotonic: Callable[[], float],
        kline_prefetch: MarketScanKlinePrefetchProtocol | None,
    ) -> None:
        self.datahub = datahub
        self.settings = datahub.settings
        self._pressure = pressure
        self._work_duration_ms = work_duration_ms
        self._sensitive_values = tuple(sensitive_values)
        self._monotonic = monotonic
        self._kline_prefetch = kline_prefetch

    async def scan_one(
        self,
        item: MarketScanResultItem,
        quote: Quote | None,
        *,
        quote_error: str | None,
        semaphore: asyncio.Semaphore,
        cancel_event: asyncio.Event,
        as_of: datetime,
        cutoff: date,
        expected_data_date: date,
        expected_quote_date: date,
        mode: MarketScanMode,
        rule_version: str,
        prefetched_cache: list[Kline] | None,
    ) -> MarketScanResultWrite:
        _raise_if_cancelled(cancel_event)
        rows: list[Kline] = []
        try:
            rows = await self._timed_kline_fetch(item.symbol, semaphore, cancel_event, prefetched_cache)
            if quote is None:
                return self.missing_quote_result(
                    item,
                    rows,
                    cutoff=cutoff,
                    expected_data_date=expected_data_date,
                    quote_error=quote_error,
                )
            return self._score(item, quote, rows, as_of, cutoff, expected_data_date, expected_quote_date, mode, rule_version)
        except asyncio.CancelledError:
            raise
        except ProviderChainUnavailable:
            raise
        except Exception as exc:
            return failed_scan_result_for_exception(
                item=item,
                quote=quote,
                rows=rows,
                cutoff=cutoff,
                exc=exc,
                sensitive_values=self._sensitive_values,
            )

    async def _timed_kline_fetch(
        self,
        symbol: str,
        semaphore: asyncio.Semaphore,
        cancel_event: asyncio.Event,
        prefetched_cache: list[Kline] | None,
    ) -> list[Kline]:
        async with semaphore:
            started = self._monotonic()
            try:
                return await self.fetch_kline(symbol, cancel_event, prefetched_cache=prefetched_cache)
            finally:
                self._work_duration_ms["klines"] += _elapsed_ms(started, self._monotonic())

    def _score(
        self,
        item: MarketScanResultItem,
        quote: Quote,
        rows: list[Kline],
        as_of: datetime,
        cutoff: date,
        expected_data_date: date,
        expected_quote_date: date,
        mode: MarketScanMode,
        rule_version: str,
    ) -> MarketScanResultWrite:
        started = self._monotonic()
        try:
            return score_market_scan_item(
                item,
                quote,
                rows,
                as_of=as_of,
                completed_cutoff=cutoff,
                expected_data_date=expected_data_date,
                expected_quote_date=expected_quote_date,
                min_history_rows=self.settings.market_scan_min_history_rows,
                min_data_quality_score=self.settings.market_scan_min_data_quality_score,
                mode=mode,
                rule_version=rule_version,
            )
        finally:
            self._work_duration_ms["scoring"] += _elapsed_ms(started, self._monotonic())

    async def fetch_kline(
        self,
        symbol: str,
        cancel_event: asyncio.Event,
        *,
        prefetched_cache: list[Kline] | None = None,
    ) -> list[Kline]:
        attempts = self.settings.market_scan_retry_attempts
        errors: list[str] = []
        for attempt in range(1, attempts + 1):
            _raise_if_cancelled(cancel_event)
            try:
                request = self._kline_request(symbol, prefetched_cache)
                return await asyncio.wait_for(request, timeout=self.settings.market_scan_symbol_timeout_seconds)
            except asyncio.CancelledError:
                raise
            except ProviderChainUnavailable:
                raise
            except (ProviderCallBusyError, TimeoutError) as exc:
                message = short_scan_error(exc, sensitive_values=self._sensitive_values)
                raise self._pressure.unavailable_error(exc, message) from exc
            except Exception as exc:
                errors.append(short_scan_error(exc, sensitive_values=self._sensitive_values))
                if attempt < attempts:
                    await asyncio.sleep(self.settings.market_scan_retry_backoff_seconds * attempt)
        raise RuntimeError("；".join(dict.fromkeys(errors)) or "日K数据不可用")

    def _kline_request(
        self,
        symbol: str,
        prefetched_cache: list[Kline] | None,
    ):
        if self._kline_prefetch is not None and prefetched_cache is not None:
            return self._kline_prefetch.market_scan_kline_from_prefetch(
                symbol,
                prefetched_cache,
                limit=self.settings.market_scan_kline_limit,
                allow_stale=True,
                require_provider_response=True,
            )
        return self.datahub.kline(
            symbol,
            limit=self.settings.market_scan_kline_limit,
            use_cache=True,
            allow_stale=True,
            require_provider_response=True,
        )

    def missing_quote_result(
        self,
        item: MarketScanResultItem,
        rows: list[Kline],
        *,
        cutoff: date,
        expected_data_date: date,
        quote_error: str | None,
    ) -> MarketScanResultWrite:
        return missing_quote_result(
            item,
            rows,
            cutoff=cutoff,
            expected_data_date=expected_data_date,
            quote_error=quote_error,
            min_history_rows=self.settings.market_scan_min_history_rows,
        )


def _raise_if_cancelled(event: asyncio.Event) -> None:
    if event.is_set():
        raise asyncio.CancelledError


def _elapsed_ms(started: float, finished: float) -> int:
    return max(0, round((finished - started) * 1000))


__all__ = ["MarketScanStockEvaluator"]
