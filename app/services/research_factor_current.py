from __future__ import annotations

from math import isclose

from app.models.analysis import (
    AnalysisResult,
    FeatureSnapshot,
    StockInsightBundle,
)
from app.models.market import Kline
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
)
from app.services.data_quality_components import PRICE_BOUNDARY_ABS_TOLERANCE, PRICE_BOUNDARY_REL_TOLERANCE
from app.services.research_factor_specs import _factor_specs
from app.services.research_factor_weights import _adjusted_factor_weight
from app.services.research_volume_scoring import volume_confirmation_inputs, volume_confirmation_score
from app.services.scoring import clamp_score as _clamp, score_level as _score_level
from app.services.completed_session import completed_quote_day, dated_rows_through_quote
from app.utils.market_data import finite_float


CURRENT_TREND_REPLAY_LIMITATION = "当前趋势包含实时换手率与当前报价时点，历史日K无法同口径还原；不生成历史校准、分桶或百分位。"


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
    factor = _build_factor(
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
        methodology=CURRENT_TREND_REPLAY_LIMITATION,
        participates_in_current_score=available,
    )
    if available and factor.calibration is not None:
        factor.calibration = factor.calibration.model_copy(update={
            "availability": "execution_evidence_unavailable",
            "unavailable_reason": CURRENT_TREND_REPLAY_LIMITATION,
            "note": CURRENT_TREND_REPLAY_LIMITATION,
        })
    return factor


def volume_confirmation_factor(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    specs: dict,
    adjustments: dict[str, float],
) -> StandardFactor:
    inputs = _current_volume_inputs(analysis, feature)
    volume_available = inputs is not None
    change, ratio = inputs if inputs is not None else (0.0, 0.0)
    return _build_factor(
        specs["volume_confirmation"],
        analysis,
        volume_confirmation_score(change, ratio) if volume_available else 50,
        (
            f"量能 {ratio:.2f}倍 / 涨跌幅 {change:.2f}%"
            if volume_available
            else "量能不可用 / 同时点收盘涨跌幅待确认"
        ),
        (
            [
                "上涨放量偏确认，下跌放量偏风险；缩量波动需要降低判断强度。",
                f"当前近5日量能约为20日均量 {ratio:.2f} 倍；使用同日收盘价计算方向。",
            ]
            if volume_available
            else ["缺少同日同价、无公司行动且会话状态明确的完整20日正成交量窗口；盘中、盘前或不可比量价不参与评分。"]
        ),
        [] if volume_available else ["连续20日正成交量及无公司行动的统一前复权价格", "同日同价且昨收一致的明确收盘报价"],
        adjustments,
        data_nature="observed" if volume_available else "unavailable",
        methodology="仅在official同日同价收盘报价下，以连续20个明确交易会话、无公司行动的统一前复权正量日K计算5/20量比；当前和历史共享准入，不是真实资金流。",
        participates_in_current_score=volume_available,
    )


def _current_volume_inputs(analysis: AnalysisResult, feature: FeatureSnapshot) -> tuple[float, float] | None:
    quote_day = completed_quote_day(analysis.quote.timestamp)
    if feature.volume_ratio_available is not True or analysis.research_mode != "official" or quote_day is None:
        return None
    rows = dated_rows_through_quote(analysis.klines, quote_day)
    if rows is None or len(rows) < 20 or rows[-1].date != quote_day.isoformat():
        return None
    window = rows[-20:]
    if not _volume_close_prices_match(analysis, window):
        return None
    return volume_confirmation_inputs(window)


def _volume_close_prices_match(analysis: AnalysisResult, rows: list[Kline]) -> bool:
    pairs = ((analysis.quote.price, rows[-1].close), (analysis.quote.prev_close, rows[-2].close))
    return all(
        (price := finite_float(quoted)) is not None and price > 0
        and isclose(price, close, rel_tol=PRICE_BOUNDARY_REL_TOLERANCE, abs_tol=PRICE_BOUNDARY_ABS_TOLERANCE)
        for quoted, close in pairs
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
    factor = _build_factor(
        specs["leadership_strength"],
        analysis,
        score,
        f"{level} / {score}分",
        evidence[:3],
        missing_data,
        adjustments,
    )
    return factor.model_copy(update={
        "aggregation_role": "composite", "participates_in_current_score": False,
        "weight": 0, "percentile": None, "calibration_buckets": [],
        "methodology": "龙头强度已合并趋势、量价及行业信息，仅作复合观察，不与底层因子重复计分。",
    })


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
        unavailable_reason=None if available else "缺少有限且非零的 PE 或 PB 估值证据",
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
        methodology="PE、PB 与同口径估值分位的规则锚；缺少有效 PE/PB 时不计分，市值仅作规模背景。",
    )


__all__ = ["build_current_factors", "valuation_anchor_factor"]
