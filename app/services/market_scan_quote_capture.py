from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from app.models.market import Quote
from app.models.market_scan import (
    MarketScanResultItem,
    MarketScanRun,
    MarketScanStage,
    is_market_scan_top100_refresh_scope,
)
from app.services.datahub_runtime import run_cache_io
from app.services.data_quality_time import parse_quote_time
from app.services.market_scan_contracts import MarketScanCacheProtocol, MarketScanSettingsProtocol
from app.services.market_scan_pressure import MarketScanPressureController
from app.services.market_scan_recovery import ProviderWaitBudget
from app.services.market_scan_modes import market_scan_temporal_contract
from app.services.market_scan_validation import MarketScanRuntimeGuard, raise_if_scan_cancelled
from app.utils.audit_time import audit_datetime_to_text, parse_audit_time
from app.utils.clock import market_now_naive, monotonic_now
from app.utils.market_time import market_local_naive
from app.utils.time import datetime_to_text
from app.utils.provider_errors import ProviderChainUnavailable


class MarketScanQuoteBatchFetcher(Protocol):
    def __call__(
        self,
        items: list[MarketScanResultItem],
        *,
        use_cache: bool,
        require_provider_response: bool,
    ) -> Awaitable[tuple[dict[str, Quote], str | None]]: ...


class MarketScanStageRecorder(Protocol):
    def __call__(
        self,
        run_id: int,
        stage: MarketScanStage,
        *,
        items: int = 0,
        message: str | None = None,
    ) -> Awaitable[None]: ...


class MarketScanRecoveryWaiter(Protocol):
    def __call__(
        self,
        errors: tuple[ProviderChainUnavailable, ...],
        *,
        kind: str,
        attempt: int,
        max_attempts: int,
        wait_budget: ProviderWaitBudget,
        cancel_event: asyncio.Event,
        minimum_delay_seconds: float = 0.0,
    ) -> Awaitable[None]: ...


@dataclass(frozen=True)
class MarketScanFrozenQuoteBatch:
    items: tuple[MarketScanResultItem, ...]
    quote_map: Mapping[str, Quote]
    quote_error: str | None
    quote_observed_at: str
    evaluation_as_of: datetime


@dataclass(frozen=True)
class MarketScanQuoteCaptureEnvelope:
    started_at: str
    finished_at: str
    duration_ms: int
    count: int
    batches: tuple[MarketScanFrozenQuoteBatch, ...]
    decision_as_of: str | None = None


class MarketScanQuoteCapture:
    """Build an immutable all-quote snapshot before any K-line request starts."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._now = now or market_now_naive
        self._monotonic = monotonic or monotonic_now
        self._started_monotonic = self._monotonic()
        self.started_at = audit_datetime_to_text(self._now())
        self._symbols: set[str] = set()
        self._batches: list[MarketScanFrozenQuoteBatch] = []
        self._sealed = False

    def observe(
        self,
        items: Sequence[MarketScanResultItem],
        quote_map: Mapping[str, Quote],
        quote_error: str | None,
    ) -> MarketScanFrozenQuoteBatch:
        if self._sealed:
            raise RuntimeError("报价采集信封已封存")
        symbols = {item.symbol for item in items}
        duplicates = symbols & self._symbols
        if duplicates:
            raise ValueError("报价采集批次包含重复股票：" + "、".join(sorted(duplicates)[:10]))
        self._symbols.update(symbols)
        observed_at = self._now()
        batch = MarketScanFrozenQuoteBatch(
            items=tuple(items),
            quote_map=dict(quote_map),
            quote_error=quote_error,
            quote_observed_at=audit_datetime_to_text(observed_at),
            evaluation_as_of=market_local_naive(observed_at),
        )
        self._batches.append(batch)
        return batch

    def seal(self) -> MarketScanQuoteCaptureEnvelope:
        if self._sealed:
            raise RuntimeError("报价采集信封已封存")
        self._sealed = True
        finished_at = audit_datetime_to_text(self._now())
        duration_ms = max(0, round((self._monotonic() - self._started_monotonic) * 1000))
        return MarketScanQuoteCaptureEnvelope(
            started_at=self.started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            count=len(self._symbols),
            batches=tuple(self._batches),
        )


async def capture_market_scan_quote_batches(
    run: MarketScanRun,
    batches: list[list[MarketScanResultItem]],
    *,
    cache: MarketScanCacheProtocol,
    settings: MarketScanSettingsProtocol,
    pressure: MarketScanPressureController,
    fetch_quote_batch: MarketScanQuoteBatchFetcher,
    record_stage: MarketScanStageRecorder,
    wait_for_recovery: MarketScanRecoveryWaiter,
    cancel_event: asyncio.Event,
    provider_wait_budget: ProviderWaitBudget,
    runtime_guard: MarketScanRuntimeGuard,
    now: Callable[[], datetime] | None,
    monotonic: Callable[[], float],
) -> tuple[MarketScanQuoteCaptureEnvelope, tuple[str, ...]]:
    capture = MarketScanQuoteCapture(now=now, monotonic=monotonic)
    await run_cache_io(cache.begin_market_scan_quote_capture, run.id, capture.started_at)
    warnings: list[str] = []
    max_attempts = settings.market_scan_batch_retry_attempts
    strict_provider_snapshot = str(run.rule_version).startswith("full-market-scan-v6:")
    for batch in batches:
        quote_map, quote_error = await _capture_one_quote_batch(
            run,
            batch,
            pressure=pressure,
            fetch_quote_batch=fetch_quote_batch,
            record_stage=record_stage,
            wait_for_recovery=wait_for_recovery,
            cancel_event=cancel_event,
            provider_wait_budget=provider_wait_budget,
            runtime_guard=runtime_guard,
            max_attempts=max_attempts,
            strict_provider_snapshot=strict_provider_snapshot,
        )
        if quote_error:
            warnings.append(quote_error)
        capture.observe(batch, quote_map, quote_error)
    envelope = capture.seal()
    decision_as_of = _quote_capture_decision_as_of(run, envelope)
    envelope = replace(envelope, decision_as_of=datetime_to_text(decision_as_of))
    assert envelope.decision_as_of is not None
    await run_cache_io(
        cache.seal_market_scan_quote_capture,
        run.id,
        finished_at=envelope.finished_at,
        decision_as_of=envelope.decision_as_of,
        duration_ms=envelope.duration_ms,
        count=envelope.count,
    )
    return envelope, tuple(dict.fromkeys(warnings))


def _quote_capture_decision_as_of(
    run: MarketScanRun,
    envelope: MarketScanQuoteCaptureEnvelope,
) -> datetime:
    try:
        decision = market_local_naive(datetime.fromisoformat(run.as_of.replace("Z", "+00:00")))
        available_at = market_local_naive(parse_audit_time(envelope.finished_at))
    except ValueError as exc:
        raise ValueError("报价采集时点合同无法解析") from exc
    for batch in envelope.batches:
        observed_at = market_local_naive(batch.evaluation_as_of)
        if observed_at > available_at:
            raise ValueError("报价接收时间晚于报价采集可用时间")
        decision = max(decision, observed_at)
        for quote in batch.quote_map.values():
            event_at = parse_quote_time(quote.timestamp)
            if event_at is None:
                continue
            if event_at > observed_at:
                raise ValueError("报价事件时间晚于报价接收时间")
            decision = max(decision, event_at)
    if decision > available_at:
        raise ValueError("扫描决策时点晚于报价采集可用时间")
    temporal = market_scan_temporal_contract(decision, run.mode)
    if (
        decision.date()
        != market_local_naive(datetime.fromisoformat(run.as_of.replace("Z", "+00:00"))).date()
        or temporal.data_date.isoformat() != run.data_date
        or temporal.quote_date.isoformat() != run.quote_date
    ):
        raise ValueError("报价采集跨越扫描模式或交易日边界")
    return decision


async def _capture_one_quote_batch(
    run: MarketScanRun,
    batch: list[MarketScanResultItem],
    *,
    pressure: MarketScanPressureController,
    fetch_quote_batch: MarketScanQuoteBatchFetcher,
    record_stage: MarketScanStageRecorder,
    wait_for_recovery: MarketScanRecoveryWaiter,
    cancel_event: asyncio.Event,
    provider_wait_budget: ProviderWaitBudget,
    runtime_guard: MarketScanRuntimeGuard,
    max_attempts: int,
    strict_provider_snapshot: bool,
) -> tuple[dict[str, Quote], str | None]:
    use_cache = False if strict_provider_snapshot else not is_market_scan_top100_refresh_scope(run.scope)
    for attempt in range(1, max_attempts + 1):
        runtime_guard.checkpoint()
        raise_if_scan_cancelled(cancel_event)
        await record_stage(
            run.id,
            "bulk_quotes",
            items=len(batch),
            message=f"正在冻结全市场报价（{len(batch)} 只）",
        )
        try:
            return await fetch_quote_batch(
                batch,
                use_cache=use_cache,
                require_provider_response=strict_provider_snapshot,
            )
        except ProviderChainUnavailable as exc:
            decision = pressure.observe_quote_failure(exc, len(batch))
            await wait_for_recovery(
                (exc,),
                kind="quote",
                attempt=attempt,
                max_attempts=max_attempts,
                wait_budget=provider_wait_budget,
                cancel_event=cancel_event,
                minimum_delay_seconds=decision.minimum_delay_seconds,
            )
    raise RuntimeError("全市场报价采集批次重试状态异常")  # pragma: no cover


__all__ = [
    "MarketScanFrozenQuoteBatch",
    "MarketScanQuoteCapture",
    "MarketScanQuoteCaptureEnvelope",
    "capture_market_scan_quote_batches",
]
