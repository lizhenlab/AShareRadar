from __future__ import annotations

from datetime import datetime
from pathlib import Path

import app.services.stock_rules as stock_rules_facade
from app.models.schemas import AbnormalEventItem, AbnormalEventSummary
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.stock_insights import build_stock_insight_bundle
from app.services.stock_rules import (
    RULE_CONFIG,
    RULE_SPECS,
    RULE_SORT_INDEX,
    RULE_VERSION,
    SCORE_VERSION,
    _apply_quality_gate,
    _raw_rule_matches,
    _rule_abnormal_risk,
    _rule_break_ma20,
    _rule_confidence,
    _rule_fund_tech_divergence,
    _rule_high_valuation_chase,
    _rule_match_context,
    _rule_support_rebound,
    _rule_volume_breakout,
    _sorted_rule_matches,
    rule_definitions,
)
from app.services.stock_rule_flow import _rule_fund_tech_divergence as implementation_fund_tech_divergence
from app.services.stock_rule_price import _rule_volume_breakout as implementation_volume_breakout
from app.services.stock_rule_registry import build_rule_match_summary as implementation_build_rule_match_summary
from app.services.stock_rule_risk import _rule_abnormal_risk as implementation_abnormal_risk
from tests.factories import make_kline, make_quote


ROOT = Path(__file__).resolve().parents[1]


def test_stock_rules_facade_reexports_rule_family_implementations() -> None:
    assert stock_rules_facade.build_rule_match_summary is implementation_build_rule_match_summary
    assert _rule_volume_breakout is implementation_volume_breakout
    assert _rule_fund_tech_divergence is implementation_fund_tech_divergence
    assert _rule_abnormal_risk is implementation_abnormal_risk


def test_stock_rule_split_modules_stay_bounded() -> None:
    facade = ROOT / "app/services/stock_rules.py"
    components = sorted((ROOT / "app/services").glob("stock_rule_*.py"))

    assert len(facade.read_text(encoding="utf-8").splitlines()) <= 130
    assert components
    assert all(len(path.read_text(encoding="utf-8").splitlines()) <= 220 for path in components)


def test_volume_breakout_hits_when_price_and_volume_confirm() -> None:
    analysis = _analysis(price=99.0)

    match = _rule_volume_breakout(analysis, latest_high_20=100.0, volume_ratio=1.5)

    assert match.status == "命中"
    assert match.level == "积极"
    assert match.confidence == 78
    assert match.evidence == ["现价 99.00 / 20日高点 100.00", "量比估算 1.50"]
    assert match.missing_data == []


def test_rule_specs_drive_definitions_config_and_raw_order() -> None:
    analysis = _analysis(price=100)
    context = _context_for(analysis)
    spec_ids = [spec.id for spec in RULE_SPECS]
    definitions = rule_definitions()
    matches = _raw_rule_matches(context)
    definitions_by_id = {item.id: item for item in definitions}

    assert len(spec_ids) == len(set(spec_ids))
    assert [item.id for item in definitions] == spec_ids
    assert [item.rule_id for item in matches] == spec_ids
    assert RULE_SORT_INDEX == {rule_id: index for index, rule_id in enumerate(spec_ids)}
    assert list(RULE_CONFIG) == spec_ids
    assert set(RULE_CONFIG) == set(spec_ids)
    assert all(item.version == RULE_VERSION and item.parameters == RULE_CONFIG[item.id] for item in definitions)
    assert all(item.parameters is not RULE_CONFIG[item.id] for item in definitions)
    for spec in RULE_SPECS:
        assert all(isinstance(_rule_confidence(spec.id, status), int) for status in ("命中", "接近", "未触发"))
    for match in matches:
        definition = definitions_by_id[match.rule_id]
        assert match.name == definition.name
        assert match.category == definition.category
        assert match.rule_version == RULE_VERSION
        assert match.score_version == SCORE_VERSION


def test_volume_breakout_is_close_and_marks_missing_volume_ratio() -> None:
    analysis = _analysis(price=99.0)

    match = _rule_volume_breakout(analysis, latest_high_20=100.0, volume_ratio=None)

    assert match.status == "接近"
    assert match.level == "观察"
    assert match.confidence == 56
    assert match.missing_data == ["近5日成交量"]


def test_volume_breakout_marks_missing_high_without_zero_evidence() -> None:
    analysis = _analysis(price=100.0)

    match = _rule_volume_breakout(analysis, latest_high_20=0, volume_ratio=1.6)

    assert match.status == "接近"
    assert match.confidence == 56
    assert match.evidence == ["现价 100.00 / 20日高点 缺失", "量比估算 1.60"]
    assert match.missing_data == ["20日高点"]


def test_volume_breakout_rejects_non_finite_volume_ratio() -> None:
    analysis = _analysis(price=99.0)

    match = _rule_volume_breakout(analysis, latest_high_20=100.0, volume_ratio=float("inf"))

    assert match.status == "接近"
    assert match.confidence == 56
    assert match.evidence == ["现价 99.00 / 20日高点 100.00", "量比估算 缺失"]
    assert match.missing_data == ["近5日成交量"]


def test_rule_context_uses_today_quote_volume_when_klines_stop_yesterday() -> None:
    quote = make_quote(
        price=99.0,
        prev_close=98.0,
        high=100.0,
        low=97.0,
        change_pct=1.02,
        timestamp="2026-05-26 10:00:00",
    ).model_copy(
        update={"open": 98.5, "volume": 1800.0},
    )
    klines = [make_kline(close=98.0, high=100.0, volume=1000.0, date=f"2026-05-{day:02d}") for day in range(1, 26)]
    analysis = _analysis_from(quote, klines)

    context = _context_for(analysis)
    match = _rule_volume_breakout(analysis, context.latest_high_20, context.volume_ratio)

    assert context.latest_high_20 == 100.0
    assert context.volume_ratio == 1.8
    assert match.status == "命中"


def test_rule_context_excludes_current_kline_from_20_day_high_threshold() -> None:
    previous_rows = [
        make_kline(close=98.0, high=100.0, low=96.0, volume=1000.0, date=f"2026-05-{day:02d}") for day in range(1, 21)
    ]
    current_row = make_kline(close=106.0, high=120.0, low=99.0, volume=1000.0, date="2026-05-21")
    quote = make_quote(
        price=106.0,
        prev_close=100.0,
        high=120.0,
        low=99.0,
        change_pct=6.0,
        timestamp="2026-05-21 10:00:00",
    ).model_copy(
        update={"open": 100.0, "volume": 2000.0},
    )
    analysis = _analysis_from(quote, [*previous_rows, current_row])

    context = _context_for(analysis)
    match = _rule_volume_breakout(analysis, context.latest_high_20, context.volume_ratio)

    assert context.latest_high_20 == 100.0
    assert context.volume_ratio == 2.0
    assert match.status == "命中"
    assert match.evidence[0] == "现价 106.00 / 20日高点 100.00"


def test_rule_context_requires_full_valid_20_day_high_window() -> None:
    quote = make_quote(
        price=99.0,
        prev_close=98.0,
        high=100.0,
        low=97.0,
        change_pct=1.02,
        timestamp="2026-05-26 10:00:00",
    ).model_copy(
        update={"open": 98.5, "volume": 1800.0},
    )
    rows = [make_kline(close=98.0, high=100.0, volume=1000.0, date=f"2026-05-{day:02d}") for day in range(1, 21)]
    analysis = _analysis_from(quote, rows)
    corrupted_rows = [*analysis.klines]
    corrupted_rows[7] = corrupted_rows[7].model_copy(update={"high": 0})
    analysis = analysis.model_copy(update={"klines": corrupted_rows})

    context = _context_for(analysis)
    match = _rule_volume_breakout(analysis, context.latest_high_20, context.volume_ratio)

    assert context.latest_high_20 == 0
    assert match.status == "接近"
    assert match.evidence[0] == "现价 99.00 / 20日高点 缺失"
    assert match.missing_data == ["20日高点"]


def test_rule_context_falls_back_to_kline_volume_when_quote_volume_is_missing() -> None:
    rows = [make_kline(close=98.0, high=100.0, volume=1000.0, date=f"2026-05-{day:02d}") for day in range(1, 25)]
    rows.append(make_kline(close=99.0, high=101.0, volume=2200.0, date="2026-05-25"))
    quote = make_quote(
        price=99.0,
        prev_close=98.0,
        high=101.0,
        low=97.0,
        change_pct=1.02,
        timestamp="2026-05-25 10:00:00",
    ).model_copy(
        update={"open": 98.5, "volume": None},
    )
    analysis = _analysis_from(quote, rows)

    context = _context_for(analysis)

    assert context.volume_ratio == 2.2


def test_break_ma20_hits_only_when_price_breaks_and_trend_is_weak() -> None:
    analysis = _analysis(price=98).model_copy(update={"ma20": 100, "trend_score": 49})

    match = _rule_break_ma20(analysis)

    assert match.status == "命中"
    assert match.level == "风险"
    assert match.confidence == 82
    assert match.evidence == ["现价 98.00", "20日线 100.00", "趋势评分 49"]


def test_break_ma20_close_boundary_is_observation() -> None:
    analysis = _analysis(price=100.5).model_copy(update={"ma20": 100, "trend_score": 80})

    match = _rule_break_ma20(analysis)

    assert match.status == "接近"
    assert match.level == "观察"
    assert match.confidence == 58


def test_break_ma20_misses_when_price_is_clear_above_ma20() -> None:
    analysis = _analysis(price=103).model_copy(update={"ma20": 100, "trend_score": 45})

    match = _rule_break_ma20(analysis)

    assert match.status == "未触发"
    assert match.confidence == 38


def test_break_ma20_treats_non_positive_quote_price_as_missing() -> None:
    analysis = _analysis(price=98).model_copy(update={"ma20": 100, "trend_score": 49})
    analysis = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"price": -1})})

    match = _rule_break_ma20(analysis)

    assert match.status == "未触发"
    assert match.level == "观察"
    assert match.evidence[0] == "现价 缺失"
    assert match.missing_data == ["现价"]


def test_fund_tech_divergence_positive_divergence_stays_observation() -> None:
    analysis, fund_flow, order_pressure = _rule_inputs(trend_score=44, fund_score=64, pressure="买盘均衡", fund_available=True)

    match = _rule_fund_tech_divergence(analysis, fund_flow, order_pressure)

    assert match.status == "命中"
    assert match.level == "观察"
    assert match.confidence == 74
    assert match.missing_data == []


def test_fund_tech_divergence_negative_or_sell_pressure_is_risk_when_hit() -> None:
    analysis, fund_flow, order_pressure = _rule_inputs(trend_score=68, fund_score=44, pressure="卖压偏强", fund_available=True)

    match = _rule_fund_tech_divergence(analysis, fund_flow, order_pressure)

    assert match.status == "命中"
    assert match.level == "风险"
    assert match.reason == "趋势评分 68，量价热度评分（衍生） 44，盘口 卖压偏强。"


def test_fund_tech_divergence_gap_only_is_close_and_tracks_missing_fund_flow() -> None:
    analysis, fund_flow, order_pressure = _rule_inputs(trend_score=60, fund_score=40, pressure="买盘均衡", fund_available=False)

    match = _rule_fund_tech_divergence(analysis, fund_flow, order_pressure)

    assert match.status == "接近"
    assert match.level == "观察"
    assert match.confidence == 55
    assert match.missing_data == ["逐笔资金流"]


def test_support_rebound_is_downgraded_when_risk_event_is_present() -> None:
    analysis, fund_flow, _order_pressure = _rule_inputs(trend_score=52, fund_score=70, pressure="买盘均衡", fund_available=True)
    analysis = analysis.model_copy(update={"support": 100})
    abnormal_events = _abnormal_summary(
        [
            _abnormal_event("长下影承接", level="积极", direction="承接"),
            _abnormal_event("放量下跌", level="风险", direction="向下"),
        ],
        level="风险",
    )

    match = _rule_support_rebound(analysis, fund_flow, abnormal_events)

    assert match.status == "接近"
    assert match.level == "谨慎"
    assert match.confidence == 42
    assert "同时存在风险异动" in match.evidence


def test_support_rebound_reports_missing_support_level() -> None:
    analysis, fund_flow, _order_pressure = _rule_inputs(trend_score=52, fund_score=70, pressure="买盘均衡", fund_available=True)
    analysis = analysis.model_copy(update={"support": 0})

    match = _rule_support_rebound(analysis, fund_flow, _abnormal_summary([]))

    assert match.status == "未触发"
    assert match.level == "中性"
    assert match.missing_data == ["支撑位"]
    assert "支撑 缺失" in match.evidence
    assert "缺少有效支撑位" in match.invalidation


def test_support_rebound_does_not_use_non_positive_quote_price() -> None:
    analysis, fund_flow, _order_pressure = _rule_inputs(trend_score=52, fund_score=70, pressure="买盘均衡", fund_available=True)
    analysis = analysis.model_copy(update={"support": 100})
    analysis = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"price": -1})})

    match = _rule_support_rebound(analysis, fund_flow, _abnormal_summary([]))

    assert match.status == "未触发"
    assert match.level == "中性"
    assert match.evidence[0] == "现价 缺失"
    assert match.missing_data == ["现价"]


def test_support_rebound_treats_non_finite_fund_score_as_missing() -> None:
    analysis, fund_flow, _order_pressure = _rule_inputs(trend_score=52, fund_score=70, pressure="买盘均衡", fund_available=True)
    analysis = analysis.model_copy(update={"support": 100})
    fund_flow = fund_flow.model_copy(update={"overall_score": float("inf")})

    match = _rule_support_rebound(analysis, fund_flow, _abnormal_summary([]))

    assert match.status == "接近"
    assert match.level == "观察"
    assert "存在止跌承接证据" not in match.evidence
    assert match.evidence[2] == "量价热度评分（衍生） 缺失"
    assert match.missing_data == ["量价热度评分（衍生）"]


def test_abnormal_risk_uses_main_signal_for_non_risk_events() -> None:
    analysis = _analysis(price=100)
    abnormal_events = _abnormal_summary([_abnormal_event("日内大振幅", level="观察", direction="波动")], main_signal="日内大振幅", level="观察")

    match = _rule_abnormal_risk(analysis, abnormal_events)

    assert match.status == "接近"
    assert match.level == "观察"
    assert match.confidence == 52
    assert match.evidence == ["日内大振幅"]


def test_abnormal_risk_keeps_empty_state_explicit() -> None:
    analysis = _analysis(price=100)

    match = _rule_abnormal_risk(analysis, _abnormal_summary([]))

    assert match.status == "未触发"
    assert match.confidence == 28
    assert match.evidence == ["暂无明显异动"]


def test_high_valuation_chase_hits_when_trend_is_strong_and_valuation_is_weak() -> None:
    analysis = _analysis(price=100).model_copy(update={"trend_score": 68})

    match = _rule_high_valuation_chase(analysis, _valuation(score=44, summary="估值偏贵。", missing_data=["同行估值"]))

    assert match.status == "命中"
    assert match.level == "风险"
    assert match.confidence == 76
    assert match.evidence == ["趋势评分 68", "估值评分 44", "估值偏贵。"]
    assert match.missing_data == ["同行估值"]


def test_high_valuation_chase_close_boundary_is_observation() -> None:
    analysis = _analysis(price=100).model_copy(update={"trend_score": 62})

    match = _rule_high_valuation_chase(analysis, _valuation(score=51))

    assert match.status == "接近"
    assert match.level == "观察"
    assert match.confidence == 55


def test_high_valuation_chase_misses_when_trend_or_valuation_do_not_match() -> None:
    weak_trend = _rule_high_valuation_chase(_analysis(price=100).model_copy(update={"trend_score": 61}), _valuation(score=30))
    healthy_valuation = _rule_high_valuation_chase(_analysis(price=100).model_copy(update={"trend_score": 80}), _valuation(score=52))

    assert weak_trend.status == "未触发"
    assert healthy_valuation.status == "未触发"
    assert healthy_valuation.confidence == 30


def test_high_valuation_chase_treats_non_positive_valuation_score_as_missing() -> None:
    analysis = _analysis(price=100).model_copy(update={"trend_score": 80})

    match = _rule_high_valuation_chase(analysis, _valuation(score=0))

    assert match.status == "未触发"
    assert match.confidence == 30
    assert match.evidence[1] == "估值评分 缺失"
    assert match.missing_data == ["估值评分"]


def test_quality_gate_keeps_high_quality_matches_unchanged() -> None:
    analysis = _quality_analysis(score=88, level="优秀")
    match = _rule_match(status="命中", level="积极", confidence=78)

    gated = _apply_quality_gate(match, analysis)

    assert gated is match


def test_quality_gate_passes_boundary_score_unchanged() -> None:
    analysis = _quality_analysis(score=70, level="良好")
    match = _rule_match(status="命中", level="积极", confidence=78)

    gated = _apply_quality_gate(match, analysis)

    assert gated is match


def test_quality_gate_keeps_risk_status_but_reduces_confidence() -> None:
    analysis = _quality_analysis(score=45, level="较弱")
    match = _rule_match(status="命中", level="风险", confidence=80, evidence=["风险信号"])

    gated = _apply_quality_gate(match, analysis)

    assert gated.status == "命中"
    assert gated.level == "风险"
    assert gated.confidence == 68
    assert gated.evidence[-1] == "数据质量较弱，该规则结论已降权。"


def test_quality_gate_downshifts_positive_hit_for_medium_quality() -> None:
    analysis = _quality_analysis(score=60, level="一般")
    match = _rule_match(status="命中", level="积极", confidence=78)

    gated = _apply_quality_gate(match, analysis)

    assert gated.status == "接近"
    assert gated.level == "观察"
    assert gated.confidence == 60


def test_quality_gate_suppresses_close_signal_when_quality_is_weak() -> None:
    analysis = _quality_analysis(score=45, level="较弱")
    match = _rule_match(status="接近", level="积极", confidence=56)

    gated = _apply_quality_gate(match, analysis)

    assert gated.status == "未触发"
    assert gated.level == "谨慎"
    assert gated.confidence == 46


def test_quality_gate_preserves_cautious_level_when_suppressing_signal() -> None:
    analysis = _quality_analysis(score=45, level="较弱")
    match = _rule_match(status="接近", level="谨慎", confidence=42)

    gated = _apply_quality_gate(match, analysis)

    assert gated.status == "未触发"
    assert gated.level == "谨慎"
    assert gated.confidence == 32


def test_quality_gate_keeps_missed_rule_low_confidence_observation() -> None:
    analysis = _quality_analysis(score=60, level="一般")
    match = _rule_match(status="未触发", level="中性", confidence=18)

    gated = _apply_quality_gate(match, analysis)

    assert gated.status == "未触发"
    assert gated.level == "观察"
    assert gated.confidence == 20


def test_sorted_rule_matches_use_spec_order_as_tie_breaker() -> None:
    analysis = _quality_analysis(score=88, level="优秀")
    earlier = _rule_match(status="接近", level="观察", confidence=55).model_copy(
        update={"rule_id": "volume_breakout_20d"}
    )
    later = _rule_match(status="接近", level="观察", confidence=55).model_copy(
        update={"rule_id": "fund_tech_divergence"}
    )

    sorted_matches = _sorted_rule_matches([later, earlier], analysis)

    assert [item.rule_id for item in sorted_matches] == ["volume_breakout_20d", "fund_tech_divergence"]


def _rule_inputs(*, trend_score: int, fund_score: int, pressure: str, fund_available: bool):
    analysis = _analysis(price=100).model_copy(update={"trend_score": trend_score})
    bundle = build_stock_insight_bundle(analysis)
    fund_flow = bundle.fund_flow.model_copy(update={"overall_score": fund_score, "available": fund_available})
    order_pressure = bundle.order_pressure.model_copy(update={"pressure_level": pressure})
    return analysis, fund_flow, order_pressure


def _analysis(*, price: float):
    quote = make_quote(price=price, prev_close=price - 1, high=price + 1, low=price - 1, change_pct=1.0, turnover_rate=4.0)
    klines = [
        make_kline(
            date=f"2026-05-{index + 1:02d}",
            close=90 + index * 0.3,
            high=91 + index * 0.3,
            low=89 + index * 0.3,
            volume=1500 + index * 20,
        )
        for index in range(40)
    ]
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
    return build_analysis(quote, klines, data_quality=quality)


def _analysis_from(quote, klines):
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 26, 16, 0, 0))
    return build_analysis(quote, klines, data_quality=quality)


def _context_for(analysis):
    bundle = build_stock_insight_bundle(analysis)
    return _rule_match_context(
        analysis,
        bundle.fund_flow,
        bundle.order_pressure,
        bundle.valuation,
        bundle.abnormal_events,
    )


def _quality_analysis(*, score: int, level: str):
    analysis = _analysis(price=100)
    quality = analysis.data_quality.model_copy(update={"score": score, "level": level})
    return analysis.model_copy(update={"data_quality": quality})


def _rule_match(*, status: str, level: str, confidence: int, evidence: list[str] | None = None):
    return _rule_volume_breakout(_analysis(price=99), latest_high_20=100, volume_ratio=1.5).model_copy(
        update={
            "status": status,
            "level": level,
            "confidence": confidence,
            "evidence": evidence or [],
        }
    )


def _valuation(*, score: int, summary: str = "估值摘要。", missing_data: list[str] | None = None):
    class ValuationStub:
        pass

    valuation = ValuationStub()
    valuation.score = score
    valuation.summary = summary
    valuation.missing_data = missing_data or []
    return valuation


def _abnormal_event(title: str, *, level: str, direction: str) -> AbnormalEventItem:
    return AbnormalEventItem(
        date="2026-05-13",
        title=title,
        level=level,
        direction=direction,
        description=title,
        evidence=[],
        watch_points=[],
    )


def _abnormal_summary(
    events: list[AbnormalEventItem],
    *,
    main_signal: str = "暂无明显异动",
    level: str = "中性",
) -> AbnormalEventSummary:
    return AbnormalEventSummary(
        symbol="600519.SH",
        updated_at="2026-05-13 10:00:00",
        score=50,
        level=level,
        main_signal=main_signal,
        events=events,
    )
