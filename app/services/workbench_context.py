from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from typing import Protocol

from app.models.research import (
    AlphaEvidenceReport,
    ChipAnalysis,
    EventDigestReport,
    EvidenceChainReport,
    FactorLabReport,
    LeadershipReport,
    MarketRegimeReport,
    PeerComparisonReport,
    RiskRadarReport,
    RiskRewardReport,
    SignalValidationReport,
    StockDiagnosis,
    StockQaReport,
    StockReplayAnalysis,
    TStrategyAssistantReport,
    ThemeContextReport,
    TimeframeAlignmentReport,
)
from app.models.analysis import (
    AnalysisResult,
    FeatureSnapshot,
    StockInsightBundle,
)
from app.services.trading_calendar import (
    expected_quote_date,
    latest_expected_daily_kline_date,
    market_session_phase,
)
from app.utils.audit_time import parse_audit_time
from app.utils.clock import ASHARE_TIMEZONE, monotonic_now, utc_now
from app.utils.symbols import normalize_symbol, standard_symbol


class WorkbenchContextIntegrityError(RuntimeError):
    """Raised when a composite workbench is not bound to its requested stock."""


class _ContextResearchChild(Protocol):
    symbol: str
    updated_at: str


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
    requested_symbol: str = ""
    observed_symbol: str = ""
    context_generated_at: str = ""
    signal_date: str = ""
    daily_bar_cutoff: str = ""
    quote_event_time: str = ""
    cache_cohort_key: str = ""


BuildWorkbenchContext = Callable[[str], Coroutine[object, object, WorkbenchContext]]
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
        _require_context_binding(context, normalized)
        if isinstance(context, WorkbenchContext) and not _context_cache_cohort_is_current(context):
            raise WorkbenchContextIntegrityError("个股工作台研究时段已切换，请重新生成")
        return context

    def _fresh_entry(self, normalized: str) -> WorkbenchContext | None:
        cached = self._entries.get(normalized)
        if not cached:
            return None
        timestamp, context = cached
        if monotonic_now() - timestamp <= self.ttl_seconds:
            try:
                _require_context_binding(context, normalized)
            except WorkbenchContextIntegrityError:
                self._entries.pop(normalized, None)
                raise
            if isinstance(context, WorkbenchContext) and not _context_cache_cohort_is_current(context):
                self._entries.pop(normalized, None)
                return None
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
                task.add_done_callback(partial(self._finalize_task, normalized))
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
        try:
            _require_context_binding(context, normalized)
        except WorkbenchContextIntegrityError:
            return
        if isinstance(context, WorkbenchContext) and not _context_cache_cohort_is_current(context):
            return
        self._entries[normalized] = (monotonic_now(), context)
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


def workbench_cache_cohort_key() -> str:
    return ":".join(
        (
            str(market_session_phase()),
            expected_quote_date().isoformat(),
            latest_expected_daily_kline_date().isoformat(),
        )
    )


def _context_cache_cohort_is_current(context: WorkbenchContext) -> bool:
    try:
        return bool(context.cache_cohort_key) and context.cache_cohort_key == workbench_cache_cohort_key()
    except (RuntimeError, ValueError):
        return False


def _require_context_binding(context: object, expected_symbol: str) -> None:
    if not isinstance(context, WorkbenchContext):
        return
    try:
        expected = standard_symbol(expected_symbol)
        requested = standard_symbol(context.requested_symbol)
        observed = standard_symbol(context.observed_symbol)
    except (AttributeError, TypeError, ValueError) as exc:
        raise WorkbenchContextIntegrityError("个股工作台请求身份字段无效") from exc
    observed_values = _context_symbols(context)
    if not observed_values or any(value != expected for value in observed_values):
        raise WorkbenchContextIntegrityError("个股工作台股票身份绑定不一致")
    if requested != expected or observed != expected:
        raise WorkbenchContextIntegrityError("个股工作台请求身份绑定不一致")
    _require_context_time_cohort(context)


def _context_symbols(context: WorkbenchContext) -> tuple[str, ...]:
    try:
        candidates = [
            f"{context.analysis.quote.code}.{context.analysis.quote.market}",
            *(child.symbol for _, child in _context_research_children(context)),
        ]
        stock_profile = getattr(context.analysis, "stock_profile", None)
        review = getattr(context.analysis, "review", None)
        if stock_profile is not None:
            candidates.append(stock_profile.symbol)
        if review is not None:
            candidates.append(review.symbol)
        return tuple(standard_symbol(value) for value in candidates)
    except (AttributeError, TypeError, ValueError) as exc:
        raise WorkbenchContextIntegrityError("个股工作台股票身份字段无效") from exc


def _require_context_time_cohort(context: WorkbenchContext) -> None:
    try:
        decision_time = parse_audit_time(context.context_generated_at)
        quote_event_time = parse_audit_time(context.quote_event_time)
        analysis_quote_time = parse_audit_time(context.analysis.quote.timestamp)
        signal_date = _context_iso_date(context.signal_date)
    except (AttributeError, TypeError, ValueError) as exc:
        raise WorkbenchContextIntegrityError("个股工作台研究时点字段无效") from exc
    if decision_time > utc_now() + timedelta(minutes=5):
        raise WorkbenchContextIntegrityError("个股工作台研究决策时点不能位于未来")
    if quote_event_time != analysis_quote_time or quote_event_time > decision_time:
        raise WorkbenchContextIntegrityError("个股工作台行情时点与研究决策不一致")
    if quote_event_time.astimezone(ASHARE_TIMEZONE).date() != signal_date:
        raise WorkbenchContextIntegrityError("个股工作台行情交易日与研究批次不一致")
    _require_context_child_times(context, decision_time, signal_date)


def _require_context_child_times(
    context: WorkbenchContext,
    decision_time: datetime,
    signal_date: date,
) -> None:
    for label, child in _context_research_children(context):
        try:
            updated_at = parse_audit_time(child.updated_at)
        except (AttributeError, TypeError, ValueError) as exc:
            raise WorkbenchContextIntegrityError(f"个股工作台 {label} 研究时点无效") from exc
        if updated_at > decision_time:
            raise WorkbenchContextIntegrityError(f"个股工作台 {label} 晚于研究决策时点")
        if updated_at.astimezone(ASHARE_TIMEZONE).date() != signal_date:
            raise WorkbenchContextIntegrityError(f"个股工作台 {label} 不属于行情交易日")


def _context_research_children(
    context: WorkbenchContext,
) -> tuple[tuple[str, _ContextResearchChild], ...]:
    insights = context.insights
    return (
        ("feature_snapshot", context.feature_snapshot),
        ("factor_lab", context.factor_lab),
        ("market_regime", context.market_regime),
        ("signal_validation", context.signal_validation),
        ("risk_reward", context.risk_reward),
        ("timeframe_alignment", context.timeframe_alignment),
        ("alpha_evidence", context.alpha_evidence),
        ("diagnosis", context.diagnosis),
        ("evidence_chain", context.evidence_chain),
        ("qa_report", context.qa_report),
        ("event_digest", context.event_digest),
        ("peer_comparison", context.peer_comparison),
        ("t_strategy", context.t_strategy),
        ("risk_radar", context.risk_radar),
        ("chip_analysis", context.chip_analysis),
        ("leadership", context.leadership),
        ("theme_context", context.theme_context),
        ("replay", context.replay),
        ("insights.overview", insights.overview),
        ("insights.fund_flow", insights.fund_flow),
        ("insights.order_pressure", insights.order_pressure),
        ("insights.events", insights.events),
        ("insights.financial_health", insights.financial_health),
        ("insights.valuation", insights.valuation),
        ("insights.lhb", insights.lhb),
        ("insights.abnormal_events", insights.abnormal_events),
        ("insights.rule_matches", insights.rule_matches),
        *(
            (f"insights.strategy_cards[{index}]", item)
            for index, item in enumerate(insights.strategy_cards)
        ),
    )


def _context_iso_date(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("context date must be ISO formatted")
    return parsed


def _positive_timeout(value: object, *, default: float) -> float:
    if not isinstance(value, str | int | float):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _consume_task_exception(task: asyncio.Future[WorkbenchContext]) -> None:
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
