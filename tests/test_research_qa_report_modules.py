from __future__ import annotations

import math
from types import SimpleNamespace

from app.models.schemas import RiskRewardReport, ScenarioPlan, StockQaItem
from app.services.research_qa_report import (
    _direct_buy_item,
    _next_session_focus_item,
    _normalize_items,
    _risk_reward_item,
    _t_strategy_item,
    _theme_context_item,
)


def test_risk_reward_qa_item_hides_missing_levels_and_ratio() -> None:
    item = _risk_reward_item(_risk_reward_report(current_price=0, upside_target=0, downside_stop=0, ratio=0))
    text = _item_text(item)

    assert "收益风险比待确认" in item.answer
    assert "上方目标 待确认，下方防守 待确认。" in item.evidence
    assert "0.00" not in text


def test_risk_reward_qa_item_rejects_levels_on_wrong_side() -> None:
    item = _risk_reward_item(_risk_reward_report(current_price=100, upside_target=96, downside_stop=103, ratio=1.8))

    assert "收益风险比待确认" in item.answer
    assert "上方目标 待确认，下方防守 待确认。" in item.evidence


def test_risk_reward_qa_item_keeps_valid_levels_readable() -> None:
    item = _risk_reward_item(_risk_reward_report(current_price=100, upside_target=112, downside_stop=95, ratio=2.4))

    assert "收益风险比 2.40" in item.answer
    assert "上方目标 112.00，下方防守 95.00。" in item.evidence


def test_risk_reward_qa_item_cleans_dirty_rating_summary_and_triggers() -> None:
    dirty_scenario = ScenarioPlan(
        name="震荡路径",
        probability=100,
        trigger="等待支撑和压力复核。",
        expected_move="先观察。",
        response="等待确认。",
        invalidation="边界失效。",
    ).model_copy(update={"trigger": math.inf})
    report = _risk_reward_report(current_price=100, upside_target=112, downside_stop=95, ratio=2.4).model_copy(
        update={"rating": math.nan, "summary": " ", "scenarios": [dirty_scenario]}
    )
    item = _risk_reward_item(report)
    text = _item_text(item)

    assert "当前评级「待确认」" in item.answer
    assert item.evidence == ["上方目标 112.00，下方防守 95.00。"]
    assert "nan" not in text.lower()
    assert "inf" not in text.lower()


def test_direct_buy_qa_item_cleans_action_and_evidence() -> None:
    item = _direct_buy_item(
        SimpleNamespace(action=math.nan, headline=" ", hard_risks=[]),
        SimpleNamespace(market_label=math.inf),
        SimpleNamespace(summary="null"),
    )
    text = _item_text(item)

    assert "当前系统建议是「观察」" in item.answer
    assert item.evidence == ["系统建议、风险收益和市场环境证据待确认。"]
    assert "nan" not in text.lower()
    assert "inf" not in text.lower()
    assert "null" not in text.lower()


def test_stock_qa_report_exit_normalizes_dirty_items() -> None:
    items = _normalize_items([
        StockQaItem(question=" ", answer="nan", evidence=[" ", "inf", "有效证据", "有效证据 "]),
    ])

    assert len(items) == 1
    assert items[0].question == "常见问题"
    assert items[0].answer == "结论待确认。"
    assert items[0].evidence == ["有效证据"]


def test_next_session_focus_hides_invalid_or_inverted_price_levels() -> None:
    item = _next_session_focus_item(
        SimpleNamespace(support=math.nan, resistance=math.inf, quote=SimpleNamespace(price=100)),
        SimpleNamespace(watch_focus=[" ", math.nan], confirmation_signals=["inf", None]),
    )
    text = _item_text(item)

    assert "支撑 待确认" in item.answer
    assert "压力 待确认" in item.answer
    assert item.evidence == ["等待支撑、压力和量能确认。"]
    assert "nan" not in text.lower()
    assert "inf" not in text.lower()
    assert "0.00" not in text


def test_next_session_focus_rejects_price_levels_on_wrong_side() -> None:
    item = _next_session_focus_item(
        SimpleNamespace(support=101, resistance=99, quote=SimpleNamespace(price=100)),
        SimpleNamespace(watch_focus=["守住支撑"], confirmation_signals=["放量突破"]),
    )

    assert "支撑 待确认" in item.answer
    assert "压力 待确认" in item.answer
    assert item.evidence == ["守住支撑", "放量突破"]


def test_t_strategy_qa_item_cleans_summary_and_plan_reasons() -> None:
    item = _t_strategy_item(
        SimpleNamespace(t_plan=[
            SimpleNamespace(reason=" "),
            SimpleNamespace(reason=math.inf),
            SimpleNamespace(reason="等待量能确认"),
        ]),
        SimpleNamespace(summary=math.nan),
    )
    text = _item_text(item)

    assert item.answer == "做T只适用于已有可卖底仓，先看区间是否足够。"
    assert item.evidence == ["等待量能确认"]
    assert "nan" not in text.lower()
    assert "inf" not in text.lower()


def test_t_strategy_qa_item_uses_fallback_when_plan_is_dirty() -> None:
    item = _t_strategy_item(
        SimpleNamespace(t_plan=[SimpleNamespace(reason=" "), SimpleNamespace(reason="null")]),
        SimpleNamespace(summary=" "),
    )

    assert item.evidence == ["T计划待确认。"]


def test_theme_context_qa_item_cleans_labels_concepts_and_risks() -> None:
    item = _theme_context_item(
        SimpleNamespace(
            level=" ",
            style=math.inf,
            summary="null",
            concepts=[
                SimpleNamespace(name=" ", change_pct=2.1),
                SimpleNamespace(name="AI", change_pct=math.inf),
                SimpleNamespace(name="机器人", change_pct=3.45),
            ],
            risks=["nan", "拥挤风险"],
        )
    )
    text = _item_text(item)

    assert "当前为「待确认 / 待确认」" in item.answer
    assert "AI：涨跌幅待确认" in item.evidence
    assert "机器人：3.45%" in item.evidence
    assert "拥挤风险" in item.evidence
    assert "nan" not in text.lower()
    assert "inf" not in text.lower()
    assert "null" not in text.lower()
    assert " ：" not in text


def _risk_reward_report(
    *,
    current_price: float,
    upside_target: float,
    downside_stop: float,
    ratio: float,
) -> RiskRewardReport:
    return RiskRewardReport(
        symbol="600519.SH",
        updated_at="2026-05-13 10:00:00",
        current_price=current_price,
        upside_target=upside_target,
        downside_stop=downside_stop,
        upside_pct=0,
        downside_pct=0,
        reward_risk_ratio=ratio,
        rating="性价比一般",
        summary="风险收益待确认。",
        scenarios=[
            ScenarioPlan(
                name="震荡路径",
                probability=100,
                trigger="等待支撑和压力复核。",
                expected_move="先观察。",
                response="等待确认。",
                invalidation="边界失效。",
            )
        ],
    )


def _item_text(item) -> str:
    return " ".join([item.question, item.answer, *item.evidence])
