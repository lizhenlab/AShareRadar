from __future__ import annotations

from dataclasses import replace
from copy import deepcopy

import pytest

from app.services.market_scan_research_challengers import (
    RESEARCH_CHALLENGERS, prepare_research_scores, research_challenger_spec,
    score_research_challenger, smooth_turnover_impact,
)
from tests.test_market_scan_raw_score_replay import _snapshot


def _prepared(monkeypatch):
    item, run = _snapshot(monkeypatch, mutation="none", mode="official")
    item = item.model_copy(update={"rank": 1})
    return prepare_research_scores([item], run), item, run


def test_production_control_preserves_frozen_score_and_inputs(monkeypatch) -> None:
    rows, item, _ = _prepared(monkeypatch)
    before = deepcopy(item.score_details)
    ranked = score_research_challenger(rows, "production_v5")
    assert ranked[0].raw_score == item.raw_score
    assert ranked[0].rank == item.rank
    assert item.score_details == before


def test_ablation_removes_only_the_continuous_component_before_clamping(monkeypatch) -> None:
    rows, _, _ = _prepared(monkeypatch)
    score = score_research_challenger(rows, "without_continuous_trend")[0]
    assert score.raw_score == pytest.approx(round(max(0, min(100, rows[0].trend_score-rows[0].quality_penalty)), 4))


@pytest.mark.parametrize("boundary", [1.0, 2.0, 8.0, 10.0, 14.0, 16.0])
def test_turnover_challenger_has_no_hard_cliff_at_declared_knots(boundary: float) -> None:
    assert abs(smooth_turnover_impact(boundary+1e-6)-smooth_turnover_impact(boundary-1e-6)) < 1e-4


@pytest.mark.parametrize("rate,expected", [(0, 0), (2, 8), (5, 8), (8, 8), (10, 0), (14, 0), (16, -5), (30, -5)])
def test_turnover_challenger_preserves_declared_plateaus(rate, expected) -> None:
    assert smooth_turnover_impact(rate) == expected


def test_same_scores_use_symbol_tiebreak_and_all_factorial_trials_are_named(monkeypatch) -> None:
    rows, _, _ = _prepared(monkeypatch)
    right = replace(rows[0], symbol="600002.SH", original_rank=2)
    for variant in RESEARCH_CHALLENGERS:
        result = score_research_challenger([right, rows[0]], variant)
        assert [row.symbol for row in result] == sorted([rows[0].symbol, right.symbol])
        spec = research_challenger_spec(variant)
        assert spec["production_mutation"] is False
        assert spec["research_only"] is True
    assert len(RESEARCH_CHALLENGERS) == 4


def test_self_consistent_tampering_is_rejected_before_challenger_replay(monkeypatch) -> None:
    item, run = _snapshot(monkeypatch, mutation="trend", mode="official")
    with pytest.raises(ValueError, match="frozen"):
        prepare_research_scores([item.model_copy(update={"rank": 1})], run)


def test_wrong_scope_and_missing_universe_are_rejected(monkeypatch) -> None:
    _, item, run = _prepared(monkeypatch)
    with pytest.raises(ValueError, match="full-market"):
        prepare_research_scores([item], run.model_copy(update={"scope": "subset"}))
    with pytest.raises(ValueError, match="universe"):
        prepare_research_scores([item], run.model_copy(update={"success_count": 2}))


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_turnover_is_not_neutralized(value) -> None:
    with pytest.raises(ValueError):
        smooth_turnover_impact(value)


def test_a_different_production_spec_cannot_enter_the_fixed_family(monkeypatch):
    from app.services import market_scan_research_challengers as challengers
    _, item, run = _prepared(monkeypatch)
    original = challengers.research_challenger_spec
    monkeypatch.setattr(challengers, "research_challenger_spec", lambda variant: {**original(variant), "production_score_contract": "b"*64})
    with pytest.raises(ValueError, match="quality threshold"):
        prepare_research_scores([item], run)


@pytest.mark.parametrize("malformation", ["empty", "duplicate", "nonfinite", "rank", "identity", "evidence"])
def test_malformed_research_universes_are_rejected(monkeypatch, malformation):
    rows, item, run = _prepared(monkeypatch)
    with pytest.raises(ValueError):
        if malformation == "empty":
            score_research_challenger([], "production_v5")
        elif malformation == "duplicate":
            prepare_research_scores([item, item], run.model_copy(update={"success_count": 2}))
        elif malformation == "nonfinite":
            score_research_challenger([replace(rows[0], production_raw_score=float("nan"))], "production_v5")
        elif malformation == "rank":
            prepare_research_scores([item.model_copy(update={"rank": 2})], run)
        elif malformation == "identity":
            prepare_research_scores([item.model_copy(update={"run_id": run.id+1})], run)
        else:
            prepare_research_scores([item.model_copy(update={"score_details": {}})], run)


def test_undeclared_variant_is_rejected():
    with pytest.raises(ValueError, match="unknown"):
        research_challenger_spec("not_registered")
