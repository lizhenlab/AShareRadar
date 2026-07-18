from __future__ import annotations

from collections.abc import Iterable

from app.models.schemas import (
    AlphaEvidenceReport,
    AnalysisResult,
    FactorLabReport,
    FeatureSnapshot,
    MarketRegimeReport,
    RiskRewardReport,
    SignalValidationReport,
    StockInsightBundle,
    TimeframeAlignmentReport,
)
from app.services.research_factors import _factor_confirmation_text, _factor_evidence_sufficiency, _factor_risk_text


RISK_REWARD_HARD_RISK_RATINGS = {"风险优先", "周期冲突", "性价比不足"}
TIMEFRAME_HARD_RISK_LEVELS = {"高冲突", "中冲突", "多周期偏弱"}


def build_confirmation_signals(
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> list[str]:
    return _dedupe(
        [
            *_trend_repair_confirmation(feature),
            *_base_confirmation_signals(feature),
            *_factor_confirmation(factor_lab),
            *_timeframe_confirmation(timeframe),
        ]
    )


def build_hard_risks(
    feature: FeatureSnapshot,
    insights: StockInsightBundle,
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    risk_reward: RiskRewardReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> list[str]:
    return _dedupe(
        [
            *_hard_risk_overlays(insights, market_regime, risk_reward, timeframe),
            *_base_hard_risks(feature),
            *_factor_risks(factor_lab),
        ]
    )


def build_watch_focus(
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    validation: SignalValidationReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> list[str]:
    return _dedupe(
        [
            *_base_watch_focus(),
            *_industry_watch_focus(feature),
            *_market_regime_watch_focus(market_regime),
            *_validation_watch_focus(validation),
            *_timeframe_watch_focus(timeframe),
            *_factor_watch_focus(factor_lab),
        ]
    )


def build_beginner_summary(analysis: AnalysisResult, final_action: str) -> str:
    return f"{analysis.quote.name}现在最重要的是先确认支撑和压力是否有效。当前建议「{final_action}」，不要只因为涨跌幅做决定。"


def build_professional_summary(
    analysis: AnalysisResult,
    insights: StockInsightBundle,
    feature: FeatureSnapshot,
    alpha: AlphaEvidenceReport,
    factor_lab: FactorLabReport | None = None,
    market_regime: MarketRegimeReport | None = None,
    validation: SignalValidationReport | None = None,
    risk_reward: RiskRewardReport | None = None,
    timeframe: TimeframeAlignmentReport | None = None,
) -> str:
    return (
        f"特征快照显示：趋势 {feature.trend_score} 分、量价热度（衍生） {feature.fund_flow_score} 分、"
        f"估值 {feature.valuation_score} 分、龙头强度 {feature.leader_score} 分。"
        f"Alpha证据结论为「{alpha.verdict}」，证据充分度 {alpha.confidence}/100。"
        f"{diagnosis_factor_regime_text(factor_lab, market_regime)}"
        f"{diagnosis_extra_text(validation, risk_reward, timeframe)}"
        f"{main_conflict_sentence(insights.overview.main_conflict)}"
    )


def diagnosis_factor_regime_text(
    factor_lab: FactorLabReport | None,
    market_regime: MarketRegimeReport | None,
) -> str:
    parts: list[str] = []
    if factor_lab:
        parts.append(
            f"因子实验室总分 {factor_lab.total_score}，证据充分度 {_factor_evidence_sufficiency(factor_lab)}/100，"
            f"样本 {factor_lab.calibration_sample_count} 个，正向 {factor_lab.positive_factor_count}，负向 {factor_lab.negative_factor_count}。"
        )
    if market_regime:
        parts.append(f"环境判断为「{market_regime.market_label}/{market_regime.stock_state}」，风险倍率 {market_regime.risk_multiplier:.2f}。")
    return "".join(parts)


def diagnosis_extra_text(
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


def main_conflict_sentence(text: str) -> str:
    if text.startswith("当前主要矛盾"):
        return text if text.endswith("。") else f"{text}。"
    return _sentence(f"当前主要矛盾是：{text}")


def _trend_repair_confirmation(feature: FeatureSnapshot) -> list[str]:
    if feature.trend_score >= 55:
        return []
    return [f"趋势评分重新回到 55 分以上，目前为 {feature.trend_score}。"]


def _base_confirmation_signals(feature: FeatureSnapshot) -> list[str]:
    signals = ["量价热度评分（衍生）维持在 60 分以上，订单压力不再显示明显卖压。"]
    if _valid_price(feature.ma5):
        signals.insert(0, f"收盘站稳5日线 {feature.ma5:.2f}，且量能不低于近20日均量的 1.1 倍。")
    if _valid_price(feature.resistance):
        signals.append(f"放量突破压力位 {feature.resistance:.2f} 后，回踩不跌回压力位下方。")
    return signals


def _factor_confirmation(factor_lab: FactorLabReport | None) -> list[str]:
    if not factor_lab or not factor_lab.top_positive:
        return []
    return [_factor_confirmation_text(factor_lab)]


def _timeframe_confirmation(timeframe: TimeframeAlignmentReport | None) -> list[str]:
    if not timeframe or timeframe.conflict_level != "多周期顺向":
        return []
    return [f"多周期目前为「{timeframe.alignment_label}」，可把顺周期信号当作辅助确认。"]


def _hard_risk_overlays(
    insights: StockInsightBundle,
    market_regime: MarketRegimeReport | None,
    risk_reward: RiskRewardReport | None,
    timeframe: TimeframeAlignmentReport | None,
) -> list[str]:
    return [
        *_risk_reward_hard_risk(risk_reward),
        *_timeframe_hard_risk(timeframe),
        *_market_regime_hard_risk(market_regime),
        *_abnormal_hard_risk(insights),
    ]


def _risk_reward_hard_risk(risk_reward: RiskRewardReport | None) -> list[str]:
    if risk_reward and risk_reward.rating in RISK_REWARD_HARD_RISK_RATINGS:
        return [f"当前风险收益结论为「{risk_reward.rating}」，不宜把局部反弹当成明确机会。"]
    return []


def _timeframe_hard_risk(timeframe: TimeframeAlignmentReport | None) -> list[str]:
    if timeframe and timeframe.conflict_level in TIMEFRAME_HARD_RISK_LEVELS:
        return [f"多周期存在「{timeframe.conflict_level}」，短线信号需要等待弱周期修复。"]
    return []


def _market_regime_hard_risk(market_regime: MarketRegimeReport | None) -> list[str]:
    if market_regime and market_regime.risk_multiplier >= 1.18:
        return [f"环境风险抬升：{market_regime.market_label}，需降低信号置信。"]
    return []


def _abnormal_hard_risk(insights: StockInsightBundle) -> list[str]:
    if insights.abnormal_events.level == "风险":
        return [f"异动风险未解除：{insights.abnormal_events.main_signal}。"]
    return []


def _base_hard_risks(feature: FeatureSnapshot) -> list[str]:
    risks = ["数据质量降到“一般”以下，所有买卖点和做T计划必须降级。"]
    if _valid_price(feature.support):
        risks.insert(0, f"有效跌破支撑位 {feature.support:.2f}。")
    if _valid_price(feature.ma20):
        risks.insert(1 if _valid_price(feature.support) else 0, f"收盘跌破20日线 {feature.ma20:.2f} 且次日不能快速修复。")
    return risks


def _factor_risks(factor_lab: FactorLabReport | None) -> list[str]:
    if not factor_lab or not factor_lab.top_negative:
        return []
    return [_factor_risk_text(factor_lab)]


def _base_watch_focus() -> list[str]:
    return [
        "先看关键价位，再看量能和量价热度（衍生）是否确认。",
        "只把策略卡当成条件清单，不把单一信号当成确定结论。",
        "做T只适用于已有可卖底仓，新增买入不参与当日T。",
    ]


def _industry_watch_focus(feature: FeatureSnapshot) -> list[str]:
    return [f"同步观察行业「{feature.industry_name}」是否继续配合。"] if feature.industry_name else []


def _market_regime_watch_focus(market_regime: MarketRegimeReport | None) -> list[str]:
    return market_regime.suggestions[:2] if market_regime else []


def _validation_watch_focus(validation: SignalValidationReport | None) -> list[str]:
    if not validation:
        return []
    return [f"验证闭环当前为「{validation.overall_status}」，先按触发-确认-失效顺序执行。"]


def _timeframe_watch_focus(timeframe: TimeframeAlignmentReport | None) -> list[str]:
    return timeframe.suggestions[:1] if timeframe else []


def _factor_watch_focus(factor_lab: FactorLabReport | None) -> list[str]:
    if factor_lab and factor_lab.calibration_sample_count:
        return [f"因子实验室本轮样本数 {factor_lab.calibration_sample_count}，样本少的因子只做辅助。"]
    return []


def _valid_price(value: float) -> bool:
    return value > 0


def _dedupe(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _sentence(text: str) -> str:
    return text if text.endswith("。") else f"{text}。"
