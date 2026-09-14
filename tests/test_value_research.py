from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.api.deps import get_datahub
from app.api.routes.fuyao import router
from app.models.fuyao import FinancialFact, FinancialPeriodRecord, FinancialReportBundle, FinancialSourceRecord
from app.models.value_research import ValueValuationEvidence
from app.services.fuyao_scoring import build_fuyao_valuation_score
from app.services.value_research import build_value_research
from tests.test_fuyao_scoring import CUTOFF, FETCHED, observation


def score(pe=20.0, pb=2.0):
    return build_fuyao_valuation_score("600519.SH", observation(pe=pe, pb=pb), CUTOFF)


def period(*, profit=100.0, cashflow=-10.0, equity=200.0, end="2025-12-31", kind="annual", fetched=FETCHED):
    specs = [("net_profit", "income", profit), ("act_cash_flow_net", "cashflow", cashflow),
             ("holder_equity_total", "balance", equity)]
    return FinancialPeriodRecord(
        period_end=end, period_type=kind, currency="CNY", alignment="complete", source="本期来源", fetched_at=fetched,
        statements=[FinancialSourceRecord(source_kind=source, currency="CNY") for _, source, _ in specs],
        metrics=[FinancialFact(key=key, label=key, source_kind=source, value=value,
                               raw_value=str(value) if value is not None else None) for key, source, value in specs],
    )


def bundle(*periods):
    return FinancialReportBundle(symbol="600519.SH", source="最新同步来源", fetched_at=CUTOFF, periods=list(periods))


def checks(report, index=0):
    return {item.key: item for item in report.periods[index].checks}


def test_positive_inverse_and_coverage_are_auditable_without_rescoring():
    original = score()
    report = build_value_research(original, bundle(period()))
    assert report.valuation.available_inputs == 2 and report.valuation.coverage == "complete"
    assert report.valuation.earnings_yield_pct == 5 and report.valuation.book_to_price_pct == 50
    assert report.valuation.source == original.source and report.valuation.fetched_at == original.fetched_at
    assert original.score == 69 and report.ranking_effect == "none" and report.point_in_time is False
    assert "不是股息率" in " ".join(report.limitations)
    assert not hasattr(report, "score") and not hasattr(report, "target_price")


@pytest.mark.parametrize("pe,pb,count,earnings,book", [
    (None, 2.0, 1, None, 50.0), (20.0, None, 1, 5.0, None), (0.0, 2.0, 1, None, 50.0),
    (20.0, 0.0, 1, 5.0, None), (-20.0, -2.0, 2, None, None), (0.0, None, 0, None, None),
])
def test_missing_zero_and_negative_values_have_distinct_coverage(pe, pb, count, earnings, book):
    value = build_value_research(score(pe, pb), None).valuation
    assert value.available_inputs == count
    assert value.earnings_yield_pct == earnings and value.book_to_price_pct == book
    assert value.coverage == {0: "unavailable", 1: "partial", 2: "complete"}[count]
    if pe is not None and pe < 0:
        assert value.checks[0].status == "attention" and value.checks[0].value == pe


def test_rejected_observation_with_retained_raw_values_cannot_bypass_admission():
    rejected = score().model_copy(update={"score": None, "score_available": False, "unavailable_reason": "晚于本次行情"})
    result = build_value_research(rejected, None).valuation
    assert result.available_inputs == 0 and result.earnings_yield_pct is None and result.book_to_price_pct is None
    assert all(item.value is None and "晚于" in item.summary for item in result.checks)


def test_tiny_positive_multiples_cannot_overflow_json_or_disappear_from_observed_coverage():
    report = build_value_research(score(5e-324, 5e-324), None)
    assert report.valuation.available_inputs == 2
    assert report.valuation.earnings_yield_pct is None and report.valuation.book_to_price_pct is None
    assert "超出" in report.valuation.earnings_yield_reason
    assert "Infinity" not in report.model_dump_json()


def test_mismatched_stock_rejected_before_financial_interpretation():
    wrong = bundle(period()).model_copy(update={"symbol": "000001.SZ"})
    with pytest.raises(ValueError, match="different stocks"):
        build_value_research(score(), wrong)


@pytest.mark.parametrize("updates", [{"available_inputs": 1}, {"coverage": "partial"}, {"checks": []},
                                     {"earnings_yield_pct": 500.0}, {"book_to_price_pct": None}])
def test_coverage_model_cannot_claim_inconsistent_input_count(updates):
    value = build_value_research(score(), None).valuation
    with pytest.raises(ValidationError):
        ValueValuationEvidence.model_validate({**value.model_dump(), **updates})


def test_complete_annual_profit_cashflow_divergence_is_a_check_not_a_fraud_or_health_score():
    report = build_value_research(score(), bundle(period()))
    result = checks(report)
    assert result["net_profit"].status == "observed"
    assert result["act_cash_flow_net"].status == "attention"
    assert result["holder_equity_total"].status == "observed"
    assert result["profit_cashflow_alignment"].status == "attention"
    assert "营运资本" in result["profit_cashflow_alignment"].summary
    assert "不据此断言" in result["profit_cashflow_alignment"].summary
    assert all(item.unit is None for item in result.values())


@pytest.mark.parametrize("profit,cashflow,equity", [(0.0, 0.0, 0.0), (-1.0, -2.0, -3.0), (1.0, 2.0, 3.0)])
def test_zero_negative_positive_signs_are_not_neutral_missing_values(profit, cashflow, equity):
    result = checks(build_value_research(score(), bundle(period(profit=profit, cashflow=cashflow, equity=equity))))
    for key, value in [("net_profit", profit), ("act_cash_flow_net", cashflow), ("holder_equity_total", equity)]:
        assert result[key].value == value
        assert result[key].status == ("observed" if value > 0 else "attention")
    assert "不代表" in result["profit_cashflow_alignment"].summary


@pytest.mark.parametrize("change", ["partial", "quarterly", "missing_statement", "duplicate_statement", "missing_profit"])
def test_joint_check_requires_complete_unique_annual_statements_and_both_values(change):
    item = period()
    if change == "partial":
        item = item.model_copy(update={"alignment": "partial"})
    elif change == "quarterly":
        item = item.model_copy(update={"period_type": "quarterly", "period_end": "2026-06-30"})
    elif change == "missing_statement":
        item = item.model_copy(update={"statements": item.statements[:2]})
    elif change == "duplicate_statement":
        item = item.model_copy(update={"statements": [*item.statements, item.statements[0]]})
    else:
        item = period(profit=None)
    result = checks(build_value_research(score(), bundle(item)))
    assert result["profit_cashflow_alignment"].status == "unavailable"


@pytest.mark.parametrize("change", ["duplicate", "wrong_source", "raw_missing", "parent_only"])
def test_ambiguous_or_parent_profit_cannot_replace_consolidated_profit(change):
    item = period()
    fact = item.metrics[0]
    if change == "duplicate":
        metrics = [*item.metrics, fact]
    else:
        update = {"wrong_source": {"source_kind": "indicators"}, "raw_missing": {"raw_value": None},
                  "parent_only": {"key": "parent_holder_net_profit"}}[change]
        metrics = [fact.model_copy(update=update), *item.metrics[1:]]
    item = item.model_copy(update={"metrics": metrics})
    result = checks(build_value_research(score(), bundle(item)))
    assert result["net_profit"].status == "unavailable" and result["net_profit"].value is None
    assert result["profit_cashflow_alignment"].status == "unavailable"


@pytest.mark.parametrize("raw", ["", "not-a-number", "-100", "NaN", "inf", "True", "100%", "1" * 513])
def test_dirty_or_inconsistent_raw_value_cannot_support_positive_financial_claims(raw):
    item = period()
    item.metrics[0] = item.metrics[0].model_copy(update={"raw_value": raw})
    result = checks(build_value_research(score(), bundle(item)))
    assert result["net_profit"].status == "unavailable"
    assert result["profit_cashflow_alignment"].status == "unavailable"


@pytest.mark.parametrize("fetched,end", [
    ("2026-09-12T10:00:00.000001+08:00", "2025-12-31"), ("2026-09-11", "2025-12-31"),
    ("invalid-time-format-000000", "2025-12-31"), (FETCHED, "2026-12-31"), (FETCHED, "2025-06-30"),
    ("2026-09-11         ", "2025-12-31"),
])
def test_future_or_unverifiable_financial_observation_cannot_form_risk_claims(fetched, end):
    report = build_value_research(score(), bundle(period(fetched=fetched, end=end)))
    assert not report.periods[0].observation_available
    assert all(item.status == "unavailable" and item.value is None for item in report.periods[0].checks)


def test_annual_and_quarterly_use_their_own_source_time_and_values():
    annual = period(fetched="2026-04-01T09:00:00+08:00")
    quarterly = period(profit=-40.0, kind="quarterly", end="2026-06-30").model_copy(update={"source": "中报来源"})
    report = build_value_research(score(), bundle(quarterly, annual))
    assert report.periods[0].source == "中报来源" and checks(report)["net_profit"].value == -40
    assert report.periods[1].fetched_at == annual.fetched_at and checks(report, 1)["net_profit"].value == 100
    assert checks(report)["profit_cashflow_alignment"].status == "unavailable"
    assert checks(report, 1)["profit_cashflow_alignment"].status == "attention"


def test_duplicate_periods_and_missing_periods_do_not_produce_an_all_clear():
    item = period()
    report = build_value_research(score(), bundle(item, item))
    assert all(not row.observation_available for row in report.periods)
    assert build_value_research(score(), None).periods == []
    legacy = item.model_copy(update={"source": None, "fetched_at": None})
    fallback = build_value_research(score(), bundle(legacy)).periods[0]
    assert fallback.source == "最新同步来源" and fallback.fetched_at == CUTOFF


def test_stock_endpoint_reuses_observations_without_provider_requests(monkeypatch):
    import app.api.routes.fuyao as routes
    record, financials = observation(pe=20, pb=2), bundle(period())
    repository = SimpleNamespace(latest=lambda *_: record, valuation_history=lambda *_: [])
    service = SimpleNamespace(repository=repository, financials=AsyncMock(return_value=financials))
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_datahub] = lambda: SimpleNamespace(fuyao=service)
    monkeypatch.setattr(routes, "audit_now_text", lambda: CUTOFF)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
            response = await client.get("/api/fuyao/stock?symbol=600519.SH")
        assert response.status_code == 200
        report = response.json()["value_research"]
        assert report["valuation"]["earnings_yield_pct"] == 5
        assert report["periods"][0]["checks"][-1]["status"] == "attention"
        assert report["evaluated_at"] == response.json()["valuation_score"]["evaluated_at"]
        assert response.json()["read_mode"] == "local_cache_only"
        service.financials.assert_awaited_once_with("600519.SH")
    asyncio.run(run())
