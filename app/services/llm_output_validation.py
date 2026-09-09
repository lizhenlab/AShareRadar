from __future__ import annotations

import json
import math
import re
from typing import Any

from app.models.analysis import AnalysisResult
from app.models.research import StockQuestionAnswer
from app.services.llm_evidence import MAX_SELECTED_EVIDENCE, answer_evidence_units, evidence_selection_contract


_RESPONSE_FIELDS = frozenset({
    "conclusion", "confidence", "support", "resistance", "actions", "invalidations",
    "contract_version", "context_id", "evidence_ids",
})


class LlmOutputValidationError(ValueError):
    pass


def authority_bindings(rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> dict[str, Any]:
    return {
        "conclusion": rule_answer.conclusion,
        "confidence": rule_answer.confidence,
        "support": analysis.support if analysis.support_available else None,
        "resistance": analysis.resistance if analysis.resistance_available else None,
        "actions": list(rule_answer.actions),
        "invalidations": list(rule_answer.invalidations),
    }


def authority_binding_issue(rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> str | None:
    if not _nonempty(rule_answer.conclusion):
        return "规则结论为空"
    if isinstance(rule_answer.confidence, bool) or not isinstance(rule_answer.confidence, int):
        return "置信度不是整数"
    if not 0 <= rule_answer.confidence <= 100:
        return "置信度超出0到100"
    for label, value, available in (
        ("支撑位", analysis.support, analysis.support_available),
        ("压力位", analysis.resistance, analysis.resistance_available),
    ):
        if available and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            return f"{label}不是有限数值"
    for label, values in (("行动", rule_answer.actions), ("失效条件", rule_answer.invalidations)):
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            return f"{label}不是文本列表"
    return None


def _validate_response_shape(output: dict[str, Any]) -> None:
    missing = sorted(_RESPONSE_FIELDS.difference(output))
    if missing:
        raise LlmOutputValidationError(f"缺少结构化字段 {', '.join(missing)}")
    extra = sorted(str(key) for key in set(output).difference(_RESPONSE_FIELDS))
    if extra:
        raise LlmOutputValidationError(f"包含未声明字段 {', '.join(extra)}")


def _validate_authority_bindings(
    output: dict[str, Any],
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
) -> None:
    expected = authority_bindings(rule_answer, analysis)
    if output["conclusion"] != expected["conclusion"]:
        raise LlmOutputValidationError("结论矛盾：conclusion未原样绑定规则结论")
    if (
        isinstance(output["confidence"], bool)
        or not isinstance(output["confidence"], int)
        or output["confidence"] != expected["confidence"]
    ):
        raise LlmOutputValidationError("置信度绑定不一致")
    for field, label in (("support", "支撑位"), ("resistance", "压力位")):
        if not _bound_optional_number_equal(output[field], expected[field]):
            raise LlmOutputValidationError(f"{label}绑定不一致")
    for field, label in (("actions", "行动"), ("invalidations", "失效条件")):
        if not isinstance(output[field], list) or output[field] != expected[field]:
            raise LlmOutputValidationError(f"{label}绑定不一致")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LlmOutputValidationError(f"JSON字段重复：{key}")
        result[key] = value
    return result


def _reject_non_finite_json(token: str) -> Any:
    raise LlmOutputValidationError(f"JSON包含非法数值：{token}")


def _decode_structured_output(raw_answer: Any) -> dict[str, Any]:
    if isinstance(raw_answer, dict):
        return raw_answer
    if not isinstance(raw_answer, str):
        raise LlmOutputValidationError("结构化输出不是文本JSON")
    text = raw_answer.strip()
    if not text:
        raise LlmOutputValidationError("模型输出为空")
    fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1)

    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_finite_json,
        )
    except LlmOutputValidationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError):
        raise LlmOutputValidationError("结构化输出不是有效JSON") from None
    if not isinstance(value, dict):
        raise LlmOutputValidationError("结构化输出顶层必须是JSON对象")
    return value


def _single_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _format_price(value: float) -> str:
    return f"{float(value):.2f}"


def _available_price(value: float, available: bool) -> str:
    return f"{_format_price(value)} 元" if available else "待确认"


def _bound_optional_number_equal(actual: Any, expected: Any) -> bool:
    if expected is None:
        return actual is None
    return _bound_number_equal(actual, expected)


def _bound_number_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    actual_value = float(actual)
    expected_value = float(expected)
    return math.isfinite(actual_value) and math.isclose(actual_value, expected_value, rel_tol=0.0, abs_tol=1e-9)


def _nonempty(value: Any) -> bool:
    return bool(str(value).strip()) if value is not None else False


def validate_and_render_answer(
    raw_answer: Any,
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
) -> str:
    output = _decode_structured_output(raw_answer)
    _validate_response_shape(output)
    _validate_authority_bindings(output, rule_answer, analysis)
    selected = _validated_evidence_selection(output, rule_answer, analysis)
    return _render_authoritative_answer(rule_answer, analysis, selected)


def _validated_evidence_selection(
    output: dict[str, Any],
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
) -> list[str]:
    expected = evidence_selection_contract(rule_answer, analysis)
    if any(output[field] != value for field, value in expected.items()):
        raise LlmOutputValidationError("证据协议版本或当前上下文绑定不一致")
    identifiers = output["evidence_ids"]
    if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= MAX_SELECTED_EVIDENCE:
        raise LlmOutputValidationError("证据选择必须为1至4个证据ID")
    if any(not isinstance(item, str) for item in identifiers):
        raise LlmOutputValidationError("证据ID必须为文本")
    if len(set(identifiers)) != len(identifiers):
        raise LlmOutputValidationError("证据ID不得重复")
    candidates = {item["id"]: item for item in answer_evidence_units(rule_answer, analysis)}
    if any(item not in candidates for item in identifiers):
        raise LlmOutputValidationError("证据ID不属于当前上下文")
    return [candidates[item]["text"] for item in identifiers]


def _render_authoritative_answer(
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
    selected: list[str],
) -> str:
    actions = "；".join(_single_line(item) for item in rule_answer.actions if _single_line(item))
    invalidations = "；".join(_single_line(item) for item in rule_answer.invalidations if _single_line(item))
    limitations = "；".join(_single_line(item) for item in analysis.data_quality.notes[:4] if _single_line(item))
    return "\n".join(
        (
            f"规则结论：{_single_line(rule_answer.conclusion)}（回答可靠度 {rule_answer.confidence}/100，该评分不是统计正确率或概率）",
            f"规则答案：{_single_line(rule_answer.answer)}",
            "模型选读证据（原文）：" + "；".join(selected),
            f"规则行动：{actions or '规则引擎未给出额外行动项'}",
            f"关键位：支撑 {_available_price(analysis.support, analysis.support_available)}；"
            f"压力 {_available_price(analysis.resistance, analysis.resistance_available)}",
            f"失效条件：{invalidations or '规则引擎未给出额外失效条件'}",
            f"数据限制：{limitations or '以当前行情和规则研究资料为限，未核验额外公告或财报'}",
        )
    )
