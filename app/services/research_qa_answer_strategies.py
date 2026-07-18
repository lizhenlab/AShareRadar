from __future__ import annotations

from app.services.research_qa_answer_actions import (
    _buy_actions,
    _default_actions,
    _event_actions,
    _peer_actions,
    _risk_actions,
    _risk_reward_actions,
    _sell_actions,
    _short_term_actions,
    _t_strategy_actions,
    _theme_actions,
)
from app.services.research_qa_answer_conclusions import (
    _buy_conclusion,
    _buy_or_short_term_invalidations,
    _default_conclusion,
    _default_invalidations,
    _event_conclusion,
    _peer_conclusion,
    _risk_conclusion,
    _risk_invalidations,
    _risk_reward_conclusion,
    _risk_reward_invalidations,
    _sell_conclusion,
    _sell_invalidations,
    _short_term_conclusion,
    _t_strategy_conclusion,
    _t_strategy_invalidations,
    _theme_conclusion,
    _theme_invalidations,
)
from app.services.research_qa_answer_contracts import TopicAnswerStrategy
from app.services.research_qa_answer_evidence import (
    _buy_evidence,
    _default_evidence,
    _event_evidence,
    _peer_evidence,
    _risk_evidence,
    _risk_reward_evidence,
    _sell_evidence,
    _short_term_evidence,
    _t_strategy_evidence,
    _theme_evidence,
)
from app.services.research_qa_answer_formatters import _clean_topic, _display_text


def _answer_prefix(topic: str, name: str, conclusion: str) -> str:
    return _answer_strategy(topic).prefix(_display_text(name, fallback="该股票"), _display_text(conclusion))


def _default_answer_prefix(name: str, conclusion: str) -> str:
    return f"{_display_text(name, fallback='该股票')}这只个股的回答是：{_display_text(conclusion)}。"


_DEFAULT_ANSWER_STRATEGY = TopicAnswerStrategy(
    evidence=_default_evidence,
    actions=_default_actions,
    invalidations=_default_invalidations,
    conclusion=_default_conclusion,
    prefix=_default_answer_prefix,
)


TOPIC_ANSWER_STRATEGIES: dict[str, TopicAnswerStrategy] = {
    "做T": TopicAnswerStrategy(
        evidence=_t_strategy_evidence,
        actions=_t_strategy_actions,
        invalidations=_t_strategy_invalidations,
        conclusion=_t_strategy_conclusion,
        prefix=lambda name, conclusion: f"{name}的做T判断是：{conclusion}。做T只服务于已有底仓降成本。",
    ),
    "风险": TopicAnswerStrategy(
        evidence=_risk_evidence,
        actions=_risk_actions,
        invalidations=_risk_invalidations,
        conclusion=_risk_conclusion,
        prefix=lambda name, conclusion: f"{name}当前风险判断是：{conclusion}。",
    ),
    "风险收益": TopicAnswerStrategy(
        evidence=_risk_reward_evidence,
        actions=_risk_reward_actions,
        invalidations=_risk_reward_invalidations,
        conclusion=_risk_reward_conclusion,
        prefix=lambda name, conclusion: f"{name}当前风险收益判断是：{conclusion}。",
    ),
    "买点": TopicAnswerStrategy(
        evidence=_buy_evidence,
        actions=_buy_actions,
        invalidations=_buy_or_short_term_invalidations,
        conclusion=_buy_conclusion,
        prefix=lambda name, conclusion: f"{name}当前不能只按“想买”处理，系统结论是：{conclusion}。",
    ),
    "卖点": TopicAnswerStrategy(
        evidence=_sell_evidence,
        actions=_sell_actions,
        invalidations=_sell_invalidations,
        conclusion=_sell_conclusion,
        prefix=lambda name, conclusion: f"{name}的卖点更适合按压力和失效条件处理，结论是：{conclusion}。",
    ),
    "同行龙头": TopicAnswerStrategy(
        evidence=_peer_evidence,
        actions=_peer_actions,
        invalidations=_default_invalidations,
        conclusion=_peer_conclusion,
        prefix=lambda name, conclusion: f"{name}的同行强弱判断是：{conclusion}。",
    ),
    "主题概念": TopicAnswerStrategy(
        evidence=_theme_evidence,
        actions=_theme_actions,
        invalidations=_theme_invalidations,
        conclusion=_theme_conclusion,
        prefix=lambda name, conclusion: f"{name}的题材背景判断是：{conclusion}。题材只解释背景，不能单独替代价格和风险收益。",
    ),
    "事件": TopicAnswerStrategy(
        evidence=_event_evidence,
        actions=_event_actions,
        invalidations=_default_invalidations,
        conclusion=_event_conclusion,
        prefix=lambda name, conclusion: f"{name}的事件影响判断是：{conclusion}。",
    ),
    "短线观察": TopicAnswerStrategy(
        evidence=_short_term_evidence,
        actions=_short_term_actions,
        invalidations=_buy_or_short_term_invalidations,
        conclusion=_short_term_conclusion,
        prefix=lambda name, conclusion: f"{name}的短线观察结论是：{conclusion}。",
    ),
    "综合判断": _DEFAULT_ANSWER_STRATEGY,
}


def _answer_strategy(topic: str) -> TopicAnswerStrategy:
    return TOPIC_ANSWER_STRATEGIES.get(_clean_topic(topic), _DEFAULT_ANSWER_STRATEGY)
