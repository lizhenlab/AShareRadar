from __future__ import annotations

import pytest

from app.services.llm_output_validation import LlmOutputValidationError, validate_and_render_answer
from tests.test_llm_explainer import _llm_test_case, _structured_output


@pytest.mark.parametrize("value", [0, 68, 100])
def test_server_renders_reliability_as_score_not_probability(value):
    analysis, answer = _llm_test_case()
    answer = answer.model_copy(update={"confidence": value})
    output = _structured_output(analysis, answer, ["reliability"])
    rendered = validate_and_render_answer(output, answer, analysis)
    assert f"回答可靠度 {value}/100" in rendered
    assert "不是统计正确率或概率" in rendered
    assert f"置信度 {value}%" not in rendered


@pytest.mark.parametrize("text", [
    "置信度为68%", "规则置信度为68％", "规则建议强度为68%",
    "现价为68分", "涨跌幅为68分", "规则建议强度为68元",
    "规则建议强度为1300分", "支撑1300%", "现价1300万元",
])
def test_model_cannot_insert_or_relabel_numeric_claims(text):
    analysis, answer = _llm_test_case()
    output = _structured_output(analysis, answer)
    output["explanation"] = text
    with pytest.raises(LlmOutputValidationError, match="未声明字段"):
        validate_and_render_answer(output, answer, analysis)


def test_equal_score_and_market_percentage_keep_server_owned_units():
    analysis, answer = _llm_test_case()
    analysis = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"change_pct": float(answer.confidence)})})
    output = _structured_output(analysis, answer, ["reliability", "change-pct"])
    rendered = validate_and_render_answer(output, answer, analysis)
    assert f"回答可靠度 {answer.confidence}/100" in rendered
    assert f"涨跌幅 {answer.confidence:.2f}%" in rendered
