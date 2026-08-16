from __future__ import annotations

from app.models.analysis import (
    AnalysisResult,
    FeatureSnapshot,
    StockInsightBundle,
)
from app.models.research import (
    ChipAnalysis,
    FactorCalibration,
    LeadershipReport,
    StandardFactor,
)
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
    available = feature.ma20_available and len(analysis.klines) >= 20
    return _build_factor(
        specs["trend_momentum"],
        analysis,
        feature.trend_score,
        f"{feature.trend_label} / {feature.trend_score}分" if available else "趋势结构证据不可用",
        (
            [
                f"现价 {feature.price:.2f}，5日线 {feature.ma5:.2f}，10日线 {feature.ma10:.2f}，20日线 {feature.ma20:.2f}。",
                f"趋势信号可靠度 {feature.signal_confidence}/100。",
            ]
            if available
            else ["有效日K不足20根，20日趋势结构不作为当前评分证据。"]
        ),
        [] if len(analysis.klines) >= 30 and available else ["至少20根有效日K及更长历史K线"],
        adjustments,
        data_nature="derived" if available else "unavailable",
        participates_in_current_score=available,
    )


def volume_confirmation_factor(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    specs: dict,
    adjustments: dict[str, float],
) -> StandardFactor:
    volume_available = feature.volume_ratio_available
    return _build_factor(
        specs["volume_confirmation"],
        analysis,
        _volume_confirmation_score(analysis, feature),
        (
            f"量能 {feature.volume_ratio:.2f}倍 / 涨跌幅 {feature.change_pct:.2f}%"
            if volume_available
            else f"量能不可用 / 涨跌幅 {feature.change_pct:.2f}%"
        ),
        (
            [
                "上涨放量偏确认，下跌放量偏风险；缩量波动需要降低判断强度。",
                f"当前近5日量能约为20日均量 {feature.volume_ratio:.2f} 倍。",
            ]
            if volume_available
            else ["近20个交易日的正成交量窗口不完整，量比不作为证据。"]
        ),
        [] if volume_available else ["完整且为正的20日成交量序列"],
        adjustments,
        data_nature="observed" if volume_available else "unavailable",
        methodology="仅在完整正成交量窗口下计算近5日/20日量比。",
        participates_in_current_score=volume_available,
    )


def risk_pressure_factor(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    specs: dict,
    adjustments: dict[str, float],
) -> StandardFactor:
    order_pressure_evidence = (
        f"盘口状态：{feature.order_pressure}。"
        if feature.order_pressure_data_nature != "unavailable"
        else "盘口证据不可用，不参与风险压力评分。"
    )
    return _build_factor(
        specs["risk_pressure"],
        analysis,
        _risk_pressure_score(analysis, insights, feature),
        f"{analysis.risk_level} / 数据质量 {feature.data_quality_level}",
        [
            f"数据质量 {feature.data_quality_score} 分；{order_pressure_evidence}",
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
    available = (
        feature.fund_flow_data_nature != "unavailable"
        and insights.fund_flow.data_nature != "unavailable"
    )
    score = feature.fund_flow_score if available else 50
    return _build_factor(
        specs["fund_flow_proxy"],
        analysis,
        score,
        f"量价热度评分（衍生） {score} / {insights.fund_flow.level}" if available else "量价热度证据不可用",
        (
            [
                insights.fund_flow.price_volume_relation,
                f"量价指标来源（衍生）：{insights.fund_flow.source}。",
            ]
            if available
            else ["特征快照或量价研究报告未提供可用的同口径证据。"]
        ),
        insights.fund_flow.notes[:1] if available and not insights.fund_flow.available else ["同口径量价热度证据"],
        adjustments,
        data_nature="derived" if available else "unavailable",
        methodology="量价规则衍生指标，不是真实资金流或主力净流入。",
        participates_in_current_score=available,
    )


def chip_position_factor(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    chip: ChipAnalysis | None,
    specs: dict,
    adjustments: dict[str, float],
) -> StandardFactor:
    chip_available = bool(chip and chip.distribution_available is True and chip.center_price > 0)
    structural_levels_available = feature.support_available or feature.resistance_available
    factor_available = chip_available or structural_levels_available
    return _build_factor(
        specs["chip_position"],
        analysis,
        _chip_position_score_current(feature, chip),
        _chip_position_value(feature, chip),
        _chip_position_evidence(feature, chip),
        (
            []
            if chip_available
            else ["可验证的筹码分布或至少一个结构价位"]
        ),
        adjustments,
        data_nature="derived" if factor_available else "unavailable",
        participates_in_current_score=factor_available,
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
    available = (
        feature.valuation_score_available
        and feature.valuation_data_nature == "derived"
        and insights.valuation.score_available
        and insights.valuation.data_nature == "derived"
    )
    score = _clamp(feature.valuation_score) if available else 50
    calibration = FactorCalibration(
        sample_count=0,
        win_rate=0,
        avg_forward_5d_return=0,
        avg_forward_10d_return=0,
        max_adverse_return=0,
        stability_score=0,
        expected_level="待补数据",
        confidence_level="待补数据" if available else "数据不可用",
        participates_in_historical_aggregate=False,
        availability="available" if available else "execution_evidence_unavailable",
        unavailable_reason=None if available else "缺少可验证的 PE、PB、市值或估值分位证据",
        note=(
            "当前只用最新可验证估值字段做安全边际观察；本项参与当前评分，不参与历史校准样本汇总。"
            if available
            else "估值字段不足，本项不参与当前综合评分或历史证据聚合。"
        ),
    )
    return StandardFactor(
        id="valuation_anchor",
        name="估值锚",
        category="基本面",
        value=(
            f"估值评分 {score} / {insights.valuation.level}"
            if available
            else "估值证据不可用"
        ),
        score=score,
        level=_score_level(score),
        direction=_factor_direction(score),
        percentile=None,
        weight=_adjusted_factor_weight("valuation_anchor", 0.8, adjustments),
        participates_in_current_score=available,
        evidence=(insights.valuation.evidence[:3] if available else ["特征快照或估值报告未提供可用的同口径证据。"]),
        missing_data=_dedupe(["历史PE/PB序列", *insights.valuation.missing_data])[:6],
        calibration=calibration,
        data_nature="derived" if available else "unavailable",
        methodology="PE、PB、市值与估值分位的规则锚；缺少这些证据时不计分。",
    )


__all__ = ["build_current_factors", "valuation_anchor_factor"]
