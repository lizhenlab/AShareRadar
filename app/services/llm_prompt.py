from __future__ import annotations

import json
from typing import Any

from app.models.schemas import AnalysisResult, StockQuestionAnswer
from app.services.llm_output_validation import authority_bindings


_SYSTEM_PROMPT = (
    "你是A股研究平台的解释层，规则引擎是唯一决策权威。"
    "你不能修改、补充或反驳规则结论、规则建议强度（兼容字段 confidence）、支撑位、压力位、行动和失效条件。"
    "只返回一个合法JSON对象，不要Markdown代码块或额外文字。"
    "conclusion、confidence、support、resistance、actions、invalidations必须从输入authoritative逐字逐值复制；"
    "confidence是0-100的规则建议强度评分，不是概率、命中率或统计置信度，不得用百分号表达；"
    "只有explanation允许自行撰写，且只能解释原因，不能给出任何买卖、仓位或持有指令，也不能作确定性承诺。"
    "explanation避免使用买入、卖出、加仓、减仓、持有、追涨、抄底、止盈、止损等动作词，优先用定性语言描述趋势、风险和数据质量。"
    "explanation如引用数字，必须来自输入并明确写出对应字段；价格可按整数或两位小数引用，比例、分数、收益风险比和风险倍率不得换算或挪作他用。"
    "不得编造新闻、公告、价格、指标或数据源。"
)


def build_chat_messages(
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
    *,
    repair: bool = False,
) -> list[dict[str, str]]:
    expected_shape = authority_bindings(rule_answer, analysis) | {
        "explanation": "仅解释趋势、风险与数据质量为何支持规则结论；不添加行动或新结论"
    }
    user_payload = json.dumps(
        {"context": answer_context(rule_answer, analysis), "expected_output": expected_shape},
        ensure_ascii=False,
        allow_nan=False,
    )
    repair_instruction = (
        "上一次输出未通过本地校验。本次explanation不得出现数字、百分号、价格、指标数值或任何买卖动作词，只做定性解释。"
        if repair
        else ""
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                repair_instruction
                + "请解释用户问题。严格按expected_output的字段和类型返回，绑定字段必须原样复制：\n"
                + user_payload
            ),
        },
    ]


def answer_context(rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> dict[str, Any]:
    quote = analysis.quote
    return {
        "question": rule_answer.question,
        "topic": rule_answer.topic,
        "symbol": rule_answer.symbol,
        "stock_name": quote.name,
        "authoritative": authority_bindings(rule_answer, analysis),
        "rule_answer": rule_answer.answer,
        "market_facts": {
            "current_price": quote.price,
            "previous_close": quote.prev_close,
            "open_price": quote.open,
            "high_price": quote.high,
            "low_price": quote.low,
            "price_change": quote.change,
            "change_pct": quote.change_pct,
            "turnover_rate": quote.turnover_rate,
            "ma5": analysis.ma5,
            "ma10": analysis.ma10,
            "ma20": analysis.ma20,
            "trend_score": analysis.trend_score,
            "trend_label": analysis.trend_label,
            "risk_level": analysis.risk_level,
        },
        "data_quality": {
            "score": analysis.data_quality.score,
            "level": analysis.data_quality.level,
            "source": analysis.data_quality.source,
            "notes": analysis.data_quality.notes[:4],
        },
        "evidence": rule_answer.evidence[:6],
    }
