from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import Settings
from app.models.research import StockQuestionInput
from app.services.datahub import DataHub
from app.workflows import individual
from tests.test_fuyao_financials import _bundle


def test_financial_fact_uses_cache_without_workbench_or_llm(monkeypatch):
    bundle = _bundle()
    source = SimpleNamespace(financials=AsyncMock(return_value=bundle))
    hub = SimpleNamespace(fuyao=source)
    workbench, enhance = AsyncMock(), AsyncMock()
    monkeypatch.setattr(individual, "stock_workbench_context", workbench)
    monkeypatch.setattr(individual, "enhance_stock_answer", enhance)
    answer = asyncio.run(individual.stock_question_answer(hub, StockQuestionInput(symbol=bundle.symbol, question="2025年营业收入是多少")))
    assert answer.answerability == "answerable" and not answer.llm_used
    assert "123456789.5" in answer.answer and "单位待核实" in answer.answer
    assert source.financials.await_args.args == (bundle.symbol,)
    workbench.assert_not_awaited()
    enhance.assert_not_awaited()


def test_financial_health_prefers_observed_period_but_does_not_score(monkeypatch):
    bundle = _bundle()
    hub = SimpleNamespace(fuyao=SimpleNamespace(financials=AsyncMock(return_value=bundle)))
    insights = AsyncMock()
    monkeypatch.setattr(individual, "stock_insight_bundle", insights)
    health = asyncio.run(individual.stock_financial_health(hub, bundle.symbol))
    assert health.report_period == "2025-12-31" and health.score is None
    assert not health.score_available and not health.formal_minimum_complete
    insights.assert_not_awaited()


def test_no_cached_reports_retains_existing_financial_fallback(monkeypatch):
    fallback = object()
    hub = SimpleNamespace(fuyao=SimpleNamespace(financials=AsyncMock(return_value=None)))
    monkeypatch.setattr(individual, "stock_insight_bundle", AsyncMock(return_value=SimpleNamespace(financial_health=fallback)))
    assert asyncio.run(individual.stock_financial_health(hub, "600519.SH")) is fallback


def test_datahub_closes_research_jobs_before_provider_runtime(tmp_path):
    async def scenario():
        hub = DataHub(settings=Settings(cache_path=tmp_path / "cache.sqlite3", fuyao_enabled=False))
        events = []
        original_close = hub.fuyao.aclose

        async def close_research():
            await original_close()
            events.append("research")

        async def close_runtime(**_kwargs):
            events.append("runtime")
            return True

        hub.fuyao.aclose = close_research
        hub._provider_runtime.aclose = close_runtime
        assert await hub.aclose()
        assert events == ["research", "runtime"]
        assert hub.fuyao.client.status()["closed"]
        assert await hub.aclose()
        assert events == ["research", "runtime"]

    asyncio.run(scenario())
