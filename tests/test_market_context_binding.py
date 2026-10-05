from __future__ import annotations

import asyncio
from dataclasses import fields
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.market_context_scoring import build_market_context_score
from app.services.workbench_context import WorkbenchContext, WorkbenchContextCache, WorkbenchContextIntegrityError
from app.workflows import workbench_pipeline as pipeline
from tests.factories import make_quote


SYMBOL = "600519.SH"
QUOTE_TIME = "2026-08-13 05:59:00.500000"
DECISION_TIME = "2026-08-13 06:00:00.500000"


def _context() -> WorkbenchContext:
    def child():
        return SimpleNamespace(symbol=SYMBOL, updated_at=QUOTE_TIME)

    quote = make_quote(timestamp=QUOTE_TIME)
    score = build_market_context_score(70, quote, market=None, industry=None, industry_name=None,
                                       evaluated_at=DECISION_TIME, reliability_score=80)
    values = {field.name: child() for field in fields(WorkbenchContext)}
    insights = SimpleNamespace(**{name: child() for name in (
        "overview", "fund_flow", "order_pressure", "events", "financial_health", "valuation",
        "lhb", "abnormal_events", "rule_matches",
    )}, strategy_cards=[child()])
    insights.overview.market_context_score = score
    insights.overview.total_score = score.score
    insights.overview.directional_score = score.base_score
    insights.overview.reliability_score = score.reliability_score
    insights.overview.score_rule_version = "current-stock-overview.v2"
    insights.overview.directional_evidence_score = 74.5
    insights.overview.risk_penalty = 4.5
    insights.overview.evidence_coverage_pct = 100
    insights.overview.factors = [SimpleNamespace(
        name=name, score=value, aggregation_role="direction", score_available=True, participates_in_total_score=True,
    ) for name, value in zip(("技术面", "量价热度（衍生）", "基本面", "事件面"), (74, 74, 75, 75), strict=True)]
    insights.overview.factors.append(SimpleNamespace(
        name="风险面", score=32, aggregation_role="risk_constraint", score_available=True, participates_in_total_score=True,
    ))
    values.update(
        analysis=SimpleNamespace(quote=quote, stock_profile=None, review=None), insights=insights,
        requested_symbol=SYMBOL, observed_symbol=SYMBOL, context_generated_at=DECISION_TIME,
        signal_date="2026-08-13", daily_bar_cutoff="2026-08-12", quote_event_time=QUOTE_TIME,
        cache_cohort_key="test", order_book_error=None,
    )
    return WorkbenchContext(**values)


@pytest.fixture(autouse=True)
def _stable_cohort(monkeypatch):
    monkeypatch.setattr("app.services.workbench_context.workbench_cache_cohort_key", lambda: "test")


@pytest.mark.parametrize("field, value", [
    ("symbol", "000001.SZ"),
    ("symbol", "000300.SH"),
    ("symbol", "invalid"),
    ("stock_event_at", "2026-08-13 05:59:00.500001"),
    ("stock_event_at", "2026-08-13 05:59:00.499999"),
    ("stock_event_at", "2026-08-12 05:59:00.500000"),
    ("stock_event_at", "2026-08-13"),
    ("evaluated_at", "2026-08-13 06:00:00.500001"),
    ("evaluated_at", "2026-08-13 06:00:00.499999"),
    ("evaluated_at", "2026-08-13"),
    ("evaluated_at", "0001-01-01 00:00:00"),
    ("score", 99),
])
def test_cached_context_rejects_swapped_owner_time_or_score_and_evicts_entry(field: str, value: object) -> None:
    async def run() -> None:
        context = _context()
        score = context.insights.overview.market_context_score
        context.insights.overview.market_context_score = score.model_copy(update={field: value})
        cache = WorkbenchContextCache()
        cache.entries[SYMBOL] = (time.monotonic(), context)
        async def unexpected(_symbol: str):
            pytest.fail("a poisoned fresh entry must not silently rebuild")
        try:
            with pytest.raises(WorkbenchContextIntegrityError):
                await cache.get(SYMBOL, unexpected)
            assert cache.entries == {}
        finally:
            await cache.aclose()
    asyncio.run(run())


def test_new_builder_context_with_wrong_owner_cannot_be_cached() -> None:
    async def run() -> None:
        context = _context()
        context.insights.overview.market_context_score.symbol = "000001.SZ"
        cache = WorkbenchContextCache()
        async def build(_symbol: str):
            return context
        try:
            with pytest.raises(WorkbenchContextIntegrityError, match="身份绑定"):
                await cache.get(SYMBOL, build)
            assert cache.entries == {}
        finally:
            await cache.aclose()
    asyncio.run(run())


def test_equivalent_timezone_instants_preserve_context_cache_reuse() -> None:
    async def run() -> None:
        context = _context()
        score = context.insights.overview.market_context_score
        context.insights.overview.market_context_score = score.model_copy(update={
            "stock_event_at": "2026-08-12T21:59:00.500000Z",
            "evaluated_at": "2026-08-12T22:00:00.500000Z",
        })
        cache = WorkbenchContextCache()
        calls = []
        async def build(symbol: str):
            calls.append(symbol)
            return context
        try:
            assert await cache.get(SYMBOL, build) is context
            assert await cache.get(SYMBOL, build) is context
            assert calls == [SYMBOL]
        finally:
            await cache.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("missing_attribute", [False, True])
def test_legacy_context_without_new_score_remains_readable(missing_attribute: bool) -> None:
    async def run() -> None:
        context = _context()
        _remove_overview_metadata(context)
        if missing_attribute:
            del context.insights.overview.market_context_score
        else:
            context.insights.overview.market_context_score = None
        cache = WorkbenchContextCache()
        async def build(_symbol: str):
            return context
        try:
            assert await cache.get(SYMBOL, build) is context
            assert await cache.get(SYMBOL, build) is context
        finally:
            await cache.aclose()
    asyncio.run(run())


def _remove_overview_metadata(context: WorkbenchContext) -> None:
    for name in ("directional_score", "reliability_score", "score_rule_version", "directional_evidence_score",
                 "risk_penalty", "evidence_coverage_pct"):
        delattr(context.insights.overview, name)


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("owner,field,value", [
    ("overview", "directional_score", 71),
    ("overview", "directional_score", "70"),
    ("overview", "directional_score", None),
    ("overview", "reliability_score", 81),
    ("overview", "reliability_score", None),
    ("overview", "directional_evidence_score", 80),
    ("overview", "directional_evidence_score", None),
    ("overview", "directional_evidence_score", float("nan")),
    ("overview", "directional_evidence_score", "74.5"),
    ("overview", "directional_evidence_score", True),
    ("overview", "risk_penalty", 5.5),
    ("overview", "risk_penalty", None),
    ("overview", "risk_penalty", float("inf")),
    ("overview", "evidence_coverage_pct", 75),
    ("overview", "evidence_coverage_pct", 80),
    ("overview", "evidence_coverage_pct", True),
    ("overview", "evidence_coverage_pct", None),
    ("overview", "market_context_score", None),
    ("overview", "factors", None),
    ("overview", "factors", [None]),
    ("context", "base_score", 69),
    ("context", "base_score", 70.0),
    ("context", "reliability_score", 79),
    ("context", "rule_version", "current-market-context.v1"),
    ("context", "rule_version", None),
    ("direction", "score_available", False),
    ("risk", "score", 50),
])
def test_score_metadata_mismatch_is_evicted_or_rejected_before_caching(cached, owner, field, value):
    async def run():
        context = _context()
        overview = context.insights.overview
        targets = {"overview": overview, "context": overview.market_context_score,
                   "direction": overview.factors[0], "risk": overview.factors[-1]}
        setattr(targets[owner], field, value)
        cache = WorkbenchContextCache()
        if cached:
            cache.entries[SYMBOL] = (time.monotonic(), context)
        async def build(_symbol):
            assert not cached, "poisoned cached metadata must fail before rebuilding"
            return context
        try:
            with pytest.raises(WorkbenchContextIntegrityError):
                await cache.get(SYMBOL, build)
            assert cache.entries == {}
        finally:
            await cache.aclose()
    asyncio.run(run())


def test_legacy_v1_context_without_new_overview_metadata_can_be_reused():
    async def run():
        context = _context()
        _remove_overview_metadata(context)
        context.insights.overview.market_context_score.rule_version = "current-market-context.v1"
        cache = WorkbenchContextCache()
        async def build(_symbol):
            return context
        try:
            assert await cache.get(SYMBOL, build) is context
            assert await cache.get(SYMBOL, build) is context
        finally:
            await cache.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("direction_count", range(5))
def test_each_fixed_share_coverage_level_with_consistent_metadata_is_cacheable(direction_count):
    async def run():
        context = _context()
        overview = context.insights.overview
        for index, factor in enumerate(overview.factors[:4]):
            factor.score_available = factor.participates_in_total_score = index < direction_count
        overview.directional_evidence_score = 50 + sum((factor.score - 50) / 4 for factor in overview.factors[:direction_count])
        overview.directional_score = round(overview.directional_evidence_score - overview.risk_penalty)
        overview.evidence_coverage_pct = direction_count * 25
        overview.market_context_score = build_market_context_score(
            overview.directional_score, context.analysis.quote, market=None, industry=None, industry_name=None,
            evaluated_at=DECISION_TIME, reliability_score=overview.reliability_score,
        )
        overview.total_score = overview.market_context_score.score
        cache = WorkbenchContextCache()
        async def build(_symbol):
            return context
        try:
            assert await cache.get(SYMBOL, build) is context
            assert await cache.get(SYMBOL, build) is context
        finally:
            await cache.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [("directional_score", 71), ("reliability_score", 81)])
def test_legacy_overview_with_present_directional_metadata_must_match_context(field, value):
    async def run():
        context = _context()
        _remove_overview_metadata(context)
        setattr(context.insights.overview, field, value)
        cache = WorkbenchContextCache()
        async def build(_symbol):
            return context
        try:
            with pytest.raises(WorkbenchContextIntegrityError, match="基础分或可靠性"):
                await cache.get(SYMBOL, build)
            assert cache.entries == {}
        finally:
            await cache.aclose()
    asyncio.run(run())


def test_workbench_collects_market_before_stock_analysis_finishes(monkeypatch) -> None:
    async def run() -> None:
        analysis_started, market_started = asyncio.Event(), asyncio.Event()
        analysis = _context().analysis
        index = make_quote(timestamp=QUOTE_TIME).model_copy(update={"code": "000300", "name": "沪深300"})
        async def analyze(_hub, _symbol, *, persist_history):
            analysis_started.set()
            await market_started.wait()
            return analysis
        async def quote(symbol):
            assert symbol == "000300.SH"
            market_started.set()
            await analysis_started.wait()
            return index
        monkeypatch.setattr(pipeline, "analyze_individual_stock", analyze)
        monkeypatch.setattr(pipeline, "audit_now_text", lambda: DECISION_TIME)
        monkeypatch.setattr(pipeline, "_market_breadth_sample_or_empty", AsyncMock(return_value=SimpleNamespace(quotes=[], warnings=())))
        monkeypatch.setattr(pipeline, "_order_book_or_error", AsyncMock(return_value=(None, None)))
        monkeypatch.setattr(pipeline, "_stock_concepts_or_error", AsyncMock(return_value=([], None)))
        inputs = await asyncio.wait_for(pipeline._collect_workbench_inputs(SimpleNamespace(quote=quote), SYMBOL), timeout=1)
        assert inputs.analysis is analysis
        assert inputs.market_quote is index
    asyncio.run(run())


def test_workflow_cancellation_drains_analysis_and_owned_index_request(monkeypatch) -> None:
    async def run() -> None:
        started = {name: asyncio.Event() for name in ("analysis", "market")}
        stopped: set[str] = set()
        async def pending(name: str):
            started[name].set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                stopped.add(name)
        async def analyze(_hub, _symbol, *, persist_history):
            return await pending("analysis")
        async def quote(_symbol):
            return await pending("market")
        monkeypatch.setattr(pipeline, "analyze_individual_stock", analyze)
        task = asyncio.create_task(pipeline._analysis_and_market(SimpleNamespace(quote=quote), SYMBOL))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped == {"analysis", "market"}
    asyncio.run(run())


def test_analysis_error_keeps_original_exception_and_waits_for_market_to_finish(monkeypatch) -> None:
    async def run() -> None:
        market_started, analysis_failed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        finished: list[str] = []
        error = RuntimeError("required stock analysis failed")
        async def analyze(_hub, _symbol, *, persist_history):
            await market_started.wait()
            analysis_failed.set()
            raise error
        async def quote(_symbol):
            market_started.set()
            await release.wait()
            finished.append("market")
            return make_quote()
        monkeypatch.setattr(pipeline, "analyze_individual_stock", analyze)
        task = asyncio.create_task(pipeline._analysis_and_market(SimpleNamespace(quote=quote), SYMBOL))
        await asyncio.wait_for(analysis_failed.wait(), timeout=1)
        assert not task.done()
        release.set()
        with pytest.raises(RuntimeError) as caught:
            await task
        assert caught.value is error
        assert finished == ["market"]
    asyncio.run(run())


def test_optional_index_request_failure_keeps_successful_analysis(monkeypatch) -> None:
    analysis = _context().analysis
    monkeypatch.setattr(pipeline, "analyze_individual_stock", AsyncMock(return_value=analysis))
    hub = SimpleNamespace(quote=AsyncMock(side_effect=RuntimeError("index unavailable")))
    result, index = asyncio.run(pipeline._analysis_and_market(hub, SYMBOL))
    assert result is analysis
    assert index is None


def test_optional_index_child_cancellation_propagates_after_analysis_finishes(monkeypatch) -> None:
    finished: list[str] = []
    async def analyze(_hub, _symbol, *, persist_history):
        await asyncio.sleep(0)
        finished.append("analysis")
        return _context().analysis
    monkeypatch.setattr(pipeline, "analyze_individual_stock", analyze)
    hub = SimpleNamespace(quote=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pipeline._analysis_and_market(hub, SYMBOL))
    assert finished == ["analysis"]
