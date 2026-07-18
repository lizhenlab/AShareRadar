from __future__ import annotations

import asyncio
import inspect
import math
from typing import Any

from app.config import Settings
from app.models.schemas import AnalysisResult, StockQuestionAnswer
from app.services.llm_output_validation import (
    LlmOutputValidationError,
    _allowed_numbers as _allowed_numbers,
    authority_binding_issue,
    validate_and_render_answer,
)
from app.services.llm_prompt import build_chat_messages
from app.services.provider_errors import sanitize_provider_error


__all__ = ["_allowed_numbers", "_call_llm", "enhance_stock_answer", "llm_available"]


def llm_available(settings: Settings) -> bool:
    return bool(
        settings.llm_enabled
        and _nonempty(settings.llm_api_key)
        and _nonempty(settings.llm_base_url)
        and _nonempty(settings.llm_model)
    )


async def enhance_stock_answer(
    *,
    settings: Settings,
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
) -> StockQuestionAnswer:
    if not llm_available(settings):
        return _fallback(rule_answer, "未配置大模型API")

    binding_issue = authority_binding_issue(rule_answer, analysis)
    if binding_issue:
        return _fallback(rule_answer, f"大模型未调用：规则绑定不可用（{binding_issue}）")

    timeout = _llm_timeout(settings)
    try:
        async with asyncio.timeout(timeout):
            answer, repaired = await _validated_llm_answer(settings, rule_answer, analysis)
    except asyncio.TimeoutError:
        return _fallback(rule_answer, f"大模型降级：请求超时（{timeout:g}秒）")
    except LlmOutputValidationError as exc:
        return _fallback(rule_answer, f"大模型输出校验失败：{_short_error(exc, settings, rule_answer)}")
    except Exception as exc:
        return _fallback(rule_answer, f"大模型降级：{_short_error(exc, settings, rule_answer)}")

    return rule_answer.model_copy(
        update={
            "answer": answer,
            "answer_source": f"大模型解释增强·{settings.llm_model}",
            "llm_used": True,
            "llm_status": (
                "结构化字段已绑定规则引擎，经一次格式纠错后仅增强解释"
                if repaired
                else "结构化字段已绑定规则引擎，仅增强解释"
            ),
        }
    )


async def _validated_llm_answer(
    settings: Settings,
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
) -> tuple[str, bool]:
    raw_answer = await _invoke_llm(settings, rule_answer, analysis)
    try:
        return validate_and_render_answer(raw_answer, rule_answer, analysis), False
    except LlmOutputValidationError:
        repaired = await _invoke_llm(settings, rule_answer, analysis, repair=True)
        try:
            return validate_and_render_answer(repaired, rule_answer, analysis), True
        except LlmOutputValidationError as exc:
            raise LlmOutputValidationError(f"纠错重试仍未通过：{exc}") from exc


async def _invoke_llm(
    settings: Settings,
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
    *,
    repair: bool = False,
) -> Any:
    result = _call_llm(settings, rule_answer, analysis, repair=repair)
    if inspect.isawaitable(result):
        return await result
    return result


async def _call_llm(
    settings: Settings,
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
    *,
    repair: bool = False,
) -> str:
    from openai import AsyncOpenAI

    timeout = _llm_timeout(settings)
    client = AsyncOpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=timeout,
        max_retries=0,
    )
    try:
        completion_call = client.chat.completions.create(
            model=settings.llm_model,
            temperature=0.0,
            max_tokens=700,
            messages=build_chat_messages(rule_answer, analysis, repair=repair),
        )
        completion = await completion_call if inspect.isawaitable(completion_call) else completion_call
        content = completion.choices[0].message.content
        if not isinstance(content, str):
            raise RuntimeError("模型未返回文本内容")
        return content
    finally:
        await _close_client(client)


async def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        pass


def _llm_timeout(settings: Settings) -> float:
    try:
        value = float(settings.llm_timeout_seconds)
    except (TypeError, ValueError):
        return 8.0
    if not math.isfinite(value) or value <= 0:
        return 8.0
    return min(value, 120.0)


def _nonempty(value: Any) -> bool:
    return bool(str(value).strip()) if value is not None else False


def _fallback(rule_answer: StockQuestionAnswer, status: str) -> StockQuestionAnswer:
    return rule_answer.model_copy(
        update={
            "answer": rule_answer.answer,
            "answer_source": "规则问诊",
            "llm_used": False,
            "llm_status": status,
        }
    )


def _short_error(
    exc: Exception,
    settings: Settings | None = None,
    rule_answer: StockQuestionAnswer | None = None,
) -> str:
    text = sanitize_provider_error(
        exc,
        sensitive_values=(
            settings.llm_api_key if settings is not None else None,
            rule_answer.question if rule_answer is not None else None,
        ),
    )
    text = " ".join(text.split()).strip()
    return text[:120] if text else exc.__class__.__name__
