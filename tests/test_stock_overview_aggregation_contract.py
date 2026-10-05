from types import SimpleNamespace

import pytest

from app.models.analysis import FactorScore
from app.services import stock_overview
from app.services.analysis import build_analysis
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_quote


NAMES = ("技术面", "量价热度（衍生）", "基本面", "事件面")


def _aggregate(monkeypatch, values, risk=50):
    analysis = build_analysis(make_quote(), [])
    analysis = analysis.model_copy(update={
        "data_quality": analysis.data_quality.model_copy(update={"score": 100}),
        "signal_snapshot": analysis.signal_snapshot.model_copy(update={"confidence": 100}),
    })
    bundle = build_stock_insight_bundle(analysis)
    factors = [
        FactorScore(
            name=name, score=value if value is not None else 50, level="测试", summary="证据", evidence=[],
            score_available=value is not None, participates_in_total_score=value is not None,
            data_nature="derived" if value is not None else "unavailable",
            unavailable_reason=None if value is not None else "缺少该类证据",
        ) for name, value in zip(NAMES, values, strict=True)
    ]
    risk_factor = FactorScore(name="风险面", score=risk, level="测试", summary="风险", evidence=[])
    factors.append(risk_factor.model_copy(update={"aggregation_role": "risk_constraint"}))
    monkeypatch.setattr(stock_overview, "_overview_factors", lambda *args, **kwargs: factors)
    return stock_overview._overview_scores(analysis, bundle.fund_flow, bundle.order_pressure, bundle.events)


def test_removing_positive_valuation_does_not_transfer_its_budget_to_stronger_factors(monkeypatch):
    complete = _aggregate(monkeypatch, [80, 80, 55, None])
    missing = _aggregate(monkeypatch, [80, 80, None, None])
    assert missing.total_score <= complete.total_score
    assert missing.factor_score == 65


def test_neutral_risk_is_identity_for_bearish_direction(monkeypatch):
    result = _aggregate(monkeypatch, [20, 20, 20, 20])
    assert result.factor_score == result.total_score == 20


def test_neutral_evidence_does_not_dilute_existing_direction(monkeypatch):
    missing = _aggregate(monkeypatch, [80, 80, None, None])
    observed_neutral = _aggregate(monkeypatch, [80, 80, 50, 50])
    assert missing.factor_score == observed_neutral.factor_score


@pytest.mark.parametrize("values", [[20]*4, [50]*4, [80]*4])
def test_risk_only_subtracts_and_never_changes_the_direction_budget(monkeypatch, values):
    safe = _aggregate(monkeypatch, values)
    risky = _aggregate(monkeypatch, values, risk=20)
    assert safe.factor_score - risky.factor_score in {7, 8}
    assert risky.directional_evidence_score == safe.directional_evidence_score
    assert risky.risk_penalty == 7.5


def test_missing_evidence_has_no_direction_and_explicit_zero_coverage(monkeypatch):
    result = _aggregate(monkeypatch, [None]*4)
    assert result.total_score == 50
    assert result.evidence_coverage_pct == 0


@pytest.mark.parametrize("value", [0, 50, 100])
def test_fixed_budget_preserves_score_bounds(monkeypatch, value):
    for risk in (0, 20, 50):
        result = _aggregate(monkeypatch, [value]*4, risk=risk)
        assert 0 <= result.total_score <= 100
        assert result.evidence_coverage_pct == 100


def test_neutral_independent_event_adds_no_bullish_points():
    event = SimpleNamespace(category="公告", title="例行披露", level="观察")
    result = stock_overview._event_factor(SimpleNamespace(events=[event], notes=[]))
    assert result.score == 50


@pytest.mark.parametrize("category", ["行业", "异动", "历史复盘", "数据", "观察"])
def test_price_derived_and_context_events_remain_visible_without_direction(category):
    event = SimpleNamespace(category=category, title="同一份行情的派生结论", level="积极")
    result = stock_overview._event_factor(SimpleNamespace(events=[event], notes=[]))
    assert not result.score_available and not result.participates_in_total_score
    assert result.evidence


def test_opposite_independent_event_remains_a_risk_deduction():
    events = [SimpleNamespace(category="公告", title="风险事项", level="风险"),
              SimpleNamespace(category="业绩", title="正面事项", level="积极")]
    result = stock_overview._event_factor(SimpleNamespace(events=events, notes=[]))
    assert result.score < 50
