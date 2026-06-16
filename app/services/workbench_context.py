from __future__ import annotations

import asyncio
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


class WorkbenchContextCache:
    def __init__(self, ttl_seconds: float = 8.0, max_size: int = 32) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._entries: dict[str, tuple[float, WorkbenchContext]] = {}
        self._inflight: dict[str, asyncio.Task[WorkbenchContext]] = {}
        self._lock = asyncio.Lock()

    @property
    def entries(self) -> dict[str, tuple[float, WorkbenchContext]]:
        return self._entries

    def clear(self) -> None:
        self._entries.clear()
        self._inflight.clear()

    def restore_entries(self, entries: dict[str, tuple[float, WorkbenchContext]]) -> None:
        self.clear()
        self._entries.update(entries)

    def trim(self) -> None:
        self._trim_entries()

    async def get(self, symbol: str, build: BuildWorkbenchContext, *, use_cache: bool = True) -> WorkbenchContext:
        normalized = _normalize_context_symbol(symbol)
        now = time.monotonic()
        if use_cache:
            cached = self._entries.get(normalized)
            if cached and now - cached[0] <= self.ttl_seconds:
                return cached[1]

        async with self._lock:
            now = time.monotonic()
            if use_cache:
                cached = self._entries.get(normalized)
                if cached and now - cached[0] <= self.ttl_seconds:
                    return cached[1]
            task = self._inflight.get(normalized)
            if task is None or (not use_cache and task.done()):
                task = asyncio.create_task(build(normalized), name=f"stock-workbench-{normalized}")
                self._inflight[normalized] = task

        try:
            context = await task
        except Exception:
            async with self._lock:
                if self._inflight.get(normalized) is task:
                    self._inflight.pop(normalized, None)
            raise

        async with self._lock:
            if self._inflight.get(normalized) is task:
                self._inflight.pop(normalized, None)
            self._entries[normalized] = (time.monotonic(), context)
            self._trim_entries()
        return context

    def _trim_entries(self) -> None:
        if len(self._entries) <= self.max_size:
            return
        stale_keys = sorted(self._entries, key=lambda key: self._entries[key][0])[: len(self._entries) - self.max_size]
        for key in stale_keys:
            self._entries.pop(key, None)


def _normalize_context_symbol(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    return f"{code}.{market.upper()}"
