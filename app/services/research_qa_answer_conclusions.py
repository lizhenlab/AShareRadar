from __future__ import annotations

from app.services.research_qa_answer_actions import _sort_score
from app.services.research_qa_answer_contracts import ConclusionContext, InvalidationContext
from app.services.research_qa_answer_formatters import _display_text, _first_clean_items, _format_number, _format_score
from app.services.research_qa_utils import dedupe


def _t_strategy_invalidations(context: InvalidationContext) -> list[str]:
    return dedupe([*context.t_strategy.stop_conditions, f"跌破支撑 {_format_number(context.analysis.support)} 或冲高回落放量。"])


def _buy_or_short_term_invalidations(context: InvalidationContext) -> list[str]:
    return dedupe([*_first_clean_items(context.evidence_chain.invalidations, 3), *_first_clean_items(context.diagnosis.hard_risks, 2), f"价格有效跌破 {_format_number(context.analysis.support)}。"])


def _sell_invalidations(context: InvalidationContext) -> list[str]:
    return dedupe([
        f"放量站稳压力 {_format_number(context.analysis.resistance)} 后，卖点需要重新评估。",
        *[item.confirmation_condition for item in context.validation.items[:2]],
    ])


def _risk_invalidations(context: InvalidationContext) -> list[str]:
    invalidations = [
        f"{_display_text(item.name)}：{_display_text(item.action)}"
        for item in context.risk_radar.items
        if _sort_score(item.score) >= 42
    ]
    return _first_clean_items(invalidations, 4)


def _risk_reward_invalidations(context: InvalidationContext) -> list[str]:
    return dedupe([
        "收益风险比跌破 1.2，或风险收益评级降为「性价比不足 / 风险优先」。",
        f"有效跌破支撑 {_format_number(context.analysis.support)}，说明下方风险开始兑现。",
        *[item.invalidation_condition for item in context.validation.items[:3]],
    ])


def _theme_invalidations(context: InvalidationContext) -> list[str]:
    theme_invalidations = [
        "概念热度转弱但个股仍无法放量走强，题材支撑需要降权。",
        "概念上涨只来自少数龙头，本股相对强弱转为落后时，不上调主题判断。",
        f"跌破关键支撑 {_format_number(context.analysis.support)} 后，题材解释不能替代价格纪律。",
    ]
    if context.theme_context and context.theme_context.missing_data:
        theme_invalidations.append("主题归属、行业涨跌或数据质量仍有缺口时，不能把题材作为核心依据。")
    return dedupe([*theme_invalidations, *context.diagnosis.hard_risks[:2]])


def _default_invalidations(context: InvalidationContext) -> list[str]:
    return dedupe([*_first_clean_items(context.evidence_chain.invalidations, 4), *_first_clean_items(context.diagnosis.hard_risks, 2)])


def _t_strategy_conclusion(context: ConclusionContext) -> str:
    return f"{_display_text(context.t_strategy.suitability)}：{_display_text(context.t_strategy.style)}"


def _risk_conclusion(context: ConclusionContext) -> str:
    primary_risks = ", ".join(item.split("：")[0] for item in _first_clean_items(context.risk_radar.top_risks, 2))
    return f"{_display_text(context.risk_radar.overall_level)}，优先看 {primary_risks or '关键风险'}"


def _risk_reward_conclusion(context: ConclusionContext) -> str:
    return f"{_display_text(context.risk_reward.rating)}，收益风险比 {_format_number(context.risk_reward.reward_risk_ratio)}，验证状态「{_display_text(context.validation.overall_status)}」"


def _buy_conclusion(context: ConclusionContext) -> str:
    return f"{_display_text(context.diagnosis.action)}，买点必须服从「{_display_text(context.validation.overall_status)}」和「{_display_text(context.risk_reward.rating)}」"


def _sell_conclusion(context: ConclusionContext) -> str:
    return f"以压力位和失效条件为先，当前总建议「{_display_text(context.diagnosis.action)}」"


def _peer_conclusion(context: ConclusionContext) -> str:
    return f"{_display_text(context.peer_comparison.strength_position)}，{_display_text(context.peer_comparison.valuation_position)}"


def _theme_conclusion(context: ConclusionContext) -> str:
    if not context.theme_context:
        return "主题概念待确认，暂不提高结论权重"
    theme = context.theme_context
    return f"{_display_text(theme.level)}，{_display_text(theme.style)}，{_display_text(theme.relative_strength)}，主题评分 {_format_score(theme.score)}"


def _event_conclusion(context: ConclusionContext) -> str:
    return _display_text(context.event_digest.impact_label)


def _short_term_conclusion(context: ConclusionContext) -> str:
    return f"短线先看确认，不抢结论；当前总建议「{_display_text(context.diagnosis.action)}」"


def _default_conclusion(context: ConclusionContext) -> str:
    return f"当前总建议「{_display_text(context.diagnosis.action)}」，风险收益评级「{_display_text(context.risk_reward.rating)}」"
