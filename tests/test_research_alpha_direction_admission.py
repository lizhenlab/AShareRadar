"""Directional Alpha evidence requires an explicit supported source conclusion."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest

from app.models.analysis import AnalysisResult, FeatureSnapshot, StockInsightBundle
from app.models.research import AlphaEvidenceReport, FactorLabReport, RiskRewardReport
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_alpha import build_alpha_evidence_report
from app.services.research_alpha_points import collect_alpha_points, event_impact, risk_reward_points, rule_match_impact
from app.services.research_factors import build_factor_lab_report
from app.services.research_features import build_feature_snapshot
from app.services.research_regime import build_market_regime_report
from app.services.research_risk_reward_report import build_risk_reward_report
from app.services.research_timeframe import build_timeframe_alignment_report
from app.services.research_validation import build_signal_validation_report
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote, make_stock_info


@dataclass
class PublicResearch:
    analysis: AnalysisResult
    insights: StockInsightBundle
    feature: FeatureSnapshot
    factors: FactorLabReport
    reward: RiskRewardReport
    alpha: AlphaEvidenceReport


def _public_research(*, count=40, price=100.5, high=102, low=98, volume=1000, change_pct=-0.5):
    end = date(2026, 8, 11)
    days = sorted([end - timedelta(days=i) for i in range(100) if (end - timedelta(days=i)).weekday() < 5][:count])
    rows = [make_kline(
        close=100, high=101, low=99, date=str(day), source="synthetic-alpha-direction", as_of=f"{day} 15:15:00",
    ) for day in days]
    quote = make_quote(
        price=price, prev_close=101, high=high, low=low, change_pct=change_pct, turnover_rate=3,
        pe=40, pb=6, timestamp=f"{end} 15:30:00", source="synthetic-alpha-direction",
    ).model_copy(update={"open": 100.0, "volume": float(volume)})
    quality = build_data_quality(quote, rows, now=datetime(2026, 8, 11, 16))
    analysis = build_analysis(quote, rows, stock_profile=make_stock_info(), data_quality=quality)
    insights = build_stock_insight_bundle(analysis)
    feature = build_feature_snapshot(analysis, insights)
    factors = build_factor_lab_report(analysis, insights, feature)
    regime = build_market_regime_report(analysis, insights, feature, factors)
    timeframe = build_timeframe_alignment_report(analysis, feature, factors)
    validation = build_signal_validation_report(analysis, feature, factors, regime, timeframe)
    reward = build_risk_reward_report(analysis, feature, factors, regime, validation, timeframe)
    alpha = build_alpha_evidence_report(analysis, insights, feature, factors, regime, timeframe, reward)
    return PublicResearch(analysis, insights, feature, factors, reward, alpha)


@pytest.fixture(scope="module")
def near_risk_research():
    return _public_research()


@pytest.mark.parametrize("rule_id", ["break_ma20_risk", "fund_tech_divergence", "high_valuation_chase_risk"])
def test_real_close_risk_and_divergence_rules_do_not_become_positive_evidence(near_risk_research, rule_id):
    report = near_risk_research
    rule = next(item for item in report.insights.rule_matches.matches if item.rule_id == rule_id)
    assert rule.status == "接近" and rule.level == "观察"
    points = collect_alpha_points(report.analysis, report.insights)
    point = next(item for item in points if item.source == "规则引擎" and item.title == rule.name)
    assert point.impact == 0
    assert all(item.title != rule.name for item in report.alpha.positives)


def test_real_nondirectional_wide_amplitude_is_not_alpha_support():
    report = _public_research(count=0, price=100, high=104, low=96)
    event = next(item for item in report.insights.abnormal_events.events if item.title == "日内大振幅")
    assert event.level == "观察" and event.direction == "波动"
    point = next(item for item in collect_alpha_points(report.analysis, report.insights) if item.title == event.title)
    assert point.impact == 0
    assert all(item.title != event.title for item in report.alpha.positives)


def test_real_missing_risk_reward_levels_do_not_become_alpha_support():
    report = _public_research(count=0)
    assert report.reward.ratio_available is False
    assert report.reward.rating == "等待确认" and report.reward.availability_reason
    assert risk_reward_points(report.reward)[0].impact == 0
    assert all(item.source != "风险收益" for item in report.alpha.positives)


@pytest.mark.parametrize("level", ["观察", "谨慎", "风险", "中性", "未知", ""])
def test_close_rules_need_explicit_positive_direction(level):
    assert rule_match_impact("接近", level) == 0


@pytest.mark.parametrize("status", ["未触发", "未知", ""])
@pytest.mark.parametrize("level", ["积极", "风险"])
def test_unmatched_rules_have_no_directional_contribution(status, level):
    assert rule_match_impact(status, level) == 0


@pytest.mark.parametrize("status,level,expected", [("命中", "积极", 16), ("命中", "风险", -18), ("接近", "积极", 6)])
def test_explicit_rule_directions_keep_existing_magnitudes(status, level, expected):
    assert rule_match_impact(status, level) == expected


@pytest.mark.parametrize("level,expected", [("积极", 10), ("风险", -14), ("观察", 0), ("谨慎", 0), ("中性", 0), ("未知", 0), ("", 0)])
def test_events_only_inherit_explicit_direction(level, expected):
    assert event_impact(level) == expected


def _risk_reward(rating, *, available):
    return RiskRewardReport(
        symbol="600519.SH", updated_at="2026-08-11 15:30:00", current_price=100,
        upside_target=110 if available else 0, downside_stop=95 if available else 0,
        upside_pct=10 if available else 0, downside_pct=5 if available else 0,
        reward_risk_ratio=2 if available else 0, upside_available=available, downside_available=available,
        ratio_available=available, upside_target_basis="resistance" if available else "unavailable",
        downside_stop_basis="structure" if available else "unavailable",
        availability_reason=None if available else "缺少可复核价格边界", rating=rating, summary="合成风险收益报告",
    )


@pytest.mark.parametrize("rating,expected", [
    ("性价比较好", 10), ("性价比一般", 2), ("风险优先", -12), ("周期冲突", -12),
    ("性价比不足", -12), ("等待确认", 0), ("未知", 0), ("", 0),
])
@pytest.mark.parametrize("available", [True, False])
def test_risk_reward_support_requires_explicit_rating_and_evidenced_ratio(rating, expected, available):
    point = risk_reward_points(_risk_reward(rating, available=available))[0]
    assert point.impact == (0 if expected > 0 and not available else expected)


@pytest.mark.parametrize("price,change_pct,expected_event,event_impact_value,expected_rule,rule_impact", [
    (104, 3, "放量上涨", 10, "放量突破20日高点", 16),
    (96, -5, "放量下跌", -14, "跌破20日线风险", -18),
])
def test_real_confirmed_positive_and_negative_events_and_rules_remain_directional(
    price, change_pct, expected_event, event_impact_value, expected_rule, rule_impact,
):
    report = _public_research(price=price, high=max(price, 102), low=min(price, 98), volume=2000, change_pct=change_pct)
    points = collect_alpha_points(report.analysis, report.insights)
    assert next(item.impact for item in points if item.title == expected_event) == event_impact_value
    assert next(item.impact for item in points if item.title == expected_rule) == rule_impact
