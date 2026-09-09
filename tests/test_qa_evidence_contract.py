from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from app.services.llm_evidence import answer_evidence_units
from app.services.llm_explainer import enhance_stock_answer
from app.services.llm_output_validation import LlmOutputValidationError, validate_and_render_answer
from app.services.research_qa_answer_report import answer_stock_question
from tests.test_llm_explainer import _enhance_with_output, _llm_settings, _llm_test_case, _structured_json, _structured_output
from tests.test_research_qa_answer_modules import _qa_args


@pytest.mark.parametrize("text", [
    "公司刚刚公告签订重大订单，政策利好已经落地，盈利能力显著改善，机构资金持续流入。",
    "依据已经充分，务必清仓。", "依据已经充分，继续持有。",
    "避免忽视风险，但建议持有。", "现阶段已经适合买入，无需等待确认。",
    "关键数字是1888，仍需观察。", "收益风险比1.18仍偏低。", "环境风险倍率0.98仍需观察。",
    "目标价1300元已经明确。", "支撑68元尚未确认。", "涨跌幅68%说明波动存在。",
    "买入信号尚未确认，因此规则保持等待。",
])
def test_free_text_never_reaches_display_even_if_plausible_or_numberless(text):
    analysis, answer = _llm_test_case()
    output = _structured_output(analysis, answer)
    output["explanation"] = text
    result = _enhance_with_output(analysis, answer, json.dumps(output, ensure_ascii=False))
    assert result.llm_used is False
    assert result.answer == answer.answer
    assert text not in result.answer


def test_model_only_selects_and_orders_exact_current_evidence():
    analysis, answer = _llm_test_case()
    answer = answer.model_copy(update={"evidence": ["量能不足，仍有反对证据。", "收益风险比1.17，环境风险倍率0.97。"]})
    result = _enhance_with_output(analysis, answer, _structured_json(analysis, answer, ["rule-1", "rule-0"]))
    assert result.llm_used is True
    assert "模型选读证据（原文）：收益风险比1.17，环境风险倍率0.97。；量能不足，仍有反对证据。" in result.answer
    assert answer.answer in result.answer
    assert all(item in result.answer for item in answer.actions + answer.invalidations)
    assert result.evidence == answer.evidence
    assert result.conclusion == answer.conclusion
    assert result.confidence == answer.confidence


@pytest.mark.parametrize("identifiers", [[], ["rule-0"] * 2, ["missing"], [1], [True], [None], "rule-0", {"id": "rule-0"}, ["rule-0", "rule-1", "rule-2", "reliability", "current-price"]])
def test_invalid_or_unbounded_selection_is_rejected(identifiers):
    analysis, answer = _llm_test_case()
    output = _structured_output(analysis, answer)
    output["evidence_ids"] = identifiers
    with pytest.raises(LlmOutputValidationError):
        validate_and_render_answer(output, answer, analysis)


@pytest.mark.parametrize("field", ["question", "updated_at", "symbol", "evidence"])
def test_selection_from_different_context_is_rejected(field):
    analysis, answer = _llm_test_case()
    output = _structured_output(analysis, answer)
    new_value = ["修正后的反对证据"] if field == "evidence" else "changed"
    changed = answer.model_copy(update={field: new_value})
    with pytest.raises(LlmOutputValidationError, match="当前上下文"):
        validate_and_render_answer(output, changed, analysis)


def test_old_protocol_does_not_silently_fall_back_to_free_generation():
    analysis, answer = _llm_test_case()
    output = _structured_output(analysis, answer)
    output["contract_version"] = "legacy-free-text.v0"
    with pytest.raises(LlmOutputValidationError, match="协议版本"):
        validate_and_render_answer(output, answer, analysis)


def test_evidence_payload_omits_nonfinite_market_values_and_keeps_source_time():
    analysis, answer = _llm_test_case()
    analysis = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"price": float("inf"), "change_pct": float("nan")})})
    units = answer_evidence_units(answer, analysis)
    assert all(item["id"] not in {"current-price", "change-pct"} for item in units)
    assert all(item["source"] and item["as_of"] for item in units)


def test_selection_does_not_hide_data_limitations():
    analysis, answer = _llm_test_case()
    analysis = analysis.model_copy(update={"data_quality": analysis.data_quality.model_copy(update={"notes": ["缺少公告原文，不能判断重大合同影响。"]})})
    rendered = validate_and_render_answer(_structured_output(analysis, answer, ["current-price"]), answer, analysis)
    assert "数据限制：缺少公告原文，不能判断重大合同影响。" in rendered


@pytest.mark.parametrize("question", [
    "公司董事长是谁？", "公司2024年营业收入是多少？", "综合分析一下2024年财报", "公告中的合同金额多少？", "天气怎么样？", "明天收盘价多少钱？",
])
def test_unanswerable_question_does_not_receive_generic_trade_advice(question):
    result = answer_stock_question(question, *_qa_args())
    assert result.answerability != "answerable"
    assert "当前资料无法回答" in result.answer
    assert result.missing_evidence
    assert result.confidence == 0
    assert result.actions == []
    assert result.invalidations == []
    assert result.evidence == []
    assert result.answer != answer_stock_question("综合说一下", *_qa_args()).answer


@pytest.mark.parametrize("question,index,updates,missing", [
    ("现在能不能买？", 0, {"support_available": False}, "支撑"),
    ("压力位附近怎么处理？", 0, {"resistance_available": False}, "压力"),
    ("当前风险收益比够不够？", 8, {"ratio_available": False}, "风险收益比"),
    ("同行里算不算龙头？", 5, {"sample_count": 1}, "同行"),
    ("近期事件有什么影响？", 4, {"positive_events": [], "negative_events": [], "watch_events": []}, "事件"),
    ("风险在哪里？", 3, {"items": []}, "风险"),
    ("综合说一下", 2, {"support": [], "opposition": []}, "研究证据"),
])
def test_supported_topics_require_their_specific_evidence(question, index, updates, missing):
    args = list(_qa_args())
    args[index] = args[index].model_copy(update=updates)
    answer = answer_stock_question(question, *args)
    assert answer.answerability == "insufficient_evidence"
    assert missing in "；".join(answer.missing_evidence)
    assert answer.actions == []


def test_missing_theme_evidence_is_unavailable_and_model_is_not_called():
    args = _qa_args(include_theme=False)
    answer = answer_stock_question("它有什么概念题材？", *args)
    with patch("app.services.llm_explainer._call_llm", side_effect=AssertionError("must not call model")) as call:
        result = asyncio.run(enhance_stock_answer(settings=_llm_settings(), rule_answer=answer, analysis=args[0]))
    call.assert_not_called()
    assert result.answerability == "insufficient_evidence"
    assert result.llm_used is False
    assert result.answer == answer.answer


@pytest.mark.parametrize("change", [
    {"quote": {"timestamp": "2026-05-27 15:00:00"}},
    {"quote": {"price": 1250.0}},
    {"data_quality": {"notes": ["日线证据发生修订"]}},
])
def test_market_or_quality_revision_invalidates_prior_evidence_selection(change):
    analysis, answer = _llm_test_case()
    output = _structured_output(analysis, answer)
    field, update = next(iter(change.items()))
    changed = analysis.model_copy(update={field: getattr(analysis, field).model_copy(update=update)})
    with pytest.raises(LlmOutputValidationError, match="当前上下文"):
        validate_and_render_answer(output, answer, changed)


@pytest.mark.parametrize("raw", [
    '{"conclusion":"a","conclusion":"b"}',
    '{"evidence_ids":[NaN]}',
    '{"confidence":Infinity}',
])
def test_duplicate_keys_and_nonfinite_json_cannot_enter_protocol(raw):
    analysis, answer = _llm_test_case()
    with pytest.raises(LlmOutputValidationError, match="重复|非法数值"):
        validate_and_render_answer(raw, answer, analysis)


def test_question_workflow_exposes_answerability_and_skips_model_transport():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.api.routes.stock import ask_stock
    from app.models.research import StockQuestionInput

    args = _qa_args()
    fields = ["analysis", "diagnosis", "evidence_chain", "risk_radar", "event_digest", "peer_comparison", "t_strategy", "market_regime", "risk_reward", "signal_validation", "timeframe_alignment", "theme_context"]
    context = SimpleNamespace(**dict(zip(fields, args, strict=True)))
    hub = SimpleNamespace(settings=_llm_settings())
    with (
        patch("app.workflows.individual.stock_workbench_context", new=AsyncMock(return_value=context)) as load,
        patch("app.services.llm_explainer._call_llm", side_effect=AssertionError("model must not run")) as call,
    ):
        answer = asyncio.run(ask_stock(StockQuestionInput(symbol="600519", question="公司2024年营业收入是多少？"), hub))
    load.assert_awaited_once_with(hub, "600519.SH")
    call.assert_not_called()
    payload = answer.model_dump(mode="json")
    assert payload["answerability"] != "answerable"
    assert payload["missing_evidence"]
    assert payload["confidence"] == 0
    assert payload["actions"] == []
    assert payload["llm_used"] is False
    assert "当前资料无法回答" in payload["answer"]


def test_known_empty_concept_report_cannot_substitute_for_actual_concepts():
    args = list(_qa_args())
    args[-1] = args[-1].model_copy(update={"concepts": []})
    answer = answer_stock_question("它有什么概念题材？", *args)
    assert answer.answerability == "insufficient_evidence"
    assert answer.actions == []


@pytest.mark.parametrize("question", [
    "今天天气怎么样？", "董事最近买了哪家公司？", "今天董事买了多少股票？",
    "今天能不能买以及董事长是谁？", "现在能不能买，去年营业收入是多少？",
    "整体分析一下美国天气", "这只股票的董事买了哪家公司？", "火灾风险在哪里？",
    "请告诉我你现在怎么看欧洲政局", "现在能不能买别的公司？",
])
def test_topic_word_collisions_and_mixed_questions_are_not_supported_intents(question):
    answer = answer_stock_question(question, *_qa_args())
    assert answer.answerability != "answerable"
    assert answer.confidence == 0
    assert answer.actions == []


def test_all_ui_recommended_questions_remain_supported_with_required_evidence():
    from app.services.research_qa_topics import DEFAULT_RELATED_QUESTIONS, RELATED_QUESTIONS

    questions = set(DEFAULT_RELATED_QUESTIONS) | {item for items in RELATED_QUESTIONS.values() for item in items}
    for question in sorted(questions):
        args = _qa_peer_valuation_inputs(peer_count=15, peer_pe=20.0)
        answer = answer_stock_question(question, *args)
        assert answer.answerability == "answerable", (question, answer.model_dump())
        assert answer.actions
        assert answer.evidence


@pytest.mark.parametrize("question", ["请问贵州茅台现在能不能买？", "600519.SH风险在哪里？", "这只股票现在能不能买？"])
def test_current_stock_subject_and_polite_variants_remain_supported(question):
    answer = answer_stock_question(question, *_qa_args())
    assert answer.answerability == "answerable"



def _qa_peer_valuation_inputs(*, peer_count: int, peer_pe: float | None, current_pe: float | None = 18.0):
    from app.services.research_features import build_feature_snapshot
    from app.services.research_peer import build_peer_comparison_report
    from app.services.stock_insights import build_stock_insight_bundle

    args = list(_qa_args())
    analysis = args[0]
    peers = [analysis.quote.model_copy(update={
        "code": f"{600100 + index:06d}", "name": f"同行{index}", "pe": peer_pe,
        "pb": None, "change_pct": float(index),
    }) for index in range(peer_count)]
    analysis = analysis.model_copy(update={
        "quote": analysis.quote.model_copy(update={"pe": current_pe}), "peer_quotes": peers,
    })
    insights = build_stock_insight_bundle(analysis)
    feature = build_feature_snapshot(analysis, insights)
    args[0] = analysis
    args[5] = build_peer_comparison_report(analysis, insights, feature)
    return args


@pytest.mark.parametrize("peer_count,peer_pe,current_pe", [
    (2, None, 18.0), (15, None, 18.0), (14, 20.0, 18.0),
    (15, 20.0, None), (15, 20.0, 0.0), (15, 20.0, -5.0),
])
def test_peer_valuation_question_requires_producer_valuation_evidence(peer_count, peer_pe, current_pe):
    args = _qa_peer_valuation_inputs(peer_count=peer_count, peer_pe=peer_pe, current_pe=current_pe)
    assert args[5].sample_count >= 2
    assert args[5].valuation_position == "估值待确认"
    answer = answer_stock_question("估值在同行里贵不贵？", *args)
    with patch("app.services.llm_explainer._call_llm", side_effect=AssertionError("must not call model")) as call:
        result = asyncio.run(enhance_stock_answer(settings=_llm_settings(), rule_answer=answer, analysis=args[0]))
    call.assert_not_called()
    assert result.answerability == "insufficient_evidence"
    assert result.confidence == 0
    assert result.actions == []
    assert "有效同行PE样本" in "；".join(result.missing_evidence)


def test_pure_peer_strength_remains_answerable_without_pe_data():
    args = _qa_peer_valuation_inputs(peer_count=2, peer_pe=None)
    answer = answer_stock_question("它相对同行强吗？", *args)
    result = _enhance_with_output(args[0], answer, _structured_json(args[0], answer))
    assert answer.answerability == "answerable"
    assert answer.missing_evidence == []
    assert answer.actions
    assert result.llm_used is True


def test_peer_valuation_zero_percentile_is_available_with_valid_pe_sources():
    args = _qa_peer_valuation_inputs(peer_count=15, peer_pe=20.0)
    assert args[5].valuation_position == "估值相对靠后"
    answer = answer_stock_question("估值在同行里贵不贵？", *args)
    result = _enhance_with_output(args[0], answer, _structured_json(args[0], answer))
    assert answer.answerability == "answerable"
    assert answer.missing_evidence == []
    assert answer.actions
    assert result.llm_used is True
