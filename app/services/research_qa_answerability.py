from __future__ import annotations

import re
from typing import Literal

from app.models.research import StockQuestionAnswer
from app.services.research_qa_answer_contracts import StockQuestionContext
from app.services.research_qa_topics import related_questions
from app.services.valuation_anchors import peer_valuation_percentile
from app.utils.market_data import finite_float


# These reports support current research summaries, not document or historical fact QA.
_DOCUMENT_FACT_REQUEST = re.compile(
    r"董事长|总经理|法人|实控人|创始人|营业收入|营收|净利润|财报|年报|季报|合同金额|"
    r"公告|"
    r"(?:19|20)\d{2}年|去年|前年|上季度|历史上"
)
_SPECIFIC_FORECAST = re.compile(r"(?:明天|后天|下周|下月).{0,12}(?:多少钱|多少元|收盘价|涨停|跌停|必涨|必跌)")
_SUPPORTED_TOPICS = frozenset({"综合判断", "主题概念", "风险收益", "风险", "买点", "卖点", "同行龙头", "事件", "短线观察", "做T"})


def unavailable_question_answer(
    question: str,
    topic: str,
    context: StockQuestionContext,
) -> StockQuestionAnswer | None:
    missing = _missing_question_evidence(question, topic, context)
    if not missing:
        return None
    status: Literal["out_of_scope", "insufficient_evidence"] = "out_of_scope" if topic not in _SUPPORTED_TOPICS or _SPECIFIC_FORECAST.search(question) else "insufficient_evidence"
    reason = "；".join(missing)
    return StockQuestionAnswer(
        symbol=f"{context.analysis.quote.code}.{context.analysis.quote.market}",
        updated_at=context.analysis.quote.timestamp,
        question=question,
        topic=topic,
        conclusion="当前资料无法回答这个问题，证据待确认",
        answer=f"当前资料无法回答这个问题：{reason}。可继续询问当前个股的趋势、风险或支撑压力；这些研究判断也需要相应行情证据。",
        confidence=0,
        confidence_note="本次问题缺少可回答证据，兼容字段记为0，不代表答案错误概率，也不沿用个股研究评分。",
        answerability=status,
        missing_evidence=missing,
        related_questions=related_questions("综合判断"),
    )


def _missing_question_evidence(question: str, topic: str, context: StockQuestionContext) -> list[str]:
    if _DOCUMENT_FACT_REQUEST.search(question):
        return ["当前上下文没有与所问人物、报告期或公告原文对应的可核验资料"]
    if _SPECIFIC_FORECAST.search(question):
        return ["当前行情和研究信号不能确定未来价格或涨跌停结果"]
    if topic not in _SUPPORTED_TOPICS:
        return ["当前问诊支持趋势、风险、买卖条件、同行、题材及事件摘要，尚未识别出可回答的研究问题类型"]
    price = finite_float(context.analysis.quote.price)
    if price is None or price <= 0 or not context.analysis.quote.timestamp:
        return ["当前个股的有效行情及行情时间"]
    return _topic_evidence_gaps(question, topic, context)


def _topic_evidence_gaps(question: str, topic: str, context: StockQuestionContext) -> list[str]:
    analysis = context.analysis
    if topic in {"买点", "卖点", "短线观察", "做T"}:
        return [label for available, label in (
            (analysis.support_available and _positive_number(analysis.support), "有效支撑位证据"),
            (analysis.resistance_available and _positive_number(analysis.resistance), "有效压力位证据"),
        ) if not available]
    if topic == "同行龙头":
        return _peer_question_evidence_gaps(question, context)
    if topic == "事件":
        return _event_question_evidence_gaps(context)
    theme = context.theme_context
    requirements = {
        "风险收益": (context.risk_reward.ratio_available and _positive_number(context.risk_reward.reward_risk_ratio), "可计算的上行空间、下行防守及风险收益比"),
        "主题概念": (theme is not None and bool(theme.concepts), "当前个股的题材及概念资料"),
        "风险": (bool(context.risk_radar.items), "当前风险项目及对应证据"),
        "综合判断": (bool(context.evidence_chain.support or context.evidence_chain.opposition), "支持或反对当前综合判断的研究证据"),
    }
    available, missing = requirements[topic]
    return [] if available else [missing]


def _event_question_evidence_gaps(context: StockQuestionContext) -> list[str]:
    report = context.event_digest
    if report.positive_events or report.negative_events or report.watch_events:
        return []
    return list(dict.fromkeys([
        "可核对的当前事件摘要；行情变化不能替代公告原文",
        *report.missing_data[:4],
    ]))


def _peer_question_evidence_gaps(question: str, context: StockQuestionContext) -> list[str]:
    if context.peer_comparison.sample_count < 2:
        return ["至少两只个股构成的有效同行比较样本"]
    if "估值" in question and peer_valuation_percentile(context.analysis, "pe") is None:
        return ["当前个股有效PE，以及达到估值分位计算要求的有效同行PE样本"]
    return []


def _positive_number(value: object) -> bool:
    number = finite_float(value)
    return number is not None and number > 0
