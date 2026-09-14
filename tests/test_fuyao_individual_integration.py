from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.models.fuyao import FinancialFact, FinancialPeriodRecord, FinancialReportBundle
from app.models.research import StockQuestionInput
from app.services.datahub import DataHub
from app.services.fuyao_financials import financial_fact_answer, financial_health_from_bundle
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


def _period_observations():
    periods = [FinancialPeriodRecord(
        period_end=end, period_type=kind, fetched_at=fetched, source=source,
        metrics=[FinancialFact(key="operating_income", label="营业收入", value=value,
                               raw_value=str(value), source_kind="income")],
    ) for end, kind, fetched, source, value in (
        ("2026-06-30", "quarterly", "2026-09-10T09:00:00+08:00", "中报观察来源", 60.0),
        ("2025-12-31", "annual", "2026-04-01T09:00:00+08:00", "年报观察来源", 100.0),
    )]
    return FinancialReportBundle(symbol="600519.SH", source="最近一次同步来源",
                                 fetched_at="2026-09-11T09:00:00+08:00", periods=periods)


@pytest.mark.parametrize("index,question", [(0, "2026年中报营业收入是多少"), (1, "2025年年报营业收入是多少")])
def test_cached_fact_answer_keeps_selected_period_observation_provenance(monkeypatch, index, question):
    bundle = _period_observations()
    period = bundle.periods[index]
    hub = SimpleNamespace(fuyao=SimpleNamespace(financials=AsyncMock(return_value=bundle)))
    workbench, enhance = AsyncMock(), AsyncMock()
    monkeypatch.setattr(individual, "stock_workbench_context", workbench)
    monkeypatch.setattr(individual, "enhance_stock_answer", enhance)
    answer = asyncio.run(individual.stock_question_answer(hub, StockQuestionInput(symbol=bundle.symbol, question=question)))
    assert answer.answerability == "answerable" and not answer.llm_used
    assert answer.updated_at == period.fetched_at
    assert answer.answer_source == f"{period.source}结构化财务记录"
    assert period.source in answer.answer and period.fetched_at in answer.answer
    assert period.fetched_at in " ".join(answer.evidence)
    assert bundle.fetched_at not in answer.answer and bundle.source not in answer.answer
    assert "单位待核实" in answer.answer and "不等同于已核实的首次披露" in answer.answer
    assert bundle.point_in_time is False and bundle.score is None
    workbench.assert_not_awaited()
    enhance.assert_not_awaited()


def test_financial_health_keeps_selected_period_source_without_granting_score():
    bundle = _period_observations()
    health = financial_health_from_bundle(bundle)
    period = bundle.periods[0]
    assert health.report_period == period.period_end
    assert health.source == period.source and health.updated_at == period.fetched_at
    assert all(metric.source == period.source for metric in health.metrics)
    assert period.source in health.highlights[0] and period.fetched_at in health.highlights[0]
    assert bundle.fetched_at not in health.highlights[0]
    assert health.score is None and not health.score_available and not health.formal_minimum_complete
    assert "季度报告的单季或累计口径" in health.missing_data


@pytest.mark.parametrize("missing_field", ["source", "fetched_at", "both"])
def test_legacy_period_metadata_falls_back_independently_to_bundle(missing_field):
    bundle = _period_observations()
    fields = ("source", "fetched_at") if missing_field == "both" else (missing_field,)
    payload = bundle.model_dump()
    for field in fields:
        payload["periods"][0].pop(field)
    bundle = FinancialReportBundle.model_validate(payload)
    period = bundle.periods[0]
    source, fetched_at = period.source or bundle.source, period.fetched_at or bundle.fetched_at
    health = financial_health_from_bundle(bundle)
    answer = financial_fact_answer("2026年中报营业收入是多少", bundle)
    assert health.source == source and health.updated_at == fetched_at
    assert answer.answerability == "answerable" and answer.updated_at == fetched_at
    assert answer.answer_source == f"{source}结构化财务记录"
    assert source in answer.answer and fetched_at in answer.answer


@pytest.mark.parametrize("unknown_unit", [False, True])
def test_missing_period_fact_keeps_its_observation_time_and_source(unknown_unit):
    bundle = _period_observations()
    period = bundle.periods[1]
    if unknown_unit:
        fact = FinancialFact(key="calculate_operating_income_yoy_growth_ratio", label="营收同比", value=3.0,
                             raw_value="3", unit=None, source_kind="indicators")
        period.metrics.append(fact)
    question = "2025年营收同比增长率是多少" if unknown_unit else "2025年净利润是多少"
    answer = financial_fact_answer(question, bundle)
    assert answer.answerability == "insufficient_evidence"
    assert answer.updated_at == period.fetched_at and answer.answer_source == f"{period.source}结构化财务记录"
    assert period.fetched_at in answer.answer and bundle.fetched_at not in answer.answer


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
