from __future__ import annotations

from app.services.research_qa_answer_contracts import EvidenceContext
from app.services.research_qa_answer_formatters import _display_text, _format_number, _format_score
from app.services.research_qa_utils import dedupe


def _base_evidence(context: EvidenceContext) -> list[str]:
    return [
        context.diagnosis.headline,
        f"当前价 {_format_number(context.analysis.quote.price)}，支撑 {_format_number(context.analysis.support)}，压力 {_format_number(context.analysis.resistance)}。",
        f"{_display_text(context.market_regime.market_label)}，风险倍率 {_format_number(context.market_regime.risk_multiplier)}。",
    ]


def _t_strategy_evidence(context: EvidenceContext) -> list[str]:
    t_plan_evidence = [item.reason for item in context.analysis.t_plan[:2]]
    return dedupe([context.t_strategy.summary, *context.t_strategy.execution_steps[:2], *_base_evidence(context), *t_plan_evidence])


def _risk_evidence(context: EvidenceContext) -> list[str]:
    return dedupe([
        context.risk_radar.summary,
        *context.risk_radar.top_risks,
        *context.diagnosis.hard_risks[:3],
        context.risk_reward.summary,
        context.timeframe.summary,
    ])


def _risk_reward_evidence(context: EvidenceContext) -> list[str]:
    scenario_text = [
        f"{_display_text(item.name)}：{_display_text(item.trigger)}；{_display_text(item.expected_move)}；应对：{_display_text(item.response)}"
        for item in context.risk_reward.scenarios[:3]
    ]
    return dedupe([
        context.risk_reward.summary,
        f"上方目标 {_format_number(context.risk_reward.upside_target)}（{_format_number(context.risk_reward.upside_pct, suffix='%')}），下方防守 {_format_number(context.risk_reward.downside_stop)}（{_format_number(context.risk_reward.downside_pct, suffix='%')}），收益风险比 {_format_number(context.risk_reward.reward_risk_ratio)}。",
        context.validation.summary,
        context.timeframe.summary,
        *scenario_text,
        *context.risk_reward.notes[:2],
    ])


def _buy_evidence(context: EvidenceContext) -> list[str]:
    buy_evidence = [item.reason for item in context.analysis.buy_points[:3]]
    confirmations = [item.confirmation_condition for item in context.validation.items[:3]]
    return dedupe([*_base_evidence(context), context.risk_reward.summary, *context.evidence_chain.support[:3], *buy_evidence, *confirmations])


def _sell_evidence(context: EvidenceContext) -> list[str]:
    sell_evidence = [item.reason for item in context.analysis.sell_points[:3]]
    return dedupe([*_base_evidence(context), context.risk_reward.summary, *context.evidence_chain.opposition[:3], *sell_evidence, *context.risk_radar.top_risks[:2]])


def _peer_evidence(context: EvidenceContext) -> list[str]:
    return dedupe([
        context.peer_comparison.summary,
        context.peer_comparison.valuation_position,
        context.peer_comparison.strength_position,
        *context.peer_comparison.metrics[:3],
        *context.peer_comparison.risks[:2],
    ])


def _theme_evidence(context: EvidenceContext) -> list[str]:
    if not context.theme_context:
        return dedupe([*_base_evidence(context), "主题概念报告暂不可用，先按行业、个股趋势和数据质量保守解释。"])
    theme = context.theme_context
    concepts = [f"{_display_text(item.name)}{_format_number(item.change_pct, suffix='%')}" for item in theme.concepts[:4]]
    industry_change = f" {_format_number(theme.industry_change_pct, suffix='%')}" if theme.industry_change_pct is not None else " 待确认"
    return dedupe([
        theme.summary,
        f"主题评分 {_format_score(theme.score)}，状态 {_display_text(theme.level)}，风格 {_display_text(theme.style)}，相对强弱 {_display_text(theme.relative_strength)}。",
        f"行业 {_display_text(theme.industry)}{industry_change}。",
        "相关概念：" + "、".join(concepts) + "。" if concepts else "相关概念待确认。",
        *theme.evidence[:4],
        *theme.risks[:2],
    ])


def _event_evidence(context: EvidenceContext) -> list[str]:
    return dedupe([
        context.event_digest.summary,
        *context.event_digest.negative_events[:3],
        *context.event_digest.positive_events[:3],
        *context.event_digest.watch_events[:3],
        *context.event_digest.missing_data[:2],
    ])


def _short_term_evidence(context: EvidenceContext) -> list[str]:
    return dedupe([
        *_base_evidence(context),
        context.timeframe.summary,
        context.validation.summary,
        *context.diagnosis.confirmation_signals[:3],
        *context.diagnosis.watch_focus[:2],
    ])


def _default_evidence(context: EvidenceContext) -> list[str]:
    return dedupe([
        *_base_evidence(context),
        context.evidence_chain.summary,
        context.risk_reward.summary,
        context.validation.summary,
        context.timeframe.summary,
        *context.evidence_chain.support[:2],
        *context.evidence_chain.opposition[:2],
    ])
