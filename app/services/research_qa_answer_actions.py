from __future__ import annotations

from app.services.research_qa_answer_contracts import ActionContext
from app.services.research_qa_answer_formatters import _display_text, _first_clean_items, _format_number
from app.services.research_qa_utils import bounded_int, dedupe


def _t_strategy_actions(context: ActionContext) -> list[str]:
    return dedupe([
        f"只在已有可卖底仓前提下执行，低吸参考 {_display_text(context.t_strategy.low_zone)}，高抛参考 {_display_text(context.t_strategy.high_zone)}。",
        *context.t_strategy.execution_steps,
        "若无法严格执行高抛低吸纪律，则宁可不做。",
    ])


def _risk_actions(context: ActionContext) -> list[str]:
    ranked_actions = [item.action for item in sorted(context.risk_radar.items, key=lambda row: _sort_score(row.score), reverse=True)]
    return _first_clean_items(ranked_actions, 4)


def _sort_score(value: object) -> int:
    return bounded_int(value, 0, 100, default=0)


def _risk_reward_actions(context: ActionContext) -> list[str]:
    rating = _display_text(context.risk_reward.rating)
    validation_status = _display_text(context.validation.overall_status)
    actions = [
        f"只有风险收益评级达到「性价比较好」或「性价比一般」，且验证状态不是「风险优先」时，才考虑观察级动作；当前为「{rating} / {validation_status}」。",
        f"若价格贴近上方目标 {_format_number(context.risk_reward.upside_target)} 或收益风险比低于 1.2，不新增追高。",
        f"若跌近下方防守 {_format_number(context.risk_reward.downside_stop)}，先执行防守而不是补仓摊低。",
    ]
    return dedupe([*actions, *[item.response for item in context.risk_reward.scenarios[:2]]])


def _buy_actions(context: ActionContext) -> list[str]:
    actions = [
        item.action_hint
        for item in context.validation.items
        if item.category == "买点" and item.status in {"接近确认", "等待确认", "低置信观察", "环境压制", "周期冲突降级"}
    ][:3]
    return dedupe([
        f"只有站稳支撑 {_format_number(context.analysis.support)} 且不过度贴近压力 {_format_number(context.analysis.resistance)} 时，才考虑观察级动作。",
        *actions,
        "风险收益比没有修复前，不把反弹直接当买点。",
    ])


def _sell_actions(context: ActionContext) -> list[str]:
    return dedupe([
        f"接近压力 {_format_number(context.analysis.resistance)} 且量价乏力时优先保护利润。",
        f"跌破支撑 {_format_number(context.analysis.support)} 后不要用主观预期替代纪律。",
        *[item.action_hint for item in context.validation.items if item.status in {"风险触发", "周期冲突降级"}][:3],
    ])


def _peer_actions(context: ActionContext) -> list[str]:
    return dedupe(["先确认强弱分位是否持续靠前，再看成交额是否同步放大。", "若同行更强而本股滞涨，不主动上调龙头判断。"])


def _theme_actions(context: ActionContext) -> list[str]:
    if not context.theme_context:
        return ["主题概念数据未确认前，不把题材当作买入理由。", "先看个股是否守住关键价位和量能确认。"]
    return dedupe([
        *context.theme_context.opportunities[:3],
        *context.theme_context.risks[:2],
        "主题只作为解释背景，具体动作仍以支撑、压力、量能和失效条件为准。",
    ])


def _event_actions(context: ActionContext) -> list[str]:
    return dedupe(["把事件作为结论修正项，不单独作为买卖依据。", "事件偏风险时，先等价格和量能验证风险是否消化。"])


def _short_term_actions(context: ActionContext) -> list[str]:
    return dedupe([
        f"明线看支撑 {_format_number(context.analysis.support)} 和压力 {_format_number(context.analysis.resistance)}。",
        *context.diagnosis.confirmation_signals[:3],
        f"环境风险倍率 {_format_number(context.market_regime.risk_multiplier)}，风险收益评级 {_display_text(context.risk_reward.rating)}。",
    ])


def _default_actions(context: ActionContext) -> list[str]:
    return dedupe([context.diagnosis.action, *context.diagnosis.watch_focus, context.validation.summary])
