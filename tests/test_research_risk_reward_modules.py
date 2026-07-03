from __future__ import annotations

import math
from types import SimpleNamespace

import app.services.research_risk_reward as risk_reward
from app.services.research_risk_reward import (
    DOWNSIDE_STOP_ADJUSTMENT_RULES,
    RISK_REWARD_RATING_RULES,
    build_risk_reward_report,
    _downside_distance_pct,
    _downside_stop,
    _normalize_scenario_probabilities,
    _reward_risk_ratio,
    _risk_reward_metrics,
    _risk_reward_rating,
    _risk_reward_summary,
    _scenario_plans,
    _scenario_probabilities,
    _upside_distance_pct,
)


def test_rating_high_timeframe_conflict_has_top_priority() -> None:
    rating = _risk_reward_rating(
        2.1,
        _factor_lab(total_score=70),
        _market_regime(risk_multiplier=1.35, breadth_score=60),
        _validation("风险优先"),
        _timeframe("高冲突"),
    )

    assert rating == "周期冲突"


def test_rating_low_ratio_medium_timeframe_conflict_blocks_action() -> None:
    rating = _risk_reward_rating(
        1.34,
        _factor_lab(total_score=70),
        _market_regime(risk_multiplier=1.0, breadth_score=60),
        _validation("条件较好"),
        _timeframe("中冲突"),
    )

    assert rating == "周期冲突"


def test_rating_external_risk_priority_beats_attractive_ratio() -> None:
    rating = _risk_reward_rating(
        2.0,
        _factor_lab(total_score=72),
        _market_regime(risk_multiplier=1.3, breadth_score=70),
        _validation("条件较好"),
    )

    assert rating == "风险优先"


def test_rating_attractive_ratio_requires_factor_validation_and_breadth() -> None:
    assert (
        _risk_reward_rating(
            1.8,
            _factor_lab(total_score=58),
            _market_regime(risk_multiplier=1.0, breadth_score=42),
            _validation("等待二次确认"),
        )
        == "性价比较好"
    )
    assert (
        _risk_reward_rating(
            1.8,
            _factor_lab(total_score=57),
            _market_regime(risk_multiplier=1.0, breadth_score=42),
            _validation("等待二次确认"),
        )
        == "性价比一般"
    )


def test_rating_waits_when_ratio_is_good_but_timeframe_is_mixed() -> None:
    rating = _risk_reward_rating(
        1.55,
        _factor_lab(total_score=70),
        _market_regime(risk_multiplier=1.0, breadth_score=60),
        _validation("条件较好"),
        _timeframe("多周期偏弱"),
    )

    assert rating == "等待确认"


def test_rating_waits_for_mixed_timeframe_mid_ratio_gap() -> None:
    assert (
        _risk_reward_rating(
            1.35,
            _factor_lab(total_score=70),
            _market_regime(risk_multiplier=1.0, breadth_score=60),
            _validation("条件较好"),
            _timeframe("中冲突"),
        )
        == "等待确认"
    )
    assert (
        _risk_reward_rating(
            1.54,
            _factor_lab(total_score=70),
            _market_regime(risk_multiplier=1.0, breadth_score=60),
            _validation("条件较好"),
            _timeframe("多周期偏弱"),
        )
        == "等待确认"
    )


def test_rating_default_boundaries_are_explicit() -> None:
    assert _risk_reward_rating(1.2, _factor_lab(), _market_regime(), _validation("中性")) == "性价比一般"
    assert _risk_reward_rating(1.19, _factor_lab(), _market_regime(), _validation("中性")) == "性价比不足"


def test_rating_rule_order_is_intentional() -> None:
    assert [rule.name for rule in RISK_REWARD_RATING_RULES] == [
        "high_timeframe_conflict",
        "low_ratio_timeframe_conflict",
        "external_risk_priority",
        "attractive_risk_reward",
        "timeframe_confirmation",
        "acceptable_risk_reward",
    ]


def test_downside_stop_uses_structural_and_volatility_candidates() -> None:
    stop = _downside_stop(
        _feature(price=100, support=95, ma20=96, atr14=1, atr_pct=1, volatility_pct=2),
        _market_regime(risk_multiplier=1.0),
    )

    assert stop == 95


def test_downside_stop_ignores_stale_structural_levels_too_far_below_price() -> None:
    stop = _downside_stop(
        _feature(price=100, support=60, ma20=96, atr14=1, atr_pct=1, volatility_pct=2),
        _market_regime(risk_multiplier=1.0),
    )

    assert stop == 96


def test_downside_stop_tightens_when_market_risk_is_high() -> None:
    normal_stop = _downside_stop(
        _feature(price=100, support=0, ma20=0, atr14=10, atr_pct=1, volatility_pct=2),
        _market_regime(risk_multiplier=1.0),
    )
    high_risk_stop = _downside_stop(
        _feature(price=100, support=0, ma20=0, atr14=10, atr_pct=1, volatility_pct=2),
        _market_regime(risk_multiplier=1.2),
    )

    assert normal_stop == 92.5
    assert high_risk_stop == 94.5


def test_downside_stop_adjustment_rules_widen_for_volatility_and_atr() -> None:
    assert [rule.name for rule in DOWNSIDE_STOP_ADJUSTMENT_RULES] == ["wide_volatility", "wide_atr"]

    stop = _downside_stop(
        _feature(price=100, support=0, ma20=0, atr14=10, atr_pct=3.2, volatility_pct=4),
        _market_regime(risk_multiplier=1.0),
    )

    assert round(stop, 2) == 90.3


def test_downside_stop_handles_invalid_price() -> None:
    assert _downside_stop(_feature(price=0), _market_regime()) == 0


def test_downside_stop_sanitizes_non_finite_inputs() -> None:
    stop = _downside_stop(
        _feature(price=100, support=math.inf, ma20=math.nan, atr14=math.inf, atr_pct=math.nan, volatility_pct=math.inf),
        _market_regime(risk_multiplier=math.inf),
    )

    assert stop == 97


def test_rating_defaults_invalid_market_risk_to_neutral_multiplier() -> None:
    rating = _risk_reward_rating(
        1.19,
        _factor_lab(total_score=70),
        _market_regime(risk_multiplier=math.inf, breadth_score=60),
        _validation("条件较好"),
    )

    assert rating == "性价比不足"


def test_rating_sanitizes_non_finite_ratio_and_scores() -> None:
    rating = _risk_reward_rating(
        math.inf,
        _factor_lab(total_score=math.inf),
        _market_regime(risk_multiplier=1.0, breadth_score=math.inf),
        _validation("条件较好"),
    )

    assert rating == "性价比不足"


def test_reward_risk_ratio_rejects_non_finite_and_non_positive_inputs() -> None:
    assert _reward_risk_ratio(6, 3) == 2
    assert _reward_risk_ratio(math.inf, 3) == 0
    assert _reward_risk_ratio(-6, 3) == 0
    assert _reward_risk_ratio(6, 0) == 0


def test_distance_pct_requires_target_and_stop_on_expected_side() -> None:
    assert _upside_distance_pct(105, 100) == 5
    assert _upside_distance_pct(95, 100) == 0
    assert _downside_distance_pct(95, 100) == 5
    assert _downside_distance_pct(105, 100) == 0


def test_metrics_drop_target_and_stop_on_wrong_side_of_current_price(monkeypatch) -> None:
    monkeypatch.setattr(risk_reward, "_upside_target", lambda feature, factor_lab: 98)
    monkeypatch.setattr(risk_reward, "_downside_stop", lambda feature, market_regime: 102)

    metrics = _risk_reward_metrics(_feature(price=100), _factor_lab(), _market_regime())

    assert metrics.upside_target == 0
    assert metrics.downside_stop == 0
    assert metrics.upside_pct == 0
    assert metrics.downside_pct == 0
    assert metrics.ratio == 0


def test_metrics_cap_extreme_upside_target_inputs() -> None:
    metrics = _risk_reward_metrics(
        _feature(price=100, resistance=1000, atr14=1000, atr_pct=1, volatility_pct=2),
        _factor_lab(total_score=80, positive_factor_count=4, negative_factor_count=0),
        _market_regime(),
    )

    assert round(metrics.upside_target, 2) == 108
    assert round(metrics.upside_pct, 2) == 8


def test_summary_requires_explicit_levels_on_expected_side() -> None:
    missing_target = _risk_reward_summary(
        "性价比一般",
        1.25,
        5,
        4,
        _market_regime(),
        feature=_feature(price=100),
        upside_target=None,
        downside_stop=96,
    )
    wrong_side_stop = _risk_reward_summary(
        "性价比一般",
        1.25,
        5,
        4,
        _market_regime(),
        feature=_feature(price=100),
        upside_target=105,
        downside_stop=102,
    )
    blank_timeframe = _risk_reward_summary(
        "性价比一般",
        1.25,
        5,
        4,
        _market_regime(),
        timeframe=SimpleNamespace(alignment_label=" "),
        feature=_feature(price=100),
        upside_target=105,
        downside_stop=96,
    )

    assert "上方预估空间待确认" in missing_target
    assert "收益风险比待确认" in missing_target
    assert "下方防守距离待确认" in wrong_side_stop
    assert "收益风险比待确认" in wrong_side_stop
    assert "多周期「待确认」" in blank_timeframe
    assert "「」" not in blank_timeframe


def test_scenario_probabilities_sum_to_100_and_sanitize_inputs() -> None:
    strong = _scenario_probabilities(90, 0.8)
    defensive = _scenario_probabilities(20, 1.4)
    malformed = _scenario_probabilities(math.inf, math.nan)

    assert strong.positive > defensive.positive
    assert defensive.risk > strong.risk
    for probabilities in (strong, defensive, malformed):
        assert probabilities.positive + probabilities.neutral + probabilities.risk == 100
        assert probabilities.neutral >= 0


def test_scenario_probabilities_keep_neutral_floor_after_normalization() -> None:
    probabilities = _scenario_probabilities(100, 10)

    assert probabilities.positive + probabilities.neutral + probabilities.risk == 100
    assert probabilities.neutral >= 10
    assert probabilities.risk > probabilities.positive


def test_scenario_probability_normalization_clamps_malformed_inputs() -> None:
    malformed = _normalize_scenario_probabilities(-10, 20.4, math.inf)
    crowded = _normalize_scenario_probabilities(1000, 0, 1000)
    fractional = _normalize_scenario_probabilities(1, 1, 1)

    for probabilities in (malformed, crowded, fractional):
        assert probabilities.positive + probabilities.neutral + probabilities.risk == 100
        assert probabilities.positive >= 0
        assert probabilities.neutral >= 0
        assert probabilities.risk >= 0
    assert malformed.positive == 0
    assert crowded.neutral >= 10
    assert fractional == risk_reward.ScenarioProbabilities(positive=34, neutral=33, risk=33)


def test_scenario_plans_use_waiting_text_for_missing_price_levels() -> None:
    scenarios = _scenario_plans(
        _analysis(action="控制风险"),
        _feature(support=math.nan, resistance=math.inf, ma20=0),
        _factor_lab(total_score=math.inf),
        _market_regime(risk_multiplier=math.nan),
        _validation("中性"),
        math.nan,
        0,
    )
    scenario_text = " ".join(
        " ".join([item.trigger, item.expected_move, item.response, item.invalidation]) for item in scenarios
    )

    assert [item.name for item in scenarios] == ["积极路径", "震荡路径", "防守路径"]
    assert sum(item.probability for item in scenarios) == 100
    assert "待确认" in scenario_text
    assert "压力位待确认，需先形成可验证突破边界" in scenario_text
    assert "0.00" not in scenario_text


def test_scenario_plans_cool_positive_probability_when_levels_are_missing() -> None:
    complete = _scenario_plans(
        _analysis(),
        _feature(price=100, support=95, resistance=108, ma20=96),
        _factor_lab(total_score=90),
        _market_regime(risk_multiplier=0.8),
        _validation("条件较好"),
        112,
        95,
    )
    missing = _scenario_plans(
        _analysis(),
        _feature(price=100, support=math.nan, resistance=math.inf, ma20=0),
        _factor_lab(total_score=90),
        _market_regime(risk_multiplier=0.8),
        _validation("条件较好"),
        math.nan,
        0,
    )

    assert missing[0].probability < complete[0].probability
    assert missing[1].probability + missing[2].probability > complete[1].probability + complete[2].probability
    assert sum(item.probability for item in missing) == 100


def test_scenario_plans_suppress_positive_path_when_current_price_is_missing() -> None:
    scenarios = _scenario_plans(
        _analysis(),
        _feature(price=math.nan, support=95, resistance=108, ma20=96),
        _factor_lab(total_score=90),
        _market_regime(risk_multiplier=0.8),
        _validation("条件较好"),
        0,
        0,
    )
    scenario_text = " ".join(
        " ".join([item.trigger, item.expected_move, item.response, item.invalidation]) for item in scenarios
    )

    assert scenarios[0].probability == 0
    assert sum(item.probability for item in scenarios) == 100
    assert "当前价待确认" in scenario_text
    assert "0.00" not in scenario_text


def test_scenario_plans_hide_positive_target_when_current_price_is_missing() -> None:
    scenarios = _scenario_plans(
        _analysis(),
        _feature(price=math.nan, support=95, resistance=108, ma20=96),
        _factor_lab(total_score=90),
        _market_regime(risk_multiplier=0.8),
        _validation("条件较好"),
        112,
        95,
    )
    scenario_text = " ".join(
        " ".join([item.trigger, item.expected_move, item.response, item.invalidation]) for item in scenarios
    )

    assert scenarios[0].probability == 0
    assert "先等上方目标确认" in scenario_text
    assert "112.00" not in scenario_text


def test_scenario_plans_reject_price_levels_on_the_wrong_side_of_current_price() -> None:
    complete = _scenario_plans(
        _analysis(),
        _feature(price=100, support=95, resistance=108, ma20=96),
        _factor_lab(total_score=90),
        _market_regime(risk_multiplier=0.8),
        _validation("条件较好"),
        112,
        95,
    )
    inverted = _scenario_plans(
        _analysis(),
        _feature(price=100, support=105, resistance=96, ma20=94),
        _factor_lab(total_score=90),
        _market_regime(risk_multiplier=0.8),
        _validation("条件较好"),
        98,
        102,
    )
    scenario_text = " ".join(
        " ".join([item.trigger, item.expected_move, item.response, item.invalidation]) for item in inverted
    )

    assert inverted[0].probability < complete[0].probability
    assert "压力位待确认" in scenario_text
    assert "支撑和压力位仍待确认" in scenario_text
    assert "防守位待确认" in scenario_text
    assert "96.00" not in scenario_text
    assert "98.00" not in scenario_text
    assert "102.00" not in scenario_text
    assert "105.00" not in scenario_text
    assert sum(item.probability for item in inverted) == 100


def test_scenario_plans_use_default_text_for_blank_action_and_validation_status() -> None:
    scenarios = _scenario_plans(
        _analysis(action="   "),
        _feature(price=100, support=95, resistance=108, ma20=96),
        _factor_lab(total_score=70),
        _market_regime(risk_multiplier=1.0),
        _validation(" "),
        112,
        95,
    )
    scenario_text = " ".join(
        " ".join([item.trigger, item.expected_move, item.response, item.invalidation]) for item in scenarios
    )

    assert "「等待确认」或更好" in scenario_text
    assert "维持「观察」口径" in scenario_text
    assert "「」" not in scenario_text


def test_scenario_plans_use_default_text_for_non_finite_action_and_validation_status() -> None:
    scenarios = _scenario_plans(
        _analysis(action=math.nan),
        _feature(price=100, support=95, resistance=108, ma20=96),
        _factor_lab(total_score=70),
        _market_regime(risk_multiplier=1.0),
        _validation(math.inf),
        112,
        95,
    )
    scenario_text = " ".join(
        " ".join([item.trigger, item.expected_move, item.response, item.invalidation]) for item in scenarios
    )

    assert "「等待确认」或更好" in scenario_text
    assert "维持「观察」口径" in scenario_text
    assert "nan" not in scenario_text.lower()
    assert "inf" not in scenario_text.lower()


def test_build_risk_reward_report_returns_finite_metrics_for_invalid_price() -> None:
    report = build_risk_reward_report(
        _analysis(),
        _feature(
            price=math.nan,
            support=math.inf,
            resistance=math.nan,
            ma20=math.inf,
            atr14=math.inf,
            atr_pct=math.nan,
            volatility_pct=math.inf,
        ),
        _factor_lab(total_score=70, positive_factor_count=3, negative_factor_count=1),
        _market_regime(risk_multiplier=math.inf, breadth_score=math.nan),
        _validation("条件较好"),
    )

    metric_fields = [
        "current_price",
        "upside_target",
        "downside_stop",
        "upside_pct",
        "downside_pct",
        "reward_risk_ratio",
        "atr14",
        "atr_pct",
        "volatility_pct",
    ]
    assert report.rating == "性价比不足"
    assert all(math.isfinite(getattr(report, field)) for field in metric_fields)
    assert all(getattr(report, field) == 0 for field in metric_fields)
    assert "上方预估空间待确认" in report.summary
    assert "下方防守距离待确认" in report.summary
    assert "收益风险比待确认" in report.summary
    assert "环境风险倍率待确认" in report.summary
    assert any("待确认项" in item for item in report.notes)
    assert report.scenarios[0].probability == 0
    assert all("0.00" not in item.trigger for item in report.scenarios)


def test_build_risk_reward_report_degrades_negative_current_price_to_waiting_plan() -> None:
    report = build_risk_reward_report(
        _analysis(),
        _feature(price=-12, support=10, resistance=14, ma20=11, atr14=1, atr_pct=2, volatility_pct=3),
        _factor_lab(total_score=80, positive_factor_count=4, negative_factor_count=0),
        _market_regime(risk_multiplier=0.8, breadth_score=70),
        _validation("条件较好"),
    )
    scenario_text = " ".join(
        " ".join([item.trigger, item.expected_move, item.response, item.invalidation]) for item in report.scenarios
    )

    assert report.current_price == 0
    assert report.upside_target == 0
    assert report.downside_stop == 0
    assert report.reward_risk_ratio == 0
    assert report.rating == "性价比不足"
    assert report.scenarios[0].probability == 0
    assert "当前价待确认" in scenario_text
    assert "收益风险比待确认" in report.summary


def test_build_risk_reward_report_sanitizes_non_finite_factor_market_and_validation_text() -> None:
    report = build_risk_reward_report(
        _analysis(action=math.inf),
        _feature(price=100, support=95, resistance=108, ma20=96, atr14=2, atr_pct=2, volatility_pct=3),
        _factor_lab(total_score=math.inf, positive_factor_count=math.inf, negative_factor_count=math.nan),
        _market_regime(risk_multiplier=math.nan, breadth_score=math.inf),
        _validation(math.inf),
    )
    scenario_text = " ".join(
        " ".join([item.trigger, item.expected_move, item.response, item.invalidation]) for item in report.scenarios
    )

    assert math.isfinite(report.reward_risk_ratio)
    assert "环境风险倍率待确认" in report.summary
    assert "「等待确认」或更好" in scenario_text
    assert "维持「观察」口径" in scenario_text
    assert "inf" not in report.summary.lower()
    assert "nan" not in report.summary.lower()
    assert "inf" not in scenario_text.lower()
    assert "nan" not in scenario_text.lower()


def test_build_risk_reward_report_caps_positive_path_when_rating_is_timeframe_conflict() -> None:
    report = build_risk_reward_report(
        _analysis(),
        _feature(price=100, support=95, resistance=108, ma20=96, atr14=1, atr_pct=1, volatility_pct=2),
        _factor_lab(total_score=90, positive_factor_count=4, negative_factor_count=0),
        _market_regime(risk_multiplier=0.8, breadth_score=70),
        _validation("条件较好"),
        _timeframe("高冲突"),
    )

    assert report.rating == "周期冲突"
    assert report.scenarios[0].probability <= 18
    assert sum(item.probability for item in report.scenarios) == 100


def test_build_risk_reward_report_handles_missing_numeric_fields() -> None:
    report = build_risk_reward_report(
        SimpleNamespace(),
        SimpleNamespace(symbol="000001", updated_at="2026-01-01T00:00:00", price=100),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    metric_fields = [
        "current_price",
        "upside_target",
        "downside_stop",
        "upside_pct",
        "downside_pct",
        "reward_risk_ratio",
        "atr14",
        "atr_pct",
        "volatility_pct",
    ]
    scenario_text = " ".join(
        " ".join([item.trigger, item.expected_move, item.response, item.invalidation]) for item in report.scenarios
    )

    assert report.current_price == 100
    assert all(math.isfinite(getattr(report, field)) for field in metric_fields)
    assert report.atr14 == 0
    assert report.atr_pct == 0
    assert report.volatility_pct == 0
    assert sum(item.probability for item in report.scenarios) == 100
    assert "环境风险倍率待确认" in report.summary
    assert "压力位待确认" in scenario_text
    assert "维持「观察」口径" in scenario_text


def _factor_lab(*, total_score: object = 50, positive_factor_count: object = 1, negative_factor_count: object = 1):
    return SimpleNamespace(
        total_score=total_score,
        positive_factor_count=positive_factor_count,
        negative_factor_count=negative_factor_count,
    )


def _market_regime(*, risk_multiplier: object = 1.0, breadth_score: object = 50):
    return SimpleNamespace(risk_multiplier=risk_multiplier, breadth_score=breadth_score)


def _feature(
    *,
    symbol: str = "000001",
    updated_at: str = "2026-01-01T00:00:00",
    price: float = 100,
    support: float = 95,
    resistance: float = 105,
    ma20: float = 96,
    atr14: float = 1,
    atr_pct: float = 1,
    volatility_pct: float = 2,
):
    return SimpleNamespace(
        symbol=symbol,
        updated_at=updated_at,
        price=price,
        support=support,
        resistance=resistance,
        ma20=ma20,
        atr14=atr14,
        atr_pct=atr_pct,
        volatility_pct=volatility_pct,
    )


def _validation(overall_status: object):
    return SimpleNamespace(overall_status=overall_status)


def _timeframe(conflict_level: str):
    return SimpleNamespace(conflict_level=conflict_level)


def _analysis(*, action: object = "观察"):
    return SimpleNamespace(action_advice=SimpleNamespace(action=action))
