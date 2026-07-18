from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.models.schemas import (
    AlphaEvidenceReport,
    AnalysisResult,
    ChipAnalysis,
    EventDigestReport,
    EvidenceChainReport,
    FactorLabReport,
    FeatureSnapshot,
    LeadershipReport,
    MarketRegimeReport,
    PeerComparisonReport,
    RiskRadarReport,
    RiskRewardReport,
    SignalValidationReport,
    StockDiagnosis,
    StockInsightBundle,
    StockQaReport,
    StockReplayAnalysis,
    TStrategyAssistantReport,
    ThemeContextReport,
    TimeframeAlignmentReport,
)
from app.utils.symbols import normalize_symbol


@dataclass
class WorkbenchContext:
    analysis: AnalysisResult
    insights: StockInsightBundle
    feature_snapshot: FeatureSnapshot
    factor_lab: FactorLabReport
    market_regime: MarketRegimeReport
    signal_validation: SignalValidationReport
    risk_reward: RiskRewardReport
    timeframe_alignment: TimeframeAlignmentReport
    alpha_evidence: AlphaEvidenceReport
    diagnosis: StockDiagnosis
    evidence_chain: EvidenceChainReport
    qa_report: StockQaReport
    event_digest: EventDigestReport
    peer_comparison: PeerComparisonReport
    t_strategy: TStrategyAssistantReport
    risk_radar: RiskRadarReport
    chip_analysis: ChipAnalysis
    leadership: LeadershipReport
    theme_context: ThemeContextReport
    replay: StockReplayAnalysis
    order_book_error: str | None = None
    advice_snapshot_saved: bool = False


BuildWorkbenchContext = Callable[[str], Awaitable[WorkbenchContext]]
CacheEntry = tuple[float, WorkbenchContext]


class WorkbenchContextCache:
    def __init__(
        self,
        ttl_seconds: float = 8.0,
        max_size: int = 32,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self.shutdown_timeout_seconds = _positive_timeout(shutdown_timeout_seconds, default=5.0)
        self._entries: dict[str, CacheEntry] = {}
        self._inflight: dict[str, asyncio.Task[WorkbenchContext]] = {}
        self._lock = asyncio.Lock()

    @property
    def entries(self) -> dict[str, CacheEntry]:
        return self._entries

    def clear(self) -> None:
        self._entries.clear()
        self._inflight.clear()

    async def aclose(self) -> None:
        async with self._lock:
            tasks = tuple(set(self._inflight.values()))
            self._inflight.clear()
            self._entries.clear()
        for task in tasks:
            task.add_done_callback(_consume_task_exception)
            task.cancel()
        if tasks:
            done, _ = await asyncio.wait(tasks, timeout=self.shutdown_timeout_seconds)
            for task in done:
                _consume_task_exception(task)

    def restore_entries(self, entries: dict[str, CacheEntry]) -> None:
        self.clear()
        self._entries.update(entries)

    def trim(self) -> None:
        self._trim_entries()

    async def get(self, symbol: str, build: BuildWorkbenchContext, *, use_cache: bool = True) -> WorkbenchContext:
        normalized = _normalize_context_symbol(symbol)
        if use_cache:
            cached = self._fresh_entry(normalized)
            if cached is not None:
                return cached

        task = await self._task_for(normalized, build, use_cache=use_cache)

        try:
            context = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                self._finalize_task(normalized, task)
            raise
        except Exception:
            self._finalize_task(normalized, task)
            raise

        self._finalize_task(normalized, task)
        return context

    def _fresh_entry(self, normalized: str) -> WorkbenchContext | None:
        cached = self._entries.get(normalized)
        if not cached:
            return None
        timestamp, context = cached
        if time.monotonic() - timestamp <= self.ttl_seconds:
            return context
        self._entries.pop(normalized, None)
        return None

    async def _task_for(self, normalized: str, build: BuildWorkbenchContext, *, use_cache: bool) -> asyncio.Task[WorkbenchContext]:
        async with self._lock:
            if use_cache:
                cached = self._fresh_entry(normalized)
                if cached is not None:
                    return _completed_context_task(cached, normalized)
            task = self._active_task(normalized)
            if task is None:
                if use_cache:
                    cached = self._fresh_entry(normalized)
                    if cached is not None:
                        return _completed_context_task(cached, normalized)
                task = asyncio.create_task(build(normalized), name=f"stock-workbench-{normalized}")
                self._inflight[normalized] = task
                task.add_done_callback(lambda completed, key=normalized: self._finalize_task(key, completed))
            return task

    def _active_task(self, normalized: str) -> asyncio.Task[WorkbenchContext] | None:
        task = self._inflight.get(normalized)
        if task and task.done():
            self._finalize_task(normalized, task)
            return None
        return task

    def _finalize_task(self, normalized: str, task: asyncio.Task[WorkbenchContext]) -> None:
        owns_task = self._inflight.get(normalized) is task
        if owns_task:
            self._inflight.pop(normalized, None)
        if task.cancelled():
            return
        try:
            context = task.result()
        except Exception:
            return
        if not owns_task:
            return
        self._entries[normalized] = (time.monotonic(), context)
        self._trim_entries()

    def _trim_entries(self) -> None:
        if len(self._entries) <= self.max_size:
            return
        stale_keys = sorted(self._entries, key=lambda key: self._entries[key][0])[: len(self._entries) - self.max_size]
        for key in stale_keys:
            self._entries.pop(key, None)


def _normalize_context_symbol(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    return f"{code}.{market.upper()}"


def _positive_timeout(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _consume_task_exception(task: asyncio.Future[object]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


def _completed_context_task(context: WorkbenchContext, normalized: str) -> asyncio.Task[WorkbenchContext]:
    task: asyncio.Task[WorkbenchContext] = asyncio.get_running_loop().create_task(_return_context(context), name=f"stock-workbench-cached-{normalized}")
    return task


async def _return_context(context: WorkbenchContext) -> WorkbenchContext:
    return context
