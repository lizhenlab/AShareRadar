from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from app.models.schemas import (
    AlphaEvidencePoint,
    AlphaEvidenceReport,
    AnalysisResult,
    ChipAnalysis,
    ChipBand,
    CalibrationBucket,
    FactorCalibration,
    FactorLabReport,
    FeatureSnapshot,
    LeadershipReport,
    EventDigestReport,
    EvidenceChainReport,
    MarketRegimeReport,
    PeerComparisonReport,
    ReplayCase,
    ReplayPatternStat,
    RiskRewardReport,
    RiskRadarItem,
    RiskRadarReport,
    ScenarioPlan,
    SignalValidationItem,
    SignalValidationReport,
    StandardFactor,
    StockConceptItem,
    StockDiagnosis,
    StockInsightBundle,
    StockQuestionAnswer,
    StockQaItem,
    StockQaReport,
    StockReplayAnalysis,
    ThemeContextReport,
    TStrategyAssistantReport,
    TimeframeAlignmentReport,
    TimeframeTrend,
)
from app.services.indicators import average_true_range, daily_return_volatility, max_drawdown, moving_average, pct_change, recent_volume_ratio
from app.utils.time import now_text


@dataclass(frozen=True)
class MarketBreadthSnapshot:
    label: str
    score: int
    up_count: int
    down_count: int
    strong_count: int
    weak_count: int
    avg_change_pct: float
    risk_adjustment: float
    summary: str


@dataclass(frozen=True)
class FactorSpec:
    id: str
    name: str
    category: str
    weight: float
    direction: str
    evaluator: Callable[[list, int], float]
    trigger: Callable[[list, int, float], bool]


FACTOR_SPECS: dict[str, FactorSpec] = {}


def build_feature_snapshot(analysis: AnalysisResult, insights: StockInsightBundle) -> FeatureSnapshot:
    quote = analysis.quote
    volume_ratio = recent_volume_ratio(analysis.klines)
    atr14 = average_true_range(analysis.klines, 14)
    atr_pct = atr14 / quote.price * 100 if quote.price else 0
    volatility_pct = daily_return_volatility(analysis.klines, 20)
    valuation_score = insights.valuation.score
    financial_score = insights.financial_health.score
    fund_flow_score = insights.fund_flow.overall_score
    leader_score = _leader_score(analysis, insights, volume_ratio)
    tags = _feature_tags(analysis, insights, volume_ratio, leader_score)
    notes = [
        f"信号可信度 {analysis.signal_snapshot.confidence}%，数据质量 {analysis.data_quality.level} {analysis.data_quality.score} 分。",
        f"趋势 {analysis.trend_label}，资金面 {insights.fund_flow.level}，估值 {insights.valuation.level}。",
    ]
    if insights.lhb.missing_data:
        notes.append("龙虎榜席位、公告和逐笔资金仍是后续精确化重点。")
    return FeatureSnapshot(
        symbol=f"{quote.code}.{quote.market}",
        updated_at=quote.timestamp,
        price=quote.price,
        change_pct=quote.change_pct,
        trend_score=analysis.trend_score,
        trend_label=analysis.trend_label,
        signal_confidence=analysis.signal_snapshot.confidence,
        data_quality_score=analysis.data_quality.score,
        data_quality_level=analysis.data_quality.level,
        leader_score=leader_score,
        leader_level=_score_level(leader_score),
        support=analysis.support,
        resistance=analysis.resistance,
        ma5=analysis.ma5,
        ma10=analysis.ma10,
        ma20=analysis.ma20,
        volume_ratio=volume_ratio,
        atr14=round(atr14, 2),
        atr_pct=round(atr_pct, 2),
        volatility_pct=round(volatility_pct, 2),
        turnover_rate=quote.turnover_rate,
        amount=quote.amount,
        valuation_score=valuation_score,
        financial_score=financial_score,
        fund_flow_score=fund_flow_score,
        order_pressure=insights.order_pressure.pressure_level,
        industry_name=analysis.industry_context.name if analysis.industry_context else None,
        industry_change_pct=analysis.industry_context.change_pct if analysis.industry_context else None,
        tags=tags,
        notes=notes,
    )


def build_factor_lab_report(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    chip: ChipAnalysis | None = None,
    leadership: LeadershipReport | None = None,
) -> FactorLabReport:
    specs = _factor_specs()
    profile_label, weight_adjustments, weight_policy = _factor_weight_policy(analysis, feature)
    factors = [
        _build_factor(
            specs["trend_momentum"],
            analysis,
            feature.trend_score,
            f"{feature.trend_label} / {feature.trend_score}分",
            [
                f"现价 {feature.price:.2f}，5日线 {feature.ma5:.2f}，10日线 {feature.ma10:.2f}，20日线 {feature.ma20:.2f}。",
                f"趋势信号置信度 {feature.signal_confidence}%。",
            ],
            [] if len(analysis.klines) >= 30 else ["更长历史K线"],
            weight_adjustments,
        ),
        _build_factor(
            specs["volume_confirmation"],
            analysis,
            _volume_confirmation_score(analysis, feature),
            f"量能 {feature.volume_ratio:.2f}倍 / 涨跌幅 {feature.change_pct:.2f}%",
            [
                "上涨放量偏确认，下跌放量偏风险；缩量波动需要降低判断强度。",
                f"当前近5日量能约为20日均量 {feature.volume_ratio:.2f} 倍。",
            ],
            [] if len(analysis.klines) >= 25 else ["更稳定的成交量序列"],
            weight_adjustments,
        ),
        _build_factor(
            specs["risk_pressure"],
            analysis,
            _risk_pressure_score(analysis, insights, feature),
            f"{analysis.risk_level} / 数据质量 {feature.data_quality_level}",
            [
                f"数据质量 {feature.data_quality_score} 分，盘口状态：{feature.order_pressure}。",
                f"异动状态：{insights.abnormal_events.main_signal}。",
            ],
            analysis.data_quality.anomalies[:3],
            weight_adjustments,
        ),
        _build_factor(
            specs["fund_flow_proxy"],
            analysis,
            feature.fund_flow_score,
            f"资金评分 {feature.fund_flow_score} / {insights.fund_flow.level}",
            [
                insights.fund_flow.price_volume_relation,
                f"资金源：{insights.fund_flow.source}。",
            ],
            insights.fund_flow.notes[:1] if not insights.fund_flow.available else [],
            weight_adjustments,
        ),
        _build_factor(
            specs["chip_position"],
            analysis,
            _chip_position_score_current(feature, chip),
            _chip_position_value(feature, chip),
            _chip_position_evidence(feature, chip),
            [] if chip and chip.concentration > 0 else ["更精细的成交分布或逐笔成交"],
            weight_adjustments,
        ),
        _build_factor(
            specs["leadership_strength"],
            analysis,
            leadership.score if leadership else feature.leader_score,
            f"{leadership.level if leadership else feature.leader_level} / {leadership.score if leadership else feature.leader_score}分",
            (leadership.evidence if leadership else [f"龙头强度 {feature.leader_score} 分。"])[:3],
            leadership.missing_data if leadership else [],
            weight_adjustments,
        ),
        StandardFactor(
            id="valuation_anchor",
            name="估值锚",
            category="基本面",
            value=f"估值评分 {feature.valuation_score} / {insights.valuation.level}",
            score=_clamp(feature.valuation_score),
            level=_score_level(feature.valuation_score),
            direction=_factor_direction(feature.valuation_score),
            percentile=None,
            weight=_adjusted_factor_weight("valuation_anchor", 0.8, weight_adjustments),
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
                note="当前没有历史估值序列，只用最新行情估值字段做安全边际观察。",
            ),
        ),
    ]
    total_score = _weighted_factor_score(factors)
    calibration_sample_count = sum((item.calibration.sample_count if item.calibration else 0) for item in factors)
    support_count = sum(
        1
        for item in factors
        if item.score >= 60
        and item.calibration
        and item.calibration.sample_count >= 5
        and item.calibration.expected_level in {"偏正", "较强"}
    )
    risk_count = sum(
        1
        for item in factors
        if (
            item.score <= 45
            or (item.calibration and item.calibration.sample_count >= 5 and item.calibration.expected_level in {"偏弱", "风险"})
        )
    )
    calibration_quality = _factor_calibration_quality(factors)
    calibrated_confidence = _clamp(
        round(
            total_score * 0.45
            + feature.signal_confidence * 0.2
            + feature.data_quality_score * 0.22
            + calibration_quality * 0.13
            + support_count * 3
            - risk_count * 4
        )
    )
    scored_factors = sorted(factors, key=_factor_score_impact, reverse=True)
    positives = [item.name for item in scored_factors if _factor_score_impact(item) > 0 and item.score >= 52][:4]
    negatives = [item.name for item in sorted(factors, key=_factor_score_impact) if _factor_score_impact(item) < 0 and item.score <= 55][:4]
    summary = _factor_lab_summary(total_score, calibrated_confidence, positives, negatives)
    return FactorLabReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        total_score=total_score,
        calibrated_confidence=calibrated_confidence,
        calibration_sample_count=calibration_sample_count,
        positive_factor_count=len(positives),
        negative_factor_count=len(negatives),
        profile_label=profile_label,
        weight_policy=weight_policy,
        factors=factors,
        top_positive=positives,
        top_negative=negatives,
        summary=summary,
        notes=[
            "因子实验室只校验单只股票自身的历史相似状态，不做组合选股或自动交易。",
            "历史校准使用日K向后5日/10日表现，样本少时只作为低置信参考。",
            f"当前画像为「{profile_label}」，因子权重已按画像动态调整。",
            f"当前共有 {calibration_sample_count} 个历史样本被用于因子校准。",
            *([f"数据质量为{feature.data_quality_level}，所有因子已按低置信口径解释。"] if feature.data_quality_score < 70 else []),
        ],
    )


def build_market_regime_report(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport | None = None,
    breadth: MarketBreadthSnapshot | None = None,
) -> MarketRegimeReport:
    breadth = breadth or build_market_breadth_snapshot([])
    industry_label = _industry_regime_label(feature)
    stock_state = _stock_state_label(analysis, insights, feature, factor_lab)
    market_label = _market_regime_label(analysis, feature, industry_label, stock_state, factor_lab, breadth)
    risk_multiplier = _regime_risk_multiplier(analysis, insights, feature, industry_label, factor_lab, breadth)
    confidence_adjustment = _bounded_int(round((1 - risk_multiplier) * 45), -20, 12)
    suggestions = _regime_suggestions(analysis, feature, industry_label, stock_state, factor_lab, breadth)
    evidence = [
        f"数据质量 {feature.data_quality_level} {feature.data_quality_score} 分。",
        f"个股趋势 {feature.trend_label} {feature.trend_score} 分，资金 {feature.fund_flow_score} 分。",
        breadth.summary,
        f"因子总分 {factor_lab.total_score}，校准置信度 {factor_lab.calibrated_confidence}%。" if factor_lab else "因子实验室暂未参与环境判断。",
    ]
    if feature.industry_name and feature.industry_change_pct is not None:
        evidence.append(f"行业 {feature.industry_name} 涨跌幅 {feature.industry_change_pct:.2f}%。")
    if insights.abnormal_events.events:
        evidence.append(f"异动：{insights.abnormal_events.main_signal} / {insights.abnormal_events.level}。")
    return MarketRegimeReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        market_label=market_label,
        breadth_label=breadth.label,
        breadth_score=breadth.score,
        industry_label=industry_label,
        stock_state=stock_state,
        risk_multiplier=risk_multiplier,
        confidence_adjustment=confidence_adjustment,
        suggestions=suggestions,
        evidence=evidence[:6],
    )


def build_alpha_evidence_report(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
    risk_reward: RiskRewardReport | None = None,
) -> AlphaEvidenceReport:
    points: list[AlphaEvidencePoint] = []
    for item in analysis.signal_snapshot.contributions:
        points.append(
            AlphaEvidencePoint(
                source=f"趋势/{item.category}",
                title=item.name,
                impact=item.impact,
                level=item.level,
                reason=item.reason,
            )
        )
    for factor in insights.overview.factors:
        impact = round((factor.score - 50) / 2)
        points.append(
            AlphaEvidencePoint(
                source="五维诊断",
                title=factor.name,
                impact=impact,
                level=factor.level,
                reason=factor.summary,
            )
        )
    if insights.rule_matches.matches:
        for match in insights.rule_matches.matches[:4]:
            impact = 16 if match.status == "命中" and match.level == "积极" else -18 if match.status == "命中" and match.level == "风险" else 6 if match.status == "接近" else 0
            points.append(
                AlphaEvidencePoint(
                    source="规则引擎",
                    title=match.name,
                    impact=impact,
                    level=match.level,
                    reason=match.reason,
                )
            )
    if insights.abnormal_events.events:
        for event in insights.abnormal_events.events[:3]:
            impact = -14 if event.level == "风险" else 10 if event.level == "积极" else 3
            points.append(
                AlphaEvidencePoint(
                    source="异动识别",
                    title=event.title,
                    impact=impact,
                    level=event.level,
                    reason=event.description,
                )
            )
    if factor_lab:
        for factor in factor_lab.factors:
            if factor.score >= 62 or factor.score <= 45:
                calibrated_boost = 0
                if factor.calibration and factor.calibration.sample_count >= 5:
                    calibrated_boost = _factor_calibration_impact(factor.calibration)
                points.append(
                    AlphaEvidencePoint(
                        source="因子实验室",
                        title=factor.name,
                        impact=_bounded_int(round(_factor_score_impact(factor) + calibrated_boost), -18, 18),
                        level=factor.level,
                        reason=_factor_alpha_reason(factor),
                    )
                )
    if market_regime:
        points.append(
            AlphaEvidencePoint(
                source="市场环境",
                title=market_regime.stock_state,
                impact=market_regime.confidence_adjustment,
                level="积极" if market_regime.confidence_adjustment > 3 else "风险" if market_regime.confidence_adjustment < -3 else "观察",
                reason=f"{market_regime.market_label}，{market_regime.industry_label}，风险倍率 {market_regime.risk_multiplier:.2f}。",
            )
        )
    if timeframe:
        timeframe_impact = 12 if timeframe.conflict_level == "多周期顺向" and timeframe.alignment_score >= 65 else -14 if timeframe.conflict_level in {"高冲突", "中冲突", "多周期偏弱"} or timeframe.alignment_label == "多周期偏弱" else 0
        points.append(
            AlphaEvidencePoint(
                source="多周期",
                title=timeframe.alignment_label,
                impact=timeframe_impact,
                level="积极" if timeframe_impact > 0 else "风险" if timeframe_impact < 0 else "观察",
                reason=timeframe.summary,
            )
        )
    if risk_reward:
        rr_impact = 10 if risk_reward.rating == "性价比较好" else -12 if risk_reward.rating in {"风险优先", "周期冲突", "性价比不足"} else 2
        points.append(
            AlphaEvidencePoint(
                source="风险收益",
                title=risk_reward.rating,
                impact=rr_impact,
                level="积极" if rr_impact > 4 else "风险" if rr_impact < 0 else "观察",
                reason=risk_reward.summary,
            )
        )
    positives = sorted([item for item in points if item.impact > 0], key=lambda item: item.impact, reverse=True)[:6]
    negatives = sorted([item for item in points if item.impact < 0], key=lambda item: item.impact)[:6]
    missing_data = _dedupe(
        [
            *insights.valuation.missing_data,
            *insights.financial_health.missing_data,
            *insights.lhb.missing_data,
            *(item for match in insights.rule_matches.matches for item in match.missing_data),
            *(_factor_missing_data(factor_lab) if factor_lab else []),
        ]
    )[:10]
    if factor_lab:
        raw_confidence = round(
            analysis.signal_snapshot.confidence * 0.3
            + analysis.data_quality.score * 0.2
            + feature.leader_score * 0.1
            + insights.overview.total_score * 0.14
            + factor_lab.calibrated_confidence * 0.26
        )
    else:
        raw_confidence = round(
            analysis.signal_snapshot.confidence * 0.45
            + analysis.data_quality.score * 0.25
            + feature.leader_score * 0.15
            + insights.overview.total_score * 0.15
        )
    if market_regime:
        raw_confidence += market_regime.confidence_adjustment
    if timeframe and timeframe.conflict_level in {"高冲突", "中冲突", "多周期偏弱"}:
        raw_confidence -= 8
    elif timeframe and timeframe.conflict_level == "多周期顺向":
        raw_confidence += 4
    if risk_reward and risk_reward.rating in {"风险优先", "周期冲突", "性价比不足"}:
        raw_confidence -= 6
    if missing_data:
        raw_confidence -= min(12, len(missing_data) * 2)
    confidence = _clamp(raw_confidence)
    verdict = _alpha_verdict(feature, positives, negatives, market_regime, timeframe, risk_reward)
    return AlphaEvidenceReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        confidence=confidence,
        verdict=verdict,
        summary=_alpha_summary(feature, positives, negatives, missing_data, confidence),
        positives=positives,
        negatives=negatives,
        missing_data=missing_data,
        data_quality_notes=analysis.data_quality.notes[:6],
    )


def build_stock_diagnosis(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    alpha: AlphaEvidenceReport,
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    validation: SignalValidationReport | None = None,
    risk_reward: RiskRewardReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> StockDiagnosis:
    confirmation_signals = [
        f"收盘站稳5日线 {feature.ma5:.2f}，且量能不低于近20日均量的 1.1 倍。",
        f"放量突破压力位 {feature.resistance:.2f} 后，回踩不跌回压力位下方。",
        f"资金面评分维持在 60 分以上，盘口不再显示明显卖压。",
    ]
    if feature.trend_score < 55:
        confirmation_signals.insert(0, f"趋势评分重新回到 55 分以上，目前为 {feature.trend_score}。")
    if factor_lab and factor_lab.top_positive:
        confirmation_signals.append(_factor_confirmation_text(factor_lab))
    if timeframe and timeframe.conflict_level == "多周期顺向":
        confirmation_signals.append(f"多周期目前为「{timeframe.alignment_label}」，可把顺周期信号当作辅助确认。")
    hard_risks = [
        f"有效跌破支撑位 {feature.support:.2f}。",
        f"收盘跌破20日线 {feature.ma20:.2f} 且次日不能快速修复。",
        "数据质量降到“一般”以下，所有买卖点和做T计划必须降级。",
    ]
    if insights.abnormal_events.level == "风险":
        hard_risks.insert(0, f"异动风险未解除：{insights.abnormal_events.main_signal}。")
    if market_regime and market_regime.risk_multiplier >= 1.18:
        hard_risks.insert(0, f"环境风险抬升：{market_regime.market_label}，需降低信号置信。")
    if timeframe and timeframe.conflict_level in {"高冲突", "中冲突", "多周期偏弱"}:
        hard_risks.insert(0, f"多周期存在「{timeframe.conflict_level}」，短线信号需要等待弱周期修复。")
    if risk_reward and risk_reward.rating in {"风险优先", "周期冲突", "性价比不足"}:
        hard_risks.insert(0, f"当前风险收益结论为「{risk_reward.rating}」，不宜把局部反弹当成明确机会。")
    if factor_lab and factor_lab.top_negative:
        hard_risks.append(_factor_risk_text(factor_lab))
    watch_focus = [
        "先看关键价位，再看量能和资金是否确认。",
        "只把策略卡当成条件清单，不把单一信号当成确定结论。",
        "做T只适用于已有可卖底仓，新增买入不参与当日T。",
    ]
    if feature.industry_name:
        watch_focus.append(f"同步观察行业「{feature.industry_name}」是否继续配合。")
    if market_regime:
        watch_focus.extend(market_regime.suggestions[:2])
    if validation:
        watch_focus.append(f"验证闭环当前为「{validation.overall_status}」，先按触发-确认-失效顺序执行。")
    if timeframe:
        watch_focus.extend(timeframe.suggestions[:1])
    if factor_lab and factor_lab.calibration_sample_count:
        watch_focus.append(f"因子实验室本轮样本数 {factor_lab.calibration_sample_count}，样本少的因子只做辅助。")
    final_action = _final_diagnosis_action(analysis, alpha, validation, risk_reward, timeframe, market_regime)
    headline = _diagnosis_headline(analysis, feature, alpha, factor_lab, market_regime, validation, risk_reward, timeframe)
    professional_summary = (
        f"特征快照显示：趋势 {feature.trend_score} 分、资金 {feature.fund_flow_score} 分、"
        f"估值 {feature.valuation_score} 分、龙头强度 {feature.leader_score} 分。"
        f"Alpha证据结论为「{alpha.verdict}」，置信度 {alpha.confidence}%。"
        f"{_diagnosis_factor_regime_text(factor_lab, market_regime)}"
        f"{_diagnosis_extra_text(validation, risk_reward, timeframe)}"
        f"{_main_conflict_sentence(insights.overview.main_conflict)}"
    )
    confidence = min(analysis.action_advice.confidence, alpha.confidence)
    if factor_lab:
        confidence = min(confidence, max(35, factor_lab.calibrated_confidence + 8))
    if market_regime:
        confidence = _clamp(confidence + market_regime.confidence_adjustment)
    if timeframe and timeframe.conflict_level in {"高冲突", "中冲突", "多周期偏弱"}:
        confidence = max(30, confidence - 10)
    if risk_reward and risk_reward.rating in {"风险优先", "周期冲突"}:
        confidence = max(28, confidence - 8)
    return StockDiagnosis(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        headline=headline,
        beginner_summary=f"{analysis.quote.name}现在最重要的是先确认支撑和压力是否有效。当前建议「{final_action}」，不要只因为涨跌幅做决定。",
        professional_summary=professional_summary,
        confirmation_signals=confirmation_signals[:5],
        hard_risks=hard_risks[:5],
        watch_focus=watch_focus[:5],
        action=final_action,
        confidence=confidence,
    )


def build_evidence_chain_report(
    diagnosis: StockDiagnosis,
    alpha: AlphaEvidenceReport,
    validation: SignalValidationReport,
    risk_reward: RiskRewardReport,
) -> EvidenceChainReport:
    support = [f"{item.title}：{item.reason}" for item in alpha.positives[:4]]
    opposition = [f"{item.title}：{item.reason}" for item in alpha.negatives[:4]]
    confirmations = [*diagnosis.confirmation_signals[:3], *[f"{item.name}：{item.confirmation_condition}" for item in validation.items[:2]]]
    invalidations = [*diagnosis.hard_risks[:3], f"风险收益降为「{risk_reward.rating}」且收益风险比低于 1.2。"]
    return EvidenceChainReport(
        verdict=alpha.verdict,
        summary=f"当前结论「{diagnosis.action}」不是单一指标给出，而是由证据、验证闭环和风险收益共同约束。",
        support=support or ["暂未形成足够强的正向证据。"],
        opposition=opposition or ["暂未识别核心反向证据，但仍需按失效条件观察。"],
        confirmations=_dedupe(confirmations)[:5],
        invalidations=_dedupe(invalidations)[:5],
    )


def build_stock_qa_report(
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    market_regime: MarketRegimeReport,
    risk_reward: RiskRewardReport,
    t_strategy: "TStrategyAssistantReport | None" = None,
    theme_context: ThemeContextReport | None = None,
) -> StockQaReport:
    t_summary = t_strategy.summary if t_strategy else "做T只适用于已有可卖底仓，先看区间是否足够。"
    direct_buy_answer = (
        f"当前系统建议是「{diagnosis.action}」。即使处在积极关注，也只适合按确认信号分步观察，不应在贴近压力或量能未确认时追高。"
        if diagnosis.action == "积极关注"
        else f"当前系统建议是「{diagnosis.action}」。不是「积极关注」时，不应把单日涨跌当成买点，先等确认信号。"
    )
    theme_answer = (
        f"当前为「{theme_context.level} / {theme_context.style}」。主题只用于解释背景，具体动作仍服从支撑、压力、量能和风险收益比。"
        if theme_context
        else "主题概念暂未确认，不应把题材当成独立买卖依据。"
    )
    theme_evidence = (
        [
            theme_context.summary,
            *[f"{item.name}：{item.change_pct:.2f}%" for item in theme_context.concepts[:3]],
            *theme_context.risks[:2],
        ]
        if theme_context
        else ["概念归属成分待补。"]
    )
    items = [
        StockQaItem(
            question="现在能不能直接买？",
            answer=direct_buy_answer,
            evidence=[diagnosis.headline, risk_reward.summary, market_regime.market_label],
        ),
        StockQaItem(
            question="当前风险收益比够不够？",
            answer=(
                f"当前评级「{risk_reward.rating}」，收益风险比 {risk_reward.reward_risk_ratio:.2f}。"
                "只有上方空间、下方防守和验证状态同时匹配时，才把它视为可观察机会。"
            ),
            evidence=[
                risk_reward.summary,
                f"上方目标 {risk_reward.upside_target:.2f}，下方防守 {risk_reward.downside_stop:.2f}。",
                *[item.trigger for item in risk_reward.scenarios[:2]],
            ],
        ),
        StockQaItem(
            question="为什么是这个结论？",
            answer="结论同时看趋势、资金、估值、环境、验证闭环和风险收益比；任一硬风险触发都会优先降级。",
            evidence=[diagnosis.professional_summary[:160], *diagnosis.hard_risks[:2]],
        ),
        StockQaItem(
            question="明天重点看什么？",
            answer=f"先看支撑 {analysis.support:.2f} 是否守住，再看压力 {analysis.resistance:.2f} 能否放量突破；若确认信号不齐，不急着给方向结论。",
            evidence=_dedupe([*diagnosis.watch_focus[:2], *diagnosis.confirmation_signals[:3]]),
        ),
        StockQaItem(
            question="适不适合做T？",
            answer=t_summary,
            evidence=analysis.t_plan[:3] and [item.reason for item in analysis.t_plan[:3]],
        ),
        StockQaItem(
            question="概念题材能不能支撑走势？",
            answer=theme_answer,
            evidence=theme_evidence,
        ),
    ]
    return StockQaReport(summary="围绕单只股票的常见问题，回答均引用当前分析结果。", items=items)


def answer_stock_question(
    question: str,
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    evidence_chain: EvidenceChainReport,
    risk_radar: RiskRadarReport,
    event_digest: EventDigestReport,
    peer_comparison: PeerComparisonReport,
    t_strategy: TStrategyAssistantReport,
    market_regime: MarketRegimeReport,
    risk_reward: RiskRewardReport,
    validation: SignalValidationReport,
    timeframe: TimeframeAlignmentReport,
    theme_context: ThemeContextReport | None = None,
) -> StockQuestionAnswer:
    clean_question = " ".join(str(question or "").strip().split())
    topic = _stock_question_topic(clean_question)
    confidence = _question_confidence(analysis, diagnosis, market_regime, validation, topic, theme_context)
    evidence = _question_evidence(
        topic,
        analysis,
        diagnosis,
        evidence_chain,
        risk_radar,
        event_digest,
        peer_comparison,
        t_strategy,
        market_regime,
        risk_reward,
        validation,
        timeframe,
        theme_context,
    )
    actions = _question_actions(topic, analysis, diagnosis, risk_radar, t_strategy, market_regime, risk_reward, validation, theme_context)
    invalidations = _question_invalidations(topic, analysis, diagnosis, evidence_chain, risk_radar, t_strategy, validation, theme_context)
    conclusion = _question_conclusion(topic, diagnosis, risk_radar, t_strategy, peer_comparison, event_digest, risk_reward, validation, theme_context)
    answer = _question_answer_text(topic, analysis, diagnosis, conclusion, actions, confidence)
    return StockQuestionAnswer(
        symbol=f"{analysis.quote.code}.{analysis.quote.market}",
        updated_at=analysis.quote.timestamp,
        question=clean_question,
        topic=topic,
        conclusion=conclusion,
        answer=answer,
        confidence=confidence,
        evidence=evidence[:6],
        actions=actions[:5],
        invalidations=invalidations[:5],
        related_questions=_related_questions(topic),
    )


def build_event_digest_report(insights: StockInsightBundle) -> EventDigestReport:
    positive_events: list[str] = []
    negative_events: list[str] = []
    watch_events: list[str] = []
    for item in insights.abnormal_events.events:
        text = f"{item.title}：{item.description}"
        if item.level == "风险" or item.direction == "利空":
            negative_events.append(text)
        elif item.direction == "利好" or item.level == "积极":
            positive_events.append(text)
        else:
            watch_events.append(text)
    for item in insights.events.events[:4]:
        text = f"{item.title}：{item.description}"
        if item.level == "风险":
            negative_events.append(text)
        elif item.level == "积极":
            positive_events.append(text)
        else:
            watch_events.append(text)
    if negative_events:
        impact = "事件偏风险"
    elif positive_events and not negative_events:
        impact = "事件偏积极"
    else:
        impact = "事件待确认"
    return EventDigestReport(
        impact_label=impact,
        summary=f"{impact}。事件层主要来自异动、行业背景和龙虎榜前置判断，正式公告/研报源仍可继续增强。",
        positive_events=_dedupe(positive_events)[:4],
        negative_events=_dedupe(negative_events)[:4],
        watch_events=_dedupe(watch_events)[:4] or ["暂无会改变结论的明确事件，继续观察行情和行业背景。"],
        missing_data=_dedupe([*insights.events.notes, *insights.lhb.missing_data, *insights.abnormal_events.notes])[:6],
    )


def build_peer_comparison_report(analysis: AnalysisResult, insights: StockInsightBundle, feature: FeatureSnapshot) -> PeerComparisonReport:
    peers = [item for item in analysis.peer_quotes if item.price > 0]
    industry = analysis.stock_profile.industry if analysis.stock_profile and analysis.stock_profile.industry else "行业待确认"
    if not peers:
        return PeerComparisonReport(
            industry=industry,
            sample_count=0,
            summary="同行样本不足，暂以个股自身历史和行业涨跌背景为主。",
            risks=["同行报价样本不足，同行估值和强弱分位需要等待缓存积累。"],
        )
    avg_change = sum(item.change_pct for item in peers) / len(peers)
    avg_amount = sum(item.amount for item in peers if item.amount) / max(1, sum(1 for item in peers if item.amount))
    stronger_count = sum(1 for item in peers if item.change_pct <= analysis.quote.change_pct)
    strength_percentile = stronger_count / len(peers) * 100
    valuation_position = _peer_position_label(insights.valuation.peer_pe_percentile, "估值")
    strength_position = _peer_position_label(strength_percentile, "强弱")
    leaders = sorted(peers, key=lambda item: (item.change_pct, item.amount or 0), reverse=True)[:3]
    risks = []
    if insights.valuation.peer_pe_percentile is not None and insights.valuation.peer_pe_percentile >= 80:
        risks.append("PE相对同行偏高，追高需要更严格确认。")
    if strength_percentile <= 35:
        risks.append("涨跌幅相对同行偏弱，暂不宜急着上调评级。")
    return PeerComparisonReport(
        industry=industry,
        sample_count=len(peers),
        valuation_position=valuation_position,
        strength_position=strength_position,
        summary=f"同行样本 {len(peers)} 只，当前个股涨跌幅相对同行约处于 {strength_percentile:.1f}% 分位。",
        metrics=[
            f"个股涨跌幅 {analysis.quote.change_pct:.2f}%，同行均值 {avg_change:.2f}%。",
            f"个股成交额 {feature.amount / 100000000:.1f} 亿。" if feature.amount else "个股成交额待确认。",
            f"同行平均成交额 {avg_amount / 100000000:.1f} 亿。" if avg_amount else "同行成交额样本不足。",
            f"同行PE分位 {insights.valuation.peer_pe_percentile:.1f}%。" if insights.valuation.peer_pe_percentile is not None else "同行PE分位待确认。",
        ],
        leaders=[f"{item.name}{item.code}：{item.change_pct:.2f}%" for item in leaders],
        risks=risks or ["同行对比暂未发现压倒性风险，仍需结合趋势和估值锚。"],
    )


def build_t_strategy_assistant_report(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
) -> TStrategyAssistantReport:
    style = _t_strategy_style(feature, market_regime)
    width_pct = (feature.resistance - feature.support) / feature.price * 100 if feature.price and feature.resistance > feature.support else 0
    if analysis.data_quality.score < 70 or market_regime.risk_multiplier >= 1.28:
        suitability = "不适合主动做T"
    elif width_pct >= max(1.2, feature.atr_pct * 0.8) and validation.overall_status != "风险优先":
        suitability = "仅底仓可做T"
    else:
        suitability = "等待更大区间"
    low_zone = f"{max(feature.support, feature.price - max(feature.atr14, feature.price * 0.012)):.2f} 附近"
    high_zone = f"{min(feature.resistance, feature.price + max(feature.atr14, feature.price * 0.012)):.2f} 附近"
    return TStrategyAssistantReport(
        style=style,
        suitability=suitability,
        summary=f"{style}，{suitability}。做T只服务于降低已有底仓成本，不等同于新增买入。",
        low_zone=low_zone,
        high_zone=high_zone,
        execution_steps=[
            "先确认手里有可卖底仓，今日新增买入部分不参与当日T。",
            f"低吸只看 {low_zone} 缩量止跌，不在放量下跌中接。",
            f"高抛只看 {high_zone} 冲高乏力或接近压力，不恋战。",
        ],
        stop_conditions=[
            f"有效跌破支撑 {feature.support:.2f}。",
            "成交突然放大向下或盘口卖压增强。",
            "区间宽度不足以覆盖交易成本和滑点。",
        ],
    )


def build_risk_radar_report(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    market_regime: MarketRegimeReport,
    risk_reward: RiskRewardReport,
    timeframe: TimeframeAlignmentReport,
) -> RiskRadarReport:
    items = [
        _risk_radar_item("趋势破位", 100 - feature.trend_score, f"趋势评分 {feature.trend_score}，20日线 {feature.ma20:.2f}。", "跌破关键均线先降级。"),
        _risk_radar_item("估值压力", 100 - feature.valuation_score, f"估值评分 {feature.valuation_score}，{insights.valuation.valuation_anchor_label}。", "估值高位时追高必须等确认。"),
        _risk_radar_item("事件异动", 75 if insights.abnormal_events.level == "风险" else 35, insights.abnormal_events.main_signal, "事件风险解除前降低仓位冲动。"),
        _risk_radar_item("流动性", 65 if feature.amount and feature.amount < 300_000_000 else 35, f"成交额 {feature.amount / 100000000:.1f} 亿。" if feature.amount else "成交额缺失。", "低流动性信号容易失真。"),
        _risk_radar_item("环境风险", round((market_regime.risk_multiplier - 0.8) * 100), f"{market_regime.market_label}，风险倍率 {market_regime.risk_multiplier:.2f}。", "环境偏冷时降低信号权重。"),
        _risk_radar_item("周期冲突", 72 if timeframe.conflict_level in {"高冲突", "多周期偏弱"} else 48 if timeframe.conflict_level == "中冲突" else 30, timeframe.summary, "周期冲突时等待主周期修复。"),
        _risk_radar_item("性价比", 68 if risk_reward.rating in {"风险优先", "周期冲突", "性价比不足"} else 38, risk_reward.summary, "收益风险比不足时不主动提高积极度。"),
        _risk_radar_item("数据质量", 100 - analysis.data_quality.score, f"数据质量 {analysis.data_quality.level} {analysis.data_quality.score} 分。", "数据差时所有买卖点降权。"),
    ]
    top = sorted(items, key=lambda item: item.score, reverse=True)[:3]
    overall_score = round(sum(item.score for item in items) / len(items))
    overall_level = "高风险" if overall_score >= 68 else "中风险" if overall_score >= 45 else "风险可控"
    return RiskRadarReport(
        overall_level=overall_level,
        summary=f"{overall_level}：优先处理" + "、".join(item.name for item in top) + "。",
        items=items,
        top_risks=[f"{item.name}：{item.reason}" for item in top],
    )


def build_signal_validation_report(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> SignalValidationReport:
    trend_factor = _find_factor(factor_lab, "trend_momentum")
    volume_factor = _find_factor(factor_lab, "volume_confirmation")
    risk_factor = _find_factor(factor_lab, "risk_pressure")
    chip_factor = _find_factor(factor_lab, "chip_position")
    items = [
        SignalValidationItem(
            name="趋势回踩验证",
            category="买点",
            status=_validation_status(feature.trend_score >= 55 and feature.price >= feature.ma5, market_regime, trend_factor, timeframe),
            confidence=_validation_confidence(feature.signal_confidence, trend_factor, market_regime, timeframe),
            trigger_condition=f"价格回到5日线 {feature.ma5:.2f} 上方，趋势评分至少 55 分。",
            confirmation_condition=f"收盘不跌回5日线，同时量能不低于20日均量 1.1 倍；当前量能 {feature.volume_ratio:.2f} 倍。",
            invalidation_condition=f"收盘跌破20日线 {feature.ma20:.2f} 或有效跌破支撑 {feature.support:.2f}。",
            historical_reference=_factor_reference(trend_factor),
            action_hint="只作为回踩确认信号，不在下跌途中提前判定止跌。",
        ),
        SignalValidationItem(
            name="压力突破验证",
            category="买点",
            status=_validation_status(feature.price >= feature.resistance * 0.985 and feature.volume_ratio >= 1.1, market_regime, volume_factor, timeframe),
            confidence=_validation_confidence(feature.signal_confidence, volume_factor, market_regime, timeframe),
            trigger_condition=f"价格接近或突破压力位 {feature.resistance:.2f}，且放量不低于20日均量 1.1 倍。",
            confirmation_condition="突破后回踩不跌回压力位下方，资金评分维持在60分附近或继续改善。",
            invalidation_condition="突破后快速缩量回落，或次日跌回压力位下方。",
            historical_reference=_factor_reference(volume_factor),
            action_hint="适合右侧确认，不适合盘中一冲就追。",
        ),
        SignalValidationItem(
            name="支撑防守验证",
            category="风控",
            status=_validation_status(feature.price > feature.support * 1.01 and feature.price >= feature.ma20 * 0.985, market_regime, risk_factor, timeframe, reverse=True),
            confidence=_validation_confidence(feature.data_quality_score, risk_factor, market_regime, timeframe),
            trigger_condition=f"价格靠近支撑 {feature.support:.2f} 时不再放量下跌。",
            confirmation_condition="支撑附近缩量止跌，且次日能重新站回短期均线。",
            invalidation_condition=f"有效跌破支撑 {feature.support:.2f} 或20日线 {feature.ma20:.2f} 后不能快速修复。",
            historical_reference=_factor_reference(risk_factor),
            action_hint="这是风险线，不是越跌越买的理由。",
        ),
        SignalValidationItem(
            name="做T区间验证",
            category="做T",
            status=_validation_status(feature.price > feature.support and feature.price < feature.resistance, market_regime, chip_factor, timeframe),
            confidence=_validation_confidence(min(feature.signal_confidence, feature.data_quality_score), chip_factor, market_regime, timeframe),
            trigger_condition=f"价格运行在支撑 {feature.support:.2f} 与压力 {feature.resistance:.2f} 之间。",
            confirmation_condition="只用已有可卖底仓，低吸后必须能在区间上沿或分时转弱处高抛。",
            invalidation_condition="区间被单边跌破、成交突然放大向下，或盘口显示明显卖压。",
            historical_reference=_factor_reference(chip_factor),
            action_hint="做T只服务于降低持仓成本，不等同于新增买入建议。",
        ),
    ]
    overall_status = _validation_overall_status(items, market_regime, timeframe)
    return SignalValidationReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        overall_status=overall_status,
        summary=_validation_summary(overall_status, items, market_regime, timeframe),
        items=items,
        notes=[
            "验证闭环把每条建议拆成触发、确认、失效和历史参考，避免单个信号直接变成买卖结论。",
            "状态为“等待确认”时，只说明条件接近，不代表已经满足。",
            *([f"多周期当前为「{timeframe.conflict_level}」，所有验证状态已按保守口径降级。"] if timeframe and timeframe.conflict_level in {"高冲突", "中冲突", "多周期偏弱"} else []),
        ],
    )


def build_risk_reward_report(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> RiskRewardReport:
    price = feature.price
    upside_target = _upside_target(feature, factor_lab)
    downside_stop = _downside_stop(feature, market_regime)
    upside_pct = pct_change(upside_target, price) if price else 0
    downside_pct = abs(pct_change(downside_stop, price)) if price else 0
    ratio = round(upside_pct / downside_pct, 2) if downside_pct > 0 else 0
    rating = _risk_reward_rating(ratio, factor_lab, market_regime, validation, timeframe)
    scenarios = _scenario_plans(analysis, feature, factor_lab, market_regime, validation, upside_target, downside_stop)
    return RiskRewardReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        current_price=round(price, 2),
        upside_target=round(upside_target, 2),
        downside_stop=round(downside_stop, 2),
        upside_pct=round(upside_pct, 2),
        downside_pct=round(downside_pct, 2),
        reward_risk_ratio=ratio,
        atr14=round(feature.atr14, 2),
        atr_pct=round(feature.atr_pct, 2),
        volatility_pct=round(feature.volatility_pct, 2),
        rating=rating,
        summary=_risk_reward_summary(rating, ratio, upside_pct, downside_pct, market_regime, timeframe, feature),
        scenarios=scenarios,
        notes=[
            "风险收益比只用于单股观察，不代表收益承诺。",
            "目标位和防守位已参考ATR和近期波动率；若数据质量或市场环境恶化，应优先使用下方失效位。",
        ],
    )


def build_timeframe_alignment_report(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
) -> TimeframeAlignmentReport:
    frames = [
        _timeframe_trend(analysis, feature, "短线", 20),
        _timeframe_trend(analysis, feature, "波段", 60),
        _timeframe_trend(analysis, feature, "中期", 120),
    ]
    valid_frames = [item for item in frames if item.window_days <= len(analysis.klines)]
    if not valid_frames:
        valid_frames = frames[:1]
    alignment_score = _timeframe_alignment_score(valid_frames, factor_lab)
    conflict_level = _timeframe_conflict_level(valid_frames)
    alignment_label = _timeframe_alignment_label(alignment_score, conflict_level)
    return TimeframeAlignmentReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        alignment_score=alignment_score,
        alignment_label=alignment_label,
        conflict_level=conflict_level,
        summary=_timeframe_summary(alignment_label, conflict_level, valid_frames),
        timeframes=valid_frames,
        suggestions=_timeframe_suggestions(valid_frames, conflict_level),
    )


def build_market_breadth_snapshot(quotes: list) -> MarketBreadthSnapshot:
    valid = [item for item in quotes if getattr(item, "price", 0) > 0]
    if not valid:
        return MarketBreadthSnapshot(
            label="市场宽度待确认",
            score=50,
            up_count=0,
            down_count=0,
            strong_count=0,
            weak_count=0,
            avg_change_pct=0,
            risk_adjustment=0,
            summary="市场宽度样本不足，环境判断暂以个股和行业为主。",
        )
    changes = [float(item.change_pct) for item in valid]
    up_count = sum(1 for item in changes if item > 0)
    down_count = sum(1 for item in changes if item < 0)
    strong_count = sum(1 for item in changes if item >= 3)
    weak_count = sum(1 for item in changes if item <= -3)
    up_ratio = up_count / len(valid)
    avg_change = sum(changes) / len(changes)
    score = _clamp(round(45 + (up_ratio - 0.5) * 70 + avg_change * 6 + (strong_count - weak_count) / len(valid) * 35))
    if score >= 68:
        label = "市场宽度强"
        adjustment = -0.08
    elif score >= 56:
        label = "市场宽度偏暖"
        adjustment = -0.03
    elif score <= 32:
        label = "市场宽度弱"
        adjustment = 0.12
    elif score <= 44:
        label = "市场宽度偏冷"
        adjustment = 0.06
    else:
        label = "市场宽度中性"
        adjustment = 0
    return MarketBreadthSnapshot(
        label=label,
        score=score,
        up_count=up_count,
        down_count=down_count,
        strong_count=strong_count,
        weak_count=weak_count,
        avg_change_pct=round(avg_change, 2),
        risk_adjustment=adjustment,
        summary=f"{label}：样本 {len(valid)} 只，上涨 {up_count}、下跌 {down_count}，平均涨跌幅 {avg_change:.2f}%。",
    )


def build_chip_analysis(analysis: AnalysisResult, feature: FeatureSnapshot) -> ChipAnalysis:
    rows = analysis.klines[-80:]
    if len(rows) < 10:
        return ChipAnalysis(
            symbol=feature.symbol,
            updated_at=feature.updated_at,
            center_price=feature.price,
            concentration=35,
            distribution_label="筹码样本不足",
            summary="K线样本不足，暂不能形成有效筹码分布估算。",
            notes=["筹码为日K成交量按价格区间近似分布，不等同于交易所真实持仓成本。"],
        )
    bins = _volume_price_bins(rows, bucket_count=12)
    total_volume = sum(item[2] for item in bins) or 1
    weighted_center = sum(((low + high) / 2) * volume for low, high, volume in bins) / total_volume
    concentration = _chip_concentration(bins, weighted_center, total_volume)
    support_bands = [
        ChipBand(label="支撑筹码区", low=low, high=high, share=round(volume / total_volume * 100, 1), note="现价下方成交密集区，跌破后支撑意义下降。")
        for low, high, volume in sorted((item for item in bins if item[1] <= feature.price), key=lambda item: item[2], reverse=True)[:3]
    ]
    pressure_bands = [
        ChipBand(label="压力筹码区", low=low, high=high, share=round(volume / total_volume * 100, 1), note="现价上方成交密集区，放量站稳后压力才会转化。")
        for low, high, volume in sorted((item for item in bins if item[0] >= feature.price), key=lambda item: item[2], reverse=True)[:3]
    ]
    label = "筹码相对集中" if concentration >= 68 else "筹码分布适中" if concentration >= 48 else "筹码较分散"
    summary = f"近{len(rows)}日估算成本中枢约 {weighted_center:.2f}，{label}。现价相对中枢 {pct_change(feature.price, weighted_center):.2f}%。"
    return ChipAnalysis(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        center_price=round(weighted_center, 2),
        concentration=concentration,
        distribution_label=label,
        summary=summary,
        support_bands=support_bands,
        pressure_bands=pressure_bands,
        notes=[
            "筹码分布用日K成交量和均价近似估算，适合判断压力/支撑区域，不代表真实股东成本。",
            "若接入逐笔成交或区间成交分布，可替换为更精确的筹码模型。",
        ],
    )


def build_leadership_report(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    concepts: list[StockConceptItem] | None = None,
) -> LeadershipReport:
    concepts = concepts or []
    evidence = [
        f"趋势评分 {feature.trend_score}，涨跌幅 {feature.change_pct:.2f}%。",
        f"成交额 {feature.amount / 100000000:.1f} 亿，量能比 {feature.volume_ratio:.2f}。" if feature.amount else f"量能比 {feature.volume_ratio:.2f}。",
        f"资金评分 {feature.fund_flow_score}，盘口状态：{feature.order_pressure}。",
    ]
    if feature.industry_name and feature.industry_change_pct is not None:
        evidence.append(f"行业 {feature.industry_name} 涨跌幅 {feature.industry_change_pct:.2f}%。")
    if concepts:
        hot_concepts = sorted(concepts, key=lambda item: item.change_pct, reverse=True)[:2]
        evidence.append(
            "概念背景：" + "、".join(f"{item.name}{item.change_pct:.2f}%" for item in hot_concepts) + "。"
        )
    missing = []
    if not insights.lhb.available:
        missing.append("龙虎榜席位")
    if not insights.fund_flow.available:
        missing.append("逐笔大单资金流")
    if not analysis.industry_context:
        missing.append("行业强度排名")
    if not concepts:
        missing.append("概念归属")
    level = feature.leader_level
    summary = "具备龙头候选特征" if feature.leader_score >= 70 else "属于强势观察个股" if feature.leader_score >= 55 else "暂不具备龙头特征"
    if feature.data_quality_score < 70:
        summary = f"数据质量{feature.data_quality_level}，{summary}需要降权。"
    return LeadershipReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        score=feature.leader_score,
        level=level,
        summary=summary,
        tags=feature.tags[:8],
        evidence=evidence,
        missing_data=missing,
    )


def build_theme_context_report(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    concepts: list[StockConceptItem] | None = None,
) -> ThemeContextReport:
    concepts = concepts or []
    symbol = f"{analysis.quote.code}.{analysis.quote.market}"
    industry = analysis.stock_profile.industry if analysis.stock_profile and analysis.stock_profile.industry else "行业待确认"
    industry_change = analysis.industry_context.change_pct if analysis.industry_context else None
    concept_avg = sum(item.change_pct for item in concepts) / len(concepts) if concepts else None
    strongest = max(concepts, key=lambda item: item.change_pct, default=None)
    relative_to_industry = analysis.quote.change_pct - industry_change if industry_change is not None else None
    relative_to_concepts = analysis.quote.change_pct - concept_avg if concept_avg is not None else None

    score = 45
    if industry_change is not None:
        score += round(industry_change * 6)
    if concept_avg is not None:
        score += round(concept_avg * 8)
    if relative_to_industry is not None:
        score += round(relative_to_industry * 4)
    if relative_to_concepts is not None:
        score += round(relative_to_concepts * 4)
    score += round((feature.trend_score - 50) * 0.25)
    if feature.data_quality_score < 70:
        score -= 8
    score = _clamp(score)

    level = _theme_level(score, industry_change, concept_avg)
    style = _theme_style(analysis.quote.change_pct, industry_change, concept_avg)
    evidence = _theme_evidence(analysis, feature, concepts, concept_avg, relative_to_industry, relative_to_concepts)
    relative_strength = _theme_relative_strength(relative_to_industry, relative_to_concepts)
    opportunities = _theme_opportunities(analysis, feature, concepts, strongest, relative_to_industry, relative_to_concepts)
    risks = _theme_risks(analysis, feature, concepts, industry_change, concept_avg, relative_to_industry, relative_to_concepts)
    missing_data = []
    if industry == "行业待确认" or industry_change is None:
        missing_data.append("行业归属或行业涨跌强度")
    if not concepts:
        missing_data.append("概念归属成分")
    if feature.data_quality_score < 80:
        missing_data.append(f"数据质量{feature.data_quality_level}")

    return ThemeContextReport(
        symbol=symbol,
        updated_at=analysis.quote.timestamp,
        industry=industry,
        industry_change_pct=industry_change,
        concepts=concepts[:8],
        score=score,
        level=level,
        style=style,
        relative_strength=relative_strength,
        summary=_theme_summary(analysis, level, style, industry, industry_change, concept_avg, strongest),
        evidence=evidence,
        opportunities=_dedupe(opportunities)[:4],
        risks=_dedupe(risks)[:4],
        missing_data=_dedupe(missing_data)[:5],
    )


def build_replay_analysis(analysis: AnalysisResult, window_days: int = 120) -> StockReplayAnalysis:
    rows = analysis.klines[-window_days:]
    symbol = f"{analysis.quote.code}.{analysis.quote.market}"
    if len(rows) < 30:
        return StockReplayAnalysis(
            symbol=symbol,
            updated_at=analysis.quote.timestamp,
            window_days=len(rows),
            sample_count=0,
            success_rate=0,
            summary="历史K线不足，暂不能做信号回放。",
            notes=["至少需要30根日K才能做基本回放。"],
        )
    cases: list[ReplayCase] = []
    for index in range(20, len(rows) - 10):
        pattern = _detect_replay_pattern(rows, index)
        if not pattern:
            continue
        entry = rows[index].close
        forward_3d = pct_change(rows[index + 3].close, entry) if index + 3 < len(rows) else None
        forward_5d = pct_change(rows[index + 5].close, entry) if index + 5 < len(rows) else None
        forward_10d = pct_change(rows[index + 10].close, entry) if index + 10 < len(rows) else None
        outcome = "有效" if (forward_5d or 0) > 2 else "风险" if (forward_5d or 0) < -3 else "震荡"
        cases.append(
            ReplayCase(
                date=rows[index].date,
                pattern=pattern,
                entry_price=round(entry, 2),
                forward_3d_return=round(forward_3d, 2) if forward_3d is not None else None,
                forward_5d_return=round(forward_5d, 2) if forward_5d is not None else None,
                forward_10d_return=round(forward_10d, 2) if forward_10d is not None else None,
                outcome=outcome,
                note=_replay_case_note(pattern, outcome),
            )
        )
    stats = _replay_stats(cases)
    success_rate = round(sum(1 for item in cases if item.outcome == "有效") / len(cases) * 100, 1) if cases else 0
    if not cases:
        summary = f"近{len(rows)}日没有识别到足够清晰的回放信号。"
    elif len(cases) < 5:
        summary = f"近{len(rows)}日仅识别到 {len(cases)} 个可回放信号，样本偏少，只适合看案例，不宜解读为稳定胜率。"
    else:
        summary = f"近{len(rows)}日识别到 {len(cases)} 个可回放信号，5日后样本有效率 {success_rate:.1f}%。"
    return StockReplayAnalysis(
        symbol=symbol,
        updated_at=analysis.quote.timestamp,
        window_days=len(rows),
        sample_count=len(cases),
        success_rate=success_rate,
        summary=summary,
        pattern_stats=stats,
        cases=cases[-8:],
        notes=[
            "回放只用于检验该股历史上相似信号的表现，不代表未来收益承诺。",
            "样本少于5个时只作为案例观察，不用于提高策略置信。",
            "后续可加入信号版本、滑点和成交约束，形成更严谨的单股验证。",
        ],
    )


def _leader_score(analysis: AnalysisResult, insights: StockInsightBundle, volume_ratio: float) -> int:
    score = 40
    score += round((analysis.trend_score - 50) * 0.45)
    score += 14 if analysis.quote.change_pct >= 5 else 8 if analysis.quote.change_pct >= 2 else -6 if analysis.quote.change_pct <= -3 else 0
    score += 10 if volume_ratio >= 1.5 and analysis.quote.change_pct > 0 else -8 if volume_ratio >= 1.5 and analysis.quote.change_pct < 0 else 0
    score += 8 if analysis.quote.amount >= 1_000_000_000 else 3 if analysis.quote.amount >= 300_000_000 else -4
    score += round((insights.fund_flow.overall_score - 50) * 0.2)
    if analysis.industry_context and analysis.industry_context.change_pct > 1:
        score += 6
    if insights.abnormal_events.level == "风险":
        score -= 12
    if analysis.data_quality.score < 70:
        score -= 10
    return _clamp(score)


def _feature_tags(analysis: AnalysisResult, insights: StockInsightBundle, volume_ratio: float, leader_score: int) -> list[str]:
    tags: list[str] = []
    if leader_score >= 70:
        tags.append("龙头候选")
    if analysis.trend_score >= 70:
        tags.append("趋势强")
    if analysis.quote.change_pct >= 5:
        tags.append("情绪强")
    if volume_ratio >= 1.5:
        tags.append("量能放大")
    if analysis.quote.turnover_rate and analysis.quote.turnover_rate >= 8:
        tags.append("换手活跃")
    if insights.fund_flow.overall_score >= 65:
        tags.append("资金配合")
    if insights.abnormal_events.level == "风险":
        tags.append("风险异动")
    if analysis.data_quality.score < 70:
        tags.append("数据降权")
    return tags or ["常规观察"]


def _factor_specs() -> dict[str, FactorSpec]:
    if not FACTOR_SPECS:
        FACTOR_SPECS.update(
            {
                "trend_momentum": FactorSpec(
                    id="trend_momentum",
                    name="趋势动量",
                    category="技术",
                    weight=1.35,
                    direction="正向",
                    evaluator=_trend_proxy_score_at,
                    trigger=_trend_trigger,
                ),
                "volume_confirmation": FactorSpec(
                    id="volume_confirmation",
                    name="量价确认",
                    category="技术",
                    weight=1.1,
                    direction="正向",
                    evaluator=_volume_proxy_score_at,
                    trigger=_volume_trigger,
                ),
                "risk_pressure": FactorSpec(
                    id="risk_pressure",
                    name="风险压力",
                    category="风控",
                    weight=1.25,
                    direction="反向",
                    evaluator=_risk_proxy_score_at,
                    trigger=_risk_trigger,
                ),
                "fund_flow_proxy": FactorSpec(
                    id="fund_flow_proxy",
                    name="资金连续性",
                    category="资金",
                    weight=1.1,
                    direction="正向",
                    evaluator=_fund_flow_proxy_score_at,
                    trigger=_fund_flow_trigger,
                ),
                "chip_position": FactorSpec(
                    id="chip_position",
                    name="筹码位置",
                    category="筹码",
                    weight=0.95,
                    direction="正向",
                    evaluator=_chip_position_score_at,
                    trigger=_chip_trigger,
                ),
                "leadership_strength": FactorSpec(
                    id="leadership_strength",
                    name="龙头强度",
                    category="强弱",
                    weight=1.05,
                    direction="正向",
                    evaluator=_leadership_proxy_score_at,
                    trigger=_leadership_trigger,
                ),
            }
        )
    return FACTOR_SPECS


def _build_factor(
    spec: FactorSpec,
    analysis: AnalysisResult,
    score: int,
    value: str,
    evidence: list[str],
    missing_data: list[str],
    weight_adjustments: dict[str, float] | None = None,
) -> StandardFactor:
    clean_score = _clamp(score)
    return StandardFactor(
        id=spec.id,
        name=spec.name,
        category=spec.category,
        value=value,
        score=clean_score,
        level=_score_level(clean_score),
        direction=_factor_direction(clean_score),
        percentile=_factor_percentile(analysis.klines, spec.evaluator, clean_score),
        weight=_adjusted_factor_weight(spec.id, spec.weight, weight_adjustments or {}),
        evidence=evidence[:4],
        missing_data=_dedupe(missing_data)[:6],
        calibration=_calibrate_factor(analysis.klines, spec, clean_score),
        calibration_buckets=_calibration_buckets(analysis.klines, spec, clean_score),
    )


def _calibrate_factor(rows: list, spec: FactorSpec, current_score: int) -> FactorCalibration:
    if len(rows) < 35:
        return FactorCalibration(
            sample_count=0,
            win_rate=0,
            avg_forward_5d_return=0,
            avg_forward_10d_return=0,
            max_adverse_return=0,
            confidence_level="样本不足",
            note="少于35根日K，暂不能形成稳定历史校准。",
        )
    forward_5d: list[float] = []
    forward_10d: list[float] = []
    adverse_returns: list[float] = []
    for index in range(25, len(rows) - 10):
        try:
            matched = spec.trigger(rows, index, current_score)
        except (ValueError, ZeroDivisionError):
            continue
        if not matched or rows[index].close <= 0:
            continue
        entry = rows[index].close
        forward_5d.append(pct_change(rows[index + 5].close, entry))
        forward_10d.append(pct_change(rows[index + 10].close, entry))
        lows = [item.low for item in rows[index + 1 : index + 6] if item.low > 0]
        adverse_returns.append(min(pct_change(low, entry) for low in lows) if lows else 0)
    if not forward_5d:
        return FactorCalibration(
            sample_count=0,
            win_rate=0,
            avg_forward_5d_return=0,
            avg_forward_10d_return=0,
            max_adverse_return=0,
            stability_score=0,
            expected_level="待确认",
            confidence_level="无相似样本",
            note=f"历史中没有找到足够接近当前「{spec.name}」状态的样本。",
        )
    sample_count = len(forward_5d)
    win_rate = sum(1 for item in forward_5d if item > 0) / sample_count * 100
    avg_5d = sum(forward_5d) / sample_count
    avg_10d = sum(forward_10d) / sample_count
    max_adverse = min(adverse_returns) if adverse_returns else 0
    expected_level = _calibration_expected_level(spec.direction, win_rate, avg_5d, avg_10d)
    stability_score = _clamp(round(50 + win_rate * 0.28 + avg_5d * 3 + avg_10d * 1.5 + max_adverse * 1.1))
    return FactorCalibration(
        sample_count=sample_count,
        win_rate=round(win_rate, 1),
        avg_forward_5d_return=round(avg_5d, 2),
        avg_forward_10d_return=round(avg_10d, 2),
        max_adverse_return=round(max_adverse, 2),
        stability_score=stability_score,
        expected_level=expected_level,
        confidence_level=_calibration_confidence_level(sample_count, win_rate, avg_5d),
        note=_calibration_note(spec.name, sample_count, win_rate, avg_5d),
    )


def _calibration_buckets(rows: list, spec: FactorSpec, current_score: int) -> list[CalibrationBucket]:
    if len(rows) < 45:
        return []
    buckets: dict[str, list[tuple[float, float]]] = {
        "强趋势": [],
        "弱趋势": [],
        "支撑附近": [],
        "压力附近": [],
    }
    for index in range(25, len(rows) - 10):
        try:
            if not spec.trigger(rows, index, current_score) or rows[index].close <= 0:
                continue
        except (ValueError, ZeroDivisionError):
            continue
        entry = rows[index].close
        forward_5d = pct_change(rows[index + 5].close, entry)
        forward_10d = pct_change(rows[index + 10].close, entry)
        trend = _trend_proxy_score_at(rows, index)
        support, resistance = _local_support_resistance(rows, index)
        if trend >= 65:
            buckets["强趋势"].append((forward_5d, forward_10d))
        if trend <= 45:
            buckets["弱趋势"].append((forward_5d, forward_10d))
        if support and entry <= support * 1.035:
            buckets["支撑附近"].append((forward_5d, forward_10d))
        if resistance and entry >= resistance * 0.985:
            buckets["压力附近"].append((forward_5d, forward_10d))
    return [_bucket_summary(name, values) for name, values in buckets.items() if values][:4]


def _bucket_summary(name: str, values: list[tuple[float, float]]) -> CalibrationBucket:
    forward_5d = [item[0] for item in values]
    forward_10d = [item[1] for item in values]
    sample_count = len(values)
    win_rate = sum(1 for item in forward_5d if item > 0) / sample_count * 100
    avg_5d = sum(forward_5d) / sample_count
    avg_10d = sum(forward_10d) / sample_count
    if sample_count < 5:
        note = "样本偏少，只作参考。"
    elif win_rate >= 58 and avg_5d > 0:
        note = "该场景历史表现偏正。"
    elif win_rate < 45 or avg_5d < 0:
        note = "该场景历史表现偏弱。"
    else:
        note = "该场景历史表现中性。"
    return CalibrationBucket(
        name=name,
        sample_count=sample_count,
        win_rate=round(win_rate, 1),
        avg_forward_5d_return=round(avg_5d, 2),
        avg_forward_10d_return=round(avg_10d, 2),
        note=note,
    )


def _local_support_resistance(rows: list, index: int) -> tuple[float, float]:
    window = rows[max(0, index - 19) : index + 1]
    if len(window) < 5:
        return 0, 0
    lows = sorted(item.low for item in window if item.low > 0)
    highs = sorted(item.high for item in window if item.high > 0)
    if not lows or not highs:
        return 0, 0
    support = lows[max(0, round((len(lows) - 1) * 0.18))]
    resistance = highs[min(len(highs) - 1, round((len(highs) - 1) * 0.82))]
    return support, resistance


def _factor_percentile(rows: list, evaluator: Callable[[list, int], float], current_score: int) -> float | None:
    if len(rows) < 30:
        return None
    values: list[float] = []
    for index in range(20, len(rows) - 1):
        try:
            values.append(evaluator(rows, index))
        except (ValueError, ZeroDivisionError):
            continue
    if not values:
        return None
    below = sum(1 for item in values if item <= current_score)
    return round(below / len(values) * 100, 1)


def _weighted_factor_score(factors: list[StandardFactor]) -> int:
    total_weight = sum(item.weight for item in factors) or 1
    return _clamp(round(sum(item.score * item.weight for item in factors) / total_weight))


def _factor_weight_policy(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
) -> tuple[str, dict[str, float], list[str]]:
    amount = feature.amount or 0
    market_cap = analysis.quote.market_cap or 0
    turnover = feature.turnover_rate or 0
    adjustments: dict[str, float] = {}
    notes: list[str] = []
    profile = "常规个股"
    if market_cap >= 500_000_000_000 or (amount >= 3_000_000_000 and turnover < 2):
        profile = "大市值稳健股"
        adjustments.update({"valuation_anchor": 1.25, "risk_pressure": 1.12, "trend_momentum": 1.08, "leadership_strength": 0.9})
        notes.append("大市值稳健股提高估值锚、风控和趋势修复权重，降低短线情绪权重。")
    elif turnover >= 8 or feature.volume_ratio >= 1.6:
        profile = "高活跃波动股"
        adjustments.update({"volume_confirmation": 1.25, "fund_flow_proxy": 1.15, "risk_pressure": 1.18, "valuation_anchor": 0.82})
        notes.append("高活跃波动股提高量价、资金和风险权重，降低静态估值权重。")
    elif amount and amount < 300_000_000:
        profile = "低流动性个股"
        adjustments.update({"risk_pressure": 1.28, "volume_confirmation": 1.15, "fund_flow_proxy": 0.86, "leadership_strength": 0.88})
        notes.append("低流动性个股提高风险和量价确认权重，降低资金估算与强弱标签权重。")
    if feature.data_quality_score < 70:
        adjustments["risk_pressure"] = adjustments.get("risk_pressure", 1.0) * 1.18
        adjustments["fund_flow_proxy"] = adjustments.get("fund_flow_proxy", 1.0) * 0.88
        notes.append("数据质量不足时提高风控权重，降低资金估算权重。")
    return profile, adjustments, notes or ["使用默认单股分析权重。"]


def _adjusted_factor_weight(factor_id: str, base_weight: float, adjustments: dict[str, float]) -> float:
    multiplier = adjustments.get(factor_id, 1.0)
    return round(max(0.5, min(1.8, base_weight * multiplier)), 2)


def _factor_calibration_quality(factors: list[StandardFactor]) -> int:
    scored = [item.calibration for item in factors if item.calibration and item.calibration.sample_count > 0]
    if not scored:
        return 35
    total_weight = sum(min(1.6, max(0.6, item.sample_count / 12)) for item in scored)
    weighted = sum(item.stability_score * min(1.6, max(0.6, item.sample_count / 12)) for item in scored)
    coverage_bonus = min(10, len(scored) * 2)
    return _clamp(round(weighted / total_weight + coverage_bonus))


def _volume_confirmation_score(analysis: AnalysisResult, feature: FeatureSnapshot) -> int:
    score = 52
    ratio = feature.volume_ratio
    change = analysis.quote.change_pct
    if change > 0 and ratio >= 1.2:
        score += 18 + round(min(10, (ratio - 1.2) * 8))
    elif change < 0 and ratio >= 1.2:
        score -= 18 + round(min(10, (ratio - 1.2) * 8))
    elif ratio < 0.7 and abs(change) >= 2:
        score -= 8
    elif 0.85 <= ratio <= 1.25:
        score += 4
    return _clamp(score)


def _risk_pressure_score(analysis: AnalysisResult, insights: StockInsightBundle, feature: FeatureSnapshot) -> int:
    score = 72
    if analysis.risk_level == "高风险":
        score -= 32
    elif analysis.risk_level == "中等风险":
        score -= 16
    elif analysis.risk_level == "低风险":
        score += 6
    score += round((feature.data_quality_score - 80) * 0.22)
    if insights.abnormal_events.level == "风险":
        score -= 14
    if "卖压" in feature.order_pressure:
        score -= 8
    if feature.price < feature.ma20:
        score -= 8
    return _clamp(score)


def _chip_position_score_current(feature: FeatureSnapshot, chip: ChipAnalysis | None) -> int:
    if not chip or chip.center_price <= 0:
        if feature.resistance and feature.price >= feature.resistance * 0.99:
            return 54
        if feature.support and feature.price <= feature.support * 1.03:
            return 48
        return 52
    distance = pct_change(feature.price, chip.center_price)
    score = 58
    if -3 <= distance <= 8:
        score += 16
    elif 8 < distance <= 16:
        score += 4
    elif distance > 16:
        score -= 14
    elif distance < -8:
        score -= 12
    score += round((chip.concentration - 50) * 0.22)
    return _clamp(score)


def _chip_position_value(feature: FeatureSnapshot, chip: ChipAnalysis | None) -> str:
    if not chip:
        return f"现价 {feature.price:.2f} / 支撑 {feature.support:.2f} / 压力 {feature.resistance:.2f}"
    return f"现价较成本中枢 {pct_change(feature.price, chip.center_price):.2f}% / 集中度 {chip.concentration}"


def _chip_position_evidence(feature: FeatureSnapshot, chip: ChipAnalysis | None) -> list[str]:
    if not chip:
        return [f"支撑位 {feature.support:.2f}，压力位 {feature.resistance:.2f}。"]
    evidence = [chip.summary]
    if chip.support_bands:
        band = chip.support_bands[0]
        evidence.append(f"最近支撑筹码区 {band.low:.2f}-{band.high:.2f}，占比 {band.share:.1f}%。")
    if chip.pressure_bands:
        band = chip.pressure_bands[0]
        evidence.append(f"最近压力筹码区 {band.low:.2f}-{band.high:.2f}，占比 {band.share:.1f}%。")
    return evidence


def _factor_direction(score: int) -> str:
    if score >= 58:
        return "正向"
    if score <= 45:
        return "负向"
    return "中性"


def _factor_score_impact(factor: StandardFactor) -> int:
    base = round((factor.score - 50) / 2)
    calibration = factor.calibration
    if not calibration or calibration.sample_count <= 0:
        return base
    if calibration.expected_level in {"较强", "偏正"}:
        return base + min(4, round((calibration.stability_score - 50) / 18))
    if calibration.expected_level in {"偏弱", "风险"}:
        return base - min(4, round((50 - calibration.stability_score) / 16))
    return base + min(2, round((calibration.stability_score - 50) / 28))


def _factor_calibration_impact(calibration: FactorCalibration) -> int:
    if calibration.sample_count < 5:
        return 0
    if calibration.expected_level in {"较强", "偏正"}:
        return min(4, round((calibration.stability_score - 50) / 16))
    if calibration.expected_level in {"偏弱", "风险"}:
        return -min(4, round((50 - calibration.stability_score) / 14))
    return min(2, round((calibration.stability_score - 50) / 24))


def _factor_lab_summary(total_score: int, confidence: int, positives: list[str], negatives: list[str]) -> str:
    positive_text = "、".join(positives) if positives else "暂无明确正向因子"
    negative_text = "、".join(negatives) if negatives else "暂无核心拖累因子"
    if total_score >= 65 and confidence >= 60:
        tone = "因子结构偏积极"
    elif total_score <= 48:
        tone = "因子结构偏谨慎"
    else:
        tone = "因子结构仍需确认"
    return f"{tone}：主要支撑来自{positive_text}；主要拖累来自{negative_text}。"


def _factor_alpha_reason(factor: StandardFactor) -> str:
    calibration = factor.calibration
    if calibration and calibration.sample_count > 0:
        bucket_text = _factor_bucket_alpha_text(factor)
        if calibration.sample_count < 5:
            return (
                f"{factor.value}；历史相似样本仅 {calibration.sample_count} 次，"
                f"样本偏少，暂不把胜率用于提高结论权重。{bucket_text}"
            )
        return (
            f"{factor.value}；历史相似样本 {calibration.sample_count} 次，"
            f"5日胜率 {calibration.win_rate:.1f}%，5日均值 {calibration.avg_forward_5d_return:.2f}%，"
            f"稳定性 {calibration.confidence_level}/{calibration.expected_level}。{bucket_text}"
        )
    return f"{factor.value}；历史校准为「{calibration.confidence_level if calibration else '暂无'}」。"


def _factor_missing_data(factor_lab: FactorLabReport) -> list[str]:
    return _dedupe([item for factor in factor_lab.factors for item in factor.missing_data])


def _factor_bucket_alpha_text(factor: StandardFactor) -> str:
    if not factor.calibration_buckets:
        return ""
    best = sorted(factor.calibration_buckets, key=lambda item: (item.sample_count >= 5, item.avg_forward_5d_return), reverse=True)[0]
    return f"分层校准中「{best.name}」样本 {best.sample_count} 个，5日均值 {best.avg_forward_5d_return:.2f}%。"


def _industry_regime_label(feature: FeatureSnapshot) -> str:
    if not feature.industry_name or feature.industry_change_pct is None:
        return "行业待确认"
    if feature.industry_change_pct >= 1.2:
        return "行业顺风"
    if feature.industry_change_pct <= -1.2:
        return "行业逆风"
    if feature.industry_change_pct > 0:
        return "行业小幅配合"
    return "行业震荡"


def _theme_level(score: int, industry_change: float | None, concept_avg: float | None) -> str:
    if industry_change is None and concept_avg is None:
        return "主题待确认"
    if score >= 72:
        return "主题顺风"
    if score >= 58:
        return "主题配合"
    if score <= 38:
        return "主题逆风"
    return "主题中性"


def _theme_style(stock_change: float, industry_change: float | None, concept_avg: float | None) -> str:
    background = max(value for value in [industry_change, concept_avg] if value is not None) if any(
        value is not None for value in [industry_change, concept_avg]
    ) else None
    if background is None:
        return "背景不足"
    if background >= 1 and stock_change >= background:
        return "个股强于主题"
    if background >= 1 and stock_change < background - 1:
        return "主题热个股弱"
    if background <= -1 and stock_change > background + 1:
        return "逆风抗跌"
    if background <= -1:
        return "主题拖累"
    return "主题震荡"


def _theme_relative_strength(relative_to_industry: float | None, relative_to_concepts: float | None) -> str:
    values = [value for value in (relative_to_industry, relative_to_concepts) if value is not None]
    if not values:
        return "强弱待确认"
    avg_gap = sum(values) / len(values)
    if avg_gap >= 2:
        return "显著强于背景"
    if avg_gap >= 0.8:
        return "强于背景"
    if avg_gap <= -2:
        return "显著弱于背景"
    if avg_gap <= -0.8:
        return "弱于背景"
    return "与背景同步"


def _theme_summary(
    analysis: AnalysisResult,
    level: str,
    style: str,
    industry: str,
    industry_change: float | None,
    concept_avg: float | None,
    strongest: StockConceptItem | None,
) -> str:
    parts = [f"{analysis.quote.name}当前属于「{style}」。"]
    if industry_change is not None:
        parts.append(f"行业「{industry}」涨跌幅 {industry_change:.2f}%。")
    if concept_avg is not None:
        concept_text = f"，最强概念为「{strongest.name}」{strongest.change_pct:.2f}%" if strongest else ""
        parts.append(f"概念平均涨跌幅 {concept_avg:.2f}%{concept_text}。")
    if level in {"主题顺风", "主题配合"}:
        parts.append("结论上可以提高趋势信号的解释权重，但买卖点仍要服从个股价位和风控。")
    elif level == "主题逆风":
        parts.append("结论上需要降低追涨冲动，优先观察个股能否持续强于背景。")
    else:
        parts.append("结论上主题背景暂不提供强支撑，仍以个股趋势、量能和风险收益比为主。")
    return "".join(parts)


def _theme_evidence(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    concepts: list[StockConceptItem],
    concept_avg: float | None,
    relative_to_industry: float | None,
    relative_to_concepts: float | None,
) -> list[str]:
    evidence = [
        f"个股涨跌幅 {analysis.quote.change_pct:.2f}%，趋势评分 {feature.trend_score}，龙头评分 {feature.leader_score}。",
    ]
    if analysis.industry_context:
        evidence.append(
            f"行业「{analysis.industry_context.name}」涨跌幅 {analysis.industry_context.change_pct:.2f}%，领涨股为{analysis.industry_context.leading_stock or '待确认'}。"
        )
    if concepts:
        top = sorted(concepts, key=lambda item: item.change_pct, reverse=True)[:3]
        evidence.append("相关概念：" + "、".join(f"{item.name}{item.change_pct:.2f}%" for item in top) + "。")
    if concept_avg is not None:
        evidence.append(f"概念平均涨跌幅 {concept_avg:.2f}%，用于判断主题是否配合个股走势。")
    if relative_to_industry is not None:
        evidence.append(f"个股相对行业强弱差 {relative_to_industry:.2f} 个百分点。")
    if relative_to_concepts is not None:
        evidence.append(f"个股相对概念均值强弱差 {relative_to_concepts:.2f} 个百分点。")
    return evidence[:6]


def _theme_opportunities(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    concepts: list[StockConceptItem],
    strongest: StockConceptItem | None,
    relative_to_industry: float | None,
    relative_to_concepts: float | None,
) -> list[str]:
    result: list[str] = []
    if relative_to_industry is not None and relative_to_industry >= 1:
        result.append("个股强于行业背景，说明资金认可度可能高于同板块平均。")
    if relative_to_concepts is not None and relative_to_concepts >= 1:
        result.append("个股强于相关概念均值，可作为龙头候选的辅助证据。")
    if strongest and strongest.change_pct >= 1.5:
        result.append(f"「{strongest.name}」概念表现活跃，若个股同步放量，短线弹性更容易被市场理解。")
    if feature.trend_score >= 65 and analysis.quote.change_pct > 0:
        result.append("趋势和主题背景同时偏正时，买点更适合等待回踩承接而不是追高。")
    return result or ["主题背景暂未给出额外加分，机会仍以个股买卖点和风险收益比为准。"]


def _theme_risks(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    concepts: list[StockConceptItem],
    industry_change: float | None,
    concept_avg: float | None,
    relative_to_industry: float | None,
    relative_to_concepts: float | None,
) -> list[str]:
    risks: list[str] = []
    if concept_avg is not None and concept_avg >= 1 and relative_to_concepts is not None and relative_to_concepts <= -1:
        risks.append("概念热但个股弱，容易出现跟风不足或冲高回落。")
    if industry_change is not None and industry_change <= -1 and analysis.quote.change_pct < 0:
        risks.append("行业逆风且个股同步走弱，短线需要降低信号权重。")
    if feature.leader_score < 50 and concepts:
        risks.append("概念归属存在，但龙头强度不足，暂不宜把题材当作核心买入理由。")
    if not concepts:
        risks.append("概念成分暂未确认，主题判断只能按行业和个股走势保守解释。")
    if feature.data_quality_score < 70:
        risks.append("数据质量不足，行业概念结论需要降权。")
    return risks or ["主题侧暂未发现明显拖累，仍需防范大盘和个股价位失效。"]


def _stock_state_label(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport | None,
) -> str:
    factor_score = factor_lab.total_score if factor_lab else feature.trend_score
    if feature.data_quality_score < 50:
        return "数据不足"
    if analysis.risk_level == "高风险" or insights.abnormal_events.level == "风险":
        return "风险优先"
    if feature.trend_score >= 65 and feature.fund_flow_score >= 58 and factor_score >= 60:
        return "右侧偏强"
    if feature.price <= feature.support * 1.03:
        return "支撑观察"
    if feature.price >= feature.resistance * 0.985:
        return "压力确认"
    return "震荡等待"


def _market_regime_label(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    industry_label: str,
    stock_state: str,
    factor_lab: FactorLabReport | None,
    breadth: MarketBreadthSnapshot | None = None,
) -> str:
    factor_score = factor_lab.total_score if factor_lab else feature.trend_score
    if feature.data_quality_score < 50:
        return "低置信环境"
    if stock_state == "风险优先" or analysis.risk_level == "高风险":
        return "风险环境"
    if breadth and breadth.score <= 35:
        return "市场偏冷环境"
    if factor_score >= 65 and industry_label in {"行业顺风", "行业小幅配合", "行业待确认"}:
        return "个股顺风环境"
    if breadth and breadth.score >= 65:
        return "市场偏暖环境"
    if "逆风" in industry_label:
        return "行业逆风环境"
    return "中性观察环境"


def _regime_risk_multiplier(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    industry_label: str,
    factor_lab: FactorLabReport | None,
    breadth: MarketBreadthSnapshot | None = None,
) -> float:
    multiplier = 1.0
    if feature.data_quality_score < 50:
        multiplier += 0.28
    elif feature.data_quality_score < 70:
        multiplier += 0.14
    if analysis.risk_level == "高风险":
        multiplier += 0.22
    elif analysis.risk_level == "中等风险":
        multiplier += 0.1
    if insights.abnormal_events.level == "风险":
        multiplier += 0.12
    if "逆风" in industry_label:
        multiplier += 0.1
    elif "顺风" in industry_label:
        multiplier -= 0.06
    if factor_lab:
        if factor_lab.calibration_sample_count >= 24 and factor_lab.positive_factor_count >= factor_lab.negative_factor_count + 2:
            multiplier -= 0.06
        if factor_lab.total_score >= 66 and factor_lab.calibrated_confidence >= 58:
            multiplier -= 0.08
        if factor_lab.total_score <= 45:
            multiplier += 0.12
        if factor_lab.negative_factor_count >= factor_lab.positive_factor_count + 2:
            multiplier += 0.05
    if breadth:
        multiplier += breadth.risk_adjustment
    return round(max(0.72, min(1.48, multiplier)), 2)


def _regime_suggestions(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    industry_label: str,
    stock_state: str,
    factor_lab: FactorLabReport | None,
    breadth: MarketBreadthSnapshot | None = None,
) -> list[str]:
    suggestions: list[str] = []
    if feature.data_quality_score < 70:
        suggestions.append("先恢复数据质量和多源一致性，再放大任何买卖点权重。")
    if stock_state == "右侧偏强":
        suggestions.append(f"只在回踩不破5日线 {feature.ma5:.2f} 或放量站稳压力位 {feature.resistance:.2f} 时提高积极度。")
    elif stock_state == "支撑观察":
        suggestions.append(f"靠近支撑 {feature.support:.2f} 时先看缩量止跌，不把下跌过程当成确定买点。")
    elif stock_state == "压力确认":
        suggestions.append(f"压力位 {feature.resistance:.2f} 附近优先看放量站稳，冲高回落则降级。")
    elif stock_state == "风险优先":
        suggestions.append("先处理硬风险，等放量下跌、异动风险或20日线破位修复后再评估。")
    else:
        suggestions.append("按支撑、压力和量能三件事等待确认，避免单日涨跌驱动判断。")
    if "逆风" in industry_label:
        suggestions.append("行业逆风时，个股信号需要更强的量价确认才能上调评级。")
    if breadth and breadth.score <= 40:
        suggestions.append("市场宽度偏冷时，优先看防守线，不把个别异动当成普遍回暖。")
    elif breadth and breadth.score >= 65:
        suggestions.append("市场宽度偏暖时，可优先跟踪放量站稳的右侧确认机会。")
    if factor_lab and factor_lab.top_negative:
        suggestions.append(f"优先跟踪拖累因子「{factor_lab.top_negative[0]}」是否修复。")
    if factor_lab and factor_lab.calibration_sample_count < 8:
        suggestions.append("因子历史样本仍偏少，建议把实验室分数当作低置信辅助项。")
    if factor_lab and factor_lab.positive_factor_count >= factor_lab.negative_factor_count + 2:
        suggestions.append("当前正向因子略占优，可以优先等价量和环境一起确认，而不是单看价格。")
    if not suggestions:
        suggestions.append(f"当前建议仍以「{analysis.action_advice.action}」为主，按条件清单执行。")
    return suggestions[:5]


def _diagnosis_factor_regime_text(
    factor_lab: FactorLabReport | None,
    market_regime: MarketRegimeReport | None,
) -> str:
    parts: list[str] = []
    if factor_lab:
        parts.append(
            f"因子实验室总分 {factor_lab.total_score}，校准置信度 {factor_lab.calibrated_confidence}%，"
            f"样本 {factor_lab.calibration_sample_count} 个，正向 {factor_lab.positive_factor_count}，负向 {factor_lab.negative_factor_count}。"
        )
    if market_regime:
        parts.append(f"环境判断为「{market_regime.market_label}/{market_regime.stock_state}」，风险倍率 {market_regime.risk_multiplier:.2f}。")
    return "".join(parts)


def _main_conflict_sentence(text: str) -> str:
    if text.startswith("当前主要矛盾"):
        return text if text.endswith("。") else f"{text}。"
    return f"当前主要矛盾是：{text}"


def _find_factor(factor_lab: FactorLabReport, factor_id: str) -> StandardFactor | None:
    return next((item for item in factor_lab.factors if item.id == factor_id), None)


def _validation_status(
    condition_met: bool,
    market_regime: MarketRegimeReport,
    factor: StandardFactor | None,
    timeframe: TimeframeAlignmentReport | None = None,
    *,
    reverse: bool = False,
) -> str:
    if market_regime.risk_multiplier >= 1.28:
        return "环境压制"
    if timeframe and timeframe.conflict_level in {"高冲突", "多周期偏弱"}:
        return "周期冲突降级"
    if factor and factor.calibration and factor.calibration.expected_level in {"风险", "偏弱"}:
        return "低置信观察"
    if reverse and not condition_met:
        return "风险触发"
    if timeframe and timeframe.conflict_level in {"中冲突", "多周期偏弱"} and condition_met:
        return "低置信观察"
    if condition_met:
        return "接近确认"
    return "等待确认"


def _validation_confidence(
    base: int,
    factor: StandardFactor | None,
    market_regime: MarketRegimeReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> int:
    score = base
    if factor:
        score = round(score * 0.45 + factor.score * 0.25 + (factor.calibration.stability_score if factor.calibration else 45) * 0.3)
    score += market_regime.confidence_adjustment
    if timeframe and timeframe.conflict_level in {"高冲突", "多周期偏弱"}:
        score -= 12
    elif timeframe and timeframe.conflict_level == "中冲突":
        score -= 6
    return _clamp(score)


def _factor_reference(factor: StandardFactor | None) -> str:
    if not factor or not factor.calibration:
        return "暂无可用历史参考。"
    calibration = factor.calibration
    if calibration.sample_count <= 0:
        return calibration.note
    return (
        f"历史相似样本 {calibration.sample_count} 个，5日胜率 {calibration.win_rate:.1f}%，"
        f"平均5日 {calibration.avg_forward_5d_return:.2f}%，最大不利 {calibration.max_adverse_return:.2f}%。"
    )


def _validation_overall_status(
    items: list[SignalValidationItem],
    market_regime: MarketRegimeReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> str:
    if timeframe and timeframe.conflict_level in {"高冲突", "多周期偏弱"}:
        return "风险优先"
    if market_regime.risk_multiplier >= 1.28 or any(item.status == "风险触发" for item in items):
        return "风险优先"
    confirmed = sum(1 for item in items if item.status == "接近确认")
    if timeframe and timeframe.conflict_level == "中冲突" and confirmed > 0:
        return "等待二次确认"
    if confirmed >= 2 and market_regime.risk_multiplier <= 1.08:
        return "条件较好"
    if confirmed >= 1:
        return "等待二次确认"
    return "观察为主"


def _validation_summary(
    overall_status: str,
    items: list[SignalValidationItem],
    market_regime: MarketRegimeReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> str:
    confirmed = [item.name for item in items if item.status == "接近确认"]
    risk = [item.name for item in items if item.status in {"风险触发", "环境压制", "低置信观察", "周期冲突降级"}]
    confirmed_text = "、".join(confirmed) if confirmed else "暂无接近确认的信号"
    risk_text = "、".join(risk) if risk else "暂无高优先级风险验证项"
    timeframe_text = f"；多周期为「{timeframe.conflict_level}」" if timeframe else ""
    return f"{overall_status}：接近确认的是{confirmed_text}；需要防守的是{risk_text}；环境风险倍率 {market_regime.risk_multiplier:.2f}{timeframe_text}。"


def _upside_target(feature: FeatureSnapshot, factor_lab: FactorLabReport) -> float:
    volatility_target = feature.price + max(feature.atr14 * 1.35, feature.price * 0.018)
    base_target = max(feature.resistance, volatility_target, feature.price * 1.025)
    if factor_lab.total_score >= 65 and factor_lab.positive_factor_count >= factor_lab.negative_factor_count + 1:
        return max(base_target, feature.price + max(feature.atr14 * 2.1, feature.price * 0.04))
    if factor_lab.total_score <= 45:
        return min(base_target, feature.price + max(feature.atr14 * 1.1, feature.price * 0.022))
    return base_target


def _downside_stop(feature: FeatureSnapshot, market_regime: MarketRegimeReport) -> float:
    price = feature.price
    if price <= 0:
        return 0
    structural_candidates = [item for item in [feature.support, feature.ma20] if item and item > 0]
    structural_stop = min(structural_candidates) if structural_candidates else price * 0.97
    atr_buffer = max(feature.atr14 * 1.15, price * 0.018)
    volatility_stop = price - atr_buffer
    raw_stop = min(structural_stop, volatility_stop)
    min_loss_pct = 0.018
    max_loss_pct = 0.075 if market_regime.risk_multiplier < 1.18 else 0.055
    if feature.volatility_pct >= 4:
        max_loss_pct += 0.012
    if feature.atr_pct >= 3.2:
        max_loss_pct += 0.01
    lower_bound = price * (1 - max_loss_pct)
    upper_bound = price * (1 - min_loss_pct)
    return min(max(raw_stop, lower_bound), upper_bound)


def _risk_reward_rating(
    ratio: float,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    timeframe: TimeframeAlignmentReport | None = None,
) -> str:
    if timeframe and timeframe.conflict_level == "高冲突":
        return "周期冲突"
    if timeframe and timeframe.conflict_level in {"中冲突", "多周期偏弱"} and ratio < 1.35:
        return "周期冲突"
    if market_regime.risk_multiplier >= 1.28 or validation.overall_status == "风险优先":
        return "风险优先"
    if ratio >= 1.8 and factor_lab.total_score >= 58 and validation.overall_status in {"条件较好", "等待二次确认"} and market_regime.breadth_score >= 42:
        return "性价比较好"
    if ratio >= 1.55 and timeframe and timeframe.conflict_level in {"中冲突", "多周期偏弱"}:
        return "等待确认"
    if ratio >= 1.2:
        return "性价比一般"
    return "性价比不足"


def _risk_reward_summary(
    rating: str,
    ratio: float,
    upside_pct: float,
    downside_pct: float,
    market_regime: MarketRegimeReport,
    timeframe: TimeframeAlignmentReport | None = None,
    feature: FeatureSnapshot | None = None,
) -> str:
    timeframe_text = f"多周期「{timeframe.alignment_label}」；" if timeframe else ""
    volatility_text = f"ATR {feature.atr_pct:.2f}%、20日波动 {feature.volatility_pct:.2f}%；" if feature else ""
    return f"{rating}：{timeframe_text}{volatility_text}上方预估空间 {upside_pct:.2f}%，下方防守距离 {downside_pct:.2f}%，收益风险比 {ratio:.2f}；环境风险倍率 {market_regime.risk_multiplier:.2f}。"


def _scenario_plans(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    upside_target: float,
    downside_stop: float,
) -> list[ScenarioPlan]:
    positive_probability = _clamp(round(32 + factor_lab.total_score * 0.25 + max(0, 1.1 - market_regime.risk_multiplier) * 18))
    risk_probability = _clamp(round(28 + market_regime.risk_multiplier * 18 + max(0, 50 - factor_lab.total_score) * 0.25))
    neutral_probability = max(10, 100 - positive_probability - risk_probability)
    total = positive_probability + neutral_probability + risk_probability
    positive_probability = round(positive_probability / total * 100)
    risk_probability = round(risk_probability / total * 100)
    neutral_probability = max(0, 100 - positive_probability - risk_probability)
    return [
        ScenarioPlan(
            name="积极路径",
            probability=positive_probability,
            trigger=f"放量站稳压力位 {feature.resistance:.2f}，且验证状态维持在「{validation.overall_status}」或更好。",
            expected_move=f"先看 {upside_target:.2f} 附近，若继续放量再重新评估。",
            response="只在确认后提高关注度，避免盘中追高。",
            invalidation=f"突破后跌回 {feature.resistance:.2f} 下方。",
        ),
        ScenarioPlan(
            name="震荡路径",
            probability=neutral_probability,
            trigger=f"价格继续在 {feature.support:.2f} 到 {feature.resistance:.2f} 区间内波动。",
            expected_move="以支撑、压力和量能变化为主，不提前给方向结论。",
            response="适合观察或仅底仓做T，新增动作等待确认。",
            invalidation="区间被放量跌破或放量突破。",
        ),
        ScenarioPlan(
            name="防守路径",
            probability=risk_probability,
            trigger=f"有效跌破 {downside_stop:.2f}，或20日线 {feature.ma20:.2f} 下方不能修复。",
            expected_move="优先看风险释放，不急于判断反转。",
            response=f"维持「{analysis.action_advice.action}」口径，先处理风控线。",
            invalidation="重新站回5日线且量能、资金同步修复。",
        ),
    ]


def _timeframe_trend(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    name: str,
    window_days: int,
) -> TimeframeTrend:
    rows = analysis.klines[-window_days:] if len(analysis.klines) >= window_days else analysis.klines[:]
    if len(rows) < 5:
        return TimeframeTrend(
            name=name,
            window_days=len(rows),
            score=50,
            label="样本不足",
            return_pct=0,
            max_drawdown_pct=0,
            above_ma=False,
            ma_value=0,
            evidence=["K线样本不足，暂按中性处理。"],
        )
    start = rows[0].close
    latest = feature.price
    return_pct = pct_change(latest, start)
    ma_window = min(20, len(rows))
    ma_value = sum(item.close for item in rows[-ma_window:]) / ma_window
    closes = [item.close for item in rows]
    drawdown = max_drawdown(closes) if closes else 0
    score = 50
    score += 16 if latest >= ma_value else -14
    score += 12 if return_pct > 5 else 6 if return_pct > 1 else -10 if return_pct < -5 else -4 if return_pct < -1 else 0
    score += -8 if drawdown < -12 else -4 if drawdown < -7 else 3
    if name == "短线":
        score = round(score * 0.55 + feature.trend_score * 0.45)
    score = _clamp(score)
    return TimeframeTrend(
        name=name,
        window_days=len(rows),
        score=score,
        label=_score_level(score),
        return_pct=round(return_pct, 2),
        max_drawdown_pct=round(drawdown, 2),
        above_ma=latest >= ma_value,
        ma_value=round(ma_value, 2),
        evidence=[
            f"区间涨跌幅 {return_pct:.2f}%。",
            f"现价 {'高于' if latest >= ma_value else '低于'} {ma_window}日均线 {ma_value:.2f}。",
            f"区间最大回撤 {drawdown:.2f}%。",
        ],
    )


def _timeframe_alignment_score(frames: list[TimeframeTrend], factor_lab: FactorLabReport) -> int:
    weights = {"短线": 0.45, "波段": 0.35, "中期": 0.2}
    total_weight = sum(weights.get(item.name, 0.25) for item in frames) or 1
    raw = sum(item.score * weights.get(item.name, 0.25) for item in frames) / total_weight
    if factor_lab.total_score >= 60:
        raw += 4
    if factor_lab.total_score <= 45:
        raw -= 5
    return _clamp(round(raw))


def _timeframe_conflict_level(frames: list[TimeframeTrend]) -> str:
    scores = [item.score for item in frames]
    if not scores:
        return "待确认"
    if max(scores) - min(scores) >= 35:
        return "高冲突"
    if any(score >= 62 for score in scores) and any(score <= 45 for score in scores):
        return "中冲突"
    if all(score >= 55 for score in scores):
        return "多周期顺向"
    if all(score <= 48 for score in scores):
        return "多周期偏弱"
    return "轻微分歧"


def _timeframe_alignment_label(score: int, conflict_level: str) -> str:
    if conflict_level == "高冲突":
        return "周期冲突明显"
    if score >= 65 and conflict_level == "多周期顺向":
        return "多周期共振"
    if score <= 45:
        return "多周期偏弱"
    return "周期仍需确认"


def _timeframe_summary(label: str, conflict_level: str, frames: list[TimeframeTrend]) -> str:
    frame_text = "；".join(f"{item.name}{item.score}分/{item.label}" for item in frames)
    return f"{label}：{frame_text}。冲突级别为「{conflict_level}」。"


def _timeframe_suggestions(frames: list[TimeframeTrend], conflict_level: str) -> list[str]:
    suggestions: list[str] = []
    weak_frames = [item.name for item in frames if item.score <= 45]
    strong_frames = [item.name for item in frames if item.score >= 62]
    if conflict_level in {"高冲突", "中冲突"}:
        suggestions.append("周期冲突时降低信号级别，先等弱周期修复。")
    if weak_frames:
        suggestions.append(f"重点观察{'、'.join(weak_frames)}周期能否重新站回均线。")
    if strong_frames:
        suggestions.append(f"{'、'.join(strong_frames)}周期相对占优，可作为确认后的辅助支撑。")
    if not suggestions:
        suggestions.append("多周期没有明显共振，继续按支撑、压力和量能等待确认。")
    return suggestions[:4]


def _factor_confirmation_text(factor_lab: FactorLabReport) -> str:
    main = factor_lab.top_positive[0] if factor_lab.top_positive else "正向因子"
    if factor_lab.calibration_sample_count >= 20:
        return f"因子实验室由「{main}」提供支撑，校准样本 {factor_lab.calibration_sample_count} 个，置信度 {factor_lab.calibrated_confidence}%。"
    return f"因子实验室出现「{main}」支撑，但样本只有 {factor_lab.calibration_sample_count} 个，仍需价量确认。"


def _factor_risk_text(factor_lab: FactorLabReport) -> str:
    main = factor_lab.top_negative[0] if factor_lab.top_negative else "负向因子"
    if factor_lab.negative_factor_count >= factor_lab.positive_factor_count:
        return f"因子实验室负向因子不少，尤其要看「{main}」是否修复。"
    return f"虽然整体不完全悲观，但「{main}」仍是当前拖累项。"


def _alpha_verdict(
    feature: FeatureSnapshot,
    positives: list[AlphaEvidencePoint],
    negatives: list[AlphaEvidencePoint],
    market_regime: MarketRegimeReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
    risk_reward: RiskRewardReport | None = None,
) -> str:
    positive_power = sum(item.impact for item in positives)
    negative_power = abs(sum(item.impact for item in negatives))
    if feature.data_quality_score < 50:
        return "暂停主动判断"
    if timeframe and timeframe.conflict_level in {"高冲突", "多周期偏弱"}:
        return "周期冲突"
    if risk_reward and risk_reward.rating in {"风险优先", "周期冲突"}:
        return "环境风险压制"
    if market_regime and market_regime.risk_multiplier >= 1.25 and negative_power >= positive_power * 0.8:
        return "环境风险压制"
    if positive_power >= negative_power + 10 and positive_power >= 18:
        return "积极证据占优"
    if negative_power > positive_power + 12:
        return "风险证据占优"
    return "等待确认"


def _alpha_summary(
    feature: FeatureSnapshot,
    positives: list[AlphaEvidencePoint],
    negatives: list[AlphaEvidencePoint],
    missing_data: list[str],
    confidence: int,
) -> str:
    top_positive = positives[0].title if positives else "暂无核心加分项"
    top_negative = negatives[0].title if negatives else "暂无核心风险项"
    missing_text = f"，但缺少{missing_data[0]}等数据" if missing_data else ""
    return f"核心加分来自「{top_positive}」，核心风险来自「{top_negative}」，综合置信度 {confidence}%{missing_text}。"


def _diagnosis_headline(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    alpha: AlphaEvidenceReport,
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    validation: SignalValidationReport | None = None,
    risk_reward: RiskRewardReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> str:
    if feature.data_quality_score < 50:
        return "数据质量不足，先暂停主动买卖判断"
    if timeframe and timeframe.conflict_level == "高冲突":
        return "多周期冲突明显，先收缩判断"
    if timeframe and timeframe.conflict_level == "多周期偏弱":
        return "多周期整体偏弱，先守风控线"
    if risk_reward and risk_reward.rating in {"风险优先", "周期冲突"}:
        return "风险收益不占优，先守风控线"
    if market_regime and market_regime.risk_multiplier >= 1.25:
        return "环境风险偏高，先缩小判断半径"
    if validation and validation.overall_status == "风险优先":
        return "验证闭环偏防守，先守风控线"
    if analysis.risk_level == "高风险" or alpha.verdict == "风险证据占优":
        return "风险信号优先，先守风控线"
    if factor_lab and factor_lab.total_score >= 68 and factor_lab.calibrated_confidence >= 62:
        return "因子和证据偏积极，等待价量确认"
    if alpha.verdict == "积极证据占优":
        return "趋势和证据偏积极，等待价量确认"
    return "信号仍需确认，按关键价位观察"


def _final_diagnosis_action(
    analysis: AnalysisResult,
    alpha: AlphaEvidenceReport,
    validation: SignalValidationReport | None,
    risk_reward: RiskRewardReport | None,
    timeframe: TimeframeAlignmentReport | None,
    market_regime: MarketRegimeReport | None,
) -> str:
    base_action = analysis.action_advice.action
    if analysis.data_quality.score < 50:
        return "控制风险"
    if timeframe and timeframe.conflict_level == "高冲突":
        return "控制风险"
    if risk_reward and risk_reward.rating in {"风险优先", "周期冲突"}:
        return "控制风险"
    if validation and validation.overall_status == "风险优先":
        return "控制风险"
    if timeframe and timeframe.conflict_level == "多周期偏弱":
        if (
            risk_reward
            and risk_reward.reward_risk_ratio >= 1.35
            and validation
            and validation.overall_status in {"等待二次确认", "观察为主"}
            and alpha.verdict != "风险证据占优"
        ):
            return "等待确认"
        return "控制风险"
    if timeframe and timeframe.conflict_level == "中冲突":
        return "等待确认"
    if market_regime and market_regime.risk_multiplier >= 1.25:
        return "轻仓观察"
    if risk_reward and risk_reward.rating == "等待确认":
        return "等待确认"
    if alpha.verdict == "积极证据占优" and base_action in {"回踩关注", "持有观察"}:
        return "积极关注"
    if base_action == "等待信号":
        return "谨慎观察"
    if base_action == "持有观察":
        return "谨慎观察"
    return base_action


def _diagnosis_extra_text(
    validation: SignalValidationReport | None,
    risk_reward: RiskRewardReport | None,
    timeframe: TimeframeAlignmentReport | None,
) -> str:
    parts: list[str] = []
    if validation:
        parts.append(f"验证闭环为「{validation.overall_status}」。")
    if risk_reward:
        parts.append(f"风险收益结论为「{risk_reward.rating}」，收益风险比 {risk_reward.reward_risk_ratio:.2f}。")
    if timeframe:
        parts.append(f"多周期为「{timeframe.alignment_label} / {timeframe.conflict_level}」。")
    return "".join(parts)


def _trend_proxy_score_at(rows: list, index: int) -> float:
    if index < 20:
        return 50
    current = rows[index]
    ma5 = _window_average_close(rows, index, 5)
    ma10 = _window_average_close(rows, index, 10)
    ma20 = _window_average_close(rows, index, 20)
    prev_ma5 = _window_average_close(rows, index - 5, 5) if index >= 25 else ma5
    score = 50
    score += 12 if current.close > ma5 else -8
    score += 10 if ma5 > ma10 else -6
    score += 10 if ma10 > ma20 else -8
    score += 7 if ma5 >= prev_ma5 else -5
    high_20 = max(item.high for item in rows[index - 19 : index + 1])
    low_20 = min(item.low for item in rows[index - 19 : index + 1])
    if current.close >= high_20 * 0.985:
        score += 10
    if current.close <= low_20 * 1.03:
        score -= 10
    return _clamp(score)


def _volume_proxy_score_at(rows: list, index: int) -> float:
    if index < 20:
        return 50
    current = rows[index]
    prev = rows[index - 1]
    ratio = _volume_ratio_at(rows, index)
    change = pct_change(current.close, prev.close)
    score = 52
    if change > 0 and ratio >= 1.2:
        score += 16 + min(12, round((ratio - 1.2) * 10))
    elif change < 0 and ratio >= 1.2:
        score -= 18 + min(12, round((ratio - 1.2) * 10))
    elif ratio < 0.7 and abs(change) >= 2:
        score -= 8
    elif 0.85 <= ratio <= 1.25:
        score += 4
    return _clamp(score)


def _risk_proxy_score_at(rows: list, index: int) -> float:
    if index < 20:
        return 58
    current = rows[index]
    prev = rows[index - 1]
    ma20 = _window_average_close(rows, index, 20)
    ratio = _volume_ratio_at(rows, index)
    change = pct_change(current.close, prev.close)
    amplitude = pct_change(current.high, current.low) if current.low else 0
    score = 72
    if current.close < ma20:
        score -= 16
    if change <= -3:
        score -= 14
    if change < 0 and ratio >= 1.5:
        score -= 12
    if amplitude >= 6:
        score -= 6
    if current.close > ma20 and change >= 1:
        score += 5
    return _clamp(score)


def _fund_flow_proxy_score_at(rows: list, index: int) -> float:
    if index < 10:
        return 50
    recent = rows[index - 4 : index + 1]
    up_amount = sum(item.close * item.volume for item in recent if item.close >= item.open)
    down_amount = sum(item.close * item.volume for item in recent if item.close < item.open)
    total = up_amount + down_amount
    if total <= 0:
        return 50
    pressure = (up_amount - down_amount) / total
    continuity = sum(1 for item in recent if item.close >= item.open)
    return _clamp(round(50 + pressure * 32 + (continuity - 2.5) * 4))


def _chip_position_score_at(rows: list, index: int) -> float:
    if index < 30:
        return 50
    window = rows[max(0, index - 59) : index + 1]
    total_volume = sum(item.volume for item in window) or 1
    center = sum(((item.high + item.low + item.close) / 3) * item.volume for item in window) / total_volume
    distance = pct_change(rows[index].close, center)
    score = 58
    if -3 <= distance <= 8:
        score += 16
    elif 8 < distance <= 16:
        score += 4
    elif distance > 16:
        score -= 14
    elif distance < -8:
        score -= 12
    return _clamp(score)


def _leadership_proxy_score_at(rows: list, index: int) -> float:
    if index < 20:
        return 45
    current = rows[index]
    prev = rows[index - 1]
    trend = _trend_proxy_score_at(rows, index)
    ratio = _volume_ratio_at(rows, index)
    change = pct_change(current.close, prev.close)
    score = 40 + round((trend - 50) * 0.45)
    score += 12 if change >= 5 else 7 if change >= 2 else -6 if change <= -3 else 0
    score += 8 if ratio >= 1.4 and change > 0 else -6 if ratio >= 1.4 and change < 0 else 0
    return _clamp(score)


def _trend_trigger(rows: list, index: int, current_score: float) -> bool:
    score = _trend_proxy_score_at(rows, index)
    if current_score >= 58:
        return score >= max(58, current_score - 8)
    if current_score <= 48:
        return score <= min(48, current_score + 8)
    return 45 < score < 62


def _volume_trigger(rows: list, index: int, current_score: float) -> bool:
    score = _volume_proxy_score_at(rows, index)
    if current_score >= 58 or current_score <= 45:
        return abs(score - current_score) <= 10 and (score >= 58 or score <= 45)
    return 45 < score < 60


def _risk_trigger(rows: list, index: int, current_score: float) -> bool:
    score = _risk_proxy_score_at(rows, index)
    if current_score <= 50:
        return score <= max(48, current_score + 8)
    if current_score >= 60:
        return score >= min(65, current_score - 8)
    return 48 < score < 65


def _fund_flow_trigger(rows: list, index: int, current_score: float) -> bool:
    score = _fund_flow_proxy_score_at(rows, index)
    if current_score >= 58 or current_score <= 45:
        return abs(score - current_score) <= 12 and (score >= 58 or score <= 45)
    return 45 < score < 60


def _chip_trigger(rows: list, index: int, current_score: float) -> bool:
    score = _chip_position_score_at(rows, index)
    return abs(score - current_score) <= 12


def _leadership_trigger(rows: list, index: int, current_score: float) -> bool:
    score = _leadership_proxy_score_at(rows, index)
    if current_score >= 58:
        return score >= max(58, current_score - 10)
    if current_score <= 48:
        return score <= min(48, current_score + 10)
    return 45 < score < 60


def _window_average_close(rows: list, index: int, window: int) -> float:
    if index < 0:
        return 0
    start = max(0, index - window + 1)
    values = [item.close for item in rows[start : index + 1]]
    return sum(values) / len(values) if values else 0


def _volume_ratio_at(rows: list, index: int, recent_window: int = 5, base_window: int = 20) -> float:
    if index < recent_window:
        return 1.0
    recent_start = max(0, index - recent_window + 1)
    base_start = max(0, index - base_window + 1)
    recent = [item.volume for item in rows[recent_start : index + 1] if item.volume > 0]
    base = [item.volume for item in rows[base_start : index + 1] if item.volume > 0]
    if not recent or not base:
        return 1.0
    base_avg = sum(base) / len(base)
    if base_avg <= 0:
        return 1.0
    return (sum(recent) / len(recent)) / base_avg


def _calibration_confidence_level(sample_count: int, win_rate: float, avg_return: float) -> str:
    if sample_count >= 12 and win_rate >= 58 and avg_return > 0.8:
        return "较高"
    if sample_count >= 8 and win_rate >= 52 and avg_return >= 0:
        return "中等"
    if sample_count < 5:
        return "偏低"
    if win_rate < 45 or avg_return < -0.5:
        return "偏弱"
    return "观察"


def _calibration_expected_level(direction: str, win_rate: float, avg_5d: float, avg_10d: float) -> str:
    effective_5d = -avg_5d if direction == "反向" else avg_5d
    effective_10d = -avg_10d if direction == "反向" else avg_10d
    if win_rate >= 58 and effective_5d > 1 and effective_10d >= 0:
        return "较强"
    if win_rate >= 52 and effective_5d >= 0:
        return "偏正"
    if win_rate < 42 and effective_5d < -0.8:
        return "风险"
    if effective_5d < -0.3 or effective_10d < -0.6:
        return "偏弱"
    return "观察"


def _calibration_note(name: str, sample_count: int, win_rate: float, avg_return: float) -> str:
    if sample_count < 5:
        return f"「{name}」相似样本只有 {sample_count} 次，暂不宜提高权重。"
    if win_rate >= 58 and avg_return > 0:
        return f"「{name}」在该股历史中表现偏正，可作为辅助确认。"
    if win_rate < 45 or avg_return < 0:
        return f"「{name}」历史表现不稳，当前触发时要降低信号权重。"
    return f"「{name}」历史表现中性，适合与价位、量能一起确认。"


def _volume_price_bins(rows, bucket_count: int) -> list[tuple[float, float, float]]:
    low = min(item.low for item in rows if item.low > 0)
    high = max(item.high for item in rows if item.high > 0)
    span = max(0.01, high - low)
    bucket_size = span / bucket_count
    buckets = [0.0 for _ in range(bucket_count)]
    for item in rows:
        typical_price = (item.high + item.low + item.close) / 3
        index = min(bucket_count - 1, max(0, int((typical_price - low) / bucket_size)))
        buckets[index] += item.volume
    return [
        (round(low + index * bucket_size, 2), round(low + (index + 1) * bucket_size, 2), volume)
        for index, volume in enumerate(buckets)
        if volume > 0
    ]


def _chip_concentration(bins: list[tuple[float, float, float]], center: float, total_volume: float) -> int:
    if not bins:
        return 0
    near_volume = sum(volume for low, high, volume in bins if low <= center <= high or abs(((low + high) / 2 - center) / center) <= 0.05)
    return _clamp(round(35 + near_volume / total_volume * 65))


def _detect_replay_pattern(rows, index: int) -> str | None:
    current = rows[index]
    prev = rows[index - 1]
    recent_20 = rows[index - 20 : index + 1]
    recent_5 = rows[index - 5 : index]
    avg_volume_5 = sum(item.volume for item in recent_5) / len(recent_5) if recent_5 else 0
    high_20 = max(item.high for item in recent_20[:-1])
    low_20 = min(item.low for item in recent_20[:-1])
    change = pct_change(current.close, prev.close)
    volume_ratio = current.volume / avg_volume_5 if avg_volume_5 else 1
    if current.close >= high_20 * 0.995 and change > 1 and volume_ratio >= 1.3:
        return "放量突破"
    if current.low <= low_20 * 1.03 and current.close > current.open and volume_ratio >= 1.05:
        return "支撑反弹"
    if change <= -3 and volume_ratio >= 1.4:
        return "放量回撤"
    return None


def _replay_stats(cases: list[ReplayCase]) -> list[ReplayPatternStat]:
    grouped: dict[str, list[ReplayCase]] = defaultdict(list)
    for item in cases:
        grouped[item.pattern].append(item)
    stats: list[ReplayPatternStat] = []
    for pattern, rows in grouped.items():
        valid_returns = [item.forward_5d_return for item in rows if item.forward_5d_return is not None]
        avg_return = sum(valid_returns) / len(valid_returns) if valid_returns else 0
        win_rate = sum(1 for item in rows if (item.forward_5d_return or 0) > 0) / len(rows) * 100
        stats.append(
            ReplayPatternStat(
                pattern=pattern,
                sample_count=len(rows),
                win_rate=round(win_rate, 1),
                avg_forward_5d_return=round(avg_return, 2),
                note=_replay_pattern_note(pattern, len(rows), win_rate, avg_return),
            )
        )
    return sorted(stats, key=lambda item: (item.sample_count, item.win_rate), reverse=True)


def _replay_case_note(pattern: str, outcome: str) -> str:
    if outcome == "有效":
        return f"{pattern}后5日表现偏正，后续可复核当时量能和关键价位。"
    if outcome == "风险":
        return f"{pattern}后出现回撤，说明该信号在本股上需要更严格确认。"
    return f"{pattern}后进入震荡，适合作为等待确认案例。"


def _replay_pattern_note(pattern: str, sample_count: int, win_rate: float, avg_return: float) -> str:
    if sample_count < 5:
        return f"{pattern}样本只有 {sample_count} 次，只适合看案例，不宜提高权重。"
    if win_rate >= 60 and avg_return > 1:
        return f"{pattern}在该股历史中相对有效，但仍需结合当前数据质量。"
    if win_rate < 45 or avg_return < 0:
        return f"{pattern}历史稳定性不足，触发时应降低信号权重。"
    return f"{pattern}历史表现中性，更适合当作辅助证据。"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _stock_question_topic(question: str) -> str:
    text = question.lower()
    if any(word in text for word in ("做t", "做 t", "t+0", "t0", "高抛低吸")) or ("高抛" in text and "低吸" in text):
        return "做T"
    if any(word in text for word in ("概念", "题材", "主题", "风口", "热点")):
        return "主题概念"
    if any(word in text for word in ("风险收益", "收益风险", "性价比", "赔率", "盈亏比", "空间够", "空间大", "值不值得")):
        return "风险收益"
    if any(word in text for word in ("风险", "止损", "跌破", "亏", "雷", "回撤", "危险")):
        return "风险"
    if any(word in text for word in ("买", "加仓", "进场", "入场", "低吸", "能不能上")):
        return "买点"
    if any(word in text for word in ("卖", "减仓", "离场", "止盈", "压力", "冲高", "高抛")):
        return "卖点"
    if any(word in text for word in ("同行", "行业", "板块", "龙头", "强不强", "排名")):
        return "同行龙头"
    if any(word in text for word in ("事件", "消息", "公告", "异动", "利好", "利空")):
        return "事件"
    if any(word in text for word in ("明天", "今天", "短线", "支撑", "压力", "怎么看")):
        return "短线观察"
    return "综合判断"


def _question_confidence(
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    market_regime: MarketRegimeReport,
    validation: SignalValidationReport,
    topic: str,
    theme_context: ThemeContextReport | None = None,
) -> int:
    confidence = min(diagnosis.confidence, analysis.signal_snapshot.confidence, analysis.data_quality.score)
    if market_regime.risk_multiplier >= 1.25:
        confidence -= 8
    if validation.overall_status == "风险优先":
        confidence -= 8
    if topic in {"事件", "同行龙头", "主题概念"} and analysis.data_quality.score < 82:
        confidence -= 5
    if topic == "主题概念":
        if not theme_context:
            confidence -= 10
        elif theme_context.missing_data:
            confidence -= min(12, 4 * len(theme_context.missing_data))
    return _bounded_int(confidence, 25, 92)


def _question_evidence(
    topic: str,
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    evidence_chain: EvidenceChainReport,
    risk_radar: RiskRadarReport,
    event_digest: EventDigestReport,
    peer_comparison: PeerComparisonReport,
    t_strategy: TStrategyAssistantReport,
    market_regime: MarketRegimeReport,
    risk_reward: RiskRewardReport,
    validation: SignalValidationReport,
    timeframe: TimeframeAlignmentReport,
    theme_context: ThemeContextReport | None = None,
) -> list[str]:
    base = [
        diagnosis.headline,
        f"当前价 {analysis.quote.price:.2f}，支撑 {analysis.support:.2f}，压力 {analysis.resistance:.2f}。",
        f"{market_regime.market_label}，风险倍率 {market_regime.risk_multiplier:.2f}。",
    ]
    if topic == "做T":
        t_plan_evidence = [item.reason for item in analysis.t_plan[:2]]
        return _dedupe([t_strategy.summary, *t_strategy.execution_steps[:2], *base, *t_plan_evidence])
    if topic == "风险":
        return _dedupe([risk_radar.summary, *risk_radar.top_risks, *diagnosis.hard_risks[:3], risk_reward.summary, timeframe.summary])
    if topic == "风险收益":
        scenario_text = [f"{item.name}：{item.trigger}；{item.expected_move}；应对：{item.response}" for item in risk_reward.scenarios[:3]]
        return _dedupe([
            risk_reward.summary,
            f"上方目标 {risk_reward.upside_target:.2f}（{risk_reward.upside_pct:.2f}%），下方防守 {risk_reward.downside_stop:.2f}（{risk_reward.downside_pct:.2f}%），收益风险比 {risk_reward.reward_risk_ratio:.2f}。",
            validation.summary,
            timeframe.summary,
            *scenario_text,
            *risk_reward.notes[:2],
        ])
    if topic == "买点":
        buy_evidence = [item.reason for item in analysis.buy_points[:3]]
        confirmations = [item.confirmation_condition for item in validation.items[:3]]
        return _dedupe([*base, risk_reward.summary, *evidence_chain.support[:3], *buy_evidence, *confirmations])
    if topic == "卖点":
        sell_evidence = [item.reason for item in analysis.sell_points[:3]]
        return _dedupe([*base, risk_reward.summary, *evidence_chain.opposition[:3], *sell_evidence, *risk_radar.top_risks[:2]])
    if topic == "同行龙头":
        return _dedupe([peer_comparison.summary, peer_comparison.valuation_position, peer_comparison.strength_position, *peer_comparison.metrics[:3], *peer_comparison.risks[:2]])
    if topic == "主题概念":
        if not theme_context:
            return _dedupe([*base, "主题概念报告暂不可用，先按行业、个股趋势和数据质量保守解释。"])
        concepts = [f"{item.name}{item.change_pct:.2f}%" for item in theme_context.concepts[:4]]
        return _dedupe([
            theme_context.summary,
            f"主题评分 {theme_context.score}，状态 {theme_context.level}，风格 {theme_context.style}，相对强弱 {theme_context.relative_strength}。",
            f"行业 {theme_context.industry}"
            + (f" {theme_context.industry_change_pct:.2f}%" if theme_context.industry_change_pct is not None else " 待确认")
            + "。",
            "相关概念：" + "、".join(concepts) + "。" if concepts else "相关概念待确认。",
            *theme_context.evidence[:4],
            *theme_context.risks[:2],
        ])
    if topic == "事件":
        return _dedupe([event_digest.summary, *event_digest.negative_events[:3], *event_digest.positive_events[:3], *event_digest.watch_events[:3], *event_digest.missing_data[:2]])
    if topic == "短线观察":
        return _dedupe([*base, timeframe.summary, validation.summary, *diagnosis.confirmation_signals[:3], *diagnosis.watch_focus[:2]])
    return _dedupe([*base, evidence_chain.summary, risk_reward.summary, validation.summary, timeframe.summary, *evidence_chain.support[:2], *evidence_chain.opposition[:2]])


def _question_actions(
    topic: str,
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    risk_radar: RiskRadarReport,
    t_strategy: TStrategyAssistantReport,
    market_regime: MarketRegimeReport,
    risk_reward: RiskRewardReport,
    validation: SignalValidationReport,
    theme_context: ThemeContextReport | None = None,
) -> list[str]:
    if topic == "做T":
        return [
            f"只在已有可卖底仓前提下执行，低吸参考 {t_strategy.low_zone}，高抛参考 {t_strategy.high_zone}。",
            *t_strategy.execution_steps,
            "若无法严格执行高抛低吸纪律，则宁可不做。",
        ]
    if topic == "风险":
        return [item.action for item in sorted(risk_radar.items, key=lambda row: row.score, reverse=True)[:4]]
    if topic == "风险收益":
        actions = [
            f"只有风险收益评级达到「性价比较好」或「性价比一般」，且验证状态不是「风险优先」时，才考虑观察级动作；当前为「{risk_reward.rating} / {validation.overall_status}」。",
            f"若价格贴近上方目标 {risk_reward.upside_target:.2f} 或收益风险比低于 1.2，不新增追高。",
            f"若跌近下方防守 {risk_reward.downside_stop:.2f}，先执行防守而不是补仓摊低。",
        ]
        return _dedupe([*actions, *[item.response for item in risk_reward.scenarios[:2]]])
    if topic == "买点":
        actions = [
            item.action_hint
            for item in validation.items
            if item.category == "买点" and item.status in {"接近确认", "等待确认", "低置信观察", "环境压制", "周期冲突降级"}
        ][:3]
        return _dedupe([
            f"只有站稳支撑 {analysis.support:.2f} 且不过度贴近压力 {analysis.resistance:.2f} 时，才考虑观察级动作。",
            *actions,
            "风险收益比没有修复前，不把反弹直接当买点。",
        ])
    if topic == "卖点":
        return _dedupe([
            f"接近压力 {analysis.resistance:.2f} 且量价乏力时优先保护利润。",
            f"跌破支撑 {analysis.support:.2f} 后不要用主观预期替代纪律。",
            *[item.action_hint for item in validation.items if item.status in {"风险触发", "周期冲突降级"}][:3],
        ])
    if topic == "同行龙头":
        return ["先确认强弱分位是否持续靠前，再看成交额是否同步放大。", "若同行更强而本股滞涨，不主动上调龙头判断。"]
    if topic == "主题概念":
        if not theme_context:
            return ["主题概念数据未确认前，不把题材当作买入理由。", "先看个股是否守住关键价位和量能确认。"]
        return _dedupe([
            *theme_context.opportunities[:3],
            *theme_context.risks[:2],
            "主题只作为解释背景，具体动作仍以支撑、压力、量能和失效条件为准。",
        ])
    if topic == "事件":
        return ["把事件作为结论修正项，不单独作为买卖依据。", "事件偏风险时，先等价格和量能验证风险是否消化。"]
    if topic == "短线观察":
        return _dedupe([
            f"明线看支撑 {analysis.support:.2f} 和压力 {analysis.resistance:.2f}。",
            *diagnosis.confirmation_signals[:3],
            f"环境风险倍率 {market_regime.risk_multiplier:.2f}，风险收益评级 {risk_reward.rating}。",
        ])
    return _dedupe([diagnosis.action, *diagnosis.watch_focus[:3], validation.summary])


def _question_invalidations(
    topic: str,
    analysis: AnalysisResult,
    diagnosis: StockDiagnosis,
    evidence_chain: EvidenceChainReport,
    risk_radar: RiskRadarReport,
    t_strategy: TStrategyAssistantReport,
    validation: SignalValidationReport,
    theme_context: ThemeContextReport | None = None,
) -> list[str]:
    if topic == "做T":
        return _dedupe([*t_strategy.stop_conditions, f"跌破支撑 {analysis.support:.2f} 或冲高回落放量。"])
    if topic in {"买点", "短线观察"}:
        return _dedupe([*evidence_chain.invalidations[:3], *diagnosis.hard_risks[:2], f"价格有效跌破 {analysis.support:.2f}。"])
    if topic == "卖点":
        return _dedupe([f"放量站稳压力 {analysis.resistance:.2f} 后，卖点需要重新评估。", *[item.confirmation_condition for item in validation.items[:2]]])
    if topic == "风险":
        return _dedupe([f"{item.name}：{item.action}" for item in risk_radar.items if item.score >= 42][:4])
    if topic == "风险收益":
        return _dedupe([
            "收益风险比跌破 1.2，或风险收益评级降为「性价比不足 / 风险优先」。",
            f"有效跌破支撑 {analysis.support:.2f}，说明下方风险开始兑现。",
            *[item.invalidation_condition for item in validation.items[:3]],
        ])
    if topic == "主题概念":
        theme_invalidations = [
            "概念热度转弱但个股仍无法放量走强，题材支撑需要降权。",
            "概念上涨只来自少数龙头，本股相对强弱转为落后时，不上调主题判断。",
            f"跌破关键支撑 {analysis.support:.2f} 后，题材解释不能替代价格纪律。",
        ]
        if theme_context and theme_context.missing_data:
            theme_invalidations.append("主题归属、行业涨跌或数据质量仍有缺口时，不能把题材作为核心依据。")
        return _dedupe([*theme_invalidations, *diagnosis.hard_risks[:2]])
    return _dedupe([*evidence_chain.invalidations[:4], *diagnosis.hard_risks[:2]])


def _question_conclusion(
    topic: str,
    diagnosis: StockDiagnosis,
    risk_radar: RiskRadarReport,
    t_strategy: TStrategyAssistantReport,
    peer_comparison: PeerComparisonReport,
    event_digest: EventDigestReport,
    risk_reward: RiskRewardReport,
    validation: SignalValidationReport,
    theme_context: ThemeContextReport | None = None,
) -> str:
    if topic == "做T":
        return f"{t_strategy.suitability}：{t_strategy.style}"
    if topic == "风险":
        return f"{risk_radar.overall_level}，优先看 {', '.join(item.split('：')[0] for item in risk_radar.top_risks[:2]) or '关键风险'}"
    if topic == "风险收益":
        return f"{risk_reward.rating}，收益风险比 {risk_reward.reward_risk_ratio:.2f}，验证状态「{validation.overall_status}」"
    if topic == "买点":
        return f"{diagnosis.action}，买点必须服从「{validation.overall_status}」和「{risk_reward.rating}」"
    if topic == "卖点":
        return f"以压力位和失效条件为先，当前总建议「{diagnosis.action}」"
    if topic == "同行龙头":
        return f"{peer_comparison.strength_position}，{peer_comparison.valuation_position}"
    if topic == "主题概念":
        if not theme_context:
            return "主题概念待确认，暂不提高结论权重"
        return f"{theme_context.level}，{theme_context.style}，{theme_context.relative_strength}，主题评分 {theme_context.score}"
    if topic == "事件":
        return event_digest.impact_label
    if topic == "短线观察":
        return f"短线先看确认，不抢结论；当前总建议「{diagnosis.action}」"
    return f"当前总建议「{diagnosis.action}」，风险收益评级「{risk_reward.rating}」"


def _question_answer_text(topic: str, analysis: AnalysisResult, diagnosis: StockDiagnosis, conclusion: str, actions: list[str], confidence: int) -> str:
    name = analysis.quote.name or analysis.quote.code
    if topic == "买点":
        prefix = f"{name}当前不能只按“想买”处理，系统结论是：{conclusion}。"
    elif topic == "卖点":
        prefix = f"{name}的卖点更适合按压力和失效条件处理，结论是：{conclusion}。"
    elif topic == "做T":
        prefix = f"{name}的做T判断是：{conclusion}。做T只服务于已有底仓降成本。"
    elif topic == "风险":
        prefix = f"{name}当前风险判断是：{conclusion}。"
    elif topic == "风险收益":
        prefix = f"{name}当前风险收益判断是：{conclusion}。"
    elif topic == "主题概念":
        prefix = f"{name}的题材背景判断是：{conclusion}。题材只解释背景，不能单独替代价格和风险收益。"
    elif topic == "同行龙头":
        prefix = f"{name}的同行强弱判断是：{conclusion}。"
    elif topic == "事件":
        prefix = f"{name}的事件影响判断是：{conclusion}。"
    elif topic == "短线观察":
        prefix = f"{name}的短线观察结论是：{conclusion}。"
    else:
        prefix = f"{name}这只个股的回答是：{conclusion}。"
    action_text = "；".join(actions[:3]) if actions else diagnosis.beginner_summary
    return f"{prefix} 我的建议是：{action_text} 这次回答置信度约 {confidence}%，需要随行情和数据质量动态更新。"


def _related_questions(topic: str) -> list[str]:
    questions = {
        "买点": ["明天重点看什么？", "跌破哪个位置结论失效？", "当前风险收益比够不够？"],
        "卖点": ["压力位附近怎么处理？", "什么情况可以继续观察？", "止损条件是什么？"],
        "做T": ["低吸区和高抛区在哪里？", "什么情况停止做T？", "没有底仓能不能做T？"],
        "风险": ["最大的风险是什么？", "哪些信号能解除风险？", "止损位置在哪里？"],
        "风险收益": ["当前风险收益比够不够？", "上方空间和下方防守在哪里？", "什么情况性价比会失效？"],
        "主题概念": ["它有哪些概念？", "题材热度能不能支撑走势？", "概念热但个股弱怎么办？"],
        "同行龙头": ["它相对同行强吗？", "估值在同行里贵不贵？", "行业里谁更强？"],
        "事件": ["近期事件偏利好还是利空？", "事件会不会改变买卖点？", "还缺哪些数据？"],
        "短线观察": ["明天重点看什么？", "支撑压力在哪里？", "能不能低吸？"],
    }
    return questions.get(topic, ["现在能不能买？", "风险在哪里？", "适不适合做T？"])


def _peer_position_label(percentile: float | None, prefix: str) -> str:
    if percentile is None:
        return f"{prefix}待确认"
    if percentile >= 75:
        return f"{prefix}相对靠前"
    if percentile <= 30:
        return f"{prefix}相对靠后"
    return f"{prefix}中等"


def _t_strategy_style(feature: FeatureSnapshot, market_regime: MarketRegimeReport) -> str:
    width_pct = (feature.resistance - feature.support) / feature.price * 100 if feature.price and feature.resistance > feature.support else 0
    if market_regime.risk_multiplier >= 1.25:
        return "风险防守型"
    if width_pct < max(1.2, feature.atr_pct * 0.7):
        return "窄幅等待型"
    if feature.trend_score >= 65 and feature.price >= feature.ma5:
        return "趋势滚动型"
    return "区间震荡型"


def _risk_radar_item(name: str, raw_score: float, reason: str, action: str) -> RiskRadarItem:
    score = _clamp(round(raw_score))
    level = "高" if score >= 68 else "中" if score >= 42 else "低"
    return RiskRadarItem(name=name, level=level, score=score, reason=reason, action=action)


def _score_level(score: int) -> str:
    if score >= 80:
        return "强"
    if score >= 65:
        return "偏强"
    if score >= 50:
        return "中性"
    if score >= 35:
        return "偏弱"
    return "弱"


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))


def _bounded_int(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))
