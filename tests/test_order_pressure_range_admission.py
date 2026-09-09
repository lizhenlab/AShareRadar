"""Unavailable quote ranges must not become directional scoring evidence."""

from datetime import date, datetime, timedelta

import pytest

from app.models.market import OrderBook, OrderBookLevel
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_alpha import build_alpha_evidence_report
from app.services.research_factors import build_factor_lab_report
from app.services.research_features import build_feature_snapshot
from app.services.stock_activity import build_order_pressure
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote, make_stock_info


def _analysis(high=102.0, low=98.0):
    end = date(2026, 8, 11)
    days = sorted([end - timedelta(days=i) for i in range(80) if (end - timedelta(days=i)).weekday() < 5][:40])
    rows = [make_kline(
        close=100, high=101, low=99, date=str(day), source="synthetic-range-test", as_of=f"{day} 15:15:00",
    ) for day in days]
    quote = make_quote(
        price=100, prev_close=101, high=high, low=low, change_pct=-0.99, turnover_rate=3,
        timestamp=f"{end} 15:30:00", source="synthetic-range-test",
    ).model_copy(update={"open": 100.0, "volume": 1000.0})
    quality = build_data_quality(quote, rows, now=datetime(2026, 8, 11, 16))
    return build_analysis(quote, rows, stock_profile=make_stock_info(), data_quality=quality)


def _research(analysis):
    insights = build_stock_insight_bundle(analysis)
    feature = build_feature_snapshot(analysis, insights)
    factors = build_factor_lab_report(analysis, insights, feature)
    alpha = build_alpha_evidence_report(analysis, insights, feature, factors)
    risk = next(factor for factor in factors.factors if factor.id == "risk_pressure")
    return insights, feature, factors, alpha, risk


@pytest.mark.parametrize("high,low", [(0, 0), (0, 98), (102, 0), (-1, -1), (102, -1), (98, 102)])
def test_invalid_range_is_unavailable_in_public_insights_and_feature(high, low):
    insights, feature, _, _, _ = _research(_analysis(high, low))
    pressure = insights.order_pressure
    assert pressure.data_nature == feature.order_pressure_data_nature == "unavailable"
    assert pressure.pressure_level == "订单压力不可用"
    assert "卖压" not in pressure.summary and "振幅约" not in pressure.summary
    assert pressure.available is False


@pytest.mark.parametrize("high,low", [(0, 0), (-1, -1), (102, 0)])
def test_missing_endpoints_cannot_add_an_extra_sell_pressure_penalty_to_factor_or_alpha(high, low):
    invalid = _analysis(high, low)
    other_missing = _analysis(0, 1)
    assert invalid.data_quality.score == other_missing.data_quality.score
    _, _, factors, alpha, risk = _research(invalid)
    _, _, reference_factors, reference_alpha, reference_risk = _research(other_missing)
    assert risk.score == reference_risk.score
    assert factors.total_score == reference_factors.total_score
    assert alpha.confidence == reference_alpha.confidence
    assert [(point.source, point.title, point.impact) for point in alpha.negatives] == [
        (point.source, point.title, point.impact) for point in reference_alpha.negatives
    ]


@pytest.mark.parametrize("high,low,amplitude", [(100, 100, "0.00%"), (102, 98, "4.00%")])
def test_valid_single_price_and_ordinary_ranges_remain_estimated(high, low, amplitude):
    pressure = build_order_pressure(_analysis(high, low))
    assert pressure.data_nature == "estimated"
    assert amplitude in pressure.summary
    assert pressure.pressure_level == "盘口需实时源确认"


@pytest.mark.parametrize("field,value", [("high", None), ("low", float("nan")), ("high", float("inf")), ("price", 0)])
def test_range_boundary_keeps_existing_defensive_handling_of_invalid_values(field, value):
    analysis = _analysis()
    analysis = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={field: value})})
    assert build_order_pressure(analysis).data_nature == "unavailable"


def test_real_order_book_remains_observed_when_quote_range_is_missing():
    book = OrderBook(
        symbol="600519.SH", code="600519", market="SH", source="synthetic-book", updated_at="2026-08-11 15:30:00",
        bid=[OrderBookLevel(price=99.9, volume=1_000)], ask=[OrderBookLevel(price=100.1, volume=5_000)],
    )
    pressure = build_order_pressure(_analysis(0, 0), order_book=book)
    assert pressure.available is True and pressure.data_nature == "observed"
    assert pressure.pressure_level == "卖压偏强（降权）"
    assert pressure.bid_ask_ratio == 0.2
