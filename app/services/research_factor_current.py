from __future__ import annotations

from app.models.schemas import AnalysisResult, ChipAnalysis, FactorCalibration, FeatureSnapshot, LeadershipReport, StandardFactor, StockInsightBundle
from app.services.research_factor_scoring import (
    _build_factor,
    _chip_position_evidence,
    _chip_position_score_current,
    _chip_position_value,
    _dedupe,
    _factor_direction,
    _risk_pressure_score,
    _volume_confirmation_score,
)
from app.services.research_factor_specs import _factor_specs
from app.services.research_factor_weights import _adjusted_factor_weight
from app.services.scoring import clamp_score as _clamp, score_level as _score_level


def build_current_factors(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    chip: ChipAnalysis | None = None,
    leadership: LeadershipReport | None = None,
    weight_adjustments: dict[str, float] | None = None,
) -> list[StandardFactor]:
    adjustments = weight_adjustments or {}
    specs = _factor_specs()
    return [
        trend_momentum_factor(analysis, feature, specs, adjustments),
        volume_confirmation_factor(analysis, feature, specs, adjustments),
        risk_pressure_factor(analysis, insights, feature, specs, adjustments),
        fund_flow_proxy_factor(analysis, insights, feature, specs, adjustments),
        chip_position_factor(analysis, feature, chip, specs, adjustments),
        leadership_strength_factor(analysis, feature, leadership, specs, adjustments),
        valuation_anchor_factor(feature, insights, adjustments),
    ]


def trend_momentum_factor(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    specs: dict,
    adjustments: dict[str, float],
) -> StandardFactor:
    return _build_factor(
        specs["trend_momentum"],
        analysis,
        feature.trend_score,
        f"{feature.trend_label} / {feature.trend_score}分",
        [
            f"现价 {feature.price:.2f}，5日线 {feature.ma5:.2f}，10日线 {feature.ma10:.2f}，20日线 {feature.ma20:.2f}。",
            f"趋势信号可靠度 {feature.signal_confidence}/100。",
        ],
        [] if len(analysis.klines) >= 30 else ["更长历史K线"],
        adjustments,
    )


def volume_confirmation_factor(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    specs: dict,
    adjustments: dict[str, float],
) -> StandardFactor:
    return _build_factor(
        specs["volume_confirmation"],
        analysis,
        _volume_confirmation_score(analysis, feature),
        f"量能 {feature.volume_ratio:.2f}倍 / 涨跌幅 {feature.change_pct:.2f}%",
        [
            "上涨放量偏确认，下跌放量偏风险；缩量波动需要降低判断强度。",
            f"当前近5日量能约为20日均量 {feature.volume_ratio:.2f} 倍。",
        ],
        [] if len(analysis.klines) >= 25 else ["更稳定的成交量序列"],
        adjustments,
    )


def risk_pressure_factor(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    specs: dict,
    adjustments: dict[str, float],
) -> StandardFactor:
    return _build_factor(
        specs["risk_pressure"],
        analysis,
        _risk_pressure_score(analysis, insights, feature),
        f"{analysis.risk_level} / 数据质量 {feature.data_quality_level}",
        [
            f"数据质量 {feature.data_quality_score} 分，盘口状态：{feature.order_pressure}。",
            f"异动状态：{insights.abnormal_events.main_signal}。",
        ],
        analysis.data_quality.anomalies[:3],
        adjustments,
    )


def fund_flow_proxy_factor(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    specs: dict,
    adjustments: dict[str, float],
) -> StandardFactor:
    return _build_factor(
        specs["fund_flow_proxy"],
        analysis,
        feature.fund_flow_score,
        f"量价热度评分（衍生） {feature.fund_flow_score} / {insights.fund_flow.level}",
        [
            insights.fund_flow.price_volume_relation,
            f"量价指标来源（衍生）：{insights.fund_flow.source}。",
        ],
        insights.fund_flow.notes[:1] if not insights.fund_flow.available else [],
        adjustments,
        data_nature="derived" if insights.fund_flow.data_nature != "unavailable" else "unavailable",
        methodology="量价规则衍生指标，不是真实资金流或主力净流入。",
    )


def chip_position_factor(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    chip: ChipAnalysis | None,
    specs: dict,
    adjustments: dict[str, float],
) -> StandardFactor:
    return _build_factor(
        specs["chip_position"],
        analysis,
        _chip_position_score_current(feature, chip),
        _chip_position_value(feature, chip),
        _chip_position_evidence(feature, chip),
        [] if chip and chip.concentration > 0 else ["更精细的成交分布或逐笔成交"],
        adjustments,
    )


def leadership_strength_factor(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    leadership: LeadershipReport | None,
    specs: dict,
    adjustments: dict[str, float],
) -> StandardFactor:
    score = leadership.score if leadership else feature.leader_score
    level = leadership.level if leadership else feature.leader_level
    evidence = leadership.evidence if leadership else [f"龙头强度 {feature.leader_score} 分。"]
    missing_data = leadership.missing_data if leadership else []
    return _build_factor(
        specs["leadership_strength"],
        analysis,
        score,
        f"{level} / {score}分",
        evidence[:3],
        missing_data,
        adjustments,
    )


def valuation_anchor_factor(
    feature: FeatureSnapshot,
    insights: StockInsightBundle,
    weight_adjustments: dict[str, float] | None = None,
) -> StandardFactor:
    adjustments = weight_adjustments or {}
    return StandardFactor(
        id="valuation_anchor",
        name="估值锚",
        category="基本面",
        value=f"估值评分 {feature.valuation_score} / {insights.valuation.level}",
        score=_clamp(feature.valuation_score),
        level=_score_level(feature.valuation_score),
        direction=_factor_direction(feature.valuation_score),
        percentile=None,
        weight=_adjusted_factor_weight("valuation_anchor", 0.8, adjustments),
        evidence=insights.valuation.evidence[:3],
        missing_data=_dedupe(["历史PE/PB序列", *insights.valuation.missing_data])[:6],
        calibration=FactorCalibration(
            sample_count=0,
            win_rate=0,
            avg_forward_5d_return=0,
            avg_forward_10d_return=0,
            max_adverse_return=0,
            stability_score=0,
            expected_level="待补数据",
            confidence_level="待补数据",
            participates_in_historical_aggregate=False,
            note=(
                "当前没有历史估值序列，只用最新行情估值字段做安全边际观察；"
                "本项参与当前评分，不参与历史校准样本汇总。"
            ),
        ),
    )


__all__ = ["build_current_factors", "valuation_anchor_factor"]
