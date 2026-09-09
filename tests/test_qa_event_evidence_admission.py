"""Display-only availability notices never authorize event question answers."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api.routes.stock import ask_stock
from app.models.analysis import StockEventItem, StockInsightBundle
from app.models.research import StockQuestionInput
from app.services.research_events import build_event_digest_report
from app.services.stock_insights import build_stock_insight_bundle
from tests.test_llm_explainer import _llm_settings, _structured_output
from tests.test_research_qa_answer_modules import _qa_args


def quiet_inputs():
    args = list(_qa_args())
    analysis = args[0]
    quote = analysis.quote.model_copy(update={"price": 1280.0, "open": 1280.0, "high": 1281.0,
        "low": 1279.0, "prev_close": 1280.0, "change_pct": 0.0})
    rows = [row.model_copy(update={"open": 1280.0, "high": 1281.0, "low": 1279.0,
        "close": 1280.0, "volume": 1000.0}) for row in analysis.klines]
    args[0] = analysis.model_copy(update={"quote": quote, "klines": rows, "review": None,
        "industry_context": None, "data_quality": analysis.data_quality.model_copy(update={"anomalies": []})})
    return args, build_stock_insight_bundle(args[0])


def ask_with_digest(args, insights):
    args[4] = build_event_digest_report(args[0], insights)
    fields = ["analysis", "diagnosis", "evidence_chain", "risk_radar", "event_digest", "peer_comparison",
        "t_strategy", "market_regime", "risk_reward", "signal_validation", "timeframe_alignment", "theme_context"]
    context = SimpleNamespace(**dict(zip(fields, args, strict=True)))

    async def select(_settings, answer, analysis, **_kwargs):
        return _structured_output(analysis, answer, ["rule-0"])

    with (
        patch("app.workflows.individual.stock_workbench_context", new=AsyncMock(return_value=context)),
        patch("app.services.llm_explainer._call_llm", new=AsyncMock(side_effect=select)) as model,
    ):
        answer = asyncio.run(ask_stock(StockQuestionInput(symbol="600519", question="近期事件偏利好还是利空？"),
            SimpleNamespace(settings=_llm_settings())))
    return answer, model.await_count


@pytest.mark.parametrize("empty", [False, True])
def test_quiet_production_event_summary_does_not_authorize_qa_or_llm(empty):
    args, insights = quiet_inputs()
    assert not insights.abnormal_events.events
    assert len(insights.events.events) == 1
    assert insights.events.events[0].title == "暂无高强度事件"
    if empty:
        insights = insights.model_copy(update={"events": insights.events.model_copy(update={"events": []})})
    answer, calls = ask_with_digest(args, insights)
    assert args[4].watch_events == []
    assert answer.answerability == "insufficient_evidence"
    assert answer.confidence == 0
    assert answer.actions == answer.invalidations == answer.evidence == []
    assert calls == 0 and not answer.llm_used
    assert any("事件摘要" in item for item in answer.missing_evidence)
    assert insights.events.notes[0] in answer.missing_evidence
    assert args[4].missing_data


def test_availability_notice_semantics_survive_serialization_without_changing_overview_scores():
    args, insights = quiet_inputs()
    event = insights.events.events[0]
    assert event.evidence_kind == "availability_notice"
    restored = StockInsightBundle.model_validate_json(insights.model_dump_json())
    assert restored.events.events[0].evidence_kind == "availability_notice"
    assert restored.overview == insights.overview
    assert next(item for item in restored.overview.factors if item.name == "事件面").score_available is False
    answer, calls = ask_with_digest(args, restored)
    assert answer.answerability == "insufficient_evidence" and calls == 0


@pytest.mark.parametrize("category", ["行业", "历史复盘", "观察"])
def test_actual_watch_evidence_remains_answerable_despite_other_missing_sources(category):
    args, insights = quiet_inputs()
    event = StockEventItem(date=args[0].quote.timestamp, title="现有来源观察", category=category, level="观察",
        description="已记录的观察资料", source="合成有效来源")
    assert event.evidence_kind == "event"
    insights = insights.model_copy(update={"events": insights.events.model_copy(update={"events": [event]})})
    answer, calls = ask_with_digest(args, insights)
    assert args[4].watch_events == ["现有来源观察：已记录的观察资料"]
    assert answer.answerability == "answerable" and calls == 1 and answer.llm_used
    assert args[4].missing_data


def test_notice_does_not_consume_evidence_slots_or_override_real_direction():
    args, insights = quiet_inputs()
    notice = insights.events.events[0].model_copy(update={"title": "仅展示占位", "level": "风险"})
    positive = StockEventItem(date=args[0].quote.timestamp, title="真实积极", category="行业", level="积极",
        description="已有行业记录", source="合成来源")
    negative = positive.model_copy(update={"title": "真实风险", "level": "风险"})
    insights = insights.model_copy(update={"events": insights.events.model_copy(update={"events": [notice] * 4 + [positive, negative]})})
    digest = build_event_digest_report(args[0], insights)
    assert digest.impact_label == "事件偏风险"
    assert digest.positive_events == ["真实积极：已有行业记录"]
    assert digest.negative_events == ["真实风险：已有行业记录"]
    assert digest.watch_events == []
    assert "仅展示占位" not in str(digest.model_dump())
