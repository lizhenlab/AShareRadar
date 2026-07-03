from __future__ import annotations

from datetime import datetime

import pytest

from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_features import build_feature_snapshot
from app.services.research_factor_weights import _adjusted_factor_weight, _factor_weight_policy
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote


def test_factor_weight_policy_uses_default_profile_without_matching_style() -> None:
    analysis, feature = _factor_weight_inputs(data_quality_score=90)

    profile, adjustments, notes = _factor_weight_policy(analysis, feature)

    assert profile == "常规个股"
    assert adjustments == {}
    assert notes == ["使用默认单股分析权重。"]


def test_factor_weight_policy_prioritizes_large_stable_profile() -> None:
    analysis, feature = _factor_weight_inputs(
        market_cap=600_000_000_000,
        turnover_rate=9.5,
        volume_ratio=2.0,
        data_quality_score=90,
    )

    profile, adjustments, notes = _factor_weight_policy(analysis, feature)

    assert profile == "大市值稳健股"
    assert adjustments == {
        "valuation_anchor": 1.25,
        "risk_pressure": 1.12,
        "trend_momentum": 1.08,
        "leadership_strength": 0.9,
    }
    assert notes == ["大市值稳健股提高估值锚、风控和趋势修复权重，降低短线情绪权重。"]


def test_factor_weight_policy_applies_low_quality_overlay() -> None:
    analysis, feature = _factor_weight_inputs(
        turnover_rate=8.2,
        volume_ratio=1.0,
        data_quality_score=65,
    )

    profile, adjustments, notes = _factor_weight_policy(analysis, feature)

    assert profile == "高活跃波动股"
    assert adjustments["volume_confirmation"] == 1.25
    assert adjustments["valuation_anchor"] == 0.82
    assert adjustments["risk_pressure"] == pytest.approx(1.18 * 1.18)
    assert adjustments["fund_flow_proxy"] == pytest.approx(1.15 * 0.88)
    assert notes == [
        "高活跃波动股提高量价、资金和风险权重，降低静态估值权重。",
        "数据质量不足时提高风控权重，降低资金估算权重。",
    ]


def test_factor_weight_policy_detects_low_liquidity_profile() -> None:
    analysis, feature = _factor_weight_inputs(
        amount=250_000_000,
        turnover_rate=3.5,
        volume_ratio=1.0,
        data_quality_score=90,
    )

    profile, adjustments, notes = _factor_weight_policy(analysis, feature)

    assert profile == "低流动性个股"
    assert adjustments["risk_pressure"] == 1.28
    assert adjustments["volume_confirmation"] == 1.15
    assert adjustments["fund_flow_proxy"] == 0.86
    assert adjustments["leadership_strength"] == 0.88
    assert notes == ["低流动性个股提高风险和量价确认权重，降低资金估算与强弱标签权重。"]


def test_adjusted_factor_weight_clamps_final_weight() -> None:
    assert _adjusted_factor_weight("risk_pressure", 2.0, {"risk_pressure": 2.0}) == 1.8
    assert _adjusted_factor_weight("valuation_anchor", 0.2, {"valuation_anchor": 0.2}) == 0.5
    assert _adjusted_factor_weight("trend_momentum", 0.9, {}) == 0.9


def _factor_weight_inputs(
    *,
    amount: float = 1_300_000_000,
    market_cap: float | None = None,
    turnover_rate: float = 4.2,
    volume_ratio: float = 1.0,
    data_quality_score: int = 90,
):
    quote = make_quote(turnover_rate=turnover_rate, market_cap=market_cap).model_copy(update={"amount": amount})
    klines = [
        make_kline(
            date=f"2026-05-{index + 1:02d}",
            close=100 + index * 0.5,
            high=101 + index * 0.5,
            low=99 + index * 0.5,
            volume=1600 + index * 30,
        )
        for index in range(40)
    ]
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)).model_copy(
        update={"score": data_quality_score, "level": "优秀" if data_quality_score >= 80 else "一般"}
    )
    analysis = build_analysis(quote, klines, data_quality=quality)
    feature = build_feature_snapshot(analysis, build_stock_insight_bundle(analysis)).model_copy(
        update={
            "amount": amount,
            "turnover_rate": turnover_rate,
            "volume_ratio": volume_ratio,
            "data_quality_score": data_quality_score,
            "data_quality_level": quality.level,
        }
    )
    return analysis, feature
