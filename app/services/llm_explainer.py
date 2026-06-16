from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.config import Settings
from app.models.schemas import AnalysisResult, StockQuestionAnswer


_GROUNDING_SKIP_PREFIXES = ("ma", "ema", "atr")
_GROUNDING_PERIOD_UNITS = ("日", "天", "分钟", "小时")
_GROUNDING_COUNT_UNITS = ("个", "条", "项", "类", "种", "层", "点")


def llm_available(settings: Settings) -> bool:
    return bool(settings.llm_enabled and settings.llm_api_key)


async def enhance_stock_answer(
    *,
    settings: Settings,
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
) -> StockQuestionAnswer:
    if not llm_available(settings):
        return _fallback(rule_answer, "未配置大模型API")
    try:
        answer = await asyncio.wait_for(
            asyncio.to_thread(_call_llm, settings, rule_answer, analysis),
            timeout=settings.llm_timeout_seconds + 2,
        )
    except Exception as exc:
        return _fallback(rule_answer, f"大模型降级：{_short_error(exc)}")
    cleaned = _clean_answer(answer)
    if not cleaned or not _numbers_are_grounded(cleaned, rule_answer, analysis):
        return _fallback(rule_answer, "大模型输出未通过事实校验，已回退规则答案")
    return rule_answer.model_copy(update={"answer": cleaned, "answer_source": f"大模型解释增强·{settings.llm_model}", "llm_used": True, "llm_status": "已基于当前分析结果生成解释"})


def _call_llm(settings: Settings, rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=settings.llm_timeout_seconds)
    payload = _answer_context(rule_answer, analysis)
    completion = client.chat.completions.create(
        model=settings.llm_model,
        temperature=0.1,
        max_tokens=700,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是A股单股研究平台的解释层，只能根据给定JSON回答。"
                    "不要编造新闻、公告、价格、指标或数据源。"
                    "不要输出JSON之外的新数字；价格位可以四舍五入到整数或两位小数，其他比例和分数必须原样引用。"
                    "不能给确定性买卖指令，必须保留风险提示。"
                    "用中文，面向小白，结构为四行：结论、为什么、接下来盯什么、失效条件。"
                ),
            },
            {
                "role": "user",
                "content": "请基于以下JSON解释用户问题，不能使用JSON之外的事实：\n" + json.dumps(payload, ensure_ascii=False),
            },
        ],
    )
    return completion.choices[0].message.content or ""


def _answer_context(rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> dict[str, Any]:
    quote = analysis.quote
    return {
        "question": rule_answer.question,
        "rule_conclusion": rule_answer.conclusion,
        "rule_answer": rule_answer.answer,
        "topic": rule_answer.topic,
        "confidence": rule_answer.confidence,
        "symbol": rule_answer.symbol,
        "stock_name": quote.name,
        "price": quote.price,
        "change_pct": quote.change_pct,
        "support": analysis.support,
        "resistance": analysis.resistance,
        "ma5": analysis.ma5,
        "ma20": analysis.ma20,
        "trend_score": analysis.trend_score,
        "trend_label": analysis.trend_label,
        "risk_level": analysis.risk_level,
        "data_quality": {
            "score": analysis.data_quality.score,
            "level": analysis.data_quality.level,
            "source": analysis.data_quality.source,
            "notes": analysis.data_quality.notes[:4],
        },
        "evidence": rule_answer.evidence[:6],
        "actions": rule_answer.actions[:5],
        "invalidations": rule_answer.invalidations[:5],
        "allowed_numbers": sorted(_allowed_numbers(rule_answer, analysis)),
    }


def _clean_answer(answer: str) -> str:
    lines = []
    for line in str(answer or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        normalized = " ".join(line.strip().split())
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)[:1200]


def _numbers_are_grounded(answer: str, rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> bool:
    allowed = _allowed_numbers(rule_answer, analysis)
    for match in re.finditer(r"\d+(?:\.\d+)?", answer):
        raw = match.group()
        if _is_grounding_exempt_number(answer, match):
            continue
        value = float(raw)
        if not any(_number_matches_allowed(value, item) for item in allowed):
            return False
    return True


def _allowed_numbers(rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> set[float]:
    values: set[float] = set()
    _add_allowed_number(values, analysis.quote.code, digits=0)
    for item in [
        analysis.quote.price,
        analysis.quote.prev_close,
        analysis.quote.open,
        analysis.quote.high,
        analysis.quote.low,
        analysis.quote.change,
        analysis.quote.change_pct,
        analysis.quote.turnover_rate,
        analysis.quote.pe,
        analysis.quote.pb,
        analysis.support,
        analysis.resistance,
        analysis.ma5,
        analysis.ma10,
        analysis.ma20,
        analysis.trend_score,
        analysis.data_quality.score,
        analysis.data_quality.kline_count,
        rule_answer.confidence,
    ]:
        _add_allowed_number(values, item)
    for text in [rule_answer.answer, *rule_answer.evidence, *rule_answer.actions, *rule_answer.invalidations]:
        for raw in re.findall(r"\d+(?:\.\d+)?", str(text)):
            values.add(round(float(raw), 2))
    return values


def _add_allowed_number(values: set[float], raw: Any, digits: int = 2) -> None:
    if raw is None:
        return
    try:
        values.add(round(float(raw), digits))
    except (TypeError, ValueError):
        return


def _number_matches_allowed(value: float, allowed: float) -> bool:
    tolerance = max(0.05, abs(allowed) * 0.005)
    return abs(value - allowed) <= tolerance


def _is_grounding_exempt_number(answer: str, match: re.Match[str]) -> bool:
    value = float(match.group())
    prefix = answer[max(0, match.start() - 4) : match.start()].lower()
    suffix = answer[match.end() : match.end() + 3]
    if prefix.endswith(_GROUNDING_SKIP_PREFIXES):
        return True
    if suffix.startswith(_GROUNDING_PERIOD_UNITS):
        return True
    if value <= 10 and suffix.startswith(_GROUNDING_COUNT_UNITS):
        return True
    return suffix[:1] in {".", "、", ")", "）", "，", ","}


def _fallback(rule_answer: StockQuestionAnswer, status: str) -> StockQuestionAnswer:
    return rule_answer.model_copy(update={"answer_source": "规则问诊", "llm_used": False, "llm_status": status})


def _short_error(exc: Exception) -> str:
    text = str(exc)
    return text[:120] if text else exc.__class__.__name__
