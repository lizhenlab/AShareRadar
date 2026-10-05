from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.market import PlateItem
from app.models.market_context import MarketContextScore
from app.services.analysis import build_analysis
from app.services.eastmoney_client import EASTMONEY_BRIDGE_SOURCE_NAME
from app.services.market_context_scoring import build_market_context_score
from app.services import market_context_scoring
from app.services.stock_insights import build_stock_insight_bundle
from app.services.stock_overview import _quality_adjusted_total_score
from app.workflows.workbench_pipeline import _market_index_or_unavailable
from tests.factories import make_kline, make_quote, make_stock_info


EVENT = "2026-09-14 15:00:00"
DECISION = "2026-09-14T07:05:00.000000Z"


def _quote(change=2.0, *, index=False):
    result = make_quote(price=100 + change, prev_close=100, change_pct=change, timestamp=EVENT)
    return result.model_copy(update={"code": "000300", "name": "沪深300"}) if index else result


def _industry(change=1.0):
    return PlateItem(rank=50, name="测试行业", symbol="BK0477", change_pct=change,
                     source=EASTMONEY_BRIDGE_SOURCE_NAME, quote_timestamp=EVENT, updated_at=DECISION)


def _score(base=80, *, stock=2.0, industry=1.0, market=0.0, reliability=100):
    return build_market_context_score(base, _quote(stock), market=_quote(market, index=True),
                                      industry=_industry(industry), industry_name="测试行业",
                                      evaluated_at=DECISION, reliability_score=reliability)


def test_three_layers_are_actual_bounded_contributions_and_do_not_telescope():
    result = _score(stock=3, industry=1, market=-2)
    assert result.stock_excess_pct == 2 and result.industry_excess_pct == 3
    assert result.relative_strength_score == 60
    assert result.before_gates_score == 81.2 and result.relative_adjustment == 1.2
    assert result.market_multiplier == 0.8 and result.industry_multiplier == 1
    assert result.raw_score == 74.96 and result.score == 75
    assert _score(stock=3, industry=2, market=-2).raw_score != result.raw_score
    assert result.market.symbol == "000300.SH" and result.industry.symbol == "BK0477"
    assert MarketContextScore.model_validate_json(result.model_dump_json()) == result
    assert result.historical_calibration_eligible is False


def test_common_rally_is_not_rewarded_multiple_times():
    neutral = _score(stock=0, industry=0, market=0)
    rally = _score(stock=4, industry=4, market=4)
    assert rally.stock_excess_pct == rally.industry_excess_pct == 0
    assert rally.relative_strength_score == 50
    assert rally.score == neutral.score == 80
    assert _score(stock=-4, industry=-4, market=-4).score < rally.score


@pytest.mark.parametrize("base", [0, 20, 40, 49, 50])
def test_relative_resistance_cannot_turn_nonpositive_base_bullish(base):
    result = _score(base, stock=-1, industry=-2, market=-3)
    assert result.stock_excess_pct > 0 and result.industry_excess_pct > 0
    assert base <= result.score <= 50


def test_market_gate_only_restricts_positive_deviation():
    bearish_market = _score(30, stock=-5, industry=-1, market=-3)
    bullish_market = _score(30, stock=-5, industry=-1, market=3)
    assert bearish_market.score == bullish_market.score
    assert _score(80, market=-3).score < _score(80, market=0).score
    assert _score(80, market=0).score >= _score(80, market=3).score


@pytest.mark.parametrize("base", [0, 30, 50, 70, 100])
@pytest.mark.parametrize("reliability", [0, 10, 50, 100])
def test_reliability_cannot_add_a_direction_or_amplify_context(base, reliability):
    full = _score(base, reliability=100)
    result = _score(base, reliability=reliability)
    assert result.raw_score == pytest.approx(50 + (full.pre_reliability_score - 50) * reliability / 100)
    assert min(50, full.score) <= result.score <= max(50, full.score)
    if reliability == 0:
        assert result.score == 50


def test_extreme_returns_saturate_and_preserve_bounds():
    result = _score(100, stock=10000, industry=-90, market=-90)
    assert result.relative_strength_score == 100
    assert result.market_multiplier == 0.5
    assert result.industry_multiplier == 1
    assert 50 <= result.score <= 75


@pytest.mark.parametrize("update", [
    {"code": "000001"}, {"market": "SZ"}, {"fallback_used": True}, {"source": ""},
    {"timestamp": "2026-09-13 15:00:00"}, {"timestamp": "2026-09-14"},
    {"timestamp": "2026-09-14 15:00:00.000001"}, {"timestamp": "2026-09-14 14:54:59.999999"},
    {"timestamp": "bad"}, {"prev_close": 0}, {"change_pct": 99},
])
def test_invalid_market_is_explicitly_missing_without_changing_industry_weight(update):
    result = build_market_context_score(80, _quote(), market=_quote(index=True).model_copy(update=update),
        industry=_industry(), industry_name="测试行业", evaluated_at=DECISION)
    assert result.market is None and result.industry is not None
    assert result.relative_weight == 0.2 and result.market_multiplier == 1
    assert result.industry_excess_pct is None and result.industry_multiplier == 1
    assert len(result.unavailable_reasons) == 1


@pytest.mark.parametrize("update", [
    {"symbol": None}, {"symbol": "881155.TI"}, {"name": "其他行业"}, {"source": "同名但不同分类"},
    {"quote_timestamp": None}, {"quote_timestamp": "2026-09-14"},
    {"quote_timestamp": "2026-09-13 15:00:00"}, {"quote_timestamp": "2026-09-14 15:00:00.000001"},
    {"updated_at": "2026-09-14 14:59:59"}, {"updated_at": "2026-09-14 15:05:00.000001"},
    {"updated_at": "2026-09-14"}, {"change_pct": float("nan")}, {"fallback_used": True},
])
def test_invalid_industry_does_not_create_relative_evidence(update):
    result = build_market_context_score(80, _quote(), market=_quote(-2, index=True),
        industry=_industry().model_copy(update=update), industry_name="测试行业", evaluated_at=DECISION)
    assert result.industry is None and result.stock_excess_pct is None
    assert result.relative_strength_score is None and result.relative_weight == 0
    assert result.before_gates_score == 80 and result.score == 74


def test_timezones_and_maximum_lag_are_compared_as_instants():
    market = _quote(index=True).model_copy(update={"timestamp": "2026-09-14T06:55:00Z"})
    result = build_market_context_score(80, _quote(), market=market, industry=None,
                                       industry_name=None, evaluated_at=DECISION)
    assert result.market is not None
    assert result.market.event_at == "2026-09-14T06:55:00.000000Z"


@pytest.mark.parametrize("cutoff", ["2026-09-14", "invalid", "2026-09-14 14:59:59.999999"])
def test_invalid_or_early_decision_rejects_both_layers(cutoff):
    result = build_market_context_score(80, _quote(), market=_quote(index=True), industry=_industry(),
                                       industry_name="测试行业", evaluated_at=cutoff)
    assert result.market is result.industry is None
    assert result.score == 80 and len(result.unavailable_reasons) == 2


def test_missing_context_preserves_base_after_reliability_without_inventing_zero_returns():
    result = build_market_context_score(80, _quote(), market=None, industry=None, industry_name=None,
                                       evaluated_at=DECISION, reliability_score=60)
    assert result.score == 68 and result.market is result.industry is None
    assert result.stock_excess_pct is result.industry_excess_pct is None


def test_reliable_bearish_direction_is_not_relabelled_positive():
    analysis = SimpleNamespace(data_quality=SimpleNamespace(score=100))
    assert _quality_adjusted_total_score(analysis, 40, 100) == 40
    assert _quality_adjusted_total_score(analysis, 40, 50) == 45
    assert _quality_adjusted_total_score(analysis, 80, 50) == 65


def test_real_insight_bundle_uses_context_score_and_preserves_underlying_evidence():
    rows = [make_kline(date=(datetime(2026, 7, 1) + timedelta(days=i)).date().isoformat(), close=100 + i / 40)
            for i in range(60)]
    analysis = build_analysis(_quote(), rows, stock_profile=make_stock_info(), industry_context=_industry())
    before = analysis.model_dump()
    result = build_stock_insight_bundle(analysis, market_quote=_quote(-3, index=True), evaluated_at=DECISION).overview
    assert result.market_context_score is not None
    assert result.total_score == result.market_context_score.score
    assert result.directional_score == result.market_context_score.base_score
    assert result.reliability_score == result.market_context_score.reliability_score
    assert analysis.model_dump() == before


def test_market_loading_is_single_optional_call_and_cancellation_propagates():
    async def check():
        hub = SimpleNamespace(quote=AsyncMock(return_value=_quote(index=True)))
        assert await _market_index_or_unavailable(hub) is not None
        hub.quote.assert_awaited_once_with("000300.SH")
        hub.quote.side_effect = RuntimeError("provider unavailable")
        assert await _market_index_or_unavailable(hub) is None
        hub.quote.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await _market_index_or_unavailable(hub)
    asyncio.run(check())


@pytest.mark.parametrize("base", [0, 20, 49, 50, 51, 80, 100])
@pytest.mark.parametrize("common_return", [-4, 0, 4])
def test_zero_excess_is_identity_before_risk_gates(base, common_return):
    result = _score(base, stock=common_return, industry=common_return, market=common_return)
    assert result.before_gates_score == base
    assert result.relative_adjustment == 0
    if common_return >= 0 or base <= 50:
        assert result.raw_score == base
    else:
        assert result.raw_score == pytest.approx(50 + (base - 50) * 0.6)


@pytest.mark.parametrize("excess", [-10, -2, 0, 2, 10])
def test_base_is_monotone_without_a_jump_at_neutral(excess):
    results = [_score(base, stock=excess, industry=0).raw_score for base in range(101)]
    assert results[50] == 50
    assert all(0 <= after - before <= 1.200001 for before, after in zip(results, results[1:], strict=False))
    assert all(value <= 50 for value in results[:51])
    assert all(value >= 50 for value in results[50:])


@pytest.mark.parametrize("relative", [0, 25, 50, 75, 100])
def test_unrounded_relative_formula_is_continuous_at_neutral(relative):
    for epsilon in [1e-3, 1e-6, 1e-9]:
        below = market_context_scoring._relative_context_score(50 - epsilon, relative)
        above = market_context_scoring._relative_context_score(50 + epsilon, relative)
        assert 50 - 1.2 * epsilon - 1e-12 <= below <= 50 <= above <= 50 + 1.2 * epsilon + 1e-12


@pytest.mark.parametrize("base", [0, 20, 49, 50, 51, 80, 100])
def test_relative_evidence_is_monotone_and_scales_with_existing_direction(base):
    results = [_score(base, stock=excess, industry=0) for excess in [-50, -10, -2, 0, 2, 10, 50]]
    assert [result.before_gates_score for result in results] == sorted(result.before_gates_score for result in results)
    assert results[0].before_gates_score == results[1].before_gates_score
    assert results[-2].before_gates_score == results[-1].before_gates_score
    for result in results:
        assert 0 <= result.raw_score <= 100
        assert abs(result.relative_adjustment) <= 0.2 * abs(base - 50) + 1e-9
        assert result.relative_adjustment == pytest.approx(result.before_gates_score - base)


def test_relative_outperformance_is_an_increment_rather_than_a_second_baseline():
    bullish = _score(80, stock=2, industry=0)
    bearish = _score(20, stock=2, industry=0)
    assert bullish.before_gates_score == 81.2
    assert bearish.before_gates_score == 21.2
    assert _score(51, stock=10, industry=0).before_gates_score == 51.2
    assert _score(49, stock=-10, industry=0).before_gates_score == 48.8


def test_v2_results_explain_new_weight_while_old_v1_payload_stays_readable():
    current = _score()
    assert current.rule_version == "current-market-context.v2"
    assert "幅度" in current.note
    old = current.model_dump(exclude={"relative_adjustment"})
    old.update(rule_version="current-market-context.v1", before_gates_score=76, raw_score=76, score=76,
               pre_reliability_score=76, note="旧版历史说明")
    restored = MarketContextScore.model_validate(old)
    assert restored.rule_version == "current-market-context.v1"
    assert restored.raw_score == restored.before_gates_score == 76
    assert restored.relative_adjustment is None and restored.note == "旧版历史说明"


def test_unversioned_legacy_payload_is_not_relabelled_as_v2():
    old = _score().model_dump(exclude={"rule_version", "relative_adjustment", "note"})
    old.update(before_gates_score=76, raw_score=76, score=76, pre_reliability_score=76)
    restored = MarketContextScore.model_validate(old)
    assert restored.rule_version == "current-market-context.v1"
    assert restored.before_gates_score == restored.raw_score == 76
    assert restored.relative_adjustment is None and "v2" not in restored.note
    compact = restored.model_dump(exclude_defaults=True)
    assert "rule_version" not in compact
    assert MarketContextScore.model_validate(compact) == restored


@pytest.mark.parametrize("stock,market", [(1e308, 0), (2, 1e308)])
def test_finite_observations_with_overflowing_excess_are_unavailable(stock, market):
    result = _score(80, stock=stock, industry=-1e308, market=market)
    assert result.industry is None
    assert result.stock_excess_pct is result.industry_excess_pct is None
    assert result.relative_strength_score is None and result.relative_adjustment == 0
    assert result.raw_score == 80
    assert any("数值范围" in reason for reason in result.unavailable_reasons)
