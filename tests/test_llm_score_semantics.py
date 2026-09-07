from __future__ import annotations

import pytest

from app.services.llm_output_validation import (
    LlmOutputValidationError,
    authority_bindings,
    validate_and_render_answer,
)
from tests.test_llm_explainer import _llm_test_case


@pytest.mark.parametrize("label", ["规则建议强度", "规则建议强度评分", "规则建议强度分数"])
def test_rule_strength_accepts_score_labels(label):
    analysis, answer = _llm_test_case()
    text = f"{label}为{answer.confidence}分，仍需核验。"
    output = {**authority_bindings(answer, analysis), "explanation": text}
    rendered = validate_and_render_answer(output, answer, analysis)
    assert text in rendered
    assert f"规则建议强度 {answer.confidence}/100" in rendered


@pytest.mark.parametrize("text", ["置信度为{value}%", "规则置信度为{value}％", "规则建议强度为{value}%"])
def test_rule_strength_cannot_be_expressed_as_probability(text):
    analysis, answer = _llm_test_case()
    output = {**authority_bindings(answer, analysis), "explanation": text.format(value=answer.confidence)}
    with pytest.raises(LlmOutputValidationError):
        validate_and_render_answer(output, answer, analysis)


@pytest.mark.parametrize("text", ["现价为{value}分", "涨跌幅为{value}分", "规则建议强度为{value}元"])
def test_equal_numbers_do_not_make_units_interchangeable(text):
    analysis, answer = _llm_test_case()
    analysis = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={
        "price": float(answer.confidence), "change_pct": float(answer.confidence),
    })})
    output = {**authority_bindings(answer, analysis), "explanation": text.format(value=answer.confidence)}
    with pytest.raises(LlmOutputValidationError, match="单位/百分比"):
        validate_and_render_answer(output, answer, analysis)


def test_known_market_number_cannot_substitute_for_rule_strength():
    analysis, answer = _llm_test_case()
    output = {**authority_bindings(answer, analysis), "explanation": f"规则建议强度为{analysis.quote.price}分"}
    with pytest.raises(LlmOutputValidationError, match="语义错配"):
        validate_and_render_answer(output, answer, analysis)


@pytest.mark.parametrize("value", [0, 100])
def test_rule_strength_scale_endpoints_are_scores(value):
    analysis, answer = _llm_test_case()
    answer = answer.model_copy(update={"confidence": value})
    text = f"{value}分的规则建议强度仍须结合证据。"
    output = {**authority_bindings(answer, analysis), "explanation": text}
    assert text in validate_and_render_answer(output, answer, analysis)


def test_equal_score_and_market_percentage_retain_distinct_units():
    analysis, answer = _llm_test_case()
    analysis = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"change_pct": float(answer.confidence)})})
    text = f"规则建议强度为{answer.confidence}分，涨跌幅为{answer.confidence}%。"
    output = {**authority_bindings(answer, analysis), "explanation": text}
    assert text in validate_and_render_answer(output, answer, analysis)
