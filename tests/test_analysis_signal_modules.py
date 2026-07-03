from __future__ import annotations

import math

from app.models.schemas import DataQuality
from app.models.schemas import SignalContribution
from app.models.schemas import SignalItem
from app.services import analysis_signals
from app.services.analysis_signal_advice import action_advice, beginner_summary
from app.services.analysis_signal_points import (
    BUY_POINT_RULES,
    RISK_LEVEL_RULES,
    SELL_POINT_RULES,
    STRENGTH_TAG_RULES,
    T_STYLE_RULES,
    buy_points,
    risk_level,
    sell_points,
    strength_tags,
    t_high_area,
    t_low_area,
    t_plan,
    t_style,
)
from app.services.analysis_signal_quality import (
    QUALITY_BLOCK_RULES,
    gate_signal_items,
    quality_blocks_active_signals,
    quality_reason,
)
from app.services.analysis_signal_snapshot import (
    signal_confidence,
    signal_snapshot,
    signal_summary,
)
from tests.factories import make_quote


def test_analysis_signals_facade_preserves_legacy_imports() -> None:
    assert analysis_signals._action_advice is action_advice
    assert analysis_signals._beginner_summary is beginner_summary
    assert analysis_signals._buy_points is buy_points
    assert analysis_signals._risk_level is risk_level
    assert analysis_signals._sell_points is sell_points
    assert analysis_signals._strength_tags is strength_tags
    assert analysis_signals._t_plan is t_plan
    assert analysis_signals._gate_signal_items is gate_signal_items
    assert (
        analysis_signals._quality_blocks_active_signals is quality_blocks_active_signals
    )
    assert analysis_signals._quality_reason is quality_reason
    assert analysis_signals._signal_confidence is signal_confidence
    assert analysis_signals._signal_snapshot is signal_snapshot
    assert analysis_signals._signal_summary is signal_summary


def test_signal_snapshot_sanitizes_non_finite_contributions_and_notes() -> None:
    quality = _quality(score=80, level="良好", anomalies=[]).model_copy(
        update={"score": math.inf, "level": " ", "notes": [" 报价滞后 ", "nan", "报价滞后", None, "inf", "K线正常"]}
    )
    contributions = [
        _contribution("均线", "趋势确认", 12, "积极", "站上5日线"),
        _contribution("均线", "趋势确认", 12, "积极", "站上5日线"),
        _contribution("量能", "nan", 20, "积极", "放量"),
        _contribution("价格", "异常正向", math.inf, "积极", "无效"),
        _contribution("价格", "日内回撤", -8, "风险", "当前跌幅偏大"),
        _contribution("价格", "空理由", -6, "风险", " "),
        _contribution("其他", "中性项", 0, "观察", "等待确认"),
    ]

    snapshot = signal_snapshot(math.inf, " ", contributions, quality, "高风险")
    snapshot_text = " ".join(
        [
            snapshot.summary,
            *snapshot.data_quality_notes,
            *snapshot.risk_notes,
            *(item.name for item in snapshot.contributions),
            *(item.reason for item in snapshot.contributions),
        ]
    ).lower()

    assert snapshot.score == 0
    assert snapshot.label == "待确认"
    assert snapshot.confidence == 35
    assert [item.name for item in snapshot.positive] == ["趋势确认"]
    assert [item.name for item in snapshot.negative] == ["日内回撤"]
    assert [item.name for item in snapshot.neutral] == ["中性项"]
    assert snapshot.data_quality_notes == ["报价滞后", "K线正常"]
    assert snapshot.risk_notes[:2] == ["当前风险级别为高风险。", "数据质量待确认，结论已自动降权。"]
    assert "nan" not in snapshot_text
    assert "inf" not in snapshot_text


def test_signal_confidence_clamps_malformed_score_and_quality() -> None:
    assert signal_confidence(-100, _quality(score=150, level="优秀")) == 95
    assert signal_confidence(math.nan, _quality(score=80, level="良好")) == 87
    assert signal_confidence(50, _quality(score=80, level="良好").model_copy(update={"score": math.inf})) == 20


def test_signal_summary_sanitizes_direct_compatibility_contributions() -> None:
    quality = _quality(score=80, level="良好").model_copy(update={"score": math.inf, "level": "nan"})
    summary = signal_summary(
        math.nan,
        " ",
        [
            _contribution("价格", "nan", 12, "积极", "无效名称"),
            _contribution("量价", "量价配合", 8, "积极", "站稳支撑"),
        ],
        [
            _contribution("风险", "inf", -9, "风险", "无效名称"),
            _contribution("回撤", "日内回撤", -7, "风险", "跌破短线"),
        ],
        quality,
    )

    assert "主要加分来自量价配合" in summary
    assert "主要扣分来自日内回撤" in summary
    assert "状态为待确认" in summary
    assert "数据质量待确认0分" in summary
    assert "nan" not in summary.casefold()
    assert "inf" not in summary.casefold()


def test_risk_level_rule_priority_is_explicit() -> None:
    assert [rule.name for rule in RISK_LEVEL_RULES] == [
        "low_quality",
        "medium_quality",
        "price_breakdown",
        "weak_trend_or_drop",
        "strong_low_risk",
    ]
    assert (
        risk_level(
            make_quote(change_pct=3.0),
            score=90,
            support=1200,
            quality=_quality(score=45, level="较弱"),
        )
        == "高风险"
    )
    assert (
        risk_level(
            make_quote(change_pct=3.0),
            score=90,
            support=1200,
            quality=_quality(score=60, level="一般"),
        )
        == "中等风险"
    )
    assert (
        risk_level(make_quote(price=1190, change_pct=1.0), score=90, support=1200)
        == "高风险"
    )
    assert risk_level(make_quote(change_pct=-5.0), score=90, support=1200) == "高风险"
    assert risk_level(make_quote(change_pct=-2.1), score=60, support=1200) == "中等风险"
    assert risk_level(make_quote(change_pct=1.0), score=75, support=1200) == "低风险"
    assert risk_level(make_quote(change_pct=0.0), score=60, support=1200) == "可控观察"


def test_risk_level_ignores_non_finite_support() -> None:
    assert (
        risk_level(make_quote(price=100, change_pct=1.0), score=80, support=math.inf)
        == "低风险"
    )
    assert (
        risk_level(
            make_quote().model_copy(update={"price": math.nan}),
            score=80,
            support=1200,
        )
        == "可控观察"
    )


def test_buy_point_rules_are_ordered_and_can_stack() -> None:
    assert [rule.name for rule in BUY_POINT_RULES] == [
        "trend_pullback",
        "breakout_watch",
        "support_pullback",
    ]

    items = buy_points(
        make_quote(price=1300, change_pct=2.0),
        score=76,
        ma5=1280,
        ma10=1260,
        support=1200,
        resistance=1310,
    )

    assert [item.title for item in items] == ["趋势试仓点", "突破观察点"]
    assert "1280.00" in items[0].reason
    assert "1310.00" in items[1].reason


def test_buy_points_use_support_or_fallback_when_trend_is_unclear() -> None:
    support_item = buy_points(
        make_quote(price=1210, change_pct=-0.5),
        score=48,
        ma5=1240,
        ma10=1220,
        support=1200,
        resistance=1350,
    )[0]
    fallback_item = buy_points(
        make_quote(price=1220, change_pct=0.2),
        score=40,
        ma5=1240,
        ma10=1230,
        support=1200,
        resistance=1350,
    )[0]

    assert support_item.title == "支撑低吸点"
    assert fallback_item.title == "暂不追买"


def test_buy_points_do_not_trigger_on_non_finite_resistance() -> None:
    items = buy_points(
        make_quote(price=100, change_pct=2.0),
        score=60,
        ma5=110,
        ma10=105,
        support=0,
        resistance=math.nan,
    )

    assert [item.title for item in items] == ["暂不追买"]


def test_sell_point_rules_are_ordered_and_can_stack() -> None:
    assert [rule.name for rule in SELL_POINT_RULES] == [
        "short_term_reduce",
        "stop_loss_guard",
        "pressure_take_profit",
        "swing_risk",
    ]

    items = sell_points(
        make_quote(price=1190, change_pct=-2.0),
        score=55,
        ma5=1200,
        ma20=1210,
        support=1185,
        resistance=1400,
    )

    assert [item.title for item in items] == ["短线减仓点", "止损保护点", "波段风控点"]
    assert "1200.00" in items[0].reason
    assert "1185.00" in items[1].reason


def test_sell_points_use_pressure_or_holding_fallback() -> None:
    pressure_item = sell_points(
        make_quote(price=1385, change_pct=0.8),
        score=60,
        ma5=1370,
        ma20=1300,
        support=1200,
        resistance=1400,
    )[0]
    fallback_item = sell_points(
        make_quote(price=1300, change_pct=0.8),
        score=75,
        ma5=1280,
        ma20=1200,
        support=1180,
        resistance=1400,
    )[0]

    assert pressure_item.title == "压力止盈点"
    assert fallback_item.title == "持有观察"


def test_sell_points_do_not_trigger_on_non_finite_levels() -> None:
    items = sell_points(
        make_quote(price=100, change_pct=0.5),
        score=60,
        ma5=90,
        ma20=80,
        support=math.nan,
        resistance=math.nan,
    )

    assert [item.title for item in items] == ["持有观察"]


def test_strength_tag_rules_are_ordered_and_fallback_is_clear() -> None:
    assert [rule.name for rule in STRENGTH_TAG_RULES] == [
        "strong_trend",
        "strong_gain",
        "active_turnover",
        "large_amount",
    ]

    active_quote = make_quote(change_pct=5.2, turnover_rate=3.5).model_copy(
        update={"amount": 6_000_000_000}
    )

    assert strength_tags(active_quote, score=82) == [
        "趋势强",
        "涨幅强",
        "换手活跃",
        "成交额大",
    ]
    assert strength_tags(make_quote(change_pct=1.0, turnover_rate=1.0), score=60) == [
        "观察中"
    ]


def test_strength_tags_ignore_non_finite_market_fields() -> None:
    quote = make_quote().model_copy(
        update={
            "amount": math.inf,
            "change_pct": math.nan,
            "turnover_rate": math.inf,
        }
    )

    assert strength_tags(quote, score=math.nan) == ["观察中"]


def test_t_style_rules_are_ordered_from_narrow_to_directional() -> None:
    assert [rule.name for rule in T_STYLE_RULES] == ["narrow", "trend", "range"]
    quote = make_quote(price=100, change_pct=3.0)

    assert t_style(quote, support=95, resistance=102, width_pct=0.8) == "窄幅"
    assert t_style(quote, support=95, resistance=102, width_pct=2.0) == "趋势型"
    assert (
        t_style(
            make_quote(price=100, change_pct=0.5),
            support=95,
            resistance=110,
            width_pct=3.0,
        )
        == "区间型"
    )
    assert (
        t_style(
            make_quote(price=100, change_pct=0.5),
            support=0,
            resistance=0,
            width_pct=3.0,
        )
        == "波动型"
    )
    assert (
        t_style(
            make_quote(price=100, change_pct=0.5),
            support=0,
            resistance=0,
            width_pct=math.nan,
        )
        == "波动型"
    )


def test_t_plan_sanitizes_non_finite_window_inputs() -> None:
    quote = make_quote().model_copy(
        update={
            "price": math.nan,
            "high": math.inf,
            "low": math.nan,
            "change_pct": math.nan,
        }
    )

    items = t_plan(quote, support=math.inf, resistance=math.nan)
    reasons = " ".join(item.reason.lower() for item in items)

    assert [item.title for item in items] == [
        "已有底仓才做T",
        "波动型低吸区",
        "波动型高抛区",
        "做T失效条件",
    ]
    assert "nan" not in reasons
    assert "inf" not in reasons
    assert "0.00" not in reasons
    assert "待确认" in reasons
    assert t_low_area(math.nan, math.inf, math.nan) == 0.0
    assert t_high_area(math.nan, math.inf, math.nan) == 0.0


def test_action_advice_blocks_active_actions_when_quality_is_weak() -> None:
    advice = action_advice(
        make_quote(),
        score=80,
        risk_level="低风险",
        support=1200,
        resistance=1400,
        quality=_quality(score=45, level="较弱", anomalies=["报价严重滞后"]),
    )

    assert advice.action == "控制风险"
    assert advice.confidence == 45
    assert "报价严重滞后" in advice.reason


def test_action_advice_uses_low_confidence_observation_for_medium_quality() -> None:
    advice = action_advice(
        make_quote(),
        score=60,
        risk_level="低风险",
        support=1200,
        resistance=1400,
        quality=_quality(score=60, level="一般"),
    )

    assert advice.action == "轻仓观察"
    assert advice.confidence == 58


def test_action_advice_strong_low_risk_trend_gets_pullback_attention() -> None:
    advice = action_advice(
        make_quote(), score=85, risk_level="低风险", support=1200, resistance=1400
    )

    assert advice.action == "回踩关注"
    assert advice.confidence == 85


def test_action_advice_prioritizes_medium_high_risk_before_hold_observation() -> None:
    advice = action_advice(
        make_quote(price=1300),
        score=70,
        risk_level="高风险",
        support=1200,
        resistance=1400,
    )

    assert advice.action == "控制风险"
    assert advice.confidence == 55
    assert "风险信号较多" in advice.reason


def test_action_advice_holds_when_trend_is_intact_and_risk_is_not_elevated() -> None:
    advice = action_advice(
        make_quote(price=1300),
        score=60,
        risk_level="低风险",
        support=1200,
        resistance=1400,
    )

    assert advice.action == "持有观察"
    assert advice.confidence == 60


def test_action_advice_waits_when_price_and_score_do_not_confirm() -> None:
    advice = action_advice(
        make_quote(price=1190),
        score=55,
        risk_level="低风险",
        support=1200,
        resistance=1400,
    )

    assert advice.action == "等待信号"


def test_quality_gate_block_rules_are_explicit_by_signal_kind() -> None:
    assert [rule.name for rule in QUALITY_BLOCK_RULES] == [
        "pause_buy_points",
        "pause_t_plan",
    ]
    quality = _quality(score=45, level="较弱", anomalies=["报价严重滞后"])

    buy = gate_signal_items([_signal("原买点", "积极")], quality, "buy")
    t_plan_items = gate_signal_items([_signal("原T计划", "观察")], quality, "t")
    sell = gate_signal_items([_signal("原卖点", "积极")], quality, "sell")

    assert [item.title for item in buy] == ["暂停新增买点"]
    assert [item.title for item in t_plan_items] == ["暂停做T", "已有底仓才做T"]
    assert [item.title for item in sell] == ["先收紧风控"]
    assert all(item.level in {"风险", "观察"} for item in [*buy, *t_plan_items, *sell])


def test_quality_gate_degrades_medium_quality_signals_without_replacing_them() -> None:
    quality = _quality(
        score=60,
        level="一般",
        anomalies=[],
    )
    items = [_signal("积极信号", "积极"), _signal("风险信号", "风险")]

    gated = gate_signal_items(items, quality, "buy")

    assert [item.title for item in gated] == ["积极信号", "风险信号"]
    assert [item.level for item in gated] == ["谨慎", "风险"]
    assert all("低置信观察" in item.reason for item in gated)


def _quality(score: int, level: str, anomalies: list[str] | None = None) -> DataQuality:
    return DataQuality(
        level=level,
        source="测试质量",
        quote_time="2026-05-13 15:00:00",
        kline_count=80,
        score=score,
        anomalies=anomalies or [],
    )


def _signal(title: str, level: str) -> SignalItem:
    return SignalItem(title=title, level=level, reason=f"{title}原因")


def _contribution(category: str, name: str, impact: object, level: str, reason: str) -> SignalContribution:
    return SignalContribution(category=category, name=name, impact=0, level=level, reason=reason).model_copy(update={"impact": impact, "name": name})
