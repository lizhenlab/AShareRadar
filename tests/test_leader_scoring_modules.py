from __future__ import annotations

from app.services.leader_scoring import (
    FEATURE_LEADER_PROFILE,
    FEATURE_TAG_RULES,
    STRONG_STOCK_LEADER_PROFILE,
    STRONG_STOCK_TAG_RULES,
    LeaderScoreInput,
    leader_score,
    leader_tags,
)


def test_feature_leader_profile_preserves_full_context_score() -> None:
    inputs = LeaderScoreInput(
        trend_score=70,
        change_pct=5.0,
        volume_ratio=1.5,
        amount=1_000_000_000,
        turnover_rate=8.0,
        fund_flow_score=65,
        industry_change_pct=1.2,
        abnormal_level="正常",
        data_quality_score=85,
    )

    assert leader_score(inputs, FEATURE_LEADER_PROFILE) == 90


def test_feature_leader_profile_applies_risk_and_quality_penalties() -> None:
    inputs = LeaderScoreInput(
        trend_score=45,
        change_pct=-3.5,
        volume_ratio=1.8,
        amount=100_000_000,
        fund_flow_score=40,
        industry_change_pct=-0.5,
        abnormal_level="风险",
        data_quality_score=62,
    )

    assert leader_score(inputs, FEATURE_LEADER_PROFILE) == 0


def test_strong_stock_profile_preserves_ranking_score_components() -> None:
    inputs = LeaderScoreInput(
        trend_score=80,
        change_pct=5.2,
        volume_ratio=1.6,
        amount=1_000_000_000,
        turnover_rate=6.0,
    )

    assert leader_score(inputs, STRONG_STOCK_LEADER_PROFILE) == 84


def test_leader_tags_keep_context_specific_thresholds() -> None:
    feature_inputs = LeaderScoreInput(
        trend_score=72,
        change_pct=5.1,
        volume_ratio=1.5,
        amount=1_000_000_000,
        turnover_rate=8.0,
        fund_flow_score=70,
        abnormal_level="风险",
        data_quality_score=65,
    )
    strong_inputs = LeaderScoreInput(
        trend_score=74,
        change_pct=5.1,
        volume_ratio=1.4,
        amount=1_000_000_000,
        turnover_rate=6.0,
    )

    assert leader_tags(feature_inputs, 72, FEATURE_TAG_RULES, "常规观察") == [
        "龙头候选",
        "趋势强",
        "情绪强",
        "量能放大",
        "换手活跃",
        "资金配合",
        "风险异动",
        "数据降权",
    ]
    assert leader_tags(strong_inputs, 68, STRONG_STOCK_TAG_RULES, "观察") == ["涨幅强", "量能放大", "换手活跃"]
