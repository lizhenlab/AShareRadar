from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.stock_insights import build_stock_insight_bundle
from app.services.stock_overview import (
    MAIN_CONFLICT_RULES,
    _event_factor,
    _fund_factor,
    _fundamental_factor,
    _key_prices,
    _main_conflict,
    _overview_scores,
    _quality_adjusted_total_score,
    _risk_factor,
    _risk_triggers,
    _support_resistance_takeaway,
    _technical_factor,
)
from app.services.research_alpha_points import overview_factor_points
from tests.factories import make_kline, make_quote, make_stock_info


def test_fundamental_factor_reports_missing_fields_without_evidence() -> None:
    analysis = _analysis(pe=None, pb=None, market_cap=None, industry=None, stock_profile=None)

    factor = _fundamental_factor(analysis)

    assert factor.score == 55
    assert factor.summary == "基础财务数据待接入"
    assert factor.evidence == ["当前只有行情字段，财报指标待接入。"]
    assert factor.missing_data == ["PE", "PB", "市值", "行业/财务明细"]
    assert factor.score_available is False
    assert factor.participates_in_total_score is False
    assert factor.data_nature == "unavailable"


def test_fundamental_factor_rewards_low_pe_and_pb_with_context_evidence() -> None:
    analysis = _analysis(pe=18.5, pb=2.2, market_cap=120_000_000_000, industry="白酒")

    factor = _fundamental_factor(analysis)

    assert factor.score == 69
    assert factor.summary == "估值字段可用"
    assert factor.evidence == ["PE 18.50", "PB 2.20", "总市值 1200.0 亿", "行业：白酒"]
    assert factor.missing_data == []
    assert factor.score_available is True
    assert factor.participates_in_total_score is True


def test_fundamental_factor_penalizes_high_pe_and_pb() -> None:
    analysis = _analysis(pe=75.0, pb=9.2, market_cap=60_000_000_000, industry="科技")

    factor = _fundamental_factor(analysis)

    assert factor.score == 41
    assert factor.level == "偏弱"
    assert factor.evidence[:2] == ["PE 75.00", "PB 9.20"]


def test_fundamental_factor_treats_non_positive_valuation_as_missing() -> None:
    analysis = _analysis(pe=-5.0, pb=0.0, market_cap=-1, industry=None, stock_profile=None)

    factor = _fundamental_factor(analysis)

    assert factor.score == 55
    assert factor.summary == "基础财务数据待接入"
    assert factor.evidence == ["当前只有行情字段，财报指标待接入。"]
    assert factor.missing_data == ["PE", "PB", "市值", "行业/财务明细"]


def test_fundamental_factor_treats_non_finite_values_and_industry_as_missing() -> None:
    analysis = _analysis(pe=18.5, pb=2.2, market_cap=120_000_000_000, industry="制造")
    quote = analysis.quote.model_copy(update={"pe": float("inf"), "pb": float("nan"), "market_cap": float("-inf")})
    stock_profile = make_stock_info().model_copy(update={"industry": float("inf")})
    analysis = analysis.model_copy(update={"quote": quote, "stock_profile": stock_profile})

    factor = _fundamental_factor(analysis)

    assert factor.score == 55
    assert factor.summary == "基础财务数据待接入"
    assert factor.evidence == ["当前只有行情字段，财报指标待接入。"]
    assert factor.missing_data == ["PE", "PB", "市值", "行业/财务明细"]


def test_overview_prefixes_main_conflict_when_data_quality_is_low() -> None:
    analysis = _analysis(pe=22.0, pb=2.5, market_cap=80_000_000_000, industry="制造", data_quality_score=60)

    overview = build_stock_insight_bundle(analysis).overview

    assert overview.main_conflict.startswith("数据质量一般，")
    assert any("数据质量 一般" in item for item in overview.beginner_takeaways)


def test_overview_labels_heuristic_scores_without_probability_wording() -> None:
    analysis = _analysis(pe=22.0, pb=2.5, market_cap=80_000_000_000, industry="制造")

    overview = build_stock_insight_bundle(analysis).overview
    takeaways = " ".join(overview.beginner_takeaways)
    risk_evidence = " ".join(next(item for item in overview.factors if item.name == "风险面").evidence)

    assert "本次信号证据充分度" in takeaways
    assert "建议强度" in takeaways
    assert "/100" in takeaways
    assert "可信度" not in takeaways
    assert "信心" not in takeaways
    assert "信号证据充分度" in risk_evidence
    assert "信号可信度" not in risk_evidence
    assert "日内振幅约" in risk_evidence
    assert "%" in risk_evidence


def test_overview_quality_prefix_boundary_is_70() -> None:
    low_quality = build_stock_insight_bundle(
        _analysis(pe=22.0, pb=2.5, market_cap=80_000_000_000, industry="制造", data_quality_score=69)
    ).overview
    acceptable_quality = build_stock_insight_bundle(
        _analysis(pe=22.0, pb=2.5, market_cap=80_000_000_000, industry="制造", data_quality_score=70)
    ).overview

    assert low_quality.main_conflict.startswith("数据质量一般，")
    assert not acceptable_quality.main_conflict.startswith("数据质量")


def test_overview_does_not_duplicate_data_quality_prefix_for_weak_quality_conflict() -> None:
    analysis = _analysis(pe=22.0, pb=2.5, market_cap=80_000_000_000, industry="制造", data_quality_score=45)

    overview = build_stock_insight_bundle(analysis).overview

    assert overview.main_conflict == "数据质量较弱，当前所有买卖点、做T和规则命中都只能低置信观察。"


def test_overview_low_quality_caps_total_score() -> None:
    analysis = _analysis(pe=18.5, pb=2.2, market_cap=120_000_000_000, industry="白酒", data_quality_score=45)

    overview = build_stock_insight_bundle(analysis).overview
    participating = [item for item in overview.factors if item.score_available and item.participates_in_total_score]
    factor_score = round(sum(item.score for item in participating) / len(participating))
    signal_quality_score = round(analysis.signal_snapshot.confidence * 0.7 + analysis.data_quality.score * 0.3)
    raw_total = round(factor_score * 0.68 + signal_quality_score * 0.32)
    capped_total = round((factor_score + analysis.data_quality.score) / 2)

    assert overview.total_score == min(raw_total, capped_total)


def test_quality_cap_treats_non_finite_quality_score_as_low_quality() -> None:
    analysis = _analysis(pe=18.5, pb=2.2, market_cap=120_000_000_000, industry="白酒", data_quality_score=80)
    quality = analysis.data_quality.model_copy(update={"score": float("nan"), "level": "一般"})
    analysis = analysis.model_copy(update={"data_quality": quality})

    assert _quality_adjusted_total_score(analysis, 100, 100) == 50


def test_overview_factor_order_stays_stable() -> None:
    overview = build_stock_insight_bundle(
        _analysis(pe=22.0, pb=2.5, market_cap=80_000_000_000, industry="制造", data_quality_score=80)
    ).overview

    assert [item.name for item in overview.factors] == ["技术面", "量价热度（衍生）", "基本面", "事件面", "风险面"]


def test_key_prices_skip_invalid_prices_and_normalize_reversed_support_resistance() -> None:
    analysis = _analysis(pe=22.0, pb=2.5, market_cap=80_000_000_000, industry="制造").model_copy(
        update={"support": 120.0, "resistance": 100.0, "ma5": float("nan"), "ma20": 0.0}
    )

    levels = _key_prices(analysis)

    assert [(item.label, item.price) for item in levels] == [("支撑位", 100.0), ("压力位", 120.0)]


def test_overview_text_uses_normalized_support_resistance_consistently() -> None:
    analysis = _analysis(pe=22.0, pb=2.5, market_cap=80_000_000_000, industry="制造").model_copy(
        update={"support": 120.0, "resistance": 100.0}
    )

    overview = build_stock_insight_bundle(analysis).overview

    assert any("100.00 支撑" in item and "120.00 压力" in item for item in overview.beginner_takeaways)
    assert "有效跌破支撑位 100.00" in overview.risk_triggers
    assert "有效跌破支撑位 120.00" not in overview.risk_triggers


def test_unavailable_structural_placeholders_do_not_change_overview_outputs() -> None:
    analysis = _analysis(pe=22.0, pb=2.5, market_cap=80_000_000_000, industry="制造")
    first = analysis.model_copy(
        update={
            "support": 1.11,
            "resistance": 2.22,
            "ma20": 3.33,
            "support_available": False,
            "resistance_available": False,
            "ma20_available": False,
        }
    )
    second = first.model_copy(update={"support": 911.11, "resistance": 922.22, "ma20": 933.33})

    def outputs(item):
        return (
            [level.model_dump() for level in _key_prices(item)],
            _support_resistance_takeaway(item),
            _risk_triggers(item, _order_pressure("均衡")),
            _technical_factor(item).model_dump(),
        )

    assert outputs(first) == outputs(second)
    rendered = str(outputs(first))
    assert "1.11" not in rendered
    assert "2.22" not in rendered
    assert "3.33" not in rendered
    assert "支撑/压力位待重算" in rendered
    assert "20日线缺失" in rendered


def test_available_structural_levels_keep_overview_price_contract() -> None:
    analysis = _analysis(pe=22.0, pb=2.5, market_cap=80_000_000_000, industry="制造")

    levels = {item.label: item.price for item in _key_prices(analysis)}

    assert levels["支撑位"] == analysis.support
    assert levels["压力位"] == analysis.resistance
    assert levels["20日线"] == analysis.ma20


def test_short_history_keeps_structural_levels_out_of_overview_strategies_and_rules() -> None:
    quote = make_quote(price=105, prev_close=104, high=106, low=103, turnover_rate=4.0)
    klines = [
        make_kline(
            date=f"2026-05-{index + 1:02d}",
            close=100 + index,
            high=101 + index,
            low=99 + index,
            volume=1500 + index * 20,
        )
        for index in range(10)
    ]
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
    analysis = build_analysis(quote, klines, data_quality=quality)

    bundle = build_stock_insight_bundle(analysis)
    rules = {item.rule_id: item for item in bundle.rule_matches.matches}

    assert analysis.support > 0 and analysis.resistance > 0 and analysis.ma20 > 0
    assert analysis.support_available is False
    assert analysis.resistance_available is False
    assert analysis.ma20_available is False
    assert {item.label for item in bundle.overview.key_prices}.isdisjoint({"支撑位", "压力位", "20日线"})
    assert bundle.strategy_cards[1].reference_price == "压力位待重算"
    assert bundle.strategy_cards[2].reference_price == "支撑位待重算"
    assert bundle.strategy_cards[3].reference_price == "做T区间待重算"
    assert bundle.strategy_cards[4].reference_price == "20日线待重算"
    assert rules["break_ma20_risk"].status == "未触发"
    assert "20日线" in rules["break_ma20_risk"].missing_data
    assert rules["support_rebound_watch"].status == "未触发"
    assert "支撑位" in rules["support_rebound_watch"].missing_data


def test_event_factor_deduplicates_events_before_score_and_evidence() -> None:
    risk_event = SimpleNamespace(date="2026-05-13", category="公告", title="股东减持计划", level="风险")
    duplicate_risk_event = SimpleNamespace(date="2026-05-13", category="公告", title="股东减持计划", level="风险")
    positive_event = SimpleNamespace(date="2026-05-14", category="业绩", title="订单增长", level="积极")

    factor = _event_factor(SimpleNamespace(events=[risk_event, duplicate_risk_event, positive_event], notes=["估算"]))

    assert factor.score == 56
    assert factor.summary == "事件需观察"
    assert factor.evidence == ["公告：股东减持计划", "业绩：订单增长"]
    assert factor.missing_data == ["公告全文", "研报摘要", "龙虎榜"]
    assert factor.score_available is True


def test_event_factor_deduplicates_same_visible_event_across_dates_and_ignores_dirty_notes() -> None:
    risk_event = SimpleNamespace(date="2026-05-13", category=" 公告 ", title=" 股东减持计划 ", level=" 风险 ")
    duplicate_with_new_date = SimpleNamespace(date="2026-05-14", category="公告", title="股东减持计划", level="风险")
    dirty_title_event = SimpleNamespace(date="2026-05-15", category="公告", title="nan", level="风险")
    positive_event = SimpleNamespace(date="2026-05-16", category="业绩", title="订单增长", level="积极")

    factor = _event_factor(
        SimpleNamespace(events=[risk_event, duplicate_with_new_date, dirty_title_event, positive_event], notes=[" ", "nan"])
    )

    assert factor.score == 56
    assert factor.evidence == ["公告：股东减持计划", "业绩：订单增长"]
    assert factor.missing_data == []


def test_quality_only_event_and_missing_fundamental_do_not_enter_overview_or_alpha_score() -> None:
    analysis = _analysis(pe=None, pb=None, market_cap=None, industry=None, stock_profile=None)
    bundle = build_stock_insight_bundle(analysis)
    quality_event = SimpleNamespace(category="数据", title="数据质量提醒", level="观察")
    events = SimpleNamespace(events=[quality_event], notes=["外部事件源不可用"])

    scores = _overview_scores(analysis, bundle.fund_flow, bundle.order_pressure, events)
    factors = {item.name: item for item in scores.factors}
    participating = [item for item in scores.factors if item.score_available and item.participates_in_total_score]
    patched_bundle = bundle.model_copy(update={"overview": bundle.overview.model_copy(update={"factors": scores.factors})})

    assert factors["基本面"].score == 55
    assert factors["基本面"].score_available is False
    assert factors["事件面"].score == 58
    assert factors["事件面"].score_available is False
    assert scores.factor_score == round(sum(item.score for item in participating) / len(participating))
    assert {item.title for item in overview_factor_points(patched_bundle)}.isdisjoint({"基本面", "事件面"})


def test_unavailable_fund_flow_factor_keeps_score_for_compatibility_but_does_not_participate() -> None:
    analysis = _analysis(pe=20, pb=2, market_cap=80_000_000_000, industry="制造")
    fund_flow = build_stock_insight_bundle(analysis).fund_flow.model_copy(
        update={"available": True, "data_nature": "unavailable", "overall_score": 88}
    )

    factor = _fund_factor(fund_flow)

    assert factor.score == 88
    assert factor.score_available is False
    assert factor.participates_in_total_score is False
    assert factor.data_nature == "unavailable"


def test_main_conflict_rules_keep_priority_explicit() -> None:
    assert [rule.name for rule in MAIN_CONFLICT_RULES] == [
        "weak_data_quality",
        "low_signal_confidence",
        "weak_trend_strong_fund_flow",
        "strong_trend_weak_fund_flow",
        "sell_pressure",
    ]
    assert _main_conflict(_conflict_analysis(data_quality_score=45, confidence=40), _fund_flow(80), _order_pressure("卖压")) == (
        "数据质量较弱，当前所有买卖点、做T和规则命中都只能低置信观察。"
    )
    assert _main_conflict(_conflict_analysis(data_quality_score=80, confidence=55), _fund_flow(80), _order_pressure("卖压")) == (
        "趋势证据和数据可信度都不够强，先降低操作频率，等待更清晰的确认。"
    )
    assert _main_conflict(_conflict_analysis(trend_score=40), _fund_flow(70), _order_pressure("均衡")) == (
        "量价热度（衍生）有尝试修复，但技术趋势仍偏弱，先等价格重新站稳短期均线。"
    )
    assert _main_conflict(_conflict_analysis(trend_score=70), _fund_flow(45), _order_pressure("均衡")) == (
        "技术面尚可，但量价热度（衍生）跟随不足，突破信号需要继续确认。"
    )
    assert _main_conflict(_conflict_analysis(), _fund_flow(55), _order_pressure("主动卖压")) == "盘口或价格位置显示上方压力，短线不宜追高。"


def test_main_conflict_ignores_fund_trend_divergence_when_fund_flow_unavailable() -> None:
    assert _main_conflict(_conflict_analysis(trend_score=40), _fund_flow(80, available=False), _order_pressure("均衡")) == (
        "当前主要矛盾是趋势确认和风险控制，优先观察关键价位是否有效。"
    )
    assert _main_conflict(_conflict_analysis(trend_score=70), _fund_flow(20, available=False), _order_pressure("主动卖压")) == (
        "盘口或价格位置显示上方压力，短线不宜追高。"
    )


def test_unavailable_order_pressure_text_cannot_change_overview_risk_outputs() -> None:
    analysis = _analysis(pe=22.0, pb=2.5, market_cap=80_000_000_000, industry="制造")
    bundle = build_stock_insight_bundle(analysis)
    pressures = [
        bundle.order_pressure.model_copy(
            update={
                "available": False,
                "data_nature": "unavailable",
                "pressure_level": level,
                "summary": f"不可信占位：{level}",
            }
        )
        for level in ("订单压力不可用", "主动卖压", "强买盘")
    ]

    outputs = [
        (
            _risk_factor(analysis, pressure).model_dump(),
            _main_conflict(analysis, bundle.fund_flow, pressure),
            _risk_triggers(analysis, pressure),
        )
        for pressure in pressures
    ]

    assert outputs[0] == outputs[1] == outputs[2]
    assert "盘口证据不可用" in str(outputs[0])
    assert "不可信占位" not in str(outputs[0])


def _conflict_analysis(*, data_quality_score: int = 80, confidence: int = 80, trend_score: int = 55):
    return SimpleNamespace(
        data_quality=SimpleNamespace(score=data_quality_score),
        signal_snapshot=SimpleNamespace(confidence=confidence),
        trend_score=trend_score,
    )


def _fund_flow(overall_score: int, *, available: bool = True):
    return SimpleNamespace(overall_score=overall_score, available=available)


def _order_pressure(pressure_level: str):
    return SimpleNamespace(
        pressure_level=pressure_level,
        available=True,
        data_nature="observed",
        summary=pressure_level,
    )


def _analysis(
    *,
    pe: float | None,
    pb: float | None,
    market_cap: float | None,
    industry: str | None = "测试行业",
    stock_profile=None,
    data_quality_score: int = 90,
):
    quote = make_quote(pe=pe, pb=pb, market_cap=market_cap, turnover_rate=4.0)
    klines = [
        make_kline(
            date=f"2026-05-{index + 1:02d}",
            close=100 + index * 0.4,
            high=101 + index * 0.4,
            low=99 + index * 0.4,
            volume=1500 + index * 20,
        )
        for index in range(40)
    ]
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0)).model_copy(
        update={"score": data_quality_score, "level": "优秀" if data_quality_score >= 80 else "一般"}
    )
    if stock_profile is None and industry is not None:
        stock_profile = make_stock_info().model_copy(update={"industry": industry})
    return build_analysis(quote, klines, stock_profile=stock_profile, data_quality=quality)
