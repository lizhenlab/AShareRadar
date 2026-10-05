from __future__ import annotations

import math

import pytest

from app.models.research import StandardFactor
from app.services.research_factor_aggregation import aggregate_factor_scores
from app.services.research_factor_scoring import _weighted_factor_score


DIRECTION_IDS = (
    "trend_momentum", "chip_position", "volume_confirmation", "fund_flow_proxy", "valuation_anchor",
)


def _factor(factor_id="trend_momentum", score=50, **updates):
    values = dict(id=factor_id, name=factor_id, category="合成", value="合成",
                  score=score, level="观察", direction="中性", weight=1,
                  participates_in_current_score=True, data_nature="derived")
    return StandardFactor(**{**values, **updates})


def _all_scores(score=50):
    return [_factor(factor_id, score) for factor_id in DIRECTION_IDS]


def test_missing_negative_slot_does_not_transfer_its_budget_to_positive_evidence():
    positive = _factor("trend_momentum", 80, weight=1.35)
    negative = _factor("fund_flow_proxy", 20, weight=1.1)
    unavailable = negative.model_copy(update={"participates_in_current_score": False, "data_nature": "unavailable"})
    assert _weighted_factor_score([positive, negative]) == 50
    assert _weighted_factor_score([positive, unavailable]) == 55


@pytest.mark.parametrize("factor_id", DIRECTION_IDS)
def test_each_missing_positive_slot_shrinks_toward_neutral(factor_id):
    factors = _all_scores(80)
    partial = [item.model_copy(update={"participates_in_current_score": False, "data_nature": "unavailable"})
               if item.id == factor_id else item for item in factors]
    expected = 70 if factor_id == "valuation_anchor" else 75
    assert _weighted_factor_score(factors) == 80
    assert _weighted_factor_score(partial) == expected


@pytest.mark.parametrize("risk_score", [50, 51, 69, 78, 82, 100])
def test_risk_observation_cannot_reward_direction(risk_score):
    assert _weighted_factor_score([*_all_scores(), _factor("risk_pressure", risk_score)]) == 50


@pytest.mark.parametrize("risk_score,expected", [(0, 68), (10, 70), (30, 75), (46, 79), (50, 80)])
def test_risk_only_deducts_one_quarter_of_its_below_neutral_distance(risk_score, expected):
    assert _weighted_factor_score([*_all_scores(80), _factor("risk_pressure", risk_score)]) == expected


@pytest.mark.parametrize("weight", [0, -3, 0.01, 1, 1000, math.inf, math.nan])
def test_external_weights_cannot_reallocate_directional_budgets(weight):
    factors = [_factor("trend_momentum", 100, weight=weight), _factor("valuation_anchor", 0, weight=3)]
    assert _weighted_factor_score(factors) == 42


@pytest.mark.parametrize("unknown_id", ["legacy", "trend", "leadership_strength"])
def test_unknown_or_composite_ids_cannot_create_directional_slots(unknown_id):
    assert _weighted_factor_score([_factor(unknown_id, 100)]) == 50


@pytest.mark.parametrize("factor_id", [*DIRECTION_IDS, "risk_pressure"])
def test_duplicate_known_identity_is_rejected_before_aggregation(factor_id):
    inactive = _factor(factor_id, 100, participates_in_current_score=False)
    with pytest.raises(ValueError, match="duplicate factor"):
        _weighted_factor_score([_factor(factor_id, 0), inactive])


@pytest.mark.parametrize("updates", [
    {"participates_in_current_score": False},
    {"aggregation_role": "composite"},
    {"data_nature": "unavailable"},
    {"score": math.nan}, {"score": math.inf}, {"score": -math.inf},
])
def test_untrusted_reconstructed_factor_cannot_supply_missing_evidence(updates):
    dirty = _factor(score=100).model_copy(update=updates)
    assert _weighted_factor_score([dirty]) == 50


def test_empty_input_is_neutral():
    assert _weighted_factor_score([]) == 50


def test_breakdown_exposes_fixed_budgets_and_all_reconstruction_terms():
    result = aggregate_factor_scores([
        _factor("trend_momentum", 80), _factor("fund_flow_proxy", 20), _factor("risk_pressure", 30),
    ])
    assert result.version == "factor-aggregation.v3"
    assert result.directional_score == 50
    assert result.risk_penalty == 5
    assert result.total_score == 45
    assert result.coverage_pct == pytest.approx(100 / 3)
    assert [group.name for group in result.groups] == ["趋势与价位", "量价", "估值"]
    assert [group.budget_pct for group in result.groups] == pytest.approx([100 / 3] * 3)
    assert [group.contribution for group in result.groups] == pytest.approx([5, -5, 0])
    assert [group.coverage_pct for group in result.groups] == [50, 50, 0]
    assert result.factor_shares == pytest.approx({
        **dict.fromkeys(DIRECTION_IDS[:-1], 100 / 6), "valuation_anchor": 100 / 3, "risk_pressure": 0,
    })


@pytest.mark.parametrize("score", [0, 20, 50, 80, 100])
def test_full_coverage_does_not_depend_on_direction_or_risk(score):
    result = aggregate_factor_scores([*_all_scores(score), _factor("risk_pressure", 0)])
    assert result.directional_score == score
    assert result.coverage_pct == 100
    assert all(group.coverage_pct == 100 for group in result.groups)
    assert result.total_score == round(max(0, score - 12.5))


def test_group_budget_does_not_move_to_its_only_available_slot():
    partial = aggregate_factor_scores([_factor("volume_confirmation", 100)])
    both = aggregate_factor_scores([_factor("volume_confirmation", 100), _factor("fund_flow_proxy", 100)])
    assert partial.groups[1].contribution == pytest.approx(50 / 6)
    assert both.groups[1].contribution == pytest.approx(50 / 3)
    assert partial.groups[1].budget_pct == both.groups[1].budget_pct


def test_risk_cannot_supply_directional_coverage():
    result = aggregate_factor_scores([_factor("risk_pressure", 0)])
    assert result.directional_score == 50
    assert result.risk_penalty == 12.5
    assert result.total_score == 38
    assert result.coverage_pct == 0
    assert all(group.coverage_pct == 0 for group in result.groups)


def test_unknown_and_inadmissible_factors_are_explicitly_excluded():
    factors = [_factor("legacy", 100), _factor("legacy", 0),
               _factor("chip_position", 100, participates_in_current_score=False),
               _factor("leadership_strength", 100, aggregation_role="composite", participates_in_current_score=False)]
    result = aggregate_factor_scores(factors)
    assert result.excluded_ids == ("chip_position", "leadership_strength", "legacy")
    assert result.total_score == 50 and result.coverage_pct == 0
    assert result == aggregate_factor_scores(list(reversed(factors)))


def test_valid_input_order_cannot_change_result():
    factors = [_factor("trend_momentum", 91), _factor("fund_flow_proxy", 24), _factor("risk_pressure", 33)]
    assert aggregate_factor_scores(factors) == aggregate_factor_scores(list(reversed(factors)))


@pytest.mark.parametrize("score", [None, True, False, "80", "bad", math.nan, math.inf, -math.inf, 10 ** 400])
def test_dirty_nonfinite_or_nonnumeric_risk_is_excluded_without_poisoning_direction(score):
    dirty = _factor("risk_pressure", 0).model_copy(update={"score": score})
    result = aggregate_factor_scores([*_all_scores(80), dirty])
    assert result.total_score == result.directional_score == 80
    assert result.risk_penalty == 0
    assert result.excluded_ids == ("risk_pressure",)


@pytest.mark.parametrize("score,expected", [(-20, 42), (150, 58)])
def test_finite_legacy_scores_are_bounded_before_contribution(score, expected):
    assert aggregate_factor_scores([_factor(score=score)]).total_score == expected


def test_rounding_occurs_once_after_risk_deduction():
    factors = [_factor(factor_id, 51) for factor_id in DIRECTION_IDS[:-1]]
    result = aggregate_factor_scores([*factors, _factor("risk_pressure", 49)])
    assert result.directional_score == pytest.approx(50 + 2 / 3)
    assert result.risk_penalty == 0.25
    assert result.total_score == 50


def test_returned_share_mapping_cannot_mutate_future_budgets():
    first = aggregate_factor_scores([])
    first.factor_shares["valuation_anchor"] = 100
    assert aggregate_factor_scores([]).factor_shares["valuation_anchor"] == pytest.approx(100 / 3)
